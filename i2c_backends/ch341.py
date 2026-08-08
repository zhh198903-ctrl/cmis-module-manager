"""CH341A USB-I2C backend using CH341DLL.dll via ctypes."""
import ctypes
import os
import sys
import time

from i2c_interface import I2CInterface, register_backend


def _exe_dir():
    """Return the directory where the EXE (or script) lives."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# DLL loading helper
# ---------------------------------------------------------------------------

def _load_ch341_dll():
    """Search EXE dir → CWD → System32 → CH341DLL_PATH env var for CH341DLL.dll."""
    search_dirs = [
        _exe_dir(),
        os.getcwd(),
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
    ]
    env_path = os.environ.get("CH341DLL_PATH", "")
    if env_path:
        search_dirs.insert(0, env_path if os.path.isdir(env_path) else os.path.dirname(env_path))

    searched = []
    for d in search_dirs:
        candidate = os.path.join(d, "CH341DLL.dll")
        searched.append(candidate)
        if os.path.isfile(candidate):
            try:
                dll = ctypes.WinDLL(candidate)
                _setup_argtypes(dll)
                return dll
            except OSError:
                bits = 8 * ctypes.sizeof(ctypes.c_void_p)
                raise OSError(
                    f"CH341DLL.dll found at {candidate} but failed to load. "
                    f"This EXE is {bits}-bit — make sure the DLL matches "
                    f"(download the {bits}-bit version from WCH CH341EVT package)."
                )

    raise OSError(
        "CH341DLL.dll not found. Place it next to the EXE or install the CH341 driver. "
        "Searched: " + "; ".join(searched)
    )


def _setup_argtypes(dll):
    """Set argtypes/restype for CH341DLL functions to ensure correct ctypes marshalling."""
    dll.CH341OpenDevice.argtypes = [ctypes.c_ulong]
    dll.CH341OpenDevice.restype = ctypes.c_long

    dll.CH341CloseDevice.argtypes = [ctypes.c_ulong]
    dll.CH341CloseDevice.restype = None

    dll.CH341SetStream.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    dll.CH341SetStream.restype = ctypes.c_bool

    dll.CH341StreamI2C.argtypes = [
        ctypes.c_ulong,                     # iIndex
        ctypes.c_ulong,                     # iWriteLength
        ctypes.c_void_p,                    # iWriteBuffer
        ctypes.c_ulong,                     # iReadLength
        ctypes.c_void_p,                    # oReadBuffer
    ]
    dll.CH341StreamI2C.restype = ctypes.c_bool


# ---------------------------------------------------------------------------
# CH341 backend
# ---------------------------------------------------------------------------

@register_backend("ch341")
class CH341Backend(I2CInterface):
    """CH341A USB-I2C adapter via CH341DLL.dll."""

    def __init__(self):
        self._dll = None
        self._device_index = 0
        self._address = 0x50
        self._connected = False

    @classmethod
    def probe_availability(cls) -> dict:
        try:
            _load_ch341_dll()
            return {'available': True, 'description': 'CH341A USB-I2C (CH341DLL.dll found)'}
        except OSError as e:
            return {'available': False, 'description': str(e)}

    def connect(self, bus: int, address: int) -> None:
        self._dll = _load_ch341_dll()
        self._device_index = bus
        self._address = address

        # CH341OpenDevice returns -1 on failure
        ret = self._dll.CH341OpenDevice(self._device_index)
        if ret == -1:
            raise IOError(f"CH341OpenDevice failed for device index {bus}")

        # Set I2C stream mode: 1 = 100 kHz (safe for CMIS modules)
        self._dll.CH341SetStream(self._device_index, 1)
        self._connected = True

    def disconnect(self) -> None:
        if self._dll and self._connected:
            try:
                self._dll.CH341CloseDevice(self._device_index)
            except Exception:
                pass
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_backend_info(self) -> dict:
        return {
            'name': 'ch341',
            'description': 'CH341A USB-I2C adapter',
            'device_index': self._device_index,
            'address': hex(self._address),
        }

    def read_bytes(self, register: int, length: int) -> bytes:
        if not self._connected:
            raise IOError("Not connected")

        # CH341StreamI2C combined write-then-read:
        #   Write phase: START → [addr_W, reg] → REPEATED_START
        #   Read phase:  [addr_R] → [length bytes] → STOP
        write_buf = (ctypes.c_ubyte * 2)(self._address << 1, register)
        read_buf = (ctypes.c_ubyte * length)()

        ret = self._dll.CH341StreamI2C(
            self._device_index,
            2,
            ctypes.byref(write_buf),
            length,
            ctypes.byref(read_buf),
        )
        if not ret:
            raise IOError(f"CH341StreamI2C read failed at register 0x{register:02X}")

        return bytes(read_buf)

    def write_bytes(self, register: int, data: bytes) -> None:
        if not self._connected:
            raise IOError("Not connected")

        # CH341StreamI2C write-only:
        #   Write phase: START → [addr_W, reg, data...] → STOP
        payload_len = 2 + len(data)
        write_buf = (ctypes.c_ubyte * payload_len)(
            self._address << 1, register, *data
        )
        # Use a 1-byte dummy read buffer instead of None — some CH341DLL
        # versions crash or silently fail when oReadBuffer is NULL.
        dummy_read = (ctypes.c_ubyte * 1)()

        ret = self._dll.CH341StreamI2C(
            self._device_index,
            payload_len,
            ctypes.byref(write_buf),
            0,
            ctypes.byref(dummy_read),
        )
        if not ret:
            raise IOError(f"CH341StreamI2C write failed at register 0x{register:02X}")

        time.sleep(0.002)
