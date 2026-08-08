"""Import all backends; gracefully ignore optional-dependency failures."""

# Importing the mock module triggers registration of all 4 mock variants
# (mock_coherent, mock_dr8, mock_sr8, mock_fr4x2) via @register_backend decorators.
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
