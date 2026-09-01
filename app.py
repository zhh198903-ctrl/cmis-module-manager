"""Flask REST API for CMIS optical module management."""
# Single source of truth for the version shown in the UI, /api/version, the
# console banner and the operation manual footer. Bump this, not the copies.
__version__ = '2.6.3'
# The CMIS revision this build decodes. The page footer and /api/version both
# read it, so the two cannot drift apart the way they did through 5.4.
_CMIS_REVISION = '5.4'

import sys
import os
import shutil
import struct
import threading
import time
import urllib.parse
import webbrowser

from flask import Flask, jsonify, render_template, request

import cmis_registers as cmis
import updater
from i2c_interface import list_backends, create_backend

# The port appears in the bind call, the accepted Host/Origin values and the
# updater's health probe; keep them from drifting apart.
PORT = 5000

_BASE = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            template_folder=os.path.join(_BASE, 'templates'),
            static_folder=os.path.join(_BASE, 'static'))
# No CORS headers are served, but that is not what keeps other sites out - it
# only stops them reading the reply. See _reject_foreign_requests() below for
# the check that actually refuses them.

# ---------------------------------------------------------------------------
# Module-level state (single-user desktop app)
# ---------------------------------------------------------------------------
_state = {
    'backend': None,
    'connected': False,
    'bus': None,
    'address': None,
    # Page and bank currently selected, or None when unknown. Never assume
    # either without having selected it.
    'page': None,
    'bank': None,
    # Lanes this module actually has, from 01h:142/174. Eight until a module
    # says otherwise; CMIS 5.4 raised the ceiling to 256.
    'lanes': 8,
    # The 5.4 advertisement block, read once at connect.
    'caps': {},
}


# Progress of the running self-update, polled by the page while the download
# runs on a worker thread. Written only by that thread and read by request
# handlers; with one worker and one request at a time there is nothing to lock.
_update = {
    'state': 'idle',   # idle|starting|probing|downloading|verifying|installing|ready|error
    'version': '',
    'done': 0,
    'total': 0,
    'source': '',      # which mirror won the speed probe
    'message': '',
}
_UPDATE_BUSY = ('starting', 'probing', 'downloading', 'verifying',
                'installing', 'ready')


def _ok(data=None):
    return jsonify({'status': 'ok', 'data': data if data is not None else {}})


def _err(message, code=400):
    return jsonify({'status': 'error', 'message': message}), code


def _require_connected():
    """Return error response if not connected, else None."""
    if not _state['connected'] or _state['backend'] is None:
        return _err("Not connected to any module", 503)
    return None


# ---------------------------------------------------------------------------
# Local-origin request guard
# ---------------------------------------------------------------------------
# Serving no CORS headers does NOT make this API unreachable from other sites.
# It only stops the attacker reading the reply; the request itself is still
# delivered and still executes. A page on any site the user happens to open can
# POST here as a CORS "simple request" - no preflight, no consent - and drive
# I2C writes on whatever module is plugged in. So provenance is checked here
# instead of being assumed from same-origin hosting.
_LOCAL_NAMES = ('127.0.0.1', 'localhost', '[::1]')
_ALLOWED_HOSTS = frozenset(
    list(_LOCAL_NAMES) + [f'{n}:{PORT}' for n in _LOCAL_NAMES])
_ALLOWED_ORIGINS = frozenset(
    [f'http://{n}:{PORT}' for n in _LOCAL_NAMES] +
    [f'http://{n}' for n in _LOCAL_NAMES])


@app.before_request
def _reject_foreign_requests():
    """Refuse anything a browser tells us came from somewhere else.

    Sec-Fetch-Site rides on every request current browsers make, including the
    ones that carry no Origin at all - a bare <img> GET, a form post - so it is
    the only signal that covers state-changing GETs. Non-browser callers (curl,
    the test suite) send none of these headers and are left alone: a local
    process already runs with the user's rights and gains nothing by coming
    through the API.

    Only /api/ is guarded. The page and its assets grant no capability - every
    way to reach the module is under /api/ - so refusing them bought nothing and
    cost something real: following a link to this tool from a wiki or a chat
    message landed the user on a 403 instead of the UI.
    """
    if not request.path.startswith('/api/'):
        return None

    site = request.headers.get('Sec-Fetch-Site')
    if site and site not in ('same-origin', 'none'):
        return _err('Refused: this request came from another site', 403)

    origin = request.headers.get('Origin')
    if origin and origin not in _ALLOWED_ORIGINS:
        return _err('Refused: cross-origin request', 403)

    # DNS rebinding: the attacker points a name they own at 127.0.0.1, so the
    # browser calls their page same-origin and can read the replies too. The
    # Host header is what gives that away - the real UI never sends another.
    if request.host not in _ALLOWED_HOSTS:
        return _err('Refused: unexpected Host header', 403)

    # application/json is not a CORS-simple content type, so requiring it forces
    # a preflight that a foreign page cannot satisfy. This is the layer that
    # holds if a browser ever omits the headers above.
    if request.method == 'POST' and request.content_length and not request.is_json:
        return _err('Expected Content-Type: application/json', 415)
    return None


# ---------------------------------------------------------------------------
# Page-switch helpers
# ---------------------------------------------------------------------------

def _invalidate_page():
    """Forget which page and bank are selected; the next access re-selects.

    Call after anything that can move the PageMapping register out from under
    us: connecting, a module reset, or a raw write touching byte 0x7E or 0x7F.
    """
    _state['page'] = None
    _state['bank'] = None


def _set_page(page: int, bank: int = 0):
    """Select an upper-memory page and bank, waiting out the access hold-off.

    CMIS gives tBPC, the maximum Bank/Page Change time, as 10 ms; reading
    sooner can return the previous page's contents. Re-selecting what is
    already current costs another hold-off for nothing, and the panels read the
    same page repeatedly - a thresholds refresh alone re-selected page 02h
    twenty times - so skip the write when both are already known.

    Bank first, then page, always both: the module holds off acting on
    BankSelect until PageSelect is written (CMIS 8.2.15), so writing only the
    bank would leave the change pending and the next read would come from the
    old one.
    """
    if _state['page'] == page and _state['bank'] == bank:
        return
    _state['page'] = None  # unknown while the writes are in flight
    _state['bank'] = None
    _state['backend'].write_bytes(cmis.REG_BANK_SELECT[1], bytes([bank, page]))
    time.sleep(0.010)
    _state['page'] = page
    _state['bank'] = bank


def _checked(raw: bytes, where: str, length: int) -> bytes:
    """Refuse a read that came back short, naming the register that did it.

    An adapter that NAKs partway through returns fewer bytes than were asked
    for, and every decoder downstream unpacks a fixed width. Without this the
    user gets "unpack requires a buffer of 2 bytes" and no idea which register,
    which module, or that the cause is a flaky bus rather than the tool.
    """
    if len(raw) != length:
        raise IOError('short read from %s: asked %d byte%s, got %d'
                      % (where, length, '' if length == 1 else 's', len(raw)))
    return raw


def _read_lower(addr: int, length: int) -> bytes:
    return _checked(_state['backend'].read_bytes(addr, length),
                    'Lower:0x%02X' % addr, length)


def _read_upper(page: int, addr: int, length: int, bank: int = 0) -> bytes:
    _set_page(page, bank)
    return _checked(_state['backend'].read_bytes(addr, length),
                    '%02Xh:0x%02X%s' % (page, addr,
                                        '' if bank == 0 else ' bank %d' % bank),
                    length)


def _read_banks(page: int, addr: int, length: int, lanes: int = 0):
    """Yield (bank, raw) for every bank covering the module's lanes.

    For registers that pack several lanes into one byte - DataPath state is
    four bits per lane, the enable masks one bit - the bytes cannot simply be
    concatenated and sliced, so callers parse each bank and join the results.
    """
    lanes = lanes or _state['lanes']
    for bank in range((lanes + 7) // 8):
        yield bank, _read_upper(page, addr, length, bank)


def _masks_per_bank(value, banks: int) -> list:
    """One mask byte per bank, from either a byte or a list of them.

    A mask covers eight lanes, so a wider module needs one per bank. Callers
    written for eight lanes still send a single number and must keep working -
    and the page only sends a list once the module is actually wider, so both
    forms genuinely arrive.
    """
    if isinstance(value, (list, tuple)):
        vals = [int(v) & 0xFF for v in value]
    else:
        vals = [int(value) & 0xFF]
    return (vals + [0] * banks)[:banks]


def _read_banked(page: int, addr: int, per_lane: int, lanes: int = 0) -> bytes:
    """Read a lane-banked register for every lane the module has.

    Lane-banked pages only ever show eight lanes at a time: lanes 9-16 are the
    same addresses again in bank 1, and so on up to the 256 lanes CMIS 5.4
    allows. Concatenating the banks here lets every caller stay written as if
    the module were flat, which is what they all assumed when eight was the
    only possibility.
    """
    lanes = lanes or _state['lanes']
    out = bytearray()
    for bank in range((lanes + 7) // 8):
        out += _read_upper(page, addr, per_lane * 8, bank)
    return bytes(out[:per_lane * lanes])


def _discover_capabilities() -> dict:
    """Read the advertisements that decide how the rest of the session behaves.

    Lane count first: every panel sizes its tables from it, and CMIS 5.4 raised
    the ceiling from 32 lanes to 256 by giving 01h:142's two-bit bank field an
    escape value. Reading it once at connect beats guessing eight everywhere.

    A pre-5.4 module has nothing at 01h:173-174; it is not required to answer
    and may return zeros or garbage, so the escape value is what gates whether
    those bytes are believed at all.
    """
    caps = {'max_lanes': 8, 'banks_supported': 1, 'cmis_revision': ''}
    try:
        rev = _read_lower(0x01, 1)[0]
        caps['cmis_revision'] = f'{(rev >> 4) & 0x0F}.{rev & 0x0F}'
        b142 = _read_upper(*cmis.REG_BANKS_SUPPORTED)[0]
        ext = _read_upper(*cmis.REG_PAGES_EXT)
        caps.update(cmis.parse_supported_pages(b142, ext))
        caps.update(cmis.parse_misc_caps(_read_upper(*cmis.REG_MISC_CAPS)[0]))
        caps['default_polarity'] = cmis.parse_default_polarity(
            _read_upper(*cmis.REG_DEFAULT_POLARITY))
        sub = _read_lower(*cmis.REG_MODULE_SUBTYPE[1:])[0]
        hs = _read_lower(0x3D, 1)[0]
        ext = cmis.parse_extended_module_info(sub, hs)
        ext['heatsink_type_name'] = cmis.HEATSINK_TYPES.get(
            ext['heatsink_type'], f"Reserved (0x{ext['heatsink_type']:X})")
        ext['fiber_face_name'] = cmis.FIBER_FACE_TYPES.get(
            ext['fiber_face_type'], f"Reserved (0x{ext['fiber_face_type']:X})")
        caps.update(ext)
    except Exception:
        # A module that cannot answer the capability block is still usable at
        # the default eight lanes; failing the whole connection over an
        # optional advertisement would be worse than assuming the minimum.
        pass
    return caps


def _compute_module_capacity(apps: list) -> tuple:
    """Compute maximum concurrent host/media lanes across all Application Descriptors.

    Uses greedy selection: picks apps in descending lane-count order and adds
    each if its host-lane assignment does not overlap previously-selected apps.
    For mutually-exclusive advertisements (same lane range), only the biggest wins.
    For non-overlapping advertisements (like 2×400G-FR4), all fit and sum up.
    """
    if not apps:
        return (0, 0)

    parsed = []
    for a in apps:
        mask = a.get('host_lane_assign_mask', 0)
        if mask == 0:
            start = 0
        else:
            start = (mask & -mask).bit_length() - 1   # lowest set bit position
        h = a.get('host_lanes', 0) or 0
        m = a.get('media_lanes', 0) or 0
        end = start + max(h, 1)
        parsed.append((start, end, h, m))

    # Greedy: pick biggest non-overlapping apps first
    parsed.sort(key=lambda x: -x[2])
    occupied = set()
    sel_host = 0
    sel_media = 0
    for start, end, h, m in parsed:
        lanes = set(range(start, end))
        if lanes & occupied:
            continue
        occupied |= lanes
        sel_host += h
        sel_media += m
    return (sel_host, sel_media)


def _format_lanes_detail(apps: list, host_total: int, media_total: int) -> str:
    """Format a friendly lane breakdown string for display."""
    if len(apps) <= 1:
        return ''
    parts = [f"AppSel#{a['app_sel']}: {a['host_lanes']}H/{a['media_lanes']}M" for a in apps]
    return ' + '.join(parts)


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route('/api/backends', methods=['GET'])
def api_backends():
    backends = list_backends()
    return _ok(backends)


@app.route('/api/connect', methods=['POST'])
def api_connect():
    body = request.get_json(silent=True) or {}
    backend_name = body.get('backend', 'mock_dr8')
    try:
        bus = int(body.get('bus', 0))
        # Accept hex string or int for address
        addr_raw = body.get('address', 80)
        if isinstance(addr_raw, str):
            address = int(addr_raw, 0)
        else:
            address = int(addr_raw)
    except (ValueError, TypeError) as e:
        return _err(f"Invalid bus or address parameter: {e}")

    # Disconnect existing backend first
    if _state['backend'] is not None:
        try:
            _state['backend'].disconnect()
        except Exception:
            pass

    try:
        backend = create_backend(backend_name)
        backend.connect(bus, address)
    except Exception as e:
        _state['backend'] = None
        _state['connected'] = False
        return _err(str(e))

    _state['backend'] = backend
    _state['connected'] = True
    _state['bus'] = bus
    _state['address'] = address
    _invalidate_page()
    caps = _discover_capabilities()
    _state['lanes'] = caps.get('max_lanes', 8)
    _state['caps'] = caps
    return _ok({'backend': backend_name, 'bus': bus, 'address': address,
                'lanes': _state['lanes'],
                'cmis_revision': caps.get('cmis_revision', '')})


@app.route('/api/disconnect', methods=['GET', 'POST'])
def api_disconnect():
    if _state['backend'] is not None:
        try:
            _state['backend'].disconnect()
        except Exception:
            pass
    _state['backend'] = None
    _state['connected'] = False
    _state['bus'] = None
    _state['address'] = None
    # A stale lane count would size the next module's tables from this one.
    _state['lanes'] = 8
    _state['caps'] = {}
    _invalidate_page()
    return _ok({'message': 'Disconnected'})


@app.route('/api/module/info', methods=['GET'])
def api_module_info():
    err = _require_connected()
    if err:
        return err
    try:
        # Lower memory: identifier and CMIS revision
        ident_raw = _read_lower(0x00, 1)
        cmis_rev_raw = _read_lower(0x01, 1)
        mem_model_raw = _read_lower(0x02, 1)
        media_type_raw = _read_lower(0x55, 1)

        # Page 00h vendor information block (addresses per CMIS 5.4 Table 8-26)
        vendor_name_raw = _read_upper(*cmis.REG_VENDOR_NAME)
        vendor_oui_raw  = _read_upper(*cmis.REG_VENDOR_OUI)
        vendor_pn_raw   = _read_upper(*cmis.REG_VENDOR_PN)
        vendor_rev_raw  = _read_upper(*cmis.REG_VENDOR_REV)
        vendor_sn_raw   = _read_upper(*cmis.REG_VENDOR_SN)
        date_code_raw   = _read_upper(*cmis.REG_DATE_CODE)
        clei_raw        = _read_upper(*cmis.REG_CLEI_CODE)

        # Capabilities
        pwr_class_raw    = _read_upper(*cmis.REG_MODULE_PWR_CLASS)
        max_pwr_raw      = _read_upper(*cmis.REG_MODULE_MAX_POWER)
        cable_len_raw    = _read_upper(*cmis.REG_CABLE_LENGTH)
        connector_raw    = _read_upper(*cmis.REG_CONNECTOR_TYPE)
        media_lane_raw   = _read_upper(*cmis.REG_MEDIA_LANE_INFO)
        media_if_tech_raw= _read_upper(*cmis.REG_MEDIA_IF_TECH)

        # Parse ALL Application Descriptors (lower mem bytes 86-117)
        # and compute module aggregate capacity across non-overlapping apps.
        appdesc_raw = _read_lower(0x56, 32)
        # The same Media Interface ID means different things on MMF and SMF,
        # so the module's global media type picks the table.
        apps = cmis.parse_application_descriptors(appdesc_raw, media_type_raw[0])
        host_lanes_app1 = apps[0]['host_lanes'] if apps else 0
        media_lanes_app1 = apps[0]['media_lanes'] if apps else 0
        host_total, media_total = _compute_module_capacity(apps)
        lanes_detail = _format_lanes_detail(apps, host_total, media_total)

        # Active FW revision: Lower Memory 0x27-0x28 (Table 8-15)
        # HW revision: Page 01h:0x82-0x83
        try:
            fw_active_raw = _read_lower(0x27, 2)
            fw_rev = f"{fw_active_raw[0]}.{fw_active_raw[1]}"
        except Exception:
            fw_rev = "N/A"
        try:
            # Major and minor are adjacent single-byte registers, so this
            # spans both rather than matching either constant's length.
            hw_rev_raw = _read_upper(cmis.REG_HW_REV_MAJOR[0],
                                     cmis.REG_HW_REV_MAJOR[1], 2)
            hw_rev = f"{hw_rev_raw[0]}.{hw_rev_raw[1]}"
        except Exception:
            hw_rev = "N/A"

        pwr_class = cmis.parse_power_class(pwr_class_raw[0])

        return _ok({
            'module_id': ident_raw[0],
            'module_type': cmis.module_id_name(ident_raw[0]),
            'cmis_revision': cmis.cmis_revision_str(cmis_rev_raw[0]),
            'memory_model': 'Flat' if (mem_model_raw[0] >> 7) & 1 else 'Paged',
            'media_type': cmis.media_type_name(media_type_raw[0]),
            'vendor_name': cmis.parse_ascii(vendor_name_raw),
            'vendor_oui':  cmis.parse_oui(vendor_oui_raw),
            'vendor_pn':   cmis.parse_ascii(vendor_pn_raw),
            'vendor_rev':  cmis.parse_ascii(vendor_rev_raw),
            'vendor_sn':   cmis.parse_ascii(vendor_sn_raw),
            'date_code':   cmis.parse_ascii(date_code_raw),
            'clei_code':   cmis.parse_ascii(clei_raw),
            'power_class':       pwr_class['class'],
            'max_power_w':       round(cmis.parse_max_power_w(max_pwr_raw[0]), 2),
            'cable_length_m':    cmis.parse_cable_length_m(cable_len_raw[0]),
            'connector_type':    cmis.connector_type_name(connector_raw[0]),
            'connector_code':    connector_raw[0],
            'media_if_tech':     cmis.media_if_tech_name(media_if_tech_raw[0]),
            'media_if_tech_code':media_if_tech_raw[0],
            'media_lane_unsupported_mask': media_lane_raw[0],
            'host_lanes':  host_total,
            'media_lanes': media_total,
            'host_lanes_app1':  host_lanes_app1,
            'media_lanes_app1': media_lanes_app1,
            'lanes_detail':     lanes_detail,
            'fw_revision': fw_rev,
            'hw_revision': hw_rev,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/status', methods=['GET'])
def api_module_status():
    err = _require_connected()
    if err:
        return err
    try:
        state_raw = _read_lower(0x03, 1)             # CORRECT: byte 3, bits[3:1]
        temp_raw  = _read_lower(0x0E, 2)
        volt_raw  = _read_lower(0x10, 2)
        mod_flags_raw = _read_lower(0x08, 6)         # Module-Level Flags 0x08-0x0D
        aux1_raw  = _read_lower(0x12, 2)
        aux2_raw  = _read_lower(0x14, 2)
        aux3_raw  = _read_lower(0x16, 2)

        # Module flags byte 0x09 = Vcc/Temp Low/High Warning/Alarm bits
        f_byte9 = mod_flags_raw[1]
        temp_alarms = {
            'temp_high_alarm': bool((f_byte9 >> 0) & 1),
            'temp_low_alarm':  bool((f_byte9 >> 1) & 1),
            'temp_high_warn':  bool((f_byte9 >> 2) & 1),
            'temp_low_warn':   bool((f_byte9 >> 3) & 1),
            'vcc_high_alarm':  bool((f_byte9 >> 4) & 1),
            'vcc_low_alarm':   bool((f_byte9 >> 5) & 1),
            'vcc_high_warn':   bool((f_byte9 >> 6) & 1),
            'vcc_low_warn':    bool((f_byte9 >> 7) & 1),
        }
        any_alarm = any(temp_alarms.values()) or bool(mod_flags_raw[0] & 0x01)

        return _ok({
            'module_state': cmis.parse_module_state(state_raw[0]),
            'interrupt_asserted': cmis.parse_interrupt_asserted(state_raw[0]),
            'temperature_c': round(cmis.parse_temperature(temp_raw), 4),
            'voltage_v': round(cmis.parse_voltage(volt_raw), 4),
            'aux1_raw': struct.unpack(">h", aux1_raw[:2])[0] if len(aux1_raw) >= 2 else 0,
            'aux2_raw': struct.unpack(">h", aux2_raw[:2])[0] if len(aux2_raw) >= 2 else 0,
            'aux3_raw': struct.unpack(">h", aux3_raw[:2])[0] if len(aux3_raw) >= 2 else 0,
            'module_state_changed': bool(mod_flags_raw[0] & 0x01),
            'alarm_active': any_alarm,
            **temp_alarms,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/ext54', methods=['GET'])
def api_module_ext54():
    """The optional pages CMIS 5.4 added, for whichever of them exist.

    Each is gated on its advertisement in 01h:173-174 rather than probed: an
    unsupported page is not required to return anything meaningful, and
    rendering whatever came back would invent per-lane readings out of
    whatever the module happened to leave on the bus.
    """
    err = _require_connected()
    if err:
        return err
    caps = _state.get('caps') or {}
    out = {'available': {}}
    try:
        if caps.get('page_0ch_supported'):
            out['supported_pages'] = cmis.parse_supported_pages_map(
                _read_upper(*cmis.REG_SUPPORTED_PAGES_MAP))
            out['consolidated_pm'] = cmis.parse_feature_advertisement(
                _read_upper(*cmis.REG_CONSOLIDATED_PM))
            out['available']['0Ch'] = True

        if caps.get('page_60h_supported'):
            polarity = []
            for _b, raw in _read_banks(*cmis.REG_POLARITY_STATUS):
                polarity += cmis.parse_polarity_status(raw)
            out['polarity_status'] = polarity[:_state['lanes']]
            out['acq_counter_advert'] = _read_upper(*cmis.REG_ACQ_COUNTER_ADV)[0]
            out['available']['60h'] = True

        if caps.get('page_61h_supported'):
            counters = []
            for _b, raw in _read_banks(*cmis.REG_ACQ_COUNTERS):
                counters += cmis.parse_acquisition_counters(raw)
            for i, c in enumerate(counters):
                c['lane'] = i + 1
            out['acquisition_counters'] = counters[:_state['lanes']]
            out['available']['61h'] = True

        if caps.get('page_62h_supported'):
            thresholds = []
            for _b, raw in _read_banks(*cmis.REG_LANE_PWR_THRESHOLDS):
                thresholds += cmis.parse_lane_power_thresholds(raw)
            for i, t in enumerate(thresholds):
                t['lane'] = i + 1
            out['lane_power_thresholds'] = thresholds[:_state['lanes']]
            out['available']['62h'] = True

        if caps.get('media_lane_switching_supported'):
            out['media_lane_switching'] = cmis.parse_media_lane_switching(
                _read_upper(*cmis.REG_MLS_ADVERT)[0],
                _read_upper(*cmis.REG_MLS_REDIRECTION),
                _read_upper(*cmis.REG_MLS_ENABLE)[0],
                _read_upper(*cmis.REG_MLS_RESULT),
                _read_upper(*cmis.REG_MLS_STATUS))
            out['available']['6Dh'] = True
        return _ok(out)
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/acq_counters/reset', methods=['POST'])
def api_reset_acq_counters():
    """Clear acquisition counters for the given lanes (60h:192-193).

    Only the two lane-counter registers are offered. Table 8-189 prints byte
    195 twice, for the media-side and host-side data path resets both, so
    which one lives at 194 cannot be read off the document - and a reset
    command sent to the wrong address clears counters nobody asked about.
    """
    err = _require_connected()
    if err:
        return err
    caps = _state.get('caps') or {}
    if not caps.get('page_60h_supported'):
        return _err('This module does not advertise Page 60h', 400)
    body = request.get_json(silent=True) or {}
    lanes = body.get('lanes') or []
    side = body.get('side', 'both')
    if not isinstance(lanes, list) or not lanes:
        return _err('No lanes given; expected {"lanes": [1, 3, ...]}', 400)
    try:
        lanes = [_as_int(l, 'Lane') for l in lanes]
    except ValueError as e:
        return _err(str(e), 400)
    out_of_range = [l for l in lanes if not 1 <= l <= _state['lanes']]
    if out_of_range:
        return _err('This module has %d lanes; %s out of range'
                    % (_state['lanes'], out_of_range), 400)
    try:
        by_bank = {}
        for lane in lanes:
            b, bit = divmod(int(lane) - 1, 8)
            by_bank[b] = by_bank.get(b, 0) | (1 << bit)
        for bank, mask in by_bank.items():
            _set_page(0x60, bank)
            if side in ('rx', 'both'):
                _state['backend'].write_bytes(cmis.REG_RESET_ACQ_RX[1], bytes([mask]))
            if side in ('tx', 'both'):
                _state['backend'].write_bytes(cmis.REG_RESET_ACQ_TX[1], bytes([mask]))
        return _ok({'lanes': lanes, 'side': side})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/media_lane_switching', methods=['POST'])
def api_media_lane_switching():
    """Stage a media lane redirection, and optionally commit it.

    Refused unless the mapping is a permutation of the lanes: the spec
    requires one, and a module that validates the command would reject it
    anyway - after the host had already been told the write succeeded.
    """
    err = _require_connected()
    if err:
        return err
    caps = _state.get('caps') or {}
    if not caps.get('media_lane_switching_supported'):
        return _err('This module does not advertise media lane switching', 400)
    body = request.get_json(silent=True) or {}
    mapping = body.get('redirection') or []
    enable = body.get('enable')
    commit = bool(body.get('commit', False))
    try:
        if mapping:
            targets = [int(v) for v in mapping][:8]
            if sorted(targets) != list(range(1, len(targets) + 1)):
                return _err('Redirection must be a permutation of the media '
                            'lanes; the module would reject anything else', 400)
            _set_page(0x6D)
            _state['backend'].write_bytes(cmis.REG_MLS_REDIRECTION[1],
                                          bytes(targets))
        if enable is not None:
            _set_page(0x6D)
            _state['backend'].write_bytes(cmis.REG_MLS_ENABLE[1],
                                          bytes([1 if enable else 0]))
        if commit:
            _set_page(0x6D)
            _state['backend'].write_bytes(cmis.REG_MLS_COMMIT[1], bytes([1]))
            time.sleep(0.1)
        return _ok({'committed': commit})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/capabilities', methods=['GET'])
def api_module_capabilities():
    """The advertisement block, plus which of its fields CMIS 5.4 introduced.

    The new-in-5.4 list is served rather than duplicated in the page, so the
    badge in the UI and the field list in the manual cannot drift apart from
    what the decoder actually reads.
    """
    err = _require_connected()
    if err:
        return err
    caps = dict(_state.get('caps') or {})
    caps['lanes'] = _state['lanes']
    caps['new_in_5_4'] = sorted(cmis.NEW_IN_5_4)
    return _ok(caps)


@app.route('/api/module/monitoring', methods=['GET'])
def api_module_monitoring():
    err = _require_connected()
    if err:
        return err
    try:
        # DataPath state and Config Status pack 4 bits per lane, so they are
        # parsed per bank; the monitors are 2 bytes per lane and concatenate.
        dp_states, cfg_statuses = [], []
        for _bank, raw in _read_banks(*cmis.REG_DP_STATE):
            dp_states += cmis.parse_dp_states(raw)
        for _bank, raw in _read_banks(*cmis.REG_CONFIG_STATUS):
            cfg_statuses += cmis.parse_config_status(raw)
        tx_power_raw  = _read_banked(*cmis.REG_TX_POWER[:2], 2)
        tx_bias_raw   = _read_banked(*cmis.REG_TX_BIAS[:2], 2)
        rx_power_raw  = _read_banked(*cmis.REG_RX_POWER[:2], 2)

        lanes = []
        for i in range(_state['lanes']):
            tx_uw = cmis.parse_power_uw(tx_power_raw[i*2:(i+1)*2])
            rx_uw = cmis.parse_power_uw(rx_power_raw[i*2:(i+1)*2])
            bias_ma = cmis.parse_tx_bias_ma(tx_bias_raw[i*2:(i+1)*2])
            lanes.append({
                'lane': i + 1,
                'tx_power_uw': round(tx_uw, 2),
                'tx_power_dbm': round(cmis.uw_to_dbm(tx_uw), 2),
                'rx_power_uw': round(rx_uw, 2),
                'rx_power_dbm': round(cmis.uw_to_dbm(rx_uw), 2),
                'tx_bias_ma': round(bias_ma, 3),
                'datapath_state': dp_states[i],
                'config_status': cfg_statuses[i],
            })

        return _ok({'lanes': lanes})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/datapath', methods=['GET'])
def api_datapath_get():
    err = _require_connected()
    if err:
        return err
    try:
        # Every one of these is per bank: a mask byte covers eight lanes and
        # AppSelect one byte each, so lanes 9+ live at the same addresses in
        # the next bank rather than further along the same page.
        def mask_per_bank(reg):
            page, addr, _ = reg
            return [raw[0] for _bank, raw in _read_banks(page, addr, 1)]

        dp_deinit_masks = mask_per_bank(cmis.REG_DP_DEINIT)
        tx_disable_masks = mask_per_bank(cmis.REG_TX_OUTPUT_DIS)
        try:
            tx_pol_masks = mask_per_bank(cmis.REG_TX_POL_FLIP)
            rx_pol_masks = mask_per_bank(cmis.REG_RX_POL_FLIP)
        except Exception:
            tx_pol_masks = rx_pol_masks = [0] * len(dp_deinit_masks)

        app_select = []
        for _bank, raw in _read_banks(*cmis.REG_APP_SELECT):
            app_select += cmis.unpack_appselect(raw)

        # Bank 0's values are what the summary fields have always reported.
        tx_disable_mask = tx_disable_masks[0]
        dp_deinit_mask = dp_deinit_masks[0]
        tx_pol_mask = tx_pol_masks[0]
        rx_pol_mask = rx_pol_masks[0]

        # One bit per lane means bank b covers lanes 8b+1..8b+8, so the bit
        # index restarts at every bank boundary rather than running to 256.
        lanes = []
        for i in range(_state['lanes']):
            b, bit = divmod(i, 8)
            lanes.append({
                'lane': i + 1,
                'tx_enable': not bool((tx_disable_masks[b] >> bit) & 1),
                'dp_deinit': bool((dp_deinit_masks[b] >> bit) & 1),
                'app_select': app_select[i] if i < len(app_select) else 0,
                'tx_polarity_flip': bool((tx_pol_masks[b] >> bit) & 1),
                'rx_polarity_flip': bool((rx_pol_masks[b] >> bit) & 1),
            })

        return _ok({
            'tx_disable_mask': tx_disable_mask,
            'dp_deinit_mask':  dp_deinit_mask,
            'tx_polarity_flip_mask': tx_pol_mask,
            'rx_polarity_flip_mask': rx_pol_mask,
            'app_select': app_select,
            'lanes': lanes,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/applications', methods=['GET'])
def api_applications():
    """Read Application Descriptors from lower memory bytes 86-117."""
    err = _require_connected()
    if err:
        return err
    try:
        data = _read_lower(0x56, 32)  # 8 descriptors × 4 bytes
        media_type = _read_lower(0x55, 1)[0]
        apps = cmis.parse_application_descriptors(data, media_type)
        return _ok({'applications': apps})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/control', methods=['GET'])
def api_module_control_get():
    """Read Module Control register at lower memory byte 0x1A."""
    err = _require_connected()
    if err:
        return err
    try:
        ctrl_raw = _read_lower(0x1A, 1)
        # 'raw' backs the UI hover tooltips, which quote the byte a control maps to
        return _ok(dict(cmis.parse_module_control(ctrl_raw[0]), raw=ctrl_raw[0]))
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/control', methods=['POST'])
def api_module_control_set():
    """Write Module Control register (software reset / low power)."""
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        action = body.get('action', '')

        # Byte 0x1A packs unrelated controls together, so read it first and
        # change only the requested bits. Rebuilding the byte from scratch used
        # to clear SquelchMethodSelect and BankBroadcastEnable every time the
        # user toggled low power.
        current = _read_lower(0x1A, 1)[0]

        if action == 'reset':
            val = cmis.update_module_control(current, software_reset=True)
        elif action == 'low_power':
            val = cmis.update_module_control(current, low_pwr=True)
        elif action == 'high_power':
            val = cmis.update_module_control(current, low_pwr=False)
        else:
            # Direct field set: only fields actually present in the body move.
            val = cmis.update_module_control(
                current,
                low_pwr=body.get('low_pwr'),
                software_reset=body.get('software_reset'),
                allow_lp_hw=body.get('allow_lp_hw'),
                squelch_method=body.get('squelch_method'),
                bank_broadcast=body.get('bank_broadcast'),
            )

        _state['backend'].write_bytes(cmis.REG_MODULE_CONTROL[1], bytes([val]))
        time.sleep(0.05)
        # A reset restarts the module, which restores PageMapping to its
        # default, so the page we think is selected no longer applies.
        _invalidate_page()
        return _ok({'message': f'Module control written (0x{val:02X})', 'value': val})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/datapath', methods=['POST'])
def api_datapath_set():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        banks = (_state['lanes'] + 7) // 8
        tx_disable = _masks_per_bank(body.get('tx_disable_mask', 0), banks)
        tx_pol = _masks_per_bank(body.get('tx_polarity_flip_mask', 0), banks)
        rx_pol = _masks_per_bank(body.get('rx_polarity_flip_mask', 0), banks)
        app_select = body.get('app_select', [1] * _state['lanes'])
        apply = bool(body.get('apply', False))

        for bank in range(banks):
            _set_page(0x10, bank)
            # 129-130 are contiguous: InputPolarityFlipTx then OutputDisableTx
            _state['backend'].write_bytes(cmis.REG_TX_POL_FLIP[1],
                                          bytes([tx_pol[bank], tx_disable[bank]]))
            _state['backend'].write_bytes(cmis.REG_RX_POL_FLIP[1],
                                          bytes([rx_pol[bank]]))
            _state['backend'].write_bytes(
                cmis.REG_APP_SELECT[1],
                cmis.pack_appselect(app_select[bank * 8:bank * 8 + 8]))

        # ApplyDPInit latches the Staged Control Set into the active
        # configuration. Every bank gets it: a module is not half configured.
        if apply:
            for bank in range(banks):
                _set_page(0x10, bank)
                _state['backend'].write_bytes(cmis.REG_APPLY_DATAPATH[1],
                                              bytes([0xFF]))
            time.sleep(0.1)

        return _ok({'message': 'DataPath configuration written'})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/flags', methods=['GET'])
def api_module_flags():
    err = _require_connected()
    if err:
        return err
    try:
        # 11h:135-152 are contiguous lane flag bytes; one burst read beats 18
        # page-select + 5 ms settle cycles on real hardware.
        blocks = [raw for _bank, raw in _read_banks(*cmis.REG_TX_FAULT_FLAGS[:2], 18)]

        def flags(addr):
            # One bit per lane, so each bank contributes its own eight.
            out = []
            for blk in blocks:
                out += cmis.parse_lane_flags(blk[addr - 0x87])
            return out[:_state['lanes']]

        tx_fault  = flags(0x87)
        tx_los    = flags(0x88)
        tx_cdrlol = flags(0x89)
        txpwr_ha  = flags(0x8B)
        txpwr_la  = flags(0x8C)
        txpwr_hw  = flags(0x8D)
        txpwr_lw  = flags(0x8E)
        txbias_ha = flags(0x8F)
        txbias_la = flags(0x90)
        txbias_hw = flags(0x91)
        txbias_lw = flags(0x92)
        rx_los    = flags(0x93)
        rx_cdrlol = flags(0x94)
        rxpwr_ha  = flags(0x95)
        rxpwr_la  = flags(0x96)
        rxpwr_hw  = flags(0x97)
        rxpwr_lw  = flags(0x98)

        lanes = []
        for i in range(_state['lanes']):
            lanes.append({
                'lane': i + 1,
                'tx_fault':           tx_fault[i],
                'tx_los':             tx_los[i],
                'tx_cdr_lol':         tx_cdrlol[i],
                'tx_power_high_alarm': txpwr_ha[i],
                'tx_power_low_alarm':  txpwr_la[i],
                'tx_power_high_warn':  txpwr_hw[i],
                'tx_power_low_warn':   txpwr_lw[i],
                'tx_bias_high_alarm':  txbias_ha[i],
                'tx_bias_low_alarm':   txbias_la[i],
                'tx_bias_high_warn':   txbias_hw[i],
                'tx_bias_low_warn':    txbias_lw[i],
                'rx_los':              rx_los[i],
                'rx_cdr_lol':          rx_cdrlol[i],
                'rx_power_high_alarm': rxpwr_ha[i],
                'rx_power_low_alarm':  rxpwr_la[i],
                'rx_power_high_warn':  rxpwr_hw[i],
                'rx_power_low_warn':   rxpwr_lw[i],
            })
        return _ok({'lanes': lanes})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/thresholds', methods=['GET'])
def api_module_thresholds():
    err = _require_connected()
    if err:
        return err
    try:
        def rd(addr): return _read_upper(0x02, addr, 2)

        temp_ha = cmis.parse_temperature(rd(0x80))
        temp_la = cmis.parse_temperature(rd(0x82))
        temp_hw = cmis.parse_temperature(rd(0x84))
        temp_lw = cmis.parse_temperature(rd(0x86))
        vcc_ha  = cmis.parse_threshold_voltage(rd(0x88))
        vcc_la  = cmis.parse_threshold_voltage(rd(0x8A))
        vcc_hw  = cmis.parse_threshold_voltage(rd(0x8C))
        vcc_lw  = cmis.parse_threshold_voltage(rd(0x8E))

        txpwr_ha_uw = cmis.parse_power_uw(rd(0xB0))
        txpwr_la_uw = cmis.parse_power_uw(rd(0xB2))
        txpwr_hw_uw = cmis.parse_power_uw(rd(0xB4))
        txpwr_lw_uw = cmis.parse_power_uw(rd(0xB6))

        txbias_ha = cmis.parse_tx_bias_ma(rd(0xB8))
        txbias_la = cmis.parse_tx_bias_ma(rd(0xBA))
        txbias_hw = cmis.parse_tx_bias_ma(rd(0xBC))
        txbias_lw = cmis.parse_tx_bias_ma(rd(0xBE))

        rxpwr_ha_uw = cmis.parse_power_uw(rd(0xC0))
        rxpwr_la_uw = cmis.parse_power_uw(rd(0xC2))
        rxpwr_hw_uw = cmis.parse_power_uw(rd(0xC4))
        rxpwr_lw_uw = cmis.parse_power_uw(rd(0xC6))

        return _ok({
            'temp_high_alarm': round(temp_ha, 2),
            'temp_low_alarm':  round(temp_la, 2),
            'temp_high_warn':  round(temp_hw, 2),
            'temp_low_warn':   round(temp_lw, 2),
            'vcc_high_alarm':  round(vcc_ha, 4),
            'vcc_low_alarm':   round(vcc_la, 4),
            'vcc_high_warn':   round(vcc_hw, 4),
            'vcc_low_warn':    round(vcc_lw, 4),
            'tx_power_high_alarm_dbm': round(cmis.uw_to_dbm(txpwr_ha_uw), 2),
            'tx_power_low_alarm_dbm':  round(cmis.uw_to_dbm(txpwr_la_uw), 2),
            'tx_power_high_warn_dbm':  round(cmis.uw_to_dbm(txpwr_hw_uw), 2),
            'tx_power_low_warn_dbm':   round(cmis.uw_to_dbm(txpwr_lw_uw), 2),
            'tx_bias_high_alarm_ma':   round(txbias_ha, 3),
            'tx_bias_low_alarm_ma':    round(txbias_la, 3),
            'tx_bias_high_warn_ma':    round(txbias_hw, 3),
            'tx_bias_low_warn_ma':     round(txbias_lw, 3),
            'rx_power_high_alarm_dbm': round(cmis.uw_to_dbm(rxpwr_ha_uw), 2),
            'rx_power_low_alarm_dbm':  round(cmis.uw_to_dbm(rxpwr_la_uw), 2),
            'rx_power_high_warn_dbm':  round(cmis.uw_to_dbm(rxpwr_hw_uw), 2),
            'rx_power_low_warn_dbm':   round(cmis.uw_to_dbm(rxpwr_lw_uw), 2),
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/squelch', methods=['GET'])
def api_squelch_get():
    err = _require_connected()
    if err:
        return err
    try:
        # 131-132 contiguous, 138-139 contiguous; one byte covers eight lanes
        # so a wider module has the same registers again in the next bank.
        tx_sqs, tx_sfs, rx_ods, rx_sqs = [], [], [], []
        for _bank, raw in _read_banks(0x10, cmis.REG_TX_SQUELCH_DIS[1], 2):
            tx_sqs.append(raw[0]); tx_sfs.append(raw[1])
        for _bank, raw in _read_banks(0x10, cmis.REG_RX_OUTPUT_DIS[1], 2):
            rx_ods.append(raw[0]); rx_sqs.append(raw[1])
        return _ok({
            # Scalars stay for the eight-lane case every existing caller assumes.
            'tx_squelch_disable': tx_sqs[0],
            'tx_squelch_force':   tx_sfs[0],
            'rx_output_disable':  rx_ods[0],
            'rx_squelch_disable': rx_sqs[0],
            'tx_squelch_disable_banks': tx_sqs,
            'tx_squelch_force_banks':   tx_sfs,
            'rx_output_disable_banks':  rx_ods,
            'rx_squelch_disable_banks': rx_sqs,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/squelch', methods=['POST'])
def api_squelch_set():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        # These read back per bank, so they have to be written per bank too:
        # a 16-lane module was reading sixteen lanes of squelch state and
        # writing only the first eight, and the page's per-bank list arrived
        # here as a list where an int was expected.
        banks = (_state['lanes'] + 7) // 8
        tx_sq = _masks_per_bank(body.get('tx_squelch_disable', 0), banks)
        tx_sf = _masks_per_bank(body.get('tx_squelch_force',   0), banks)
        rx_od = _masks_per_bank(body.get('rx_output_disable',  0), banks)
        rx_sq = _masks_per_bank(body.get('rx_squelch_disable', 0), banks)
        for b in range(banks):
            _set_page(0x10, b)
            _state['backend'].write_bytes(cmis.REG_TX_SQUELCH_DIS[1],
                                          bytes([tx_sq[b], tx_sf[b]]))
            _state['backend'].write_bytes(cmis.REG_RX_OUTPUT_DIS[1],
                                          bytes([rx_od[b], rx_sq[b]]))
        return _ok({'message': 'Squelch/output controls written'})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/loopback', methods=['GET'])
def api_loopback_get():
    err = _require_connected()
    if err:
        return err
    try:
        # Four contiguous bitmask bytes, one bit per lane, so a wider module
        # has the same four again in the next bank.
        blocks = [raw for _b, raw in _read_banks(cmis.REG_MEDIA_OUT_LB[0], cmis.REG_MEDIA_OUT_LB[1], 4)]
        cols = [[blk[i] for blk in blocks] for i in range(4)]
        return _ok({
            # Bank 0 stays scalar for every caller written before banks existed.
            'media_side_output': cols[0][0],
            'media_side_input':  cols[1][0],
            'host_side_output':  cols[2][0],
            'host_side_input':   cols[3][0],
            'media_side_output_banks': cols[0],
            'media_side_input_banks':  cols[1],
            'host_side_output_banks':  cols[2],
            'host_side_input_banks':   cols[3],
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/loopback', methods=['POST'])
def api_loopback_set():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        banks = (_state['lanes'] + 7) // 8
        media_out = _masks_per_bank(body.get('media_side_output', 0), banks)
        media_in  = _masks_per_bank(body.get('media_side_input',  0), banks)
        host_out  = _masks_per_bank(body.get('host_side_output',  0), banks)
        host_in   = _masks_per_bank(body.get('host_side_input',   0), banks)
        for b in range(banks):
            _set_page(0x13, b)
            _state['backend'].write_bytes(cmis.REG_MEDIA_OUT_LB[1], bytes([media_out[b], media_in[b],
                                                       host_out[b], host_in[b]]))
        return _ok({'message': 'Loopback configuration written'})
    except Exception as e:
        return _err(str(e), 500)


def _read_prbs_block(base_addr: int) -> dict:
    """Read the 8-byte PRBS block for every bank: masks, then pattern x4.

    The masks are one bit per lane and the patterns four bits, so both repeat
    per bank. Bank 0's values stay under the original keys because everything
    written before banks existed reads them.
    """
    blocks = [raw for _b, raw in _read_banks(0x13, base_addr, 8)]
    patterns = []
    for raw in blocks:
        patterns += cmis.unpack_prbs_patterns(raw[4:8])
    return {
        'enable_mask':       blocks[0][0],
        'invert_mask':       blocks[0][1],
        'byte_swap_mask':    blocks[0][2],
        'fec_mask':          blocks[0][3],   # PreFEC for gen, PostFEC for chk
        'patterns':          patterns[:_state['lanes']],
        'enable_mask_banks':    [b[0] for b in blocks],
        'invert_mask_banks':    [b[1] for b in blocks],
        'byte_swap_mask_banks': [b[2] for b in blocks],
        'fec_mask_banks':       [b[3] for b in blocks],
    }


@app.route('/api/module/prbs', methods=['GET'])
def api_prbs_get():
    err = _require_connected()
    if err:
        return err
    try:
        # Try to also read pattern checker LOL flags from Page 14h
        try:
            host_lol = _read_upper(*cmis.REG_HOST_PRBS_LOL)[0]
            media_lol = _read_upper(*cmis.REG_MEDIA_PRBS_LOL)[0]
        except Exception:
            host_lol = 0
            media_lol = 0
        return _ok({
            'host_gen':  _read_prbs_block(0x90),
            'media_gen': _read_prbs_block(0x98),
            'host_chk':  _read_prbs_block(0xA0),
            'media_chk': _read_prbs_block(0xA8),
            'host_chk_lol_mask':  host_lol,
            'media_chk_lol_mask': media_lol,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/prbs', methods=['POST'])
def api_prbs_set():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        banks = (_state['lanes'] + 7) // 8
        for key, base_addr in [
            ('host_gen',  0x90),
            ('media_gen', 0x98),
            ('host_chk',  0xA0),
            ('media_chk', 0xA8),
        ]:
            section = body.get(key, {})
            if not section:
                continue
            en  = _masks_per_bank(section.get('enable_mask', 0), banks)
            inv = _masks_per_bank(section.get('invert_mask', 0), banks)
            sw  = _masks_per_bank(section.get('byte_swap_mask', 0), banks)
            fec = _masks_per_bank(section.get('fec_mask', 0), banks)
            # Patterns are one flat list over all lanes; each bank takes its
            # own eight, so lanes 9-16 are not left on whatever was there.
            patterns = list(section.get('patterns', [0] * _state['lanes']))
            patterns += [0] * (banks * 8 - len(patterns))
            for b in range(banks):
                _set_page(0x13, b)
                block = (bytes([en[b], inv[b], sw[b], fec[b]])
                         + cmis.pack_prbs_patterns(patterns[b * 8:b * 8 + 8]))
                _state['backend'].write_bytes(base_addr, block)
        return _ok({'message': 'PRBS configuration written'})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/snr', methods=['GET'])
def api_module_snr():
    """Read per-lane SNR using diagnostic selector 0x06."""
    err = _require_connected()
    if err:
        return err
    try:
        _set_page(0x14)
        _state['backend'].write_bytes(cmis.REG_DIAG_SELECTOR[1], bytes([0x06]))
        time.sleep(0.005)
        # Selector 0x06: bytes 192-207 reserved, 208-223 host SNR, 240-255 media SNR
        # Host SNR at offset 16 (bytes 208-223), media SNR at offset 48.
        # Each bank carries its own eight lanes at the same offsets.
        host_snr = []
        media_snr = []
        for _bank, data in _read_banks(*cmis.REG_DIAG_DATA):
            for i in range(8):
                host_snr.append(round(cmis.parse_snr_db(data[16 + i*2:18 + i*2]), 3))
                media_snr.append(round(cmis.parse_snr_db(data[48 + i*2:50 + i*2]), 3))
        host_snr = host_snr[:_state['lanes']]
        media_snr = media_snr[:_state['lanes']]
        return _ok({
            'host_snr_db':  host_snr,
            'media_snr_db': media_snr,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/ber', methods=['GET'])
def api_module_ber():
    err = _require_connected()
    if err:
        return err
    try:
        # Write selector 0x01 = BER F16
        _set_page(0x14)
        _state['backend'].write_bytes(cmis.REG_DIAG_SELECTOR[1], bytes([0x01]))
        time.sleep(0.005)
        # Host BER at 0xC0–0xCF, Media BER at 0xD0–0xDF (8 lanes × 2B each)
        lanes = []
        for _bank, ber_raw in _read_banks(*cmis.REG_DIAG_DATA[:2], 32):
            for i in range(8):
                lanes.append({
                    'lane': len(lanes) + 1,
                    'host_ber': cmis.parse_f16_ber(ber_raw[i*2:(i+1)*2]),
                    'media_ber': cmis.parse_f16_ber(ber_raw[16 + i*2:16 + (i+1)*2]),
                })
        lanes = lanes[:_state['lanes']]
        return _ok({'lanes': lanes})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/laser', methods=['GET'])
def api_laser_get():
    """Read laser tuning capabilities (Page 04h) and current state (Page 12h)."""
    err = _require_connected()
    if err:
        return err
    try:
        # Capabilities (Page 04h)
        grid_sup = _read_upper(*cmis.REG_GRID_SUPPORTED)
        fine_res = _read_upper(*cmis.REG_FINE_RESOLUTION)
        fine_low = _read_upper(*cmis.REG_FINE_LOW_OFFSET)
        fine_high = _read_upper(*cmis.REG_FINE_HIGH_OFFSET)
        pwr_min = _read_upper(*cmis.REG_PROG_PWR_MIN)
        pwr_max = _read_upper(*cmis.REG_PROG_PWR_MAX)

        grids_supported = []
        grid_names = ['3.125 GHz','6.25 GHz','12.5 GHz','25 GHz',
                      '50 GHz','100 GHz','33 GHz','75 GHz']
        for i, name in enumerate(grid_names):
            if (grid_sup[0] >> i) & 1:
                grids_supported.append(name)
        if (grid_sup[1] >> 6) & 1:
            grids_supported.append('150 GHz')
        # CMIS 5.4 added the 300 GHz grid; a 5.3 module leaves this bit clear.
        grid_300_supported = bool((grid_sup[1] >> 5) & 1)
        if grid_300_supported:
            grids_supported.append('300 GHz')
        fine_tuning_supported = bool((grid_sup[1] >> 7) & 1)

        grid_300_range = None
        if grid_300_supported:
            g300 = _read_upper(*cmis.REG_GRID_300_CHANNELS)
            grid_300_range = [struct.unpack('>h', g300[0:2])[0],
                              struct.unpack('>h', g300[2:4])[0]]
        # 04h:196.6 advertises the 5.4 power-relative supervision thresholds.
        rel_supported = bool((_read_upper(*cmis.REG_REL_THR_CAP)[0] >> 6) & 1)
        rel_thresholds = (cmis.parse_relative_thresholds(
                              _read_upper(*cmis.REG_REL_THRESHOLDS))
                          if rel_supported else {})

        # Current state (Page 12h), bank by bank
        grid_spacing = _read_banked(*cmis.REG_GRID_SPACING_TX[:2], 1)
        channel_num  = _read_banked(*cmis.REG_CHANNEL_NUM_TX[:2], 2)
        fine_offset  = _read_banked(*cmis.REG_FINE_OFFSET_TX[:2], 2)
        current_freq = _read_banked(*cmis.REG_CURRENT_FREQ_TX[:2], 4)
        target_pwr   = _read_banked(*cmis.REG_TARGET_PWR_TX[:2], 2)
        tuning_status= _read_banked(*cmis.REG_TUNING_STATUS_TX[:2], 1)

        grid_codes = cmis.GRID_CODES

        lanes = []
        for i in range(_state['lanes']):
            gs = grid_spacing[i]
            gc = (gs >> 4) & 0x0F
            fine_en = bool(gs & 0x01)
            ch = struct.unpack(">h", channel_num[i*2:i*2+2])[0]
            ft = struct.unpack(">h", fine_offset[i*2:i*2+2])[0]
            freq_mhz = struct.unpack(">I", current_freq[i*4:i*4+4])[0]
            freq_thz = freq_mhz / 1e6
            tgt_pwr = struct.unpack(">h", target_pwr[i*2:i*2+2])[0] * 0.01
            st = tuning_status[i]
            lanes.append({
                'lane': i + 1,
                'grid': grid_codes.get(gc, f'Unknown({gc})'),
                'grid_code': gc,
                'channel': ch,
                'fine_tuning_enabled': fine_en,
                'fine_offset_ghz': ft * 0.001,
                'frequency_thz': round(freq_thz, 6),
                'target_power_dbm': round(tgt_pwr, 2),
                'tuning_in_progress': bool((st >> 1) & 1),
                'wavelength_locked': not bool(st & 1),
                # 5.4: when set, Page 02h's absolute Tx power thresholds stop
                # applying to this lane and the relative ones take over.
                'relative_thresholds_enabled': bool((gs >> 1) & 1),
            })

        return _ok({
            'grids_supported': grids_supported,
            'grid_300ghz_supported': grid_300_supported,
            'grid_300ghz_range': grid_300_range,
            'relative_power_thresholds_supported': rel_supported,
            'relative_power_thresholds': rel_thresholds,
            'fine_tuning_supported': fine_tuning_supported,
            'fine_resolution_ghz': struct.unpack(">H", fine_res)[0] * 0.001,
            'fine_range_ghz': [
                struct.unpack(">h", fine_low)[0] * 0.001,
                struct.unpack(">h", fine_high)[0] * 0.001,
            ],
            'power_range_dbm': [
                struct.unpack(">h", pwr_min)[0] * 0.01,
                struct.unpack(">h", pwr_max)[0] * 0.01,
            ],
            'lanes': lanes,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/laser', methods=['POST'])
def api_laser_set():
    """Write laser tuning parameters to Page 12h."""
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        _set_page(0x12)
        lanes = body.get('lanes', [])
        # A body this handler does not understand used to come back as
        # "parameters written" having written nothing, so a caller with the
        # wrong shape was told its tuning had been applied.
        if not isinstance(lanes, list) or not lanes:
            return _err('No lanes given; expected {"lanes": [{"lane": 1, ...}]}', 400)
        written = 0
        for ldata in lanes:
            if not isinstance(ldata, dict):
                return _err('Each lane entry must be an object, got %r' % (ldata,), 400)
            try:
                lane = _as_int(ldata.get('lane', 1), 'Lane') - 1
            except ValueError as e:
                return _err(str(e), 400)
            if not (0 <= lane < 8):
                continue
            if 'grid_code' in ldata:
                gc = int(ldata['grid_code']) & 0x0F
                fine_en = 1 if ldata.get('fine_tuning_enabled', False) else 0
                _state['backend'].write_bytes(cmis.REG_GRID_SPACING_TX[1] + lane,
                                              bytes([(gc << 4) | fine_en]))
            if 'channel' in ldata:
                ch = int(ldata['channel'])
                ch_bytes = struct.pack(">h", ch)
                _state['backend'].write_bytes(cmis.REG_CHANNEL_NUM_TX[1] + lane * 2, ch_bytes)
            if 'fine_offset_ghz' in ldata:
                ft = int(round(float(ldata['fine_offset_ghz']) / 0.001))
                _state['backend'].write_bytes(cmis.REG_FINE_OFFSET_TX[1] + lane * 2,
                                              struct.pack(">h", ft))
            if 'target_power_dbm' in ldata:
                pwr = int(round(float(ldata['target_power_dbm']) / 0.01))
                _state['backend'].write_bytes(cmis.REG_TARGET_PWR_TX[1] + lane * 2,
                                              struct.pack(">h", pwr))
            written += 1
        if not written:
            return _err('No lane in range 1-8 was given', 400)
        return _ok({'message': 'Laser tuning parameters written', 'lanes': written})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/counters', methods=['GET'])
def api_module_counters():
    """Read error/bit counters using diagnostic selectors 0x02-0x05."""
    err = _require_connected()
    if err:
        return err
    try:
        lanes = []
        banks = (_state['lanes'] + 7) // 8
        for bank in range(banks):
          for sel, lane_start, side in [
            (0x02, 0, 'host'), (0x03, 4, 'host'),
            (0x04, 0, 'media'), (0x05, 4, 'media'),
          ]:
            # The selector is written into the bank being read: each bank
            # keeps its own diagnostic result window.
            _set_page(0x14, bank)
            _state['backend'].write_bytes(cmis.REG_DIAG_SELECTOR[1], bytes([sel]))
            time.sleep(0.005)
            data = _read_upper(*cmis.REG_DIAG_DATA, bank)
            for li in range(4):
                off = li * 16
                error_count = struct.unpack("<Q", data[off:off+8])[0]
                total_bits_raw = struct.unpack("<Q", data[off+8:off+16])[0]
                psl = total_bits_raw & 1  # pattern sync loss indicator
                total_bits = total_bits_raw & ~1
                lane_idx = bank * 8 + lane_start + li
                # Find or create lane entry
                entry = None
                for e in lanes:
                    if e['lane'] == lane_idx + 1:
                        entry = e
                        break
                if entry is None:
                    entry = {'lane': lane_idx + 1}
                    lanes.append(entry)
                entry[f'{side}_error_count'] = error_count
                entry[f'{side}_total_bits'] = total_bits
                entry[f'{side}_psl'] = bool(psl)
                if total_bits > 0:
                    entry[f'{side}_ber'] = error_count / total_bits
                else:
                    entry[f'{side}_ber'] = 0.0

        lanes.sort(key=lambda x: x['lane'])
        return _ok({'lanes': lanes})
    except Exception as e:
        return _err(str(e), 500)


def _as_int(value, what):
    """Parse a page/address/length from the UI, which sends hex or decimal.

    Raises ValueError with a message meant for the user; the callers turn that
    into a 400. Letting int() raise instead produced a 500, which is what a
    failed I2C transfer looks like.
    """
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise ValueError('%s must be a number, got %r' % (what, value))


@app.route('/api/register/read', methods=['POST'])
def api_register_read():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        try:
            page = _as_int(body.get('page', 0), 'Page')
            address = _as_int(body.get('address', 0), 'Address')
            length = _as_int(body.get('length', 1), 'Length')
        except ValueError as e:
            return _err(str(e))
        if length < 1 or length > 128:
            return _err("Length must be 1–128")
        if not (0 <= page <= 0xFF):
            return _err("Page must be 0x00–0xFF")
        if not (0 <= address <= 0xFF):
            return _err("Address must be 0x00–0xFF")
        if address + length > 0x100:
            return _err(f"Read would cross end of page (address 0x{address:02X} + length {length} > 0x100)")

        if address >= 0x80:
            data = _read_upper(page, address, length)
        else:
            data = _read_lower(address, length)

        return _ok({
            'page': page,
            'address': address,
            'length': length,
            'data': list(data),
            'hex': ' '.join(f'{b:02X}' for b in data),
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/register/write', methods=['POST'])
def api_register_write():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        try:
            page = _as_int(body.get('page', 0), 'Page')
            address = _as_int(body.get('address', 0), 'Address')
            data_list = body.get('data', [])
            if not data_list:
                return _err("No data provided")
            if isinstance(data_list, str):
                # A string of bytes has always meant space-separated hex, with
                # no 0x prefixes, so it keeps being read that way.
                data = bytes(int(h, 16) & 0xFF for h in data_list.split())
            else:
                data = bytes(_as_int(b, 'Data byte') & 0xFF for b in data_list)
        except ValueError as e:
            return _err(str(e))

        if not (0 <= page <= 0xFF):
            return _err("Page must be 0x00–0xFF")
        if not (0 <= address <= 0xFF):
            return _err("Address must be 0x00–0xFF")
        if address + len(data) > 0x100:
            return _err(f"Write would cross end of page (address 0x{address:02X} + {len(data)} bytes > 0x100)")
        # Running a multi-byte write through 0x7F would reprogram the page
        # select mid-transfer and dump the remaining bytes into whatever page
        # that byte happened to name.
        if address < 0x7F < address + len(data):
            return _err(f"Write from 0x{address:02X} would run through the page "
                        f"select register at 0x7F; split it into two writes")

        if address >= 0x80:
            _set_page(page)
        _state['backend'].write_bytes(address, data)
        # A raw write may land on the PageMapping register itself, or on the
        # control byte that resets the module - either moves the selected page
        # out from under us.
        if address <= 0x7F < address + len(data) or address < 0x80:
            _invalidate_page()

        return _ok({
            'page': page,
            'address': address,
            'bytes_written': len(data),
        })
    except Exception as e:
        return _err(str(e), 500)


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.route('/api/version', methods=['GET'])
def api_version():
    return _ok({
        'version': __version__,
        'cmis_revision_supported': _CMIS_REVISION,
        'frozen': bool(getattr(sys, 'frozen', False)),
    })


@app.route('/api/update/check', methods=['GET'])
def api_update_check():
    """Ask GitHub whether a newer release exists.

    Only ever runs when the user clicks Update - the tool is used on isolated
    lab networks and must not reach out on its own.
    """
    rel = updater.fetch_latest_release()
    if rel is None:
        # Never report "up to date" for a failed lookup: on a lab network with
        # no route to GitHub that would tell the user they are current when
        # nothing was actually checked.
        return _err('Could not reach GitHub. Check the network connection, '
                    'or download manually from '
                    f'https://github.com/{updater.GITHUB_OWNER}/{updater.GITHUB_REPO}/releases',
                    502)
    return _ok({
        'current_version': __version__,
        'latest_version': rel['version'],
        'update_available': updater.is_newer(rel['version'], __version__),
        'can_self_update': updater.is_frozen(),
        'asset_name': rel['asset_name'],
        'asset_size': rel['asset_size'],
        'release_url': rel['html_url'],
        'notes': rel['notes'][:4000],
        'published_at': rel['published_at'],
    })


def _run_update(rel):
    """Fetch and install one release. Runs on its own thread; see api_update_apply."""
    staged = updater.staging_dir()
    try:
        if os.path.isdir(staged):
            shutil.rmtree(staged, ignore_errors=True)
        os.makedirs(staged, exist_ok=True)
        archive = os.path.join(staged, rel['asset_name'])
        # The partial lives outside `staged`, which was just wiped: on a link
        # that keeps dropping, the bytes already fetched are the only thing
        # that makes the next attempt shorter than the last.
        updater.discard_stale_partials(rel['asset_name'])
        # Which of the two carries the bytes faster changes by the hour, so it
        # is measured rather than assumed. Both serve the same asset and the
        # digest below is what decides installability, so the mirror never
        # needs to be trusted - only to be quick.
        _update['state'] = 'probing'
        ranked = updater.order_sources([
            rel['asset_url'],
            updater.mirror_url(rel['version'], rel['asset_name']),
        ])
        _update['source'] = urllib.parse.urlparse(ranked[0][0]).hostname or ''
        _update['state'] = 'downloading'

        def progress(done, total):
            _update['done'] = done
            _update['total'] = total or rel['asset_size']

        updater.download_asset([u for u, _ in ranked], archive,
                               progress_cb=progress,
                               total_hint=rel['asset_size'],
                               part_path=updater.partial_path(rel['asset_name']),
                               keep_partial=True)
        _update['state'] = 'verifying'
        if not updater.verify_sha256(archive, rel['sha256']):
            shutil.rmtree(staged, ignore_errors=True)
            # Separate messages: "we checked and it was wrong" and "there was
            # nothing to check against" call for different reactions, and the
            # second one is not the user's fault.
            if not rel['sha256']:
                _fail_update(
                    f'Release {rel["version"]} publishes no SHA-256 digest, so the '
                    'download cannot be verified; nothing was installed. Download '
                    'it from the release page by hand if you trust it.')
            else:
                _fail_update('Downloaded file failed its checksum; update aborted')
            return
        _update['state'] = 'installing'
        updater.extract_payload(archive, staged)
        os.remove(archive)
    except Exception as e:
        shutil.rmtree(staged, ignore_errors=True)
        _fail_update(f'Update download failed: {e}')
        return

    updater.stage_and_swap(staged)
    _update['state'] = 'ready'
    # The helper is now waiting for this process to release the exe. Give the
    # browser a moment to see 'ready', then quit so the swap can proceed.
    threading.Timer(1.5, lambda: os._exit(0)).start()


def _fail_update(message):
    _update['state'] = 'error'
    _update['message'] = message


@app.route('/api/update/apply', methods=['POST'])
def api_update_apply():
    """Start the download on a worker thread and report progress separately.

    Doing the transfer inside this request meant the server answered nothing
    else until it finished - the whole UI froze, with the only feedback a toast
    that expired after twenty seconds. That is invisible on a fast link and
    forty minutes of an apparently hung tool on a slow one, which is exactly
    when a user gives up and kills the process.

    The server stays single-threaded on purpose: one I2C connection and one
    cached page selection cannot survive interleaved requests. That constraint
    is about concurrent *requests*, though, and a worker thread doing network
    I/O is not one - so the request loop is free to answer /api/update/progress
    while the bytes arrive.
    """
    if not updater.is_frozen():
        return _err('Running from source — upgrade with git pull instead of '
                    'replacing an executable', 400)
    if _update['state'] in _UPDATE_BUSY:
        return _err(f'An update is already {_update["state"]}', 409)
    rel = updater.fetch_latest_release()
    if rel is None:
        return _err('Could not reach GitHub to download the update', 502)
    if not updater.is_newer(rel['version'], __version__):
        return _err(f'Already on the newest version ({__version__})', 400)

    _update.update(state='starting', version=rel['version'], done=0,
                   total=rel['asset_size'], source='', message='')
    threading.Thread(target=_run_update, args=(rel,), daemon=True).start()
    return _ok({
        'version': rel['version'],
        'total': rel['asset_size'],
        'message': f'Downloading {rel["version"]} in the background; '
                   'poll /api/update/progress for how far along it is.',
    })


@app.route('/api/update/progress', methods=['GET'])
def api_update_progress():
    """How far the running update has got. Cheap enough to poll every second."""
    return _ok(dict(_update))


@app.route('/')
def index():
    return render_template('index.html', version=__version__,
                           cmis_revision=_CMIS_REVISION)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    url = f'http://127.0.0.1:{PORT}'
    print(f"CMIS Module Manager v{__version__} starting on {url}")
    # Set CMIS_NO_BROWSER=1 to start the server without opening a tab. Repeated
    # automated launches otherwise leave a pile of tabs behind, and after a
    # self-update the relaunched instance would open yet another one on top of
    # the page the user is already looking at.
    if os.environ.get('CMIS_NO_BROWSER', '').strip() not in ('1', 'true', 'True'):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host='127.0.0.1', port=PORT, debug=False, threaded=False)
