"""Silicon Labs CP2112 USB-to-SMBus/I2C bridge.

Protocol per Silicon Labs AN495 "CP2112 Interface Specification", Rev. 0.3,
sections 5.2 (SMBus Configuration) and 6.1-6.8 (data transfer reports).

Why this adapter is worth supporting: it is a HID device, so Windows drives it
with its own hidclass driver and nothing has to be installed - the same
out-of-the-box property the CH341 has. Unlike the CH341 it reports whether the
module acknowledged its address, so an empty bus is an error here rather than
a page of 0xFF that later gets decoded into a module that does not exist.

A CMIS register read is one Data Write Read Request (report 0x11): the CP2112
issues the register address, a repeated START, and then the read, all in one
bus transaction. Splitting it into a write and a separate read would put a
STOP in between and let another master move the module's address pointer.
"""
import time

from i2c_interface import I2CInterface, register_backend
from i2c_backends import _hid

VENDOR_ID = 0x10C4
PRODUCT_ID = 0xEA90

# Report IDs (AN495 sections 5.2 and 6.1-6.8)
REPORT_SMBUS_CONFIG = 0x06
REPORT_DATA_READ_REQUEST = 0x10
REPORT_DATA_WRITE_READ_REQUEST = 0x11
REPORT_DATA_READ_FORCE_SEND = 0x12
REPORT_DATA_READ_RESPONSE = 0x13
REPORT_DATA_WRITE = 0x14
REPORT_TRANSFER_STATUS_REQUEST = 0x15
REPORT_TRANSFER_STATUS_RESPONSE = 0x16
REPORT_CANCEL_TRANSFER = 0x17

# Status 0 of the Transfer Status Response (AN495 6.7)
STATUS_IDLE = 0x00
STATUS_BUSY = 0x01
STATUS_COMPLETE = 0x02
STATUS_COMPLETE_WITH_ERROR = 0x03

# AN495 6.5: one Data Write report carries at most 61 payload bytes.
MAX_WRITE_PAYLOAD = 61
# AN495 6.4: one Data Read Response carries at most 61 data bytes.
MAX_READ_CHUNK = 61
# AN495 6.2: the target address of a Write Read Request is 1 to 16 bytes.
MAX_TARGET_ADDRESS = 16

_BUSY_DETAIL = {
    0x00: 'the address was acknowledged',
    0x01: 'the address was not acknowledged',
    0x02: 'a data read is in progress',
    0x03: 'a data write is in progress',
}

_DONE_DETAIL = {
    0x00: 'timed out, address not acknowledged',
    0x01: 'timed out, the bus never became free (SCL held low)',
    0x02: 'arbitration was lost to another master',
    0x03: 'the read did not complete',
    0x04: 'the write did not complete',
    0x05: 'succeeded',
}


def slave_address_byte(address):
    """The 7-bit address as the CP2112 wants it: shifted up, R/W bit clear.

    AN495 requires the least significant bit to be zero in every report that
    carries a slave address - the CP2112 supplies the direction bit itself.
    """
    return (address << 1) & 0xFE


def smbus_config(clock_hz=100000, device_address=0x02, auto_send_read=False,
                 write_timeout_ms=0, read_timeout_ms=0, scl_low_timeout=False,
                 retries=0):
    """Payload of Set SMBus Configuration, report 0x06 (AN495 5.2).

    Clock Speed is a big-endian 32-bit value in hertz. These settings live in
    RAM, not in the device's PROM, so they have to be sent after every plug-in
    - a CP2112 that was last used at 400 kHz by another program would
    otherwise keep that rate.
    """
    return bytes([
        (clock_hz >> 24) & 0xFF, (clock_hz >> 16) & 0xFF,
        (clock_hz >> 8) & 0xFF, clock_hz & 0xFF,
        device_address & 0xFE,
        0x01 if auto_send_read else 0x00,
        (write_timeout_ms >> 8) & 0xFF, write_timeout_ms & 0xFF,
        (read_timeout_ms >> 8) & 0xFF, read_timeout_ms & 0xFF,
        0x01 if scl_low_timeout else 0x00,
        (retries >> 8) & 0xFF, retries & 0xFF,
    ])


def data_write_read_request(address, target_address, length):
    """Payload of Data Write Read Request, report 0x11 (AN495 6.2).

    This is the combined transaction: the target address bytes go out, then a
    repeated START, then `length` bytes come back.
    """
    target = bytes(target_address)
    if not 1 <= len(target) <= MAX_TARGET_ADDRESS:
        raise ValueError('The target address must be 1 to %d bytes, not %d'
                         % (MAX_TARGET_ADDRESS, len(target)))
    if not 1 <= length <= 512:
        raise ValueError('A CP2112 read is 1 to 512 bytes, not %d' % length)
    payload = bytearray(20)
    payload[0] = slave_address_byte(address)
    payload[1] = (length >> 8) & 0xFF
    payload[2] = length & 0xFF
    payload[3] = len(target)
    payload[4:4 + len(target)] = target
    return bytes(payload)


def data_write(address, data):
    """Payload of Data Write, report 0x14 (AN495 6.5)."""
    data = bytes(data)
    if not 1 <= len(data) <= MAX_WRITE_PAYLOAD:
        raise ValueError('A CP2112 write is 1 to %d bytes, not %d'
                         % (MAX_WRITE_PAYLOAD, len(data)))
    return bytes([slave_address_byte(address), len(data)]) + data


def data_read_force_send(length):
    """Payload of Data Read Force Send, report 0x12 (AN495 6.3)."""
    return bytes([(length >> 8) & 0xFF, length & 0xFF])


def transfer_status_request():
    """Payload of Transfer Status Request, report 0x15 (AN495 6.6).

    The value has to be exactly 0x01; anything else is silently ignored,
    which would leave the poll loop waiting for an answer that never comes.
    """
    return bytes([0x01])


def parse_transfer_status(payload):
    """Decode a Transfer Status Response, report 0x16 (AN495 6.7)."""
    if len(payload) < 6:
        raise IOError('A CP2112 transfer status reply was %d bytes, not 6'
                      % len(payload))
    return {
        'status0': payload[0],
        'status1': payload[1],
        'retries': (payload[2] << 8) | payload[3],
        'bytes_read': (payload[4] << 8) | payload[5],
    }


def describe_transfer_status(status):
    """The AN495 6.7 meaning of a status pair, in the user's terms."""
    s0, s1 = status['status0'], status['status1']
    if s0 == STATUS_IDLE:
        return 'the adapter is idle'
    if s0 == STATUS_BUSY:
        return 'the adapter is busy: ' + _BUSY_DETAIL.get(
            s1, 'condition 0x%02X' % s1)
    detail = _DONE_DETAIL.get(s1, 'condition 0x%02X' % s1)
    if s0 == STATUS_COMPLETE:
        return 'the transfer completed and ' + detail
    if s0 == STATUS_COMPLETE_WITH_ERROR:
        return 'the transfer failed: ' + detail
    return 'unknown status 0x%02X/0x%02X' % (s0, s1)


def transfer_failed(status):
    """True when the transfer ended without moving the data.

    Status 0 of 0x02 means the transfer finished; 0x03 means it finished
    badly. Treating 0x03 as success is how an unanswered address turns into a
    buffer full of whatever the CP2112 happened to sample.
    """
    return status['status0'] == STATUS_COMPLETE_WITH_ERROR


def parse_read_response(payload):
    """Decode a Data Read Response, report 0x13 (AN495 6.4).

    Returns (status, data). Only `Length` bytes of the 61-byte data field are
    valid; the rest is whatever was left in the buffer.
    """
    if len(payload) < 2:
        raise IOError('A CP2112 read response was %d bytes, too short to hold '
                      'a status and a length' % len(payload))
    status, length = payload[0], payload[1]
    if length > MAX_READ_CHUNK:
        raise IOError('A CP2112 read response claimed %d bytes; one report '
                      'carries at most %d' % (length, MAX_READ_CHUNK))
    return status, bytes(payload[2:2 + length])


@register_backend('cp2112')
class CP2112Backend(I2CInterface):
    """Silicon Labs CP2112, driven as a plain Windows HID device."""

    #: How long to keep polling one transfer before giving up.
    TRANSFER_TIMEOUT_S = 2.0

    def __init__(self, bus=0, address=0x50):
        self._device = None
        self._device_index = bus
        self._address = address
        self._connected = False

    @classmethod
    def probe_availability(cls):
        return _hid.availability(VENDOR_ID, PRODUCT_ID, 'CP2112 adapter')

    def connect(self, bus, address):
        self._device_index = bus
        self._address = address
        device = _hid.open_first(VENDOR_ID, PRODUCT_ID, bus, 'CP2112 adapter')
        try:
            # Auto Send Read stays off: with it on the device streams read
            # responses whenever it feels like it, and one left over from a
            # previous transfer would be read as the answer to this one.
            device.write_report(REPORT_SMBUS_CONFIG, smbus_config())
            device.drain()
        except Exception:
            device.close()
            raise
        self._device = device
        self._connected = True

    def disconnect(self):
        if self._device is not None:
            try:
                self._device.write_report(REPORT_CANCEL_TRANSFER, bytes([0x01]))
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
            'name': 'cp2112',
            'description': 'Silicon Labs CP2112 USB-I2C (HID)',
            'device_index': self._device_index,
            'address': hex(self._address),
        }
        if self._device is not None:
            info['product'] = self._device.info.get('product', '')
        return info

    def _require(self):
        if not self._connected or self._device is None:
            raise IOError('Not connected')

    def _expect(self, report_id, timeout_ms=1000, attempts=8):
        """Read reports until the expected one shows up.

        The CP2112 interleaves status responses with read responses, so the
        next report is not always the one just asked for. Returning the wrong
        one would decode a status byte as module data.
        """
        for _ in range(attempts):
            got_id, payload = self._device.read_report(timeout_ms)
            if got_id == report_id:
                return payload
        raise IOError('The CP2112 never sent report 0x%02X' % report_id)

    def _await_transfer(self, what):
        """Poll Transfer Status until the bus transaction has finished."""
        deadline = time.perf_counter() + self.TRANSFER_TIMEOUT_S
        status = None
        while time.perf_counter() < deadline:
            self._device.write_report(REPORT_TRANSFER_STATUS_REQUEST,
                                      transfer_status_request())
            status = parse_transfer_status(
                self._expect(REPORT_TRANSFER_STATUS_RESPONSE))
            if transfer_failed(status):
                raise IOError(
                    'No acknowledgement for %s at I2C address 0x%02X: %s.'
                    % (what, self._address, describe_transfer_status(status)))
            if status['status0'] == STATUS_COMPLETE:
                return status
            if status['status0'] == STATUS_IDLE:
                # Idle without ever reporting completion means the transfer
                # was dropped - the address went unanswered.
                raise IOError(
                    'No acknowledgement for %s at I2C address 0x%02X: the '
                    'adapter went idle without completing the transfer.'
                    % (what, self._address))
        raise IOError('%s at I2C address 0x%02X did not finish within %.1f s '
                      '(last status: %s)'
                      % (what, self._address, self.TRANSFER_TIMEOUT_S,
                         describe_transfer_status(status) if status
                         else 'none'))

    def read_bytes(self, register, length):
        self._require()
        if length <= 0:
            return b''
        self._device.write_report(
            REPORT_DATA_WRITE_READ_REQUEST,
            data_write_read_request(self._address, bytes([register & 0xFF]),
                                    length))
        self._await_transfer('the register read')

        out = bytearray()
        deadline = time.perf_counter() + self.TRANSFER_TIMEOUT_S
        while len(out) < length:
            if time.perf_counter() > deadline:
                raise IOError('The CP2112 returned %d of %d bytes from '
                              'register 0x%02X' % (len(out), length, register))
            self._device.write_report(REPORT_DATA_READ_FORCE_SEND,
                                      data_read_force_send(length - len(out)))
            status, chunk = parse_read_response(
                self._expect(REPORT_DATA_READ_RESPONSE))
            if status == STATUS_COMPLETE_WITH_ERROR:
                raise IOError('The CP2112 reported an error while returning '
                              'the data read from register 0x%02X' % register)
            out += chunk
        return bytes(out[:length])

    def write_bytes(self, register, data):
        self._require()
        data = bytes(data)
        if not data:
            return
        # One report carries the register byte plus 60 payload bytes. Longer
        # writes are split, each chunk re-stating where it starts, because the
        # module's address pointer is not guaranteed to survive the STOP that
        # ends each report's transaction.
        step = MAX_WRITE_PAYLOAD - 1
        for offset in range(0, len(data), step):
            chunk = data[offset:offset + step]
            self._device.write_report(
                REPORT_DATA_WRITE,
                data_write(self._address,
                           bytes([(register + offset) & 0xFF]) + chunk))
            self._await_transfer('the register write')
