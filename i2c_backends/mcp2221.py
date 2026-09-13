"""Microchip MCP2221/MCP2221A USB-to-I2C bridge.

Protocol per the MCP2221A data sheet DS20005565B, sections 3.1.1
(Status/Set Parameters) and 3.1.5-3.1.10 (the I2C commands). The MCP2221 and
the MCP2221A share this command set.

Like the CP2112 this is a HID device, so Windows drives it with its own
hidclass driver and the user installs nothing. Every packet is 64 bytes and
the reports are unnumbered, so report ID 0 carries the command code as its
first byte - the opposite of the CP2112, where the command code *is* the
report ID.

A CMIS register read is three commands: I2C Write Data No Stop puts the
register address on the bus and holds it, I2C Read Data Repeated-START turns
the bus around without releasing it, and I2C Read Data - Get I2C Data collects
what came back. Using a plain write and a plain read instead would put a STOP
between the two halves and let the module's address pointer move.
"""
import time

from i2c_interface import I2CInterface, register_backend
from i2c_backends import _hid

VENDOR_ID = 0x04D8
PRODUCT_ID = 0x00DD

PACKET_LEN = 64

# Command codes (DS20005565B 3.1)
CMD_STATUS_SET_PARAMETERS = 0x10
CMD_I2C_WRITE_DATA = 0x90
CMD_I2C_WRITE_DATA_REPEATED_START = 0x92
CMD_I2C_WRITE_DATA_NO_STOP = 0x94
CMD_I2C_READ_DATA = 0x91
CMD_I2C_READ_DATA_REPEATED_START = 0x93
CMD_I2C_GET_DATA = 0x40

# Status/Set Parameters sub-commands (DS20005565B Table 3-1)
SUBCMD_CANCEL_TRANSFER = 0x10
SUBCMD_SET_SPEED = 0x20

# Command response byte 1 (DS20005565B Tables 3-21, 3-27, ...)
RESPONSE_OK = 0x00
RESPONSE_BUSY = 0x01
# Get I2C Data response byte 1 (Table 3-31)
RESPONSE_READ_ERROR = 0x41
# Get I2C Data response byte 3 (Table 3-31)
READ_LENGTH_ERROR = 127

# One command carries at most 60 payload bytes: indices 4-63 (Table 3-20).
MAX_PAYLOAD = 60
# Get I2C Data returns at most 60 bytes per packet (Table 3-31).
MAX_READ_CHUNK = 60

# The internal clock the I2C divider runs from (DS20005565B 1.12).
INTERNAL_CLOCK_HZ = 12000000
# The data sheet describes byte 4 only as "the I2C/SMBus system clock divider"
# and does not publish the mapping to a bit rate. 12 MHz / (divider + 3) is
# what Microchip's own utility and the Linux hid-mcp2221 driver use, and it is
# the reason 100 kHz comes out as 117 rather than 120. Anything within the
# adapter's documented ceiling of 400 kHz (DS20005565B 1.13) is safe; getting
# it wrong only changes the bus rate, it does not corrupt data.
CLOCK_DIVIDER_OFFSET = 3


def slave_address_byte(address, read):
    """The 8-bit address byte: 7-bit address shifted up, R/W in bit 0.

    DS20005565B spells this out in a note under every I2C command table -
    even values write, odd values read.
    """
    return ((address << 1) & 0xFE) | (1 if read else 0)


def speed_divider(clock_hz):
    """The divider byte for a wanted bit rate."""
    if clock_hz <= 0:
        raise ValueError('The I2C clock must be positive')
    divider = int(round(float(INTERNAL_CLOCK_HZ) / clock_hz)) \
        - CLOCK_DIVIDER_OFFSET
    if not 0 <= divider <= 255:
        raise ValueError('%d Hz is outside the range this adapter can divide '
                         'its %d Hz clock down to' % (clock_hz,
                                                      INTERNAL_CLOCK_HZ))
    return divider


def _packet(*values):
    """A command padded to the 64 bytes every MCP2221A report carries."""
    packet = bytearray(PACKET_LEN)
    packet[0:len(values)] = bytes(values)
    return bytes(packet)


def status_request():
    """Status/Set Parameters with both sub-commands inert (DS20005565B 3.1.1).

    Byte 2 must not be 0x10 and byte 3 must not be 0x20, or this read of the
    engine's state would cancel a transfer or change the bus rate as a side
    effect.
    """
    return _packet(CMD_STATUS_SET_PARAMETERS, 0x00, 0x00, 0x00)


def cancel_transfer():
    """Status/Set Parameters with the cancel sub-command."""
    return _packet(CMD_STATUS_SET_PARAMETERS, 0x00, SUBCMD_CANCEL_TRANSFER,
                   0x00)


def set_speed(divider):
    """Status/Set Parameters with the set-speed sub-command."""
    return _packet(CMD_STATUS_SET_PARAMETERS, 0x00, 0x00, SUBCMD_SET_SPEED,
                   divider & 0xFF)


def parse_status(response):
    """Decode a Status/Set Parameters response (DS20005565B Table 3-2)."""
    if len(response) < 18:
        raise IOError('An MCP2221A status reply was %d bytes, not 64'
                      % len(response))
    return {
        'speed_set': response[3],
        'engine_state': response[8],
        'requested': response[9] | (response[10] << 8),
        'transferred': response[11] | (response[12] << 8),
        'divider': response[14],
        'address': response[16] | (response[17] << 8),
    }


def write_data(address, data, command=CMD_I2C_WRITE_DATA):
    """An I2C write command (DS20005565B 3.1.5 / 3.1.6 / 3.1.7).

    The length field is the length of the whole bus transfer, which is why it
    is 16-bit while one packet carries only 60 bytes: a longer transfer is
    continued by repeating the same command with the next chunk.
    """
    data = bytes(data)
    if not 1 <= len(data) <= MAX_PAYLOAD:
        raise ValueError('One MCP2221A write packet carries 1 to %d bytes, '
                         'not %d' % (MAX_PAYLOAD, len(data)))
    return _packet(command, len(data) & 0xFF, (len(data) >> 8) & 0xFF,
                   slave_address_byte(address, read=False), *data)


def read_data(address, length, repeated_start=False):
    """An I2C read command (DS20005565B 3.1.8 / 3.1.9)."""
    if not 1 <= length <= 0xFFFF:
        raise ValueError('An MCP2221A read is 1 to 65535 bytes, not %d'
                         % length)
    command = (CMD_I2C_READ_DATA_REPEATED_START if repeated_start
               else CMD_I2C_READ_DATA)
    return _packet(command, length & 0xFF, (length >> 8) & 0xFF,
                   slave_address_byte(address, read=True))


def get_i2c_data():
    """I2C Read Data - Get I2C Data (DS20005565B 3.1.10)."""
    return _packet(CMD_I2C_GET_DATA)


def parse_get_i2c_data(response):
    """Decode a Get I2C Data response (DS20005565B Table 3-31).

    Returns (ready, data). `ready` is False while the engine has nothing yet,
    which is not an error - the read is still in flight.
    """
    if len(response) < 4:
        raise IOError('An MCP2221A read reply was %d bytes, too short to hold '
                      'a status and a length' % len(response))
    if response[1] == RESPONSE_READ_ERROR:
        raise IOError('The MCP2221A could not read the data back from the I2C '
                      'engine')
    count = response[3]
    if count == READ_LENGTH_ERROR:
        raise IOError('No acknowledgement from the module: the MCP2221A '
                      'flagged the read as failed')
    if count > MAX_READ_CHUNK:
        raise IOError('An MCP2221A read reply claimed %d bytes; one packet '
                      'carries at most %d' % (count, MAX_READ_CHUNK))
    return count > 0, bytes(response[4:4 + count])


@register_backend('mcp2221')
class MCP2221Backend(I2CInterface):
    """Microchip MCP2221/MCP2221A, driven as a plain Windows HID device."""

    #: How long to keep polling one transfer before giving up.
    TRANSFER_TIMEOUT_S = 2.0

    def __init__(self, bus=0, address=0x50):
        self._device = None
        self._device_index = bus
        self._address = address
        self._connected = False

    @classmethod
    def probe_availability(cls):
        return _hid.availability(VENDOR_ID, PRODUCT_ID, 'MCP2221A adapter')

    def connect(self, bus, address):
        self._device_index = bus
        self._address = address
        device = _hid.open_first(VENDOR_ID, PRODUCT_ID, bus,
                                 'MCP2221A adapter')
        self._device = device
        self._connected = True
        try:
            device.drain()
            # Cancel first: a transfer left in flight by whatever had the
            # adapter before makes the engine refuse the new bus rate with
            # 0x21, and the speed would silently stay at the old value.
            self._command(cancel_transfer())
            reply = self._command(set_speed(speed_divider(100000)))
            if reply[3] == 0x21:
                raise IOError('The MCP2221A refused to set the I2C bit rate: '
                              'a transfer is still in progress.')
        except Exception:
            self._connected = False
            self._device = None
            device.close()
            raise

    def disconnect(self):
        if self._device is not None:
            try:
                self._device.write_report(0x00, cancel_transfer())
            except Exception:
                pass
            self._device.close()
        self._device = None
        self._connected = False

    @property
    def is_connected(self):
        return self._connected

    def get_backend_info(self):
        info = {
            'name': 'mcp2221',
            'description': 'Microchip MCP2221A USB-I2C (HID)',
            'device_index': self._device_index,
            'address': hex(self._address),
        }
        if self._device is not None:
            info['product'] = self._device.info.get('product', '')
        return info

    def _require(self):
        if not self._connected or self._device is None:
            raise IOError('Not connected')

    def _command(self, packet, what=None):
        """Send one 64-byte command and return its 64-byte reply.

        The reply always echoes the command code in byte 0. Checking that is
        what keeps a reply left over from a previous command from being read
        as the answer to this one.
        """
        self._device.write_report(0x00, packet)
        _, reply = self._device.read_report()
        if not reply:
            raise IOError('The MCP2221A sent an empty reply')
        if reply[0] != packet[0]:
            raise IOError('The MCP2221A answered command 0x%02X with a reply '
                          'for 0x%02X' % (packet[0], reply[0]))
        if what is not None and reply[1] == RESPONSE_BUSY:
            raise IOError('The MCP2221A engine was busy and did not accept %s'
                          % what)
        return reply

    def _await_transfer(self, what):
        """Wait until the engine has moved every byte it was asked to move.

        The data sheet does not enumerate the engine's internal state values,
        so completion is judged only on the two counters it does document:
        requested length and already-transferred length (Table 3-2, bytes
        9-12). An engine that stops short has lost the module.
        """
        deadline = time.perf_counter() + self.TRANSFER_TIMEOUT_S
        status = None
        while time.perf_counter() < deadline:
            status = parse_status(self._command(status_request()))
            if status['requested'] and \
                    status['transferred'] >= status['requested']:
                return status
            if not status['engine_state'] and status['transferred'] < \
                    status['requested']:
                self._command(cancel_transfer())
                raise IOError(
                    'No acknowledgement for %s at I2C address 0x%02X: the '
                    'engine stopped after %d of %d bytes.'
                    % (what, self._address, status['transferred'],
                       status['requested']))
        self._command(cancel_transfer())
        raise IOError('%s at I2C address 0x%02X did not finish within %.1f s'
                      % (what, self._address, self.TRANSFER_TIMEOUT_S))

    def read_bytes(self, register, length):
        self._require()
        if length <= 0:
            return b''
        self._command(write_data(self._address, bytes([register & 0xFF]),
                                 CMD_I2C_WRITE_DATA_NO_STOP),
                      'the register address')
        self._command(read_data(self._address, length, repeated_start=True),
                      'the read request')

        out = bytearray()
        deadline = time.perf_counter() + self.TRANSFER_TIMEOUT_S
        while len(out) < length:
            if time.perf_counter() > deadline:
                self._command(cancel_transfer())
                raise IOError('The MCP2221A returned %d of %d bytes from '
                              'register 0x%02X' % (len(out), length, register))
            ready, chunk = parse_get_i2c_data(self._command(get_i2c_data()))
            if ready:
                out += chunk
        return bytes(out[:length])

    def write_bytes(self, register, data):
        self._require()
        data = bytes(data)
        if not data:
            return
        # One packet carries the register byte plus 59 payload bytes. Each
        # chunk re-states where it starts: every packet ends its own bus
        # transaction with a STOP, and the module's address pointer is not
        # guaranteed to stay put across one.
        step = MAX_PAYLOAD - 1
        for offset in range(0, len(data), step):
            chunk = data[offset:offset + step]
            self._command(
                write_data(self._address,
                           bytes([(register + offset) & 0xFF]) + chunk),
                'the register write')
            self._await_transfer('the register write')
