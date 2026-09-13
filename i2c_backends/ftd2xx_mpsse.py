"""FTDI USB-I2C backend over the D2XX driver's MPSSE engine.

Why this exists next to the pyftdi backend: on Windows pyftdi talks through
libusb, which needs the FTDI driver replaced with WinUSB (Zadig) before it can
claim the device. That is not something a user can be asked to do - it also
stops every other FTDI application on the machine from working. FTDI's own
driver installs ftd2xx.dll and keeps the device, and D2XX exposes the same
MPSSE engine, so this path works with the stock driver and nothing else.

The I2C waveform is built out of MPSSE opcodes as described in FTDI AN_113
"Interfacing FT2232H Hi-Speed Devices to I2C Bus". The pin assignment it uses
is the conventional one:

    AD0  SCL
    AD1  SDA out      AD1 and AD2 are tied together on the board; MPSSE
    AD2  SDA in       cannot read back a pin it is driving.

MPSSE cannot do a true open-drain output, so command 0x9E puts AD0/AD1 into
drive-low-only mode: writing a 1 tri-states the pin and the bus pull-ups take
it high, which is what I2C requires and what makes clock stretching and
multi-master safe.
"""
import ctypes
import os
import sys

from i2c_interface import I2CInterface, register_backend

# ---------------------------------------------------------------------------
# MPSSE opcodes (FTDI AN_108)
# ---------------------------------------------------------------------------
_CLOCK_BYTES_OUT = 0x11     # MSB first, data changes on the falling edge
_CLOCK_BITS_OUT = 0x13
_CLOCK_BYTES_IN = 0x20      # MSB first, data sampled on the rising edge
_CLOCK_BITS_IN = 0x22
_SET_BITS_LOW = 0x80
_SET_CLK_DIVISOR = 0x86
_DISABLE_CLK_DIV5 = 0x8A
_ENABLE_3PHASE = 0x8C
_DISABLE_ADAPTIVE = 0x97
_DRIVE_ZERO_ONLY = 0x9E
_SEND_IMMEDIATE = 0x87

# Low byte: bit 0 = SCL, bit 1 = SDA out, bit 2 = SDA in.
_DIR_SDA_OUT = 0x03         # SCL and SDA driven, SDA-in stays an input
_DIR_SDA_IN = 0x01          # release SDA so the module can drive it
_BOTH_HIGH = 0x03
_SCL_HIGH_SDA_LOW = 0x01
_BOTH_LOW = 0x00

# 3-phase clocking spends one extra state per bit, so the line rate is two
# thirds of the divisor's nominal frequency: 60 MHz / ((1 + 199) * 2) * 2/3
# is 100 kHz, which is the rate every CMIS module is required to support.
_DIVISOR_100KHZ = 199


def _set_low(value: int, direction: int) -> bytes:
    return bytes([_SET_BITS_LOW, value, direction])


def start_condition() -> bytes:
    """SDA falls while SCL is high, then SCL is taken low.

    Each state is repeated so the line is held for a few clock periods; at
    100 kHz a single MPSSE command is far shorter than the bus setup time the
    specification asks for.
    """
    out = bytearray()
    for _ in range(4):
        out += _set_low(_BOTH_HIGH, _DIR_SDA_OUT)
    for _ in range(4):
        out += _set_low(_SCL_HIGH_SDA_LOW, _DIR_SDA_OUT)
    out += _set_low(_BOTH_LOW, _DIR_SDA_OUT)
    return bytes(out)


def stop_condition() -> bytes:
    """SDA rises while SCL is high, and the bus is left idle."""
    out = bytearray()
    for _ in range(4):
        out += _set_low(_SCL_HIGH_SDA_LOW, _DIR_SDA_OUT)
    for _ in range(4):
        out += _set_low(_BOTH_HIGH, _DIR_SDA_OUT)
    # Release both lines; the pull-ups hold an idle bus high.
    out += _set_low(_BOTH_HIGH, _DIR_SDA_IN)
    return bytes(out)


def write_byte_with_ack(value: int) -> bytes:
    """Clock one byte out, then release SDA and clock the ACK bit back in."""
    return bytes([
        _CLOCK_BYTES_OUT, 0x00, 0x00, value & 0xFF,
    ]) + _set_low(_BOTH_LOW, _DIR_SDA_IN) + bytes([
        _CLOCK_BITS_IN, 0x00,
        _SEND_IMMEDIATE,
    ])


def read_byte_with_ack(ack: bool) -> bytes:
    """Clock one byte in, then drive the ACK (0) or NACK (1) bit out.

    The last byte of a read must be NACKed: it is how the host tells the
    module to let go of SDA so a STOP can be issued.
    """
    return _set_low(_BOTH_LOW, _DIR_SDA_IN) + bytes([
        _CLOCK_BYTES_IN, 0x00, 0x00,
    ]) + _set_low(_BOTH_LOW, _DIR_SDA_OUT) + bytes([
        _CLOCK_BITS_OUT, 0x00, 0x00 if ack else 0x80,
        _SEND_IMMEDIATE,
    ])


def configure_mpsse(divisor: int = _DIVISOR_100KHZ) -> bytes:
    """The one-time setup an MPSSE engine needs before it can speak I2C."""
    return bytes([
        _DISABLE_CLK_DIV5,        # H-series: run the master clock at 60 MHz
        _DISABLE_ADAPTIVE,        # no RTCK feedback; this is not JTAG
        _ENABLE_3PHASE,           # data valid on both clock edges, as I2C wants
        _SET_CLK_DIVISOR, divisor & 0xFF, (divisor >> 8) & 0xFF,
        _DRIVE_ZERO_ONLY, 0x03, 0x00,   # AD0/AD1 open-drain
    ]) + _set_low(_BOTH_HIGH, _DIR_SDA_IN)


def combined_read(address: int, register: int, length: int) -> bytes:
    """The MPSSE program for a CMIS register read.

    START, address+W, register, repeated START, address+R, then `length`
    bytes each ACKed except the last. Repeated START rather than STOP-START
    because releasing the bus between the two halves lets another master take
    it and leaves the module's address pointer somewhere else.
    """
    out = bytearray(start_condition())
    out += write_byte_with_ack((address << 1) | 0)
    out += write_byte_with_ack(register & 0xFF)
    out += start_condition()                       # repeated START
    out += write_byte_with_ack((address << 1) | 1)
    for i in range(length):
        out += read_byte_with_ack(ack=i < length - 1)
    out += stop_condition()
    return bytes(out)


def combined_write(address: int, register: int, data: bytes) -> bytes:
    """START, address+W, register, the payload, STOP."""
    out = bytearray(start_condition())
    out += write_byte_with_ack((address << 1) | 0)
    out += write_byte_with_ack(register & 0xFF)
    for b in data:
        out += write_byte_with_ack(b)
    out += stop_condition()
    return bytes(out)


def expected_reply_len(program: bytes) -> int:
    """How many bytes the device will send back for a program.

    Every ACK read and every data byte read produces exactly one byte on the
    read pipe. Counting them here rather than guessing keeps the reply parser
    and the program that produced it from drifting apart.
    """
    n = 0
    i = 0
    while i < len(program):
        op = program[i]
        if op in (_SET_BITS_LOW,):
            i += 3
        elif op in (_CLOCK_BYTES_OUT,):
            i += 4
        elif op in (_CLOCK_BITS_OUT,):
            i += 3
        elif op in (_CLOCK_BYTES_IN,):
            n += 1
            i += 3
        elif op in (_CLOCK_BITS_IN,):
            n += 1
            i += 2
        elif op in (_SEND_IMMEDIATE, _DISABLE_CLK_DIV5, _DISABLE_ADAPTIVE,
                    _ENABLE_3PHASE):
            i += 1
        elif op == _SET_CLK_DIVISOR:
            i += 3
        elif op == _DRIVE_ZERO_ONLY:
            i += 3
        else:
            i += 1
    return n


# ---------------------------------------------------------------------------
# DLL loading
# ---------------------------------------------------------------------------
def _exe_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _load_d2xx():
    """Search EXE dir → CWD → module dir → System32 → FTD2XX_DLL_PATH."""
    search_dirs = [
        _exe_dir(),
        os.getcwd(),
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32'),
    ]
    env_path = os.environ.get('FTD2XX_DLL_PATH', '')
    if env_path:
        search_dirs.insert(
            0, env_path if os.path.isdir(env_path) else os.path.dirname(env_path))

    searched = []
    for d in search_dirs:
        candidate = os.path.join(d, 'ftd2xx.dll')
        searched.append(candidate)
        if os.path.isfile(candidate):
            try:
                dll = ctypes.WinDLL(candidate)
            except OSError:
                bits = 8 * ctypes.sizeof(ctypes.c_void_p)
                raise OSError(
                    'ftd2xx.dll found at %s but failed to load. This build is '
                    '%d-bit - install the matching FTDI D2XX driver.'
                    % (candidate, bits))
            _setup_argtypes(dll)
            return dll
    raise OSError(
        'ftd2xx.dll not found. Install the FTDI D2XX driver (it ships with the '
        'standard FTDI VCP package), or place the DLL next to the EXE. '
        'Searched: ' + '; '.join(searched))


def _setup_argtypes(dll):
    ul, ulp, vp = ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p
    dll.FT_CreateDeviceInfoList.argtypes = [ulp]
    dll.FT_Open.argtypes = [ctypes.c_int, ctypes.POINTER(vp)]
    dll.FT_Close.argtypes = [vp]
    dll.FT_ResetDevice.argtypes = [vp]
    dll.FT_Purge.argtypes = [vp, ul]
    dll.FT_SetUSBParameters.argtypes = [vp, ul, ul]
    dll.FT_SetLatencyTimer.argtypes = [vp, ctypes.c_ubyte]
    dll.FT_SetTimeouts.argtypes = [vp, ul, ul]
    dll.FT_SetBitMode.argtypes = [vp, ctypes.c_ubyte, ctypes.c_ubyte]
    dll.FT_Write.argtypes = [vp, ctypes.c_char_p, ul, ulp]
    dll.FT_Read.argtypes = [vp, ctypes.c_char_p, ul, ulp]
    dll.FT_GetQueueStatus.argtypes = [vp, ulp]


_FT_OK = 0
_PURGE_RX_TX = 3
_BITMODE_RESET = 0x00
_BITMODE_MPSSE = 0x02


@register_backend('ftd2xx')
class FTD2XXBackend(I2CInterface):
    """FTDI FT232H/FT2232H style adapter driven through the D2XX driver."""

    def __init__(self, bus: int = 0, address: int = 0x50):
        self._dll = None
        self._handle = ctypes.c_void_p()
        self._device_index = bus
        self._address = address
        self._connected = False

    @classmethod
    def probe_availability(cls) -> dict:
        try:
            dll = _load_d2xx()
        except OSError as e:
            return {'available': False, 'description': str(e)}
        count = ctypes.c_ulong(0)
        status = dll.FT_CreateDeviceInfoList(ctypes.byref(count))
        if status != _FT_OK:
            return {'available': False,
                    'description': 'ftd2xx.dll loaded but FT_CreateDeviceInfoList '
                                   'returned status %d' % status}
        if not count.value:
            return {'available': False,
                    'description': 'FTDI D2XX driver present, but no FTDI device '
                                   'is attached'}
        return {'available': True,
                'description': 'FTDI USB-I2C via D2XX MPSSE (%d device%s)'
                               % (count.value, '' if count.value == 1 else 's')}

    def _check(self, status, what):
        if status != _FT_OK:
            raise IOError('%s failed with D2XX status %d' % (what, status))

    def connect(self, bus: int, address: int) -> None:
        self._dll = _load_d2xx()
        self._device_index = bus
        self._address = address
        self._check(self._dll.FT_Open(bus, ctypes.byref(self._handle)),
                    'FT_Open(%d)' % bus)
        h = self._handle
        try:
            self._check(self._dll.FT_ResetDevice(h), 'FT_ResetDevice')
            self._check(self._dll.FT_Purge(h, _PURGE_RX_TX), 'FT_Purge')
            self._check(self._dll.FT_SetUSBParameters(h, 65536, 65536),
                        'FT_SetUSBParameters')
            # 1 ms, so a short reply is not held back by the default 16 ms
            # poll - every I2C byte here is a separate USB round trip.
            self._check(self._dll.FT_SetLatencyTimer(h, 1), 'FT_SetLatencyTimer')
            self._check(self._dll.FT_SetTimeouts(h, 1000, 1000), 'FT_SetTimeouts')
            self._check(self._dll.FT_SetBitMode(h, 0x00, _BITMODE_RESET),
                        'FT_SetBitMode(reset)')
            self._check(self._dll.FT_SetBitMode(h, 0x00, _BITMODE_MPSSE),
                        'FT_SetBitMode(MPSSE)')
            self._raw_write(configure_mpsse())
            self._connected = True
        except Exception:
            try:
                self._dll.FT_Close(h)
            except Exception:
                pass
            self._handle = ctypes.c_void_p()
            raise

    def disconnect(self) -> None:
        if self._dll and self._connected:
            try:
                self._dll.FT_SetBitMode(self._handle, 0x00, _BITMODE_RESET)
                self._dll.FT_Close(self._handle)
            except Exception:
                pass
        self._handle = ctypes.c_void_p()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_backend_info(self) -> dict:
        return {
            'name': 'ftd2xx',
            'description': 'FTDI USB-I2C via D2XX MPSSE',
            'device_index': self._device_index,
            'address': hex(self._address),
        }

    def _raw_write(self, payload: bytes) -> None:
        written = ctypes.c_ulong(0)
        self._check(self._dll.FT_Write(self._handle, payload, len(payload),
                                       ctypes.byref(written)), 'FT_Write')
        if written.value != len(payload):
            raise IOError('FT_Write sent %d of %d bytes'
                          % (written.value, len(payload)))

    def _raw_read(self, count: int) -> bytes:
        if not count:
            return b''
        buf = ctypes.create_string_buffer(count)
        got = ctypes.c_ulong(0)
        self._check(self._dll.FT_Read(self._handle, buf, count,
                                      ctypes.byref(got)), 'FT_Read')
        if got.value != count:
            raise IOError('FT_Read returned %d of %d bytes - the adapter did '
                          'not answer' % (got.value, count))
        return buf.raw[:count]

    def _run(self, program: bytes) -> bytes:
        self._raw_write(program)
        return self._raw_read(expected_reply_len(program))

    def _check_acks(self, acks, what):
        """Raise unless the module pulled SDA low for every byte sent.

        MPSSE clocks the bit in MSB first, so an acknowledged byte comes back
        with bit 7 clear. Unlike the CH341, whose API cannot report a missing
        ACK at all, this adapter hands the bit straight back - so a module
        that is absent, at another address, or holding the bus says so here
        instead of turning into a page full of 0xFF.
        """
        for i, b in enumerate(acks):
            if b & 0x80:
                raise IOError(
                    'No acknowledgement for %s (byte %d) at I2C address '
                    '0x%02X: no module is answering there.'
                    % (what, i + 1, self._address))

    def read_bytes(self, register: int, length: int) -> bytes:
        if not self._connected:
            raise IOError('Not connected')
        program = combined_read(self._address, register, length)
        reply = self._run(program)
        # Three ACK bits come back first - address+W, the register, then
        # address+R after the repeated START - and the data follows.
        self._check_acks(reply[:3], 'the register read setup')
        return bytes(reply[3:3 + length])

    def write_bytes(self, register: int, data: bytes) -> None:
        if not self._connected:
            raise IOError('Not connected')
        program = combined_write(self._address, register, bytes(data))
        self._check_acks(self._run(program), 'the register write')
