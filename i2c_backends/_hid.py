"""Windows HID transport for the adapters that need no vendor DLL.

The CP2112 and the MCP2221A are both plain USB HID devices: Windows binds its
own hidclass driver to them when they are plugged in, and their I2C protocol
is carried in ordinary HID reports. Nothing has to be installed and no driver
has to be replaced - the same "works out of the box" property that makes the
CH341 usable, but without the CH341's inability to report a missing
acknowledgement.

Only two libraries are used and both ship with Windows:

    setupapi.dll   enumerate the HID interfaces that are present
    hid.dll        ask each one for its VID/PID and its report sizes

Reads are overlapped. A blocking ReadFile on a HID handle waits forever, and
an adapter that stops answering would take the whole Flask worker with it.
"""
import ctypes
import sys

_IS_WINDOWS = sys.platform == 'win32'

if _IS_WINDOWS:
    from ctypes import wintypes
else:  # pragma: no cover - the backends refuse before reaching any of this
    wintypes = None


class HIDError(IOError):
    """Anything that goes wrong talking to a HID device."""


# ---------------------------------------------------------------------------
# Win32 declarations
# ---------------------------------------------------------------------------
_DIGCF_PRESENT = 0x02
_DIGCF_DEVICEINTERFACE = 0x10
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x01
_FILE_SHARE_WRITE = 0x02
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_IO_PENDING = 997
_WAIT_OBJECT_0 = 0


def detail_struct_size(pointer_size):
    """sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) as a C compiler computes it.

    The struct is {DWORD cbSize; WCHAR DevicePath[1];} and SetupAPI compares
    cbSize against that size: 8 where pointers are 8 bytes (the struct takes
    the pointer's 8-byte alignment) and 6 where they are 4 (a WCHAR aligns to
    2). ctypes cannot be asked for the number - it has to be spelled out, and a
    wrong one makes SetupDiGetDeviceInterfaceDetailW fail with
    ERROR_INVALID_USER_BUFFER and no device is ever found. The released EXE is
    32-bit, so 6 is the value customers run with and 8 is the one this
    development machine would use.
    """
    return 8 if pointer_size == 8 else 6


_DETAIL_CB_SIZE = detail_struct_size(ctypes.sizeof(ctypes.c_void_p))


if _IS_WINDOWS:
    class _GUID(ctypes.Structure):
        _fields_ = [('Data1', ctypes.c_ulong),
                    ('Data2', ctypes.c_ushort),
                    ('Data3', ctypes.c_ushort),
                    ('Data4', ctypes.c_ubyte * 8)]

    class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD),
                    ('InterfaceClassGuid', _GUID),
                    ('Flags', wintypes.DWORD),
                    ('Reserved', ctypes.POINTER(ctypes.c_ulong))]

    class _HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [('Size', ctypes.c_ulong),
                    ('VendorID', ctypes.c_ushort),
                    ('ProductID', ctypes.c_ushort),
                    ('VersionNumber', ctypes.c_ushort)]

    class _HIDP_CAPS(ctypes.Structure):
        _fields_ = [('Usage', ctypes.c_ushort),
                    ('UsagePage', ctypes.c_ushort),
                    ('InputReportByteLength', ctypes.c_ushort),
                    ('OutputReportByteLength', ctypes.c_ushort),
                    ('FeatureReportByteLength', ctypes.c_ushort),
                    ('Reserved', ctypes.c_ushort * 17),
                    ('NumberLinkCollectionNodes', ctypes.c_ushort),
                    ('NumberInputButtonCaps', ctypes.c_ushort),
                    ('NumberInputValueCaps', ctypes.c_ushort),
                    ('NumberInputDataIndices', ctypes.c_ushort),
                    ('NumberOutputButtonCaps', ctypes.c_ushort),
                    ('NumberOutputValueCaps', ctypes.c_ushort),
                    ('NumberOutputDataIndices', ctypes.c_ushort),
                    ('NumberFeatureButtonCaps', ctypes.c_ushort),
                    ('NumberFeatureValueCaps', ctypes.c_ushort),
                    ('NumberFeatureDataIndices', ctypes.c_ushort)]

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [('Internal', ctypes.POINTER(ctypes.c_ulong)),
                    ('InternalHigh', ctypes.POINTER(ctypes.c_ulong)),
                    ('Offset', wintypes.DWORD),
                    ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE)]


_libs = {}


def _load():
    """Load setupapi/hid/kernel32 once, with argtypes pinned.

    Pinning argtypes is not cosmetic here: a HANDLE passed as a default int is
    truncated to 32 bits on a 64-bit build, and the resulting failure looks
    like a missing device rather than a calling-convention bug.
    """
    if _libs:
        return _libs
    if not _IS_WINDOWS:
        raise HIDError('HID adapters are supported on Windows only; this is %s'
                       % sys.platform)
    try:
        setupapi = ctypes.WinDLL('setupapi', use_last_error=True)
        hid = ctypes.WinDLL('hid', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    except OSError as e:
        raise HIDError('Windows HID libraries could not be loaded: %s' % e)

    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(_GUID)]
    hid.HidD_GetHidGuid.restype = None
    hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(_HIDD_ATTRIBUTES)]
    hid.HidD_GetAttributes.restype = wintypes.BOOLEAN
    hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE,
                                          ctypes.POINTER(ctypes.c_void_p)]
    hid.HidD_GetPreparsedData.restype = wintypes.BOOLEAN
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidD_FreePreparsedData.restype = wintypes.BOOLEAN
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS)]
    hid.HidP_GetCaps.restype = ctypes.c_long
    hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                          ctypes.c_ulong]
    hid.HidD_GetProductString.restype = wintypes.BOOLEAN

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_wchar_p, wintypes.HANDLE,
        wintypes.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_GUID),
        wintypes.DWORD, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                  wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD),
                                  ctypes.c_void_p]
    kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.BOOL, ctypes.c_wchar_p]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetOverlappedResult.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL]
    kernel32.CancelIo.argtypes = [wintypes.HANDLE]

    _libs.update(setupapi=setupapi, hid=hid, kernel32=kernel32)
    return _libs


def _describe_device(libs, path):
    """VID/PID, report sizes and product name, without claiming the device.

    Opening with a desired access of zero is deliberate: it answers every
    question below while leaving a device that is already open elsewhere
    (another copy of this tool, a vendor utility) undisturbed.
    """
    k, hid = libs['kernel32'], libs['hid']
    handle = k.CreateFileW(path, 0, _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                           None, _OPEN_EXISTING, 0, None)
    if handle == _INVALID_HANDLE_VALUE or not handle:
        return None
    try:
        attrs = _HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(attrs)
        if not hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
            return None
        ppd = ctypes.c_void_p()
        caps = _HIDP_CAPS()
        if hid.HidD_GetPreparsedData(handle, ctypes.byref(ppd)):
            try:
                hid.HidP_GetCaps(ppd, ctypes.byref(caps))
            finally:
                hid.HidD_FreePreparsedData(ppd)
        buf = ctypes.create_unicode_buffer(128)
        product = ''
        if hid.HidD_GetProductString(handle, buf, ctypes.sizeof(buf)):
            product = buf.value
        return {
            'path': path,
            'vendor_id': attrs.VendorID,
            'product_id': attrs.ProductID,
            'version': attrs.VersionNumber,
            'product': product,
            'usage_page': caps.UsagePage,
            'usage': caps.Usage,
            'input_len': caps.InputReportByteLength,
            'output_len': caps.OutputReportByteLength,
        }
    finally:
        k.CloseHandle(handle)


def enumerate_devices(vendor_id=None, product_id=None):
    """Every present HID interface, optionally filtered by VID/PID."""
    libs = _load()
    setupapi, hid = libs['setupapi'], libs['hid']

    guid = _GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    dev_info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if dev_info == _INVALID_HANDLE_VALUE:
        raise HIDError('SetupDiGetClassDevsW failed with error %d'
                       % ctypes.get_last_error())
    found = []
    try:
        index = 0
        while True:
            iface = _SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(iface)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    dev_info, None, ctypes.byref(guid), index,
                    ctypes.byref(iface)):
                break
            index += 1
            needed = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                dev_info, ctypes.byref(iface), None, 0, ctypes.byref(needed),
                None)
            if not needed.value:
                continue
            detail = ctypes.create_string_buffer(needed.value)
            ctypes.cast(detail,
                        ctypes.POINTER(wintypes.DWORD))[0] = _DETAIL_CB_SIZE
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    dev_info, ctypes.byref(iface), detail, needed.value,
                    ctypes.byref(needed), None):
                continue
            # The path is a wide string laid out right after the cbSize field.
            path = ctypes.wstring_at(
                ctypes.addressof(detail) + ctypes.sizeof(wintypes.DWORD))
            info = _describe_device(libs, path)
            if info is None:
                continue
            if vendor_id is not None and info['vendor_id'] != vendor_id:
                continue
            if product_id is not None and info['product_id'] != product_id:
                continue
            found.append(info)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(dev_info)
    return found


class HIDDevice(object):
    """One open HID device, exchanging whole reports.

    Report IDs are kept explicit rather than folded into the payload because
    the two adapters disagree about them: every CP2112 command has its own
    report ID, and the MCP2221A uses unnumbered reports (ID 0) with the command
    code as the first payload byte. Hiding that difference here would mean one
    of the two backends indexing its own packet off by one.
    """

    def __init__(self, info):
        libs = _load()
        self._k = libs['kernel32']
        self._info = info
        self._input_len = info['input_len']
        self._output_len = info['output_len']
        self._handle = self._k.CreateFileW(
            info['path'], _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED, None)
        if self._handle == _INVALID_HANDLE_VALUE or not self._handle:
            raise HIDError(
                'Could not open the HID device (Windows error %d). Another '
                'program may already have it open.' % ctypes.get_last_error())

    @property
    def info(self):
        return dict(self._info)

    def close(self):
        if self._handle:
            self._k.CloseHandle(self._handle)
            self._handle = None

    def _overlapped(self, func, buf, length, timeout_ms, what):
        ov = _OVERLAPPED()
        ov.hEvent = self._k.CreateEventW(None, True, False, None)
        if not ov.hEvent:
            raise HIDError('CreateEventW failed')
        transferred = wintypes.DWORD(0)
        try:
            ok = func(self._handle, buf, length, ctypes.byref(transferred),
                      ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != _ERROR_IO_PENDING:
                    raise HIDError('%s failed with Windows error %d'
                                   % (what, err))
                if self._k.WaitForSingleObject(
                        ov.hEvent, timeout_ms) != _WAIT_OBJECT_0:
                    self._k.CancelIo(self._handle)
                    # Let the cancelled request retire before the OVERLAPPED
                    # goes out of scope; the driver writes into it.
                    self._k.WaitForSingleObject(ov.hEvent, 200)
                    raise HIDError('%s timed out after %d ms - the adapter '
                                   'did not answer' % (what, timeout_ms))
                if not self._k.GetOverlappedResult(
                        self._handle, ctypes.byref(ov),
                        ctypes.byref(transferred), False):
                    raise HIDError('%s failed with Windows error %d'
                                   % (what, ctypes.get_last_error()))
        finally:
            self._k.CloseHandle(ov.hEvent)
        return transferred.value

    def write_report(self, report_id, payload=b'', timeout_ms=1000):
        """Send one output report, zero-padded to the device's report size.

        Windows rejects a short write outright, so the padding is not
        optional: the buffer handed to WriteFile has to be exactly
        OutputReportByteLength bytes with the report ID in front.
        """
        payload = bytes(payload)
        if len(payload) + 1 > self._output_len:
            raise HIDError('Report 0x%02X is %d bytes but this device takes at '
                           'most %d' % (report_id, len(payload) + 1,
                                        self._output_len))
        # create_string_buffer zero-fills up to the size it is given, and
        # that is where the padding comes from. Building a pre-padded
        # bytearray as well would leave two places to get the length right.
        buf = ctypes.create_string_buffer(bytes([report_id]) + payload,
                                          self._output_len)
        n = self._overlapped(self._k.WriteFile, buf, self._output_len,
                             timeout_ms,
                             'Writing HID report 0x%02X' % report_id)
        if n != self._output_len:
            raise HIDError('Writing HID report 0x%02X sent %d of %d bytes'
                           % (report_id, n, self._output_len))

    def read_report(self, timeout_ms=1000):
        """Return (report_id, payload) for the next input report."""
        buf = ctypes.create_string_buffer(self._input_len)
        n = self._overlapped(self._k.ReadFile, buf, self._input_len,
                             timeout_ms, 'Reading a HID report')
        raw = buf.raw[:n]
        if not raw:
            raise HIDError('Reading a HID report returned nothing')
        return raw[0], raw[1:]

    def drain(self, timeout_ms=20):
        """Discard reports left over from an earlier session.

        Both adapters keep answering after the program that asked goes away,
        so the first report read on a fresh connection can belong to the
        previous one. Reading a stale report as the answer to the current
        request shifts every register value that follows.
        """
        while True:
            try:
                self.read_report(timeout_ms)
            except HIDError:
                return


def availability(vendor_id, product_id, what):
    """A {'available', 'description'} pair for a backend's probe."""
    if not _IS_WINDOWS:
        return {'available': False,
                'description': '%s is supported on Windows only' % what}
    try:
        devices = enumerate_devices(vendor_id, product_id)
    except HIDError as e:
        return {'available': False, 'description': str(e)}
    if not devices:
        return {'available': False,
                'description': 'No %s is attached (looking for USB '
                               '%04X:%04X). No driver needs to be installed - '
                               'Windows uses its own HID driver.'
                               % (what, vendor_id, product_id)}
    name = devices[0].get('product') or what
    return {'available': True,
            'description': '%s (%d device%s)'
                           % (name, len(devices),
                              '' if len(devices) == 1 else 's')}


def usable_interfaces(devices):
    """Only the interfaces that can carry a command and an answer.

    A single USB device often publishes several HID collections and some of
    them have no output report at all - on this machine a wireless receiver
    publishes three, two of which are input-only. Opening one of those
    succeeds and then every write fails, which reads as a broken adapter
    rather than as the wrong interface having been chosen.
    """
    return [d for d in devices if d['output_len'] > 1 and d['input_len'] > 1]


def open_first(vendor_id, product_id, index, what):
    """Open the index'th matching device, or say precisely what is missing."""
    devices = enumerate_devices(vendor_id, product_id)
    if devices and not usable_interfaces(devices):
        raise HIDError('A %s is attached (USB %04X:%04X) but none of its %d '
                       'HID interfaces accepts reports in both directions.'
                       % (what, vendor_id, product_id, len(devices)))
    devices = usable_interfaces(devices)
    if not devices:
        raise HIDError('No %s found (USB %04X:%04X). Check that it is plugged '
                       'in; it needs no driver install on Windows.'
                       % (what, vendor_id, product_id))
    if index >= len(devices):
        raise HIDError('Adapter number %d was asked for but only %d %s%s '
                       'attached' % (index, len(devices), what,
                                     ' is' if len(devices) == 1 else 's are'))
    return HIDDevice(devices[index])
