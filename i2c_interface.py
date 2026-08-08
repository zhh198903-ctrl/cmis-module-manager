"""Abstract base class and registry for I2C backends."""
from abc import ABC, abstractmethod

_BACKENDS = {}


def register_backend(name):
    """Decorator to register a backend class under a given name."""
    def decorator(cls):
        _BACKENDS[name] = cls
        return cls
    return decorator


class I2CInterface(ABC):
    """Abstract base class for I2C hardware backends."""

    @abstractmethod
    def connect(self, bus: int, address: int) -> None:
        """Open connection to the I2C bus and target address."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection."""

    @abstractmethod
    def read_bytes(self, register: int, length: int) -> bytes:
        """Read `length` bytes starting at `register`."""

    @abstractmethod
    def write_bytes(self, register: int, data: bytes) -> None:
        """Write `data` starting at `register`."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the backend is currently connected."""

    @abstractmethod
    def get_backend_info(self) -> dict:
        """Return metadata about this backend instance."""


def list_backends() -> list:
    """Probe each registered backend and return availability info."""
    # Import backends to trigger registration
    import i2c_backends  # noqa: F401
    result = []
    for name, cls in _BACKENDS.items():
        try:
            info = cls.probe_availability()
        except Exception as e:
            info = {'available': False, 'description': str(e)}
        info['name'] = name
        result.append(info)
    return result


def create_backend(name: str) -> I2CInterface:
    """Instantiate and return the named backend."""
    import i2c_backends  # noqa: F401
    if name not in _BACKENDS:
        raise ValueError(f"Unknown backend: {name!r}. Available: {list(_BACKENDS.keys())}")
    return _BACKENDS[name]()
