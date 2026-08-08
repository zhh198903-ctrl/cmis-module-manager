"""FTDI FT232H I2C backend using pyftdi."""
from i2c_interface import I2CInterface, register_backend

try:
    from pyftdi.i2c import I2cController as _I2cController
    _PYFTDI_AVAILABLE = True
except ImportError:
    _PYFTDI_AVAILABLE = False
    _I2cController = None


@register_backend("ftdi")
class FTDIBackend(I2CInterface):
    """FTDI FT232H USB-I2C adapter via pyftdi."""

    def __init__(self):
        self._controller = None
        self._port = None
        self._connected = False
        self._bus = 0
        self._address = 0x50

    @classmethod
    def probe_availability(cls) -> dict:
        if not _PYFTDI_AVAILABLE:
            return {'available': False, 'description': 'pyftdi not installed (pip install pyftdi)'}
        try:
            from pyftdi.ftdi import Ftdi
            devices = Ftdi.find_all([(0x0403, 0x6014)])  # FT232H VID/PID
            if devices:
                return {'available': True, 'description': f'FTDI FT232H ({len(devices)} device(s) found)'}
            return {'available': False, 'description': 'No FT232H device detected'}
        except Exception as e:
            return {'available': False, 'description': f'FTDI probe failed: {e}'}

    def connect(self, bus: int, address: int) -> None:
        if not _PYFTDI_AVAILABLE:
            raise ImportError("pyftdi is not installed. Run: pip install pyftdi")

        self._bus = bus
        self._address = address
        url = f"ftdi://ftdi:232h/{bus + 1}"

        self._controller = _I2cController()
        self._controller.configure(url)
        self._port = self._controller.get_port(address)
        self._connected = True

    def disconnect(self) -> None:
        if self._controller:
            try:
                self._controller.terminate()
            except Exception:
                pass
        self._controller = None
        self._port = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_backend_info(self) -> dict:
        return {
            'name': 'ftdi',
            'description': 'FTDI FT232H USB-I2C adapter',
            'bus': self._bus,
            'address': hex(self._address),
        }

    def read_bytes(self, register: int, length: int) -> bytes:
        if not self._connected:
            raise IOError("Not connected")
        result = self._port.exchange([register], length)
        return bytes(result)

    def write_bytes(self, register: int, data: bytes) -> None:
        if not self._connected:
            raise IOError("Not connected")
        self._port.write([register] + list(data))
