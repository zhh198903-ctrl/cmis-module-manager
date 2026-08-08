"""CH347 USB-I2C backend using CH347DLL.dll via ctypes."""
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


def _load_ch347_dll():
    """Search EXE dir → CWD → System32 → CH347DLL_PATH env var for CH347DLL.dll."""
    search_dirs = [
        _exe_dir(),
        os.getcwd(),
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
    ]
    env_path = os.environ.get("CH347DLL_PATH", "")
    if env_path:
        search_dirs.insert(0, env_path if os.path.isdir(env_path) else os.path.dirname(env_path))

    searched = []
    for d in search_dirs:
        candidate = os.path.join(d, "CH347DLL.dll")
        searched.append(candidate)
        if os.path.isfile(candidate):
            try:
                dll = ctypes.WinDLL(candidate)
                _setup_argtypes(dll)
                return dll
            except OSError:
                bits = 8 * ctypes.sizeof(ctypes.c_void_p)
                raise OSError(
                    f"CH347DLL.dll found at {candidate} but failed to load. "
                    f"This EXE is {bits}-bit — make sure the DLL matches "
                    f"(download the {bits}-bit version from WCH CH347EVT package)."
                )

    raise OSError(
        "CH347DLL.dll not found. Place it next to the EXE or install the CH347 driver. "
        "Searched: " + "; ".join(searched)
    )


def _setup_argtypes(dll):
    """Set argtypes/restype for CH347DLL functions to ensure correct ctypes marshalling."""
    dll.CH347OpenDevice.argtypes = [ctypes.c_ulong]
    dll.CH347OpenDevice.restype = ctypes.c_long

    dll.CH347CloseDevice.argtypes = [ctypes.c_ulong]
    dll.CH347CloseDevice.restype = None

    dll.CH347I2C_Set.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    dll.CH347I2C_Set.restype = ctypes.c_bool

    dll.CH347StreamI2C.argtypes = [
        ctypes.c_ulong,                     # iIndex
        ctypes.c_ulong,                     # iWriteLength
        ctypes.c_void_p,                    # iWriteBuffer
        ctypes.c_ulong,                     # iReadLength
        ctypes.c_void_p,                    # oReadBuffer
    ]
    dll.CH347StreamI2C.restype = ctypes.c_bool


@register_backend("ch347")
class CH347Backend(I2CInterface):
    """CH347 USB-I2C adapter via CH347DLL.dll."""

    def __init__(self):
        self._dll = None
        self._device_index = 0
        self._address = 0x50
        self._connected = False

    @classmethod
    def probe_availability(cls) -> dict:
        try:
            _load_ch347_dll()
            return {'available': True, 'description': 'CH347 USB-I2C (CH347DLL.dll found)'}
        except OSError as e:
            return {'available': False, 'description': str(e)}

    def connect(self, bus: int, address: int) -> None:
        self._dll = _load_ch347_dll()
        self._device_index = bus
        self._address = address

        # CH347OpenDevice returns HANDLE: INVALID_HANDLE_VALUE (-1) or NULL (0) on failure
        ret = self._dll.CH347OpenDevice(self._device_index)
        if ret in (-1, 0):
            raise IOError(f"CH347OpenDevice failed for device index {bus}")

        # Set I2C speed: 0=20kHz, 1=100kHz, 2=400kHz, 3=750kHz
        self._dll.CH347I2C_Set(self._device_index, 1)
        self._connected = True

    def disconnect(self) -> None:
        if self._dll and self._connected:
            try:
                self._dll.CH347CloseDevice(self._device_index)
            except Exception:
                pass
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_backend_info(self) -> dict:
        return {
            'name': 'ch347',
            'description': 'CH347 USB-I2C adapter',
            'device_index': self._device_index,
            'address': hex(self._address),
        }

    def read_bytes(self, register: int, length: int) -> bytes:
        if not self._connected:
            raise IOError("Not connected")

        write_buf = (ctypes.c_ubyte * 2)(self._address << 1, register)
        read_buf = (ctypes.c_ubyte * length)()

        ret = self._dll.CH347StreamI2C(
            self._device_index,
            2,
            ctypes.byref(write_buf),
            length,
            ctypes.byref(read_buf),
        )
        if not ret:
            raise IOError(f"CH347StreamI2C read failed at register 0x{register:02X}")

        return bytes(read_buf)

    def write_bytes(self, register: int, data: bytes) -> None:
        if not self._connected:
            raise IOError("Not connected")

        payload_len = 2 + len(data)
        write_buf = (ctypes.c_ubyte * payload_len)(
            self._address << 1, register, *data
        )
        dummy_read = (ctypes.c_ubyte * 1)()

        ret = self._dll.CH347StreamI2C(
            self._device_index,
            payload_len,
            ctypes.byref(write_buf),
            0,
            ctypes.byref(dummy_read),
        )
        if not ret:
            raise IOError(f"CH347StreamI2C write failed at register 0x{register:02X}")

        time.sleep(0.002)
