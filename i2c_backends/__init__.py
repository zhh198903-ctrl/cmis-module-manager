"""Import all backends; gracefully ignore optional-dependency failures."""

# Importing the mock module registers every mock variant via @register_backend.
# The count is deliberately not repeated here: it has been wrong twice already,
# and i2c_interface.list_backends() is the only answer that cannot go stale.
from i2c_backends import mock  # noqa: F401 — always available

try:
    from i2c_backends.ch341 import CH341Backend  # noqa: F401
except Exception:
    pass

try:
    from i2c_backends.ch347 import CH347Backend  # noqa: F401
except Exception:
    pass

try:
    from i2c_backends.ftdi_backend import FTDIBackend  # noqa: F401
except Exception:
    pass
