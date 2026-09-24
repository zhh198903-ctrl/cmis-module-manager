"""Flask REST API for CMIS optical module management."""
# Single source of truth for the version shown in the UI, /api/version, the
# console banner and the operation manual footer. Bump this, not the copies.
__version__ = '2.131.0'
# The CMIS revision this build decodes. The page footer and /api/version both
# read it, so the two cannot drift apart the way they did through 5.4.
_CMIS_REVISION = '5.4'

import sys
import os
import json
import shutil
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

from flask import Flask, jsonify, render_template, request

import cmis_registers as cmis
import updater
from i2c_interface import list_backends, create_backend

# The port the server listens on when nothing says otherwise. The one actually
# in use is _port_state['active']: the user can choose another, and the default
# may be taken by another program (macOS's AirPlay Receiver listens on 5000).
DEFAULT_PORT = 5000
# Below 1024 are the well-known ports; a local tool has no business there, and
# on most systems an unprivileged process cannot bind them anyway.
PORT_MIN, PORT_MAX = 1024, 65535
SETTINGS_FILE = 'cmis_settings.json'

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
    # Pages this module has been seen to accept. 8.2.15 says a module clears
    # PageSelect rather than refuse a page it does not have, so the only way
    # to find out is to read the byte back - once per page is enough, since
    # what a module supports does not change while it is plugged in.
    'pages_ok': set(),
    # Section 5.2.2.1: "A host may read N bytes ... 1 <= N <= Nmax. By
    # default, Nmax = 8. When full page read is supported ... then Nmax =
    # 128." Eight until 01h:251 says otherwise, so the reads made while
    # discovering that are legal on a module that does not support more.
    'max_read': 8,
    # tBPC is the specification's worst case for a bank or page change. A
    # module may need only tBPC / 2^i of it and says so in 01h:169.3-0; ten
    # milliseconds until it does, since this hold-off is paid before that
    # byte can be read.
    'bpc_sleep': 0.010,
    # 01h:176-190, a static advertisement the DataPath panel needs on
    # every refresh. Kept out of caps because that dict is serialised.
    'media_lane_assign': None,
    # CMIS Flags are latched with clear-on-read: reading the byte that
    # holds one clears it. Polling therefore consumes them, and an event
    # that came and went between two refreshes exists only in whichever
    # reply happened to carry it. Remembering is the host's job, so this
    # keeps what has fired since the operator last cleared it.
    'flag_history': {},
    'flag_history_since': None,
    # When each lane was first seen in the DataPath state it is in now. A
    # transient state is bounded by the module's own advertisement, so how
    # long it has been in one is the only way to tell a slow commissioning
    # from a module that has stopped.
    'dp_state_since': {},
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


def _monitor_present(key: str) -> bool:
    """Whether the module advertises the named monitor (01h:159-160).

    Table 8-53 makes every one of these optional. The register exists either
    way, so an unimplemented monitor reads as zero - and zero is a plausible
    reading: 0.0000 V is an unpowered module, 0.000 mA is a dark laser and the
    dBm conversion of zero microwatts is the floor the panel paints in alarm
    red. Reporting them was the tool inventing a fault the module never
    claimed, which is the same mistake the Aux monitors already avoid.

    Unknown until the advertisement has been read: with no capabilities at all
    nothing is hidden, because hiding every reading would be the worse error.
    """
    mons = (_state.get('caps') or {}).get('monitors') or {}
    return mons.get(key, True) if mons else True


# 8.14.1: "Monitors with associated alarm and/or warning thresholds have
# associated alarm Flags, warning Flags", and those Flags are typed Adv. while
# the ones that always exist are Rqd. So the threshold Flags are advertised by
# 01h:159-160 (Table 8-53, the monitors) rather than by 01h:157-158
# (Table 8-52, which covers only Tx fault, LOS, CDR LOL and adaptive eq fail).
_THRESHOLD_FLAG_MONITOR = {
    'tx_power_high_alarm': 'tx_optical_power',
    'tx_power_low_alarm': 'tx_optical_power',
    'tx_power_high_warn': 'tx_optical_power',
    'tx_power_low_warn': 'tx_optical_power',
    'tx_bias_high_alarm': 'tx_bias',
    'tx_bias_low_alarm': 'tx_bias',
    'tx_bias_high_warn': 'tx_bias',
    'tx_bias_low_warn': 'tx_bias',
    'rx_power_high_alarm': 'rx_optical_power',
    'rx_power_low_alarm': 'rx_optical_power',
    'rx_power_high_warn': 'rx_optical_power',
    'rx_power_low_warn': 'rx_optical_power',
}

# Built from the same table the Flags are decoded with, so a monitor cannot
# be gated on one advertisement and read from another. 'temp' is the only
# prefix Table 8-53 spells differently from Table 8-9.
_MODULE_FLAG_MONITOR = {
    '%s_%s' % (_prefix, _level): {'temp': 'temperature'}.get(_prefix, _prefix)
    for _addr, _first, _second in cmis.MODULE_MONITOR_FLAG_BYTES
    for _prefix in (_first, _second)
    for _level in cmis.MONITOR_FLAG_LEVELS
}


def _media_lane_present(lane: int) -> bool:
    """Whether the module has this media lane (00h:210, Table 8-36).

    Table 8-99 calls Tx power, Tx bias and Rx power "Media Lane-Specific
    Monitors", so on a module whose media lanes are fewer than its host lanes
    - a coherent one carries eight host lanes into a single optical carrier -
    the rows past the last media lane are reading registers for lanes that do
    not exist. Those read zero, and zero is a plausible-looking measurement:
    0.0 uW is the bottom of the dBm scale, which is what the panel paints in
    alarm red.

    `lane` is 1-based.

    On a module with more than eight lanes the register is not used at all.
    The specification's note is that such a module "can therefore not
    unambiguously advertise unsupported media lanes" - so its eight bits
    cannot be taken to mean media lanes 1-8 either, and half of an ambiguous
    statement is not a safer thing to act on than none of it.
    """
    mask = (_state.get('caps') or {}).get('media_lane_unsupported_mask')
    if mask is None or _state.get('lanes', 8) > 8 or not 1 <= lane <= 8:
        return True
    return not (mask >> (lane - 1)) & 1


def _media_lanes_present() -> list:
    """_media_lane_present for every lane row, lane 1 first."""
    return [_media_lane_present(i + 1) for i in range(_state['lanes'])]


def _require_connected():
    """Return error response if not connected, else None."""
    if not _state['connected'] or _state['backend'] is None:
        return _err("Not connected to any module", 503)
    return None


# Everything this tool shows beyond the vendor block lives on a page: the
# monitors and Flags on 11h, the controls on 10h, tuning on 12h, diagnostics
# on 13h and 14h. "Unlike a Paged Memory module, a Flat Memory module does not
# support dynamic Paging into Upper Memory" - it has Lower Memory and Page
# 00h, and a read of any other page is answered from Page 00h.
#
# So these panels were not reading a module that had nothing to say. They were
# reading its vendor name and serial number and decoding them as lane states,
# Flags and monitor values. A refusal that says why is the only honest answer.
def _require_diagnostics(what: str):
    """Return an error response when Pages 13h-14h are not advertised.

    01h:142.5 DiagnosticPagesSupported - "Banked Pages 13h-14h supported" -
    was parsed at connect and never asked. Every other optional page in this
    file gates its reads on its own advertisement: 0Ch, 60h, 61h and 62h each
    have one, three lines apart. The diagnostics family did not, so on a
    module without them the page select is cleared, Page 00h stays mapped, and
    the vendor block is decoded as loopback capabilities, pattern support and
    bit error counts.

    Every demo module implements these pages, which is why the standing sweep
    over "every mock, every endpoint, no page redirects" has never caught it.
    """
    if not (_state.get('caps') or {}).get('diagnostic_pages_supported', True):
        return _err(
            '%s lives on Pages 13h-14h, which this module does not advertise '
            '(01h:142.5 is clear). A module clears the page select rather '
            'than refuse it, so reading on would return Page 00h' % what, 409)
    return None


def _require_paged(what: str):
    """Return an error response on a flat memory module, else None."""
    if (_state.get('caps') or {}).get('flat_memory'):
        return _err(
            '%s lives on a page this module does not have: 00h:2.7 says flat '
            'memory, and a flat memory module supports only Lower Memory and '
            'Page 00h. Reading it would return Page 00h - the vendor block - '
            'decoded as something else' % what, 409)
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


def _allowed_hosts():
    """Host values the real UI sends - on the port this server is using now.

    Worked out per request rather than once at import: the port can be chosen
    by the user and moved while the server runs, and a list frozen at 5000
    would refuse every request the page makes on any other port.
    """
    port = _port_state['active']
    return frozenset(list(_LOCAL_NAMES)
                     + [f'{n}:{port}' for n in _LOCAL_NAMES])


def _allowed_origins():
    port = _port_state['active']
    return frozenset([f'http://{n}:{port}' for n in _LOCAL_NAMES]
                     + [f'http://{n}' for n in _LOCAL_NAMES])


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
    if origin and origin not in _allowed_origins():
        return _err('Refused: cross-origin request', 403)

    # DNS rebinding: the attacker points a name they own at 127.0.0.1, so the
    # browser calls their page same-origin and can read the replies too. The
    # Host header is what gives that away - the real UI never sends another.
    if request.host not in _allowed_hosts():
        return _err('Refused: unexpected Host header', 403)

    # application/json is not a CORS-simple content type, so requiring it forces
    # a preflight that a foreign page cannot satisfy. This is the layer that
    # holds if a browser ever omits the headers above.
    if request.method == 'POST' and request.content_length and not request.is_json:
        return _err('Expected Content-Type: application/json', 415)
    return None


# ---------------------------------------------------------------------------
# Local port: which one, where it is kept, and moving to another
# ---------------------------------------------------------------------------
# active     - the port this process is serving on right now
# configured - the one it was asked to use (CMIS_PORT, then the settings file,
#              then DEFAULT_PORT), and `source` says which of the three
# conflict   - set when `configured` was taken at start-up and the server fell
#              back to a port the system picked; the page asks the user for one
_port_state = {'active': DEFAULT_PORT, 'configured': DEFAULT_PORT,
               'source': 'default', 'conflict': None}

# The server this process runs, and the one to switch to when it stops. Only
# the entry point sets these; under the test client or `flask run` they stay
# None and a port change is saved for the next start instead.
_servers = {'current': None, 'next': None}


def _settings_path() -> str:
    """Beside the exe, or beside app.py when run from source.

    The same place update.log goes. A portable zip keeps its settings with it,
    and the self-update swap only replaces the files the release carries.
    """
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, SETTINGS_FILE)


def _load_settings() -> dict:
    try:
        with open(_settings_path(), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_settings(**changes) -> None:
    """Merge into the settings file. Written beside and renamed over it, so a
    crash mid-write cannot leave a half file that the next start reads as no
    settings at all."""
    data = _load_settings()
    data.update(changes)
    path = _settings_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _parse_port(value):
    """A port number from a request body, the environment or the file, or None.

    Strings of digits are accepted because a hand-edited file and an
    environment variable both arrive as text. True and False are ints in
    Python, 1 and 0, and the range turns them away with no case of their own.
    """
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    return value if PORT_MIN <= value <= PORT_MAX else None


def _configured_port():
    """(port, source): CMIS_PORT beats the settings file beats the default.

    The environment wins so a script, or the updater relaunching the app, can
    say where it wants the server without editing anyone's saved choice.
    """
    env = _parse_port(os.environ.get('CMIS_PORT', ''))
    if env:
        return env, 'env'
    saved = _parse_port(_load_settings().get('port'))
    if saved:
        return saved, 'file'
    return DEFAULT_PORT, 'default'


def _listening(port: int) -> bool:
    """Whether something already accepts connections on 127.0.0.1:port.

    Asked by connecting rather than by trying to bind. The server binds with
    SO_REUSEADDR, and on Windows that lets a second process take over a port
    another one is listening on - both then answer, and the browser reaches
    whichever the system picks. A trial bind without that option has the
    opposite fault: it fails on sockets merely left in TIME_WAIT by the last
    run, and would report the tool's own port as taken after every restart.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def _cmis_answers_on(port: int) -> bool:
    """Whether what is listening there is this tool - a second double-click
    on the exe, say - rather than some other program."""
    # No proxy: urllib otherwise takes the system's, and a machine that routes
    # its traffic through one would send this loopback probe to it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/version',
                         timeout=1.5) as r:
            data = json.loads(r.read().decode('utf-8')).get('data') or {}
    except Exception:
        return False
    return isinstance(data, dict) and 'cmis_revision_supported' in data


def _make_server(port: int):
    from werkzeug.serving import make_server
    # threaded=False as before: one request at a time is what keeps two
    # panels' reads from interleaving on the one I2C bus.
    return make_server('127.0.0.1', port, app, threaded=False)


def _start_server():
    """Bind the server for this run. Returns (server, None), or (None, port)
    when this tool is already serving on the configured port.

    When the configured port is taken by something else, the server comes up
    on a port the system chooses instead, and _port_state['conflict'] makes
    the page ask the user which port to use. Refusing to start would leave no
    page to ask it on.
    """
    wanted, source = _configured_port()
    _port_state.update(configured=wanted, source=source, conflict=None)
    if _listening(wanted):
        if _cmis_answers_on(wanted):
            return None, wanted
        reason = 'another program is already listening on it'
    else:
        try:
            return _make_server(wanted), None
        except OSError as exc:
            # Windows reserves port ranges (Hyper-V, WSL) that nothing listens
            # on and nothing can bind: WinError 10013.
            reason = str(exc)
    _port_state['conflict'] = {'port': wanted, 'reason': reason}
    return _make_server(0), None


def _serve(server) -> None:
    """Serve until stopped; when a port change queued a successor, go on
    serving that one. Returns when the process should exit."""
    while server is not None:
        _servers['current'] = server
        _port_state['active'] = server.server_port
        server.serve_forever()
        server.server_close()
        server, _servers['next'] = _servers['next'], None


@app.route('/api/settings/port', methods=['GET'])
def api_port_get():
    """Which port is in use, which was asked for and why, and where it is kept."""
    env = _parse_port(os.environ.get('CMIS_PORT', ''))
    return _ok(dict(_port_state,
                    default=DEFAULT_PORT, min=PORT_MIN, max=PORT_MAX,
                    saved=_parse_port(_load_settings().get('port')),
                    env_override=env,
                    settings_file=_settings_path(),
                    url=f'http://127.0.0.1:{_port_state["active"]}/'))


@app.route('/api/settings/port', methods=['POST'])
def api_port_set():
    """Keep a port for the next start and, where this process runs its own
    server, move to it now.

    The new socket is bound before anything is saved or stopped: a port that
    turns out to be unusable is reported with the server still where it was.
    """
    body = request.get_json(silent=True) or {}
    bad = _reject_unknown(body, ('port',))
    if bad:
        return bad
    port = _parse_port(body.get('port'))
    if port is None:
        return _err('The port must be a whole number from %d to %d'
                    % (PORT_MIN, PORT_MAX), 400)
    active = _port_state['active']
    note = None
    env = _parse_port(os.environ.get('CMIS_PORT', ''))
    if env and env != port:
        note = ('CMIS_PORT=%d is set in the environment and takes precedence '
                'at the next start' % env)

    if port == active:
        try:
            _save_settings(port=port)
        except OSError as exc:
            return _err('Could not save the port to %s: %s'
                        % (_settings_path(), exc), 500)
        _port_state.update(configured=port, source='file', conflict=None)
        return _ok({'port': port, 'switched': False, 'note': note,
                    'url': f'http://127.0.0.1:{port}/'})

    if _listening(port):
        return _err('Port %d is already in use by another program on this '
                    'computer; choose another' % port, 409)

    current = _servers['current']
    successor = None
    if current is not None:
        try:
            successor = _make_server(port)
        except OSError as exc:
            return _err('Port %d cannot be used: %s' % (port, exc), 409)
    try:
        _save_settings(port=port)
    except OSError as exc:
        if successor is not None:
            successor.server_close()
        return _err('Could not save the port to %s: %s'
                    % (_settings_path(), exc), 500)
    _port_state.update(configured=port, source='file', conflict=None)

    if successor is None:
        # No server of our own to move (the test client, `flask run`): the
        # choice is saved and applies from the next start.
        return _ok({'port': port, 'switched': False, 'restart_needed': True,
                    'note': note, 'url': f'http://127.0.0.1:{port}/'})

    _servers['next'] = successor
    # shutdown() waits for serve_forever() to return, and that cannot happen
    # while this very request is being handled on the serving thread - so it
    # is called from another one. The reply below goes out first.
    threading.Thread(target=current.shutdown, daemon=True).start()
    return _ok({'port': port, 'switched': True, 'note': note,
                'url': f'http://127.0.0.1:{port}/'})


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


def _forget_verified_pages():
    """After a connect or a reset: a different module may be on the bus."""
    _state['pages_ok'] = set()


def _set_page(page: int, bank: int = 0):
    """Select an upper-memory page and bank, waiting out the access hold-off.

    CMIS gives tBPC, the maximum Bank/Page Change time, as 10 ms; reading
    sooner can return the previous page's contents. Re-selecting what is
    already current costs another hold-off for nothing, and the panels read the
    same page repeatedly - a thresholds refresh alone re-selected page 02h
    twenty times - so skip the write when both are already known.

    Ten milliseconds is the worst case, not this module's case:
    MaxDurationBPC (01h:169.3-0) scales it down by 2^i, so a module that
    advertises 3 is held off for 1.25 ms and the tool was waiting eight times
    longer than it said it needed on every page change.

    Bank first, then page, always both: the module holds off acting on
    BankSelect until PageSelect is written (CMIS 8.2.15), so writing only the
    bank would leave the change pending and the next read would come from the
    old one.

    And then read it back. 8.2.15 again: "When a host write would result in a
    not supported Page Address in the PageMapping register, the module clears
    the PageSelect Byte ... such that the resulting PageMapping register
    selects Page 00h". So asking for a page the module does not have is not an
    error anywhere - it silently leaves Page 00h mapped, and every read after
    it returns the vendor block decoded as whatever was expected.

    The specification puts the answer in a register, so it is read: once per
    page per connection, because what a module supports does not change while
    it is plugged in.
    """
    if _state['page'] == page and _state['bank'] == bank:
        return
    _state['page'] = None  # unknown while the writes are in flight
    _state['bank'] = None
    _bus_write(cmis.REG_BANK_SELECT[1], bytes([bank, page]))
    time.sleep(_state.get('bpc_sleep')
               or cmis.TIMING_SECONDS['tBPC'])
    if page and page not in _state['pages_ok']:
        got = _bus_read(cmis.REG_PAGE_SELECT[1], 1)
        if len(got) == 1 and got[0] != page:
            raise IOError(
                'this module does not have Page %02Xh: it answered the page '
                'select with %02Xh. CMIS 8.2.15 has a module clear PageSelect '
                'rather than refuse, so reading on would have returned Page '
                '00h - the vendor block - decoded as Page %02Xh'
                % (page, got[0], page))
        _state['pages_ok'].add(page)
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


def _bus_read(addr: int, length: int) -> bytes:
    return _retry_rejected(_state['backend'].read_bytes, addr, length)


def _bus_write(addr: int, data: bytes) -> None:
    """One WRITE, and the hold-off it may start.

    Table 10-4: after a WRITE the module may reject every ACCESS for up to
    tWRITE (10 ms), or tWRITENV (80 ms) after a write to non-volatile memory
    - the user EEPROM on Page 03h: "The module rejects register ACCESS until
    a WRITE to EEPROM is completed internally" (8.7). The next access, read
    or write, may be refused in that window, and 5.2.3 lets a host retry.
    """
    _retry_rejected(_state['backend'].write_bytes, addr, data)
    nv = addr >= 0x80 and _state.get('page') == 0x03
    _state['holdoff_until'] = time.monotonic() + cmis.TIMING_SECONDS[
        'tWRITENV' if nv else 'tWRITE']


def _retry_rejected(access, *args):
    """Run one ACCESS, retrying it while the module may still be holding
    off after the last WRITE.

    5.2.2.1/5.2.2.2: "A rejected READ access can simply be retried until
    eventual success", and a rejected WRITE "has no effect in the target", so
    retrying it is safe too. Every adapter here reports a NACK as an IOError.
    An attempt that fails after the longest hold-off Table 10-4 allows is a
    real failure and is raised - the window is the specification's, not a
    patience setting.
    """
    while True:
        started = time.monotonic()
        try:
            return access(*args)
        except (IOError, OSError):
            if started >= _state.get('holdoff_until', 0.0):
                raise
            # A polling cadence, not a Chapter 10 number: Table 10-4 bounds
            # how long the module may refuse, not how often to ask.
            time.sleep(0.001)


def _read_chunked(addr: int, length: int) -> bytes:
    """Read `length` bytes without asking for more at once than Nmax.

    A module only has to answer a READ of up to 8 bytes unless it advertises
    full page read (01h:251.1-0), and this tool asks for 64 at a time from
    the diagnostics window alone. What a module does with an over-long READ
    is its own business - wrapping the address or NAKing part-way - so the
    result was never something to rely on.

    Splitting is safe for what is read here: CMIS scalars are at most 8 bytes
    and sit on 8-byte boundaries, so a chunk never lands in the middle of
    one, and the specification does not promise coherency across register
    arrays in the first place (section 5.2.5.1). Nor, for the same reason,
    for a changing value read as part of one - those go through
    _read_scalars.
    """
    limit = _state.get('max_read') or 8
    if length <= limit:
        return _bus_read(addr, length)
    out = bytearray()
    while len(out) < length:
        n = min(limit, length - len(out))
        out += _bus_read(addr + len(out), n)
    return bytes(out)


def _read_scalars(addr: int, length: int, size: int) -> bytes:
    """Read `length` bytes of back-to-back `size`-byte scalars, one READ each.

    5.2.5.1: a size-matched READ of one scalar multi-byte read-only register
    is atomic - "the module ensures not to update parts of a scalar read-only
    register during a size-matched READ of that multi-byte register" - and
    this "applies in particular to any scalar 2-byte, 4-byte, or 8-byte
    status or monitoring register". But "READ access to ... multiple
    registers or register arrays does not guarantee coherency": a monitor
    read as part of a block can come back with its high byte from one sample
    and its low byte from the next, 256 counts off, looking like a reading.
    """
    return b''.join(_bus_read(addr + pos, size)
                    for pos in range(0, length, size))


def _read_upper_scalars(page: int, addr: int, length: int, size: int,
                        bank: int = 0) -> bytes:
    _set_page(page, bank)
    return _checked(_read_scalars(addr, length, size),
                    '%02Xh:0x%02X%s' % (page, addr,
                                        '' if bank == 0 else ' bank %d' % bank),
                    length)


# 5.2.2.2: "A successful WRITE writes a sequence of up to eight given byte
# values". Longer is allowed only "when specified explicitly in chapter 8",
# and nothing this tool writes is such a place.
MAX_WRITE = 8


def _write_chunked(addr: int, data: bytes) -> int:
    """Write `data` as WRITEs of at most MAX_WRITE bytes; returns how many.

    Every adapter here sends a write as one transaction however long it is -
    the CH341 in one piece, the CP2112 and MCP2221 in pieces of about sixty,
    which is their packet size and not the module's limit. A module may
    reject a longer WRITE, and "a rejected WRITE access has no effect".
    Multi-byte WRITEs are not atomic in any case (section 5.2.5.2), so
    nothing is lost by splitting.
    """
    if len(data) <= MAX_WRITE:
        _bus_write(addr, data)
        return 1
    count = 0
    for pos in range(0, len(data), MAX_WRITE):
        _bus_write(addr + pos, data[pos:pos + MAX_WRITE])
        count += 1
    return count


def _read_lower(addr: int, length: int) -> bytes:
    return _checked(_read_chunked(addr, length),
                    'Lower:0x%02X' % addr, length)


def _read_upper(page: int, addr: int, length: int, bank: int = 0) -> bytes:
    _set_page(page, bank)
    return _checked(_read_chunked(addr, length),
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


def _read_banks_scalars(page: int, addr: int, length: int, size: int,
                        lanes: int = 0):
    """_read_banks for arrays of changing multi-byte values - monitors,
    counters, latencies - each read with a READ of its own size (5.2.5.1)."""
    lanes = lanes or _state['lanes']
    for bank in range((lanes + 7) // 8):
        yield bank, _read_upper_scalars(page, addr, length, size, bank)


def _read_diag_banks(sel: int, length: int = 0, lanes: int = 0):
    """Yield (bank, data) from Page 14h, selecting the window in each bank.

    8.17: "Page 14h may optionally be Banked. Each Bank of Page 14h refers to
    8 lanes." The DiagnosticsSelector at 14h:128 and the Diagnostics Data it
    selects (14h:192-255) are both inside that banked page, so each bank keeps
    its own selector and its own result window.

    Writing the selector once and then reading every bank returns lanes 9 and
    up out of whatever window that bank happened to be left on - a number in a
    plausible range, decoded as whatever the caller asked for.
    """
    page, addr, full = cmis.REG_DIAG_DATA
    lanes = lanes or _state['lanes']
    for bank in range((lanes + 7) // 8):
        _set_page(page, bank)
        _bus_write(cmis.REG_DIAG_SELECTOR[1], bytes([sel]))
        # tDDCS, Table 10-5. Five milliseconds was half of it, and this is
        # the class of wait the module does not enforce: "ACCESS that is too
        # early is not rejected". A short wait here returns the previous
        # selector's sixty-four bytes decoded as whatever this selector
        # means - BER values read as error counters, and no error anywhere.
        time.sleep(cmis.TIMING_SECONDS['tDDCS'])
        # Table 8-139: selectors 02h-05h are U64 counters, 01h F16 BERs and
        # 06h U16 SNRs - each read at its own size, since the window is
        # updated while a measurement runs.
        size = 8 if 2 <= sel <= 5 else 2
        yield bank, _read_upper_scalars(page, addr, length or full, size, bank)


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


def _bank_broadcast_active() -> bool:
    """Lower 0x1A.7, and only where 01h:156.7 advertises the control.

    The bit is RW on every module but is only acted on where it is
    advertised, so a module that does not advertise it cannot be in this
    state however the byte happens to read.
    """
    if not (_state.get('caps') or {}).get('controls', {}).get('bank_broadcast'):
        return False
    try:
        return bool((_read_lower(0x1A, 1)[0] >> 7) & 1)
    except Exception:
        return False


def _refuse_broadcast_divergence(named: dict):
    """Table 8-11: with bank broadcast enabled, a write to a control register
    in any bank of a lane-banked page is executed in every bank. Per-bank
    values that differ cannot be expressed that way - writing them in turn
    leaves the last bank's value on every lane, which is not what was asked
    for, and the write reports success.
    """
    if (_state['lanes'] + 7) // 8 < 2 or not _bank_broadcast_active():
        return None
    for name, values in named.items():
        vals = list(values)
        if len(set(vals)) > 1:
            return _err(
                'Bank broadcast is enabled (Lower 0x1A.7), so a write to any '
                'bank lands in all of them. %s differs between banks (%s) and '
                'cannot be written while it is on - clear bank broadcast '
                'first, or send one value for every bank' % (name, vals), 400)
    return None


def _refuse_broadcast_tuning(plan):
    """The laser tuning plan is (bank, address, bytes) per field and lane,
    and Page 12h is lane-banked by media lane (8.15). Under bank broadcast
    (Table 8-11) each of those writes lands at the same address of every
    bank, so tuning media lane 3 would retune lanes 11 and 19 alike. The plan
    is only what gets done if every bank is written with the same value.
    """
    banks = (_state['lanes'] + 7) // 8
    if banks < 2 or not _bank_broadcast_active():
        return None
    fields = (('grid', cmis.REG_GRID_SPACING_TX[1], 1),
              ('channel', cmis.REG_CHANNEL_NUM_TX[1], 2),
              ('fine-tuning offset', cmis.REG_FINE_OFFSET_TX[1], 2),
              ('target output power', cmis.REG_TARGET_PWR_TX[1], 2))
    by_addr = {}
    for bank, addr, payload in plan:
        by_addr.setdefault(addr, {})[bank] = payload
    for addr, per_bank in by_addr.items():
        if len(per_bank) == banks and len(set(per_bank.values())) == 1:
            continue
        name, slot = next((n, (addr - base) // width)
                          for n, base, width in fields
                          if base <= addr < base + 8 * width)
        lanes = lambda bs: ', '.join(str(8 * b + slot + 1) for b in bs)
        others = [b for b in range(banks) if b not in per_bank]
        what = ('setting the %s of media lane %s would set it on media lane%s '
                '%s too' % (name, lanes(sorted(per_bank)),
                            '' if len(others) == 1 else 's', lanes(others))
                if others else
                'the %s asked for differs between media lanes %s, and each '
                'write lands on all of them' % (name, lanes(range(banks))))
        return _err(
            'Bank broadcast is enabled (Lower 0x1A.7, Table 8-11), so a write '
            'to any bank of Page 12h lands in all of them: %s. Clear bank '
            'broadcast first, or give media lanes %s one value together'
            % (what, lanes(range(banks))), 400)
    return None


def _read_grid_ranges(with_300: bool) -> bytes:
    """04h:130-165, plus 166-169 when the module advertises the 300 GHz grid.

    The two are contiguous and hold the same structure, so this is one read;
    what varies is how far it goes. Reading the extra four bytes on a module
    that does not advertise that grid would turn whatever happens to be there
    into an advertised channel range.
    """
    page, addr, length = cmis.REG_GRID_CHANNELS
    return _read_upper(page, addr, length + (4 if with_300 else 0))


def _si_nibbles(reg) -> list:
    """A 4-bit signal integrity value per lane, across every bank.

    Four bytes hold eight lanes, and the next eight lanes are those same four
    addresses in the next bank. Unpacking bank 0 alone returned eight values
    however wide the module was, and the panel sizes its table from the length
    of the answer - so a sixteen lane module got an eight row table under a
    DataPath list of sixteen.
    """
    out = []
    for _bank, raw in _read_banks(*reg):
        out += cmis.unpack_nibbles(raw)
    return out[:_state['lanes']]


def _si_pairs(reg) -> list:
    """A 2-bit signal integrity value per lane, across every bank."""
    out = []
    for _bank, raw in _read_banks(*reg):
        out += cmis.unpack_pairs(raw)
    return out[:_state['lanes']]


def _si_lane_flags(reg) -> list:
    """One signal integrity bit per lane, across every bank.

    _read_banked concatenates the banks, so indexing [0] took the first byte
    and threw the rest away - the same eight-lane answer by a different
    route.
    """
    out = []
    for _bank, raw in _read_banks(reg[0], reg[1], 1):
        out += cmis.parse_lane_flags(raw[0])
    return out[:_state['lanes']]


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


def _read_banked_scalars(page: int, addr: int, size: int) -> bytes:
    """_read_banked for one changing `size`-byte value per lane (a monitor, a
    measured frequency), each read on its own so it is coherent (5.2.5.1)."""
    lanes = _state['lanes']
    out = bytearray()
    for bank in range((lanes + 7) // 8):
        out += _read_upper_scalars(page, addr, size * 8, size, bank)
    return bytes(out[:size * lanes])


def _verify_page_checksums(caps: dict) -> list:
    """Check each static page against the checksum the module puts on it.

    Section 8.3.11: "The page checksum is a one-byte code that can be used to
    verify that the read-only static data on Page 00h is valid." Every static
    page carries one, and this tool exists to drive a two-wire link that goes
    wrong - a corrupted advertisement read is the failure it is for, and the
    module hands over a one-byte way to notice.

    A page the module does not serve is not a mismatch, and checking one
    would be worse than not checking at all: an unserved page reads as
    whatever the module does with it, and a false alarm about corrupt data is
    exactly the wrong thing for this to produce. Pages 01h and 02h exist on a
    paged module (00h:2.7), and Page 04h only where the transmitter is
    tunable (01h:155.6).
    """
    out = []
    paged = (caps.get('config') or {}).get('memory_model') == 'Paged'
    tunable = (caps.get('controls') or {}).get('transmitter_tunable', False)
    for page, at, first, last in cmis.PAGE_CHECKSUMS:
        if page in (0x01, 0x02) and not paged:
            continue
        if page == 0x04 and not tunable:
            continue
        try:
            data = _read_upper(page, 0x80, 128)
        except Exception:
            continue
        try:
            want = cmis.page_checksum(data, first, last)
        except ValueError:
            continue
        got = data[at - 128]
        out.append({
            'page': '%02Xh' % page,
            'address': at,
            'covers': '%d-%d' % (first, last),
            'expected': want,
            'reported': got,
            'ok': want == got,
        })
    return out


def _read_nad_applications(media_type: int = 0x02):
    """Page 1Ch (section 8.24), if the module has one.

    01h:175 NADBanksSupported is the only thing that says it does, and the
    Applications panel has been reporting that number while saying "this tool
    does not read Page 1Ch" underneath the fifteen it could see. On a module
    advertising four banks that is sixty Applications summarised as fifteen.

    Each bank is one NAD Block, so this is banked by block index rather than
    by lane - _read_banks counts lanes, which is a different thing here, so
    the banks are walked directly.
    """
    nad = (_state.get('caps') or {}).get('nad') or {}
    banks = nad.get('banks') or 0
    if not banks:
        return None
    page, addr, length = cmis.REG_NAD_BLOCK
    out = []
    for bank in range(banks):
        out += cmis.parse_nad_block(
            _read_upper(page, addr, length, bank), bank, media_type)
    return out


def _read_dp_latency(lanes: int):
    """Page 15h (section 8.18, Table 8-141), if the module has one.

    01h:145.3 is the only thing that says this page exists; the capabilities
    panel has been showing that bit since the byte was decoded, worded as
    "advertised - this tool does not read Page 15h" because it did not. This
    is that sentence coming off the panel.

    Banked, and that is the part worth getting right: "Page 15h may optionally
    be Banked. Each Bank of Page 15h refers to 8 lanes." Reading bank 0 alone
    on a sixteen lane module would fill lanes 9-16 with lanes 1-8's numbers,
    which is not a blank to notice but eight plausible wrong answers.
    """
    caps = _state.get('caps') or {}
    if not (caps.get('aux') or {}).get('timing_page_15h'):
        return None
    out = {}
    for kind, reg in (('rx', cmis.REG_DP_RX_LATENCY),
                      ('tx', cmis.REG_DP_TX_LATENCY)):
        vals = []
        for _bank, chunk in _read_banks_scalars(*reg, 2, lanes):
            vals += cmis.parse_dp_latency(chunk)
        out[kind] = vals[:lanes]
    return out


def _read_cu_attenuation(caps):
    """00h:204-208, Table 8-35, but only where the specification defines it.

    8.3.6: "For active linear copper cables with host-programmable gain, the
    characteristics are reported for the 0dB gain setting. For other modules
    bytes 204-209 are reserved." An optical module answers the read - these
    are ordinary readable bytes on a page every module has - and the answer
    means nothing. Reported unconditionally, the panel would print a cable
    loss for a module that has no cable.

    So the media type gates it, and then the module's own "0 dB indicates
    this characteristic is not available" removes the row again on a cable
    assembly that did not fill the block in - which is how an active optical
    cable, sharing media type 04h with active copper, answers.
    """
    if not cmis.is_copper_media(caps.get('media_type_code')):
        return None
    att = cmis.parse_cu_attenuation(_read_upper(*cmis.REG_CU_ATTENUATION))
    # Every figure absent is not a cable with no loss.
    return att if any(a['db'] is not None for a in att) else None


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
    _state['max_read'] = 8
    _state['bpc_sleep'] = 0.010
    _state['media_lane_assign'] = None
    try:
        rev = _read_lower(0x01, 1)[0]
        caps['cmis_revision'] = f'{(rev >> 4) & 0x0F}.{rev & 0x0F}'
        # First, and from Lower Memory, which every module has: whether this
        # module has an Upper Memory to page into at all.
        #
        # "Unlike a Paged Memory module, a Flat Memory module does not support
        # dynamic Paging into Upper Memory", and every advertisement below
        # lives on Page 01h. A flat module answers a read of those addresses
        # from Page 00h - vendor name, part number, serial number - so what
        # came back was ASCII, decoded as capability bits and presented as
        # this module's advertisements: which monitors it has, how many lanes,
        # how long its transient states take, what wavelength it emits.
        #
        # None of it was true, and none of it looked wrong. The lane count in
        # particular sizes every per-lane panel in the tool.
        # 00h:85, Lower Memory, so a flat module answers it too. Read here
        # because two Page 00h blocks below are defined only for some media
        # types and are Reserved for the rest.
        caps['media_type_code'] = _read_lower(*cmis.REG_MEDIA_TYPE[1:])[0]
        # Lower 56-57 (Table 8-18), and read before the flat branch for the
        # same reason: a passive cable is exactly the module whose answer
        # matters, and it returns early below.
        caps.update(cmis.parse_state_machines(
            *_read_lower(*cmis.REG_CMIS_SM_SUPPORT[1:])))
        caps['config'] = cmis.parse_config_capabilities(
            _read_lower(*cmis.REG_MEMORY_MODEL[1:])[0])
        caps['flat_memory'] = caps['config'].get('memory_model') == 'Flat'
        if caps['flat_memory']:
            # 8.2: a flat memory module is a static memory module, "supporting
            # only constant read-only, i.e. immutable data". Tables 8-9 and
            # 8-10 are titled "not for static memory modules", so the Flags
            # and the module-level monitors are not absent-because-unreadable
            # but absent because this kind of module does not have them. Said
            # as an advertisement of nothing rather than left unknown, because
            # unknown means "show everything" everywhere downstream.
            caps['monitors'] = cmis.parse_supported_monitors(b'\x00\x00')
            caps['flags_supported'] = cmis.parse_supported_flags(b'\x00\x00')
            caps['page_checksums'] = _verify_page_checksums(caps)
            caps['media_lane_unsupported_mask'] = _read_upper(
                *cmis.REG_MEDIA_LANE_INFO)[0]
            caps['far_end'] = cmis.parse_far_end_config(
                _read_upper(*cmis.REG_FAR_END_CFG)[0])
            caps['cu_attenuation'] = _read_cu_attenuation(caps)
            return caps
        # Before anything long is read: everything below asks for more than
        # eight bytes at a time, and whether that is allowed is this byte's
        # answer to give.
        b251 = _read_upper(*cmis.REG_MISC_FEATURES)[0]
        caps['features'] = cmis.parse_misc_features(b251)
        _state['max_read'] = cmis.max_read_bytes(b251)
        caps['max_read'] = _state['max_read']
        # Sent once so the register panel can warn before a read rather than
        # after it. Deriving it in the page would be a second copy of a table
        # taken from the specification, and the two would drift.
        caps['clear_on_read_blocks'] = [
            {'page': p, 'first': f, 'last': l, 'holds': h}
            for p, f, l, h in cmis.CLEAR_ON_READ_BLOCKS]
        # How long the module says its own transient states take, and how
        # long it actually needs after a page change.
        # 146-150: the temperature range the module is allowed to run in and
        # the supply voltage it needs. The summary line coloured temperature
        # by two numbers written into the page instead.
        caps['limits'] = cmis.parse_module_limits(
            _read_upper(*cmis.REG_MODULE_LIMITS))
        caps['wavelength'] = cmis.parse_wavelength_info(
            _read_upper(*cmis.REG_WAVELENGTH))
        # 163-166 (Table 8-55). Whether this module does CDB messaging at
        # all, and the two facts a host needs before it starts one: whether
        # the module answers other reads while a command runs, and how long
        # it may stay busy.
        caps['cdb'] = cmis.parse_cdb_advertisement(
            _read_upper(*cmis.REG_CDB_CAPS))
        # Whether the fifteen Applications a host can read the classical way
        # are all of them.
        caps['nad'] = cmis.parse_nad_support(
            _read_upper(*cmis.REG_NAD_BANKS)[0])
        dur = _read_upper(*cmis.REG_DURATIONS)
        caps['durations'] = cmis.parse_durations(
            dur[0], dur[1], _read_upper(*cmis.REG_DURATIONS_EXT))
        _state['bpc_sleep'] = caps['durations'].get('bpc_seconds') or 0.010
        b142 = _read_upper(*cmis.REG_BANKS_SUPPORTED)[0]
        ext = _read_upper(*cmis.REG_PAGES_EXT)
        caps.update(cmis.parse_supported_pages(b142, ext))
        caps.update(cmis.parse_misc_caps(_read_upper(*cmis.REG_MISC_CAPS)[0]))
        caps['controls'] = cmis.parse_supported_controls(
            _read_upper(*cmis.REG_SUPPORTED_CONTROLS))
        caps['si'] = cmis.parse_si_controls_adv(
            _read_upper(*cmis.REG_SI_CONTROLS_ADV))
        caps['si'].update(cmis.parse_si_maxima(
            _read_upper(*cmis.REG_SI_MAXIMA)))
        # 152, read after the two bytes above because what it is worth
        # depends on which CDRs this module will let the host bypass. It is
        # the number behind the decision the CDR columns present, and the one
        # byte of 145-154 that had never been read.
        caps['cdr_power'] = cmis.parse_cdr_power_saved(
            _read_upper(*cmis.REG_CDR_POWER_SAVED)[0],
            caps.get('max_lanes', 8),
            cmis.cdr_host_controllable(caps['si'], 'tx'),
            cmis.cdr_host_controllable(caps['si'], 'rx'))
        caps['rx_tx'] = cmis.parse_rx_tx_characteristics(
            _read_upper(*cmis.REG_RX_TX_CHARACTER)[0])
        caps['aux'] = cmis.parse_aux_observables(
            _read_upper(*cmis.REG_AUX_OBSERVABLE)[0])
        caps['flags_supported'] = cmis.parse_supported_flags(
            _read_upper(*cmis.REG_SUPPORTED_FLAGS))
        caps['monitors'] = cmis.parse_supported_monitors(
            _read_upper(*cmis.REG_SUPPORTED_MONITORS))
        # Section 8.4.13 makes these eight bits mean one of three things, and
        # which one depends on the lane count and on whether Page 60h is
        # there - both already read above. Reported as eight lanes whatever
        # the module was, the row said nothing about lanes 9 and up on a wide
        # module that has no Page 60h to say it instead.
        caps['default_polarity_scope'] = cmis.default_polarity_scope(
            caps.get('max_lanes', 8), caps.get('page_60h_supported', False))
        caps['default_polarity'] = cmis.default_polarity_lanes(
            cmis.parse_default_polarity(
                _read_upper(*cmis.REG_DEFAULT_POLARITY)),
            caps.get('max_lanes', 8), caps.get('page_60h_supported', False))
        sub = _read_lower(*cmis.REG_MODULE_SUBTYPE[1:])[0]
        hs = _read_lower(0x3D, 1)[0]
        ext = cmis.parse_extended_module_info(sub, hs)
        ext['heatsink_type_name'] = cmis.HEATSINK_TYPES.get(
            ext['heatsink_type'], f"Reserved (0x{ext['heatsink_type']:X})")
        ext['fiber_face_name'] = cmis.FIBER_FACE_TYPES.get(
            ext['fiber_face_type'], f"Reserved (0x{ext['fiber_face_type']:X})")
        caps.update(ext)
        # 11h:240-255 (Table 8-107) is an advertisement rather than live
        # state, so it is read once here with the rest rather than on every
        # monitoring poll. The width comes from caps and not from
        # _state['lanes']: that is assigned from this dict only after
        # discovery returns, so reading it here would size this module's
        # banks from the one connected before it.
        # 00h:210 (Table 8-36) says which media lanes are NOT supported. It
        # is an advertisement, so it is read here rather than on every poll.
        # The specification's own note limits what it can say: "This lane
        # related register is on a non-banked page. Modules with more than 8
        # host lanes can therefore not unambiguously advertise unsupported
        # media lanes" - so it is taken to cover media lanes 1-8 and nothing
        # is inferred beyond them.
        caps['media_lane_unsupported_mask'] = _read_upper(
            *cmis.REG_MEDIA_LANE_INFO)[0]
        # 00h:211, between the two bytes above and below it that were already
        # read. On a cable assembly it is the only place that says which host
        # lanes reach which far end module.
        caps['far_end'] = cmis.parse_far_end_config(
            _read_upper(*cmis.REG_FAR_END_CFG)[0])
        caps['cu_attenuation'] = _read_cu_attenuation(caps)
        lane_count = caps.get('max_lanes', 8)
        caps['media_lane_map'] = cmis.parse_media_lane_mapping(
            b''.join(raw for _b, raw in
                     _read_banks(*cmis.REG_MEDIA_LANE_MAP, lane_count)),
            lane_count)
        # Table 8-46 defines the wavelength fields for single wavelength
        # modules and says the interpretation is not uniquely defined
        # otherwise. The lane mapping is what knows.
        caps['wavelength']['multi_wavelength'] = cmis.is_multi_wavelength(
            caps['media_lane_map'])
        # Static, so read once: whether the monitors report Table 7-8's NA
        # values. Parsed on the 5.4 panel and applied nowhere, it let a
        # temperature with no valid sample read -128 C.
        caps['na_values'] = bool(caps.get('page_0ch_supported')) and \
            cmis.na_values_advertised(
                _read_upper(*cmis.REG_CONSOLIDATED_PM),
                _read_upper(*cmis.REG_FEATURE_DETAILS)[0])
        # Last: which pages exist is decided by advertisements read above, so
        # checking earlier would gate on a capability block that is not
        # filled in yet and quietly skip every page but 00h.
        caps['page_checksums'] = _verify_page_checksums(caps)
    except Exception:
        # A module that cannot answer the capability block is still usable at
        # the default eight lanes; failing the whole connection over an
        # optional advertisement would be worse than assuming the minimum.
        pass
    return caps


def _flat_memory() -> bool:
    """00h:2.7. Read once on connect, because the fourth byte of every
    Application Descriptor means a different thing on each kind of module."""
    config = (_state.get('caps') or {}).get('config') or {}
    return config.get('memory_model') == 'Flat'


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
        # None on a flat memory module, whose fourth descriptor byte is
        # the HostInterfaceGID and says nothing about where an Application may
        # start.
        mask = a.get('host_lane_assign_mask') or 0
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
    # The text rather than the number: an Application whose width the module
    # left to its interface ID reads "0H/0M" otherwise, which is the one thing
    # it does not mean. The H and M only read as units after a digit, so a
    # width that is not a number takes them as a label instead.
    def part(text, letter):
        return f'{text}{letter}' if text.isdigit() else f'{letter}={text}'

    parts = [f"AppSel#{a['app_sel']}: "
             f"{part(str(a.get('host_lanes_text', a['host_lanes'])), 'H')}"
             f"/{part(str(a.get('media_lanes_text', a['media_lanes'])), 'M')}"
             for a in apps]
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

    # Nothing on the bus answers as 0xFF: an I2C line with no module on it is
    # held high by its pull-ups, and CH341StreamI2C reports success for that
    # read because the adapter cannot see the missing ACK. Without this the
    # tool "connected" to an empty adapter and presented what those bytes
    # decode to - CMIS 15.15, 256 lanes, a Reserved module state and vendor
    # strings of 0xFF - which is a whole module the interface invented.
    # All-zero is the same situation with the bus held low.
    try:
        probe = backend.read_bytes(0x00, 3)
    except Exception as e:
        try:
            backend.disconnect()
        except Exception:
            pass
        _state['backend'] = None
        _state['connected'] = False
        return _err('Opened %s but the module did not answer: %s'
                    % (backend_name, e))
    if not probe or all(b == 0xFF for b in probe) or all(b == 0 for b in probe):
        try:
            backend.disconnect()
        except Exception:
            pass
        _state['backend'] = None
        _state['connected'] = False
        return _err(
            'The adapter opened, but nothing is answering at I2C address '
            '0x%02X: bytes 0-2 of lower memory read %s. Check that a module '
            'is seated, that the adapter is wired to its I2C lines, and that '
            'the address is right - a bus with no module on it reads as all '
            'ones.' % (address, ' '.join('%02X' % b for b in probe)), 502)

    _state['backend'] = backend
    _state['connected'] = True
    _state['max_read'] = 8
    _state['bpc_sleep'] = 0.010
    _state['media_lane_assign'] = None
    _state['dp_state_since'] = {}
    # The Flag history is a record of what *this* module has fired. Connecting
    # kept the previous one's, and history_since with it - so a fresh module
    # was shown as having raised alarms minutes before it was plugged in, and
    # the only way to clear them was a button the operator had no reason to
    # press. Disconnecting already cleared both; connecting straight to
    # another module did not, which is exactly how the documented way to move
    # between the demo profiles works.
    _state['flag_history'] = {}
    _state['flag_history_since'] = None
    _state['bus'] = bus
    _state['address'] = address
    _invalidate_page()
    _forget_verified_pages()
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
    _state['flag_history'] = {}
    _state['flag_history_since'] = None
    _invalidate_page()
    _forget_verified_pages()
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

        # Page 00h vendor information block (addresses per CMIS 5.4 Table 8-29)
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
        apps = cmis.parse_application_descriptors(
            appdesc_raw, media_type_raw[0], _additional_app_descriptors(),
            _media_lane_assignments(), _flat_memory())
        host_lanes_app1 = apps[0]['host_lanes'] if apps else 0
        media_lanes_app1 = apps[0]['media_lanes'] if apps else 0
        host_total, media_total = _compute_module_capacity(apps)
        lanes_detail = _format_lanes_detail(apps, host_total, media_total)

        # Active FW revision: Lower Memory 0x27-0x28 (Table 8-15)
        # HW revision: Page 01h:0x82-0x83
        try:
            fw_active_raw = _read_lower(0x27, 2)
            # 8.2.9: FFh.FFh is not a version, it is "the active firmware load
            # is invalid", and 0.0 is a module with no firmware. Printed as
            # numbers, the fault reads as an ordinary release.
            fw_active = cmis.parse_firmware_revision(*fw_active_raw[:2])
            fw_rev = (fw_active['version'] or
                      ('Invalid firmware load' if fw_active['invalid']
                       else 'No firmware'))
        except Exception:
            fw_rev = "N/A"
            fw_active = None
        # 01h:128-137 are ten contiguous required bytes: the inactive firmware
        # revision (Table 8-44), the hardware revision, and the supported link
        # length per fibre type (Table 8-45). One burst rather than three
        # reads, because on real hardware each one costs a page select.
        try:
            blk = _read_upper(cmis.REG_FW_INACT_MAJOR[0],
                              cmis.REG_FW_INACT_MAJOR[1], 10)
            # Same encoding (Table 8-44), and "a module without inactive
            # firmware clears these fields" - so 0.0 here is the common case,
            # not a module without firmware.
            fw_inact = cmis.parse_firmware_revision(blk[0], blk[1])
            fw_inactive = (fw_inact['version'] or
                           ('Invalid firmware load' if fw_inact['invalid']
                            else 'None'))
            hw_rev = f"{blk[2]}.{blk[3]}"
            link_lengths = cmis.parse_link_lengths(blk[4:10])
        except Exception:
            fw_inactive = "N/A"
            fw_inact = None
            hw_rev = "N/A"
            link_lengths = []

        pwr_class = cmis.parse_power_class(pwr_class_raw[0])

        return _ok({
            'module_id': ident_raw[0],
            'module_type': cmis.module_id_name(ident_raw[0]),
            'cmis_revision': cmis.cmis_revision_str(cmis_rev_raw[0]),
            'memory_model': 'Flat' if (mem_model_raw[0] >> 7) & 1 else 'Paged',
            'config_capabilities': cmis.parse_config_capabilities(
                mem_model_raw[0]),
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
            # The same byte read for what it says rather than what it
            # multiplies out to: FFh is "greater than 6300 m" and a zero base
            # is an undefined length, neither of which is a measurement.
            'cable_length':      cmis.parse_cable_length(cable_len_raw[0]),
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
            # The decoded form beside the text, so the panel can mark an
            # invalid load as the fault it is rather than as a version.
            'fw_active': fw_active,
            'fw_inactive_decoded': fw_inact,
            'fw_inactive_revision': fw_inactive,
            'hw_revision': hw_rev,
            'link_lengths': link_lengths,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/status', methods=['GET'])
def api_module_status():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('The module state machine')
    if err:
        return err
    try:
        state_raw = _read_lower(0x03, 1)             # CORRECT: byte 3, bits[3:1]
        temp_raw  = _read_lower(0x0E, 2)
        volt_raw  = _read_lower(0x10, 2)
        mod_flags_raw = _read_lower(0x08, 6)         # Module-Level Flags 0x08-0x0D
        # The Masks for exactly those bytes, Lower 31-36 (Table 8-12). A
        # Flag whose Mask is set is one the module will not raise the
        # Interrupt line for, so a panel showing Flags without them cannot
        # explain the one state that confuses a reader: Flags standing while
        # Interrupt stays deasserted. The Mask bytes mirror the Flag bytes
        # one for one, so the same decoder names them.
        mod_masks_raw = _read_lower(*cmis.REG_MODULE_FLAG_MASKS[1:])
        aux1_raw  = _read_lower(0x12, 2)
        aux2_raw  = _read_lower(0x14, 2)
        aux3_raw  = _read_lower(0x16, 2)

        # An S16 with no unit is not a reading. 01h:145 says what each Aux
        # monitor measures and 01h:159 whether it exists at all; without both
        # the three registers are just numbers, which is why nothing showed
        # them.
        _caps = _state.get('caps') or {}
        _obs = _caps.get('aux') or {}
        _mons = _caps.get('monitors') or {}

        # Lower 9-11 (Table 8-9): the temperature and Vcc Flags share the block
        # with the three Aux monitors and the Custom one, and all of it is
        # RO/COR. Reading part of it clears the rest, so a decode that stopped
        # at byte 9 did not leave the Aux alarms for anyone else to find - it
        # consumed them and reported none.
        temp_alarms = cmis.parse_module_monitor_flags(mod_flags_raw)
        module_flag_masks = cmis.parse_module_monitor_flags(mod_masks_raw)
        # 8.14.1: a threshold Flag belongs to a monitor, and every one of these
        # monitors is optional (Table 8-53). An absent one reads zero, which is
        # indistinguishable from healthy, so it is reported as unknown instead.
        for name in list(temp_alarms):
            if not _monitor_present(_MODULE_FLAG_MONITOR[name]):
                temp_alarms[name] = None

        # Table 7-8: where the module uses NA values, these raw readings are
        # its statement that there is no valid sample - not a temperature of
        # -128 C or a supply of 0 V.
        na_on = bool(_caps.get('na_values'))
        na = {
            'temperature': na_on and (struct.unpack('>h', temp_raw[:2])[0]
                                      == cmis.NA_TEMPERATURE),
            'vcc': na_on and (struct.unpack('>H', volt_raw[:2])[0]
                              == cmis.NA_VCC),
        }

        aux_monitors = []
        for idx, raw in ((1, aux1_raw), (2, aux2_raw), (3, aux3_raw)):
            key = 'aux%d' % idx
            if _obs and not _mons.get(key, False):
                continue                  # the module says it has no such monitor
            observable = _obs.get(key, 'custom')
            value, unit = cmis.parse_aux_value(raw, observable)
            aux_na = (na_on and observable in cmis.NA_AUX and
                      struct.unpack('>h', raw[:2])[0] == cmis.NA_AUX[observable])
            name_en, name_zh = cmis.AUX_OBSERVABLE_NAMES[observable]
            aux_monitors.append({
                'index': idx, 'observable': observable, 'name': name_en,
                'name_zh': name_zh, 'value': None if aux_na else value,
                'unit': unit, 'na': aux_na,
                # The value and its thresholds were already on screen; this is
                # the module's own verdict on them, and it is destroyed by the
                # read that produced the value.
                'flags': {level: temp_alarms.get('%s_%s' % (key, level))
                          for level in cmis.MONITOR_FLAG_LEVELS},
            })

        state_changed = bool(mod_flags_raw[0] & 0x01)
        # A state change is an event, not a fault. Folding it in here lit the
        # alarm indicator every time somebody reset the module on purpose, and
        # an indicator that cries wolf is one people stop reading.
        any_alarm = any(v is True for v in temp_alarms.values())

        # Module-level Flags are latched and clear-on-read like the lane ones,
        # so the read that reports them is the read that destroys them.
        module_seen = _state['flag_history'].setdefault('module', set())
        if state_changed:
            module_seen.add('module_state_changed')
        for name, value in temp_alarms.items():
            if value is True:
                module_seen.add(name)
        if _state['flag_history_since'] is None:
            _state['flag_history_since'] = time.time()

        return _ok({
            'module_state': cmis.parse_module_state(state_raw[0]),
            'interrupt_asserted': cmis.parse_interrupt_asserted(state_raw[0]),
            'module_flag_masks': module_flag_masks,
            # Set, and masked: the module is reporting the condition and has
            # been told not to interrupt about it.
            'module_flags_masked_set': sorted(
                k for k, v in temp_alarms.items()
                if v and module_flag_masks.get(k)),
            'temperature_c': (round(cmis.parse_temperature(temp_raw), 4)
                              if _monitor_present('temperature')
                              and not na['temperature'] else None),
            'voltage_v': (round(cmis.parse_voltage(volt_raw), 4)
                          if _monitor_present('vcc') and not na['vcc']
                          else None),
            'na': na,
            # So the panel can say which reading is missing and why, rather
            # than leaving a blank cell that reads as a failed poll.
            'monitors_present': {
                key: _monitor_present(key)
                for key in ('temperature', 'vcc', 'aux1', 'aux2', 'aux3',
                            'custom')
            },
            'aux1_raw': struct.unpack(">h", aux1_raw[:2])[0] if len(aux1_raw) >= 2 else 0,
            'aux2_raw': struct.unpack(">h", aux2_raw[:2])[0] if len(aux2_raw) >= 2 else 0,
            'aux3_raw': struct.unpack(">h", aux3_raw[:2])[0] if len(aux3_raw) >= 2 else 0,
            'aux': aux_monitors,
            'module_state_changed': state_changed,
            'alarm_active': any_alarm,
            'seen': sorted(module_seen),
            # What this module says it is allowed to run in. The summary
            # coloured temperature by 60 and 70 written into the page, which
            # is neither this nor the module's own alarm thresholds.
            'limits': _state['caps'].get('limits', {}),
            'wavelength': _state['caps'].get('wavelength', {}),
            'page_checksums': _state['caps'].get('page_checksums', []),
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
    err = _require_paged('The CMIS 5.4 extended status')
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
            # Table 8-72 names two features and the panel reported one.
            # Firmware load management is the other, and it is the one this
            # tool's own update path would care about.
            out['load_management'] = cmis.parse_feature_advertisement(
                _read_upper(*cmis.REG_LOAD_MANAGEMENT))
            # 192-195 (Table 8-73), the details each claim above is measured
            # against. "When a module advertises that a named feature is not
            # supported, the feature details of that feature ... should be
            # ignored by the host" - so an unsupported feature gets no
            # details rather than a row of bits that mean nothing.
            det = _read_upper(*cmis.REG_FEATURE_DETAILS)
            conflicts = []
            if out['consolidated_pm']['supported']:
                out['pm_details'] = cmis.parse_support_details(
                    det[0], cmis.NA_SUPPORT_BITS, cmis.NA_FULL_PATTERNS, 0x80)
                conflicts += cmis.feature_claim_conflicts(
                    out['consolidated_pm'], out['pm_details'],
                    'Consolidated PM')
            if out['load_management']['supported']:
                out['fw_details'] = cmis.parse_support_details(
                    det[2], cmis.FW_SUPPORT_BITS, cmis.FW_FULL_PATTERNS, 0x88)
                # 195.7 is a recommended option, not part of the profile.
                out['fw_details']['fixed_fallback'] = bool(det[3] & 0x80)
                conflicts += cmis.feature_claim_conflicts(
                    out['load_management'], out['fw_details'],
                    'Firmware load management')
            out['feature_conflicts'] = conflicts
            out['available']['0Ch'] = True

        if caps.get('page_60h_supported'):
            polarity = []
            for _b, raw in _read_banks(*cmis.REG_POLARITY_STATUS):
                polarity += cmis.parse_polarity_status(raw)
            # "Each Bank of Page 60h refers to 8 lanes" (section 8.30), and
            # the parser numbers each bank's eight from one. Concatenating
            # them without renumbering put three lanes called 1 in the reply
            # of a 24-lane module - the table below reads positionally and
            # looked right, but the lane each entry names did not.
            for i, entry in enumerate(polarity):
                entry['lane'] = i + 1
            out['polarity_status'] = polarity[:_state['lanes']]
            out['acq_counter_advert'] = _read_upper(*cmis.REG_ACQ_COUNTER_ADV)[0]
            out['available']['60h'] = True

        if caps.get('page_61h_supported'):
            counters = []
            for _b, raw in _read_banks_scalars(*cmis.REG_ACQ_COUNTERS, 2):
                counters += cmis.parse_acquisition_counters(raw)
            for i, c in enumerate(counters):
                c['lane'] = i + 1
            out['acquisition_counters'] = counters[:_state['lanes']]
            out['available']['61h'] = True

        if caps.get('page_62h_supported'):
            thresholds = []
            for _b, raw in _read_banks(*cmis.REG_LANE_PWR_THRESHOLDS):
                thresholds += cmis.parse_lane_power_thresholds(raw)
            # Table 8-192 calls 62h:128-191 "Per-media-lane warning and
            # alarm thresholds". Truncating to the host lane count handed back
            # eight sets on a coherent module, seven of them the thresholds of
            # media lanes it does not have.
            for i, t in enumerate(thresholds):
                t['lane'] = i + 1
            out['lane_power_thresholds'] = [
                t for t in thresholds[:_state['lanes']]
                if _media_lane_present(t['lane'])]
            out['available']['62h'] = True

        if caps.get('media_lane_switching_supported'):
            # Section 8.33: each Bank of 6Dh covers a group of 8 lanes, so a
            # wider module has a switch per group and reading bank 0 governed
            # the first eight lanes while reporting them as the whole module.
            def _mls_banks(reg):
                return b''.join(raw for _b, raw in _read_banks(*reg))

            out['media_lane_switching'] = cmis.parse_media_lane_switching(
                _read_upper(*cmis.REG_MLS_ADVERT)[0],
                _mls_banks(cmis.REG_MLS_REDIRECTION),
                [raw[0] for _b, raw in _read_banks(*cmis.REG_MLS_ENABLE)],
                _mls_banks(cmis.REG_MLS_RESULT),
                _mls_banks(cmis.REG_MLS_STATUS),
                _state['lanes'])
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
    err = _require_paged('Acquisition counter control')
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
        # Page 60h is lane-banked ("Each Bank of Page 60h refers to 8 lanes",
        # 8.30). Under bank broadcast a reset written to one bank resets the
        # same lanes of every bank - counters nobody asked to clear.
        bad = _refuse_broadcast_divergence({
            'The lanes to reset (60h:192-193)':
                [by_bank.get(b, 0) for b in range((_state['lanes'] + 7) // 8)],
        })
        if bad:
            return bad
        for bank, mask in by_bank.items():
            _set_page(0x60, bank)
            if side in ('rx', 'both'):
                _bus_write(cmis.REG_RESET_ACQ_RX[1], bytes([mask]))
            if side in ('tx', 'both'):
                _bus_write(cmis.REG_RESET_ACQ_TX[1], bytes([mask]))
        return _ok({'lanes': lanes, 'side': side})
    except Exception as e:
        return _err(str(e), 500)


def _await_mls_commit(banks: int) -> dict:
    """Wait for a redirection commit, for as long as the module asked.

    6Dh:128.7-4 is MaxRedirectionCommitDuration, "maximum duration of the
    execution of a CommitMediaLaneRedirection command being in progress",
    in the Table 8-49 encoding. The tool already decoded and displayed it and
    then waited a hundred milliseconds of its own, so on a module that
    advertises longer the result was read back mid-execution: every lane
    reporting RedirectionCommitResult 2, "Command execution in progress",
    under a panel line telling the operator to press Commit.

    Polls rather than sleeping the whole budget: a module that finishes in a
    millisecond should not cost the advertised maximum.
    """
    code = (_read_upper(*cmis.REG_MLS_ADVERT)[0] >> 4) & 0x0F
    budget = cmis.state_duration(code).get('max_seconds')
    # An unbounded advertisement (Table 8-49 runs to "50 min or more") must
    # not hold the request open; the panel is told what was waited on.
    budget = min(budget if budget is not None else 0.1, 5.0)
    deadline = time.time() + budget
    running = True
    while True:
        running = False
        for bank in range(banks):
            _set_page(0x6D, bank)
            if 2 in _bus_read(cmis.REG_MLS_RESULT[1],
                                                 cmis.REG_MLS_RESULT[2]):
                running = True
                break
        if not running or time.time() >= deadline:
            break
        # A polling cadence, not a Chapter 10 wait: the loop has its own
        # deadline and stops on the module's own answer, so this only decides
        # how often it asks. Nothing in Chapter 10 governs it.
        time.sleep(0.01)
    return {'commit_complete': not running,
            'commit_max_seconds': budget,
            'commit_duration_label': cmis.state_duration(code).get('label')}


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
    err = _require_paged('Media lane switching')
    if err:
        return err
    caps = _state.get('caps') or {}
    if not caps.get('media_lane_switching_supported'):
        return _err('This module does not advertise media lane switching', 400)
    body = request.get_json(silent=True) or {}
    bad = _reject_unknown(body, ('redirection', 'enable', 'commit'))
    if bad:
        return bad
    mapping = body.get('redirection') or []
    enable = body.get('enable')
    commit = bool(body.get('commit', False))
    banks = (_state['lanes'] + 7) // 8
    try:
        # Table 8-196: with EnableMediaLaneRedirection clear "commit command
        # is without effect" - nothing moves and no RedirectionCommitResult is
        # written, so the module never says it ignored anything. This used to
        # write the commit anyway and answer committed, and the panel then
        # told the operator to press Commit again. The page sends its Enable
        # box with every request, so an unticked box turned every Commit into
        # a disable followed by a commit that could not happen.
        # Judged with this request's own enable applied, since that is written
        # first, and before anything is written: a stage that goes through
        # while the commit it came with is refused is half an action.
        if commit:
            enables = ([1 if enable else 0] * banks if enable is not None
                       else [raw[0] for _b, raw
                             in _read_banks(*cmis.REG_MLS_ENABLE)])
            off = cmis.mls_disabled_groups(enables)
            if off:
                where = ('' if banks == 1 else
                         ' on group%s %s' % ('s' if len(off) > 1 else '',
                                             ', '.join(map(str, off))))
                return _err(
                    'Media lane redirection is disabled%s (6Dh:152), and '
                    'Table 8-196 says a commit is then without effect: the '
                    'module changes nothing and writes no result to say so. '
                    'Enable redirection in the same request, or first.'
                    % where, 400)
        if mapping:
            # A target is a lane of its own group of eight (section 8.33), so
            # the request is one permutation per group. Truncating at eight
            # accepted a sixteen lane request, wrote half of it and answered
            # ok - the panel then showed a switch configuration for lanes 9
            # and up that the module had never been asked for.
            targets = [int(v) for v in mapping]
            if len(targets) != _state['lanes']:
                # A short list is ambiguous - it could mean "only the first
                # group" or "I forgot the rest" - and guessing is what this
                # was doing when it truncated at eight. Committing after a
                # partial stage would also commit whatever the untouched
                # groups already had staged, which nobody asked for in that
                # action.
                return _err('This module has %d media lanes and the '
                            'redirection must name a target for each of them; '
                            '%d were sent'
                            % (_state['lanes'], len(targets)), 400)
            groups = [targets[i * 8:i * 8 + 8]
                      for i in range((len(targets) + 7) // 8)]
            for bank, group in enumerate(groups):
                if sorted(group) != list(range(1, len(group) + 1)):
                    return _err(
                        'Redirection must be a permutation of the media lanes '
                        'within each group of 8 (6Dh is banked, and a target '
                        'is a lane of its own group). Lanes %d-%d are not one; '
                        'the module would reject it'
                        % (bank * 8 + 1, bank * 8 + len(group)), 400)
            err = _refuse_broadcast_divergence({'The redirection': groups})
            if err:
                return err
            for bank, group in enumerate(groups):
                _set_page(0x6D, bank)
                _bus_write(cmis.REG_MLS_REDIRECTION[1],
                                              bytes(group))
        # Enable and commit are per bank too, and doing them in bank 0 alone
        # left the other groups neither enabled nor committed while the panel
        # reported the whole module enabled and committed.
        if enable is not None:
            for bank in range(banks):
                _set_page(0x6D, bank)
                _bus_write(cmis.REG_MLS_ENABLE[1],
                                              bytes([1 if enable else 0]))
        out = {'committed': commit, 'banks': banks}
        if commit:
            for bank in range(banks):
                _set_page(0x6D, bank)
                _bus_write(cmis.REG_MLS_COMMIT[1], bytes([1]))
            out.update(_await_mls_commit(banks))
        return _ok(out)
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


def _dp_state_overruns(lanes) -> None:
    """Mark lanes stuck in a transient state past what the module advertised.

    Tables 8-48 and 8-56 say these fields exist "so that hosts can determine
    when something failed in the module during these states, for example a
    module firmware hang up". Watching a lane sit in DPInit with no idea
    whether that is normal is the situation they were written for.
    """
    seen = _state['dp_state_since']
    durations = _state['caps'].get('durations') or {}
    now = time.time()
    for lane in lanes:
        num = lane['lane']
        state = lane.get('datapath_state')
        was = seen.get(num)
        if not was or was[0] != state:
            seen[num] = (state, now)
            was = seen[num]
        field = cmis.DP_STATE_DURATION_FIELD.get(state)
        adv = (durations.get(field) or {}) if field else {}
        limit = adv.get('max_seconds')
        label = adv.get('label')
        # Table 10-8 caps what may be advertised for turning a Tx output on
        # or off. A module that advertises more has not bought itself the
        # extra time: the lane has overrun at the table's limit.
        ceiling = adv.get('spec_ceiling_s')
        from_spec = bool(ceiling and (limit is None or ceiling < limit))
        if from_spec:
            limit = ceiling
            label = '%d ms (Table 10-8 %s; the module advertises %s)' % (
                round(ceiling * 1000), adv.get('spec_symbol'), adv.get('label'))
        elapsed = now - was[1]
        lane['state_seconds'] = round(elapsed, 1)
        lane['state_max_seconds'] = limit
        lane['state_max_label'] = label
        lane['state_max_from_spec'] = from_spec
        # Only a transient state can overrun: the steady ones last as long as
        # the module is left in them.
        lane['state_overrun'] = bool(limit and elapsed > limit)


@app.route('/api/module/monitoring', methods=['GET'])
def api_module_monitoring():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Lane monitoring')
    if err:
        return err
    try:
        # DataPath state and Config Status pack 4 bits per lane, so they are
        # parsed per bank; the monitors are 2 bytes per lane and concatenate.
        dp_states, cfg_statuses, cfg_codes = [], [], []
        for _bank, raw in _read_banks(*cmis.REG_DP_STATE):
            dp_states += cmis.parse_dp_states(raw)
        for _bank, raw in _read_banks(*cmis.REG_CONFIG_STATUS):
            cfg_statuses += cmis.parse_config_status(raw)
            cfg_codes += cmis.parse_config_status_codes(raw)
        # 01h:160.4-3 multiplies the 2 uA bias increment, so a module using
        # x2 or x4 reads half or a quarter of its real bias without it.
        bias_scale = ((_state.get('caps') or {}).get('monitors')
                      or {}).get('tx_bias_scale', 1)
        # 11h:132-133 (Table 8-95, RO/Required) report whether an output is
        # really carrying a valid signal, "independent of the state of the
        # DPSM instances associated with those output lanes" (8.14.2). Four
        # controls in this tool mute an output and none of them change the
        # DataPath State, so without this a muted lane looks fully Activated.
        out_rx, out_tx = [], []
        for _bank, raw in _read_banks(cmis.REG_OUTPUT_STATUS_RX[0],
                                      cmis.REG_OUTPUT_STATUS_RX[1], 2):
            out_rx += cmis.parse_lane_flags(raw[0])
            out_tx += cmis.parse_lane_flags(raw[1])
        # 8.32: Page 62h is the per-media-lane version of the Tx output power
        # thresholds, in 0.01 dBm rather than Page 02h's module-wide 0.1 uW.
        # Where the module publishes it, it is what applies to each lane -
        # colouring a lane against the module-wide numbers instead is a
        # judgement the module did not make. Read here rather than cached at
        # connect because 7.5.3 lets a lane's thresholds move with its
        # programmed output power.
        lane_thr = []
        if (_state.get('caps') or {}).get('page_62h_supported'):
            for _bank, raw in _read_banks(*cmis.REG_LANE_PWR_THRESHOLDS):
                lane_thr += cmis.parse_lane_power_thresholds(raw)
        tx_power_raw  = _read_banked_scalars(*cmis.REG_TX_POWER[:2], 2)
        tx_bias_raw   = _read_banked_scalars(*cmis.REG_TX_BIAS[:2], 2)
        rx_power_raw  = _read_banked_scalars(*cmis.REG_RX_POWER[:2], 2)

        # 01h:160.0-2 (Table 8-53): each of these three lane monitors is
        # optional. An unimplemented one reads zero, and zero is not a
        # non-answer here - it is a dark laser and an unpowered module. The
        # readings are left out rather than reported as measurements.
        has_tx_pwr = _monitor_present('tx_optical_power')
        has_rx_pwr = _monitor_present('rx_optical_power')
        has_bias = _monitor_present('tx_bias')

        # Table 7-8's NA values, where the module advertises them: 0 for Tx
        # power and bias, and for Rx power 0 (lane not in use) or 1 (in use,
        # no valid sample) - the last one reads as 0.1 uW, -40 dBm, and was
        # shown as a measurement.
        na_on = bool((_state.get('caps') or {}).get('na_values'))

        lanes = []
        for i in range(_state['lanes']):
            tx_uw = cmis.parse_power_uw(tx_power_raw[i*2:(i+1)*2])
            rx_uw = cmis.parse_power_uw(rx_power_raw[i*2:(i+1)*2])
            bias_ma = cmis.parse_tx_bias_ma(tx_bias_raw[i*2:(i+1)*2], bias_scale)
            raw_of = lambda block: int.from_bytes(block[i*2:(i+1)*2], 'big')
            lane_na = []
            rx_na = None
            if na_on:
                if raw_of(tx_power_raw) == cmis.NA_TX_POWER:
                    lane_na.append('tx_power')
                if raw_of(tx_bias_raw) == cmis.NA_TX_BIAS:
                    lane_na.append('tx_bias')
                rx_na = cmis.NA_RX_POWER.get(raw_of(rx_power_raw))
                if rx_na:
                    lane_na.append('rx_power')
            has_tx = has_tx_pwr and 'tx_power' not in lane_na
            has_rx = has_rx_pwr and 'rx_power' not in lane_na
            has_b = has_bias and 'tx_bias' not in lane_na
            # Table 8-99 calls these three "Media Lane-Specific Monitors", and
            # the rows are host lanes. On a module that carries more host
            # lanes than media lanes - a coherent one takes eight into a
            # single optical carrier - the rows past the last media lane were
            # reading registers for lanes the module says it does not have.
            # Those read zero, which is 0.0 uW: the bottom of the dBm scale,
            # and the value the panel paints in alarm red.
            media = _media_lane_present(i + 1)
            lanes.append({
                'lane': i + 1,
                'media_lane_present': media,
                'tx_power_uw': round(tx_uw, 2) if has_tx and media else None,
                'tx_power_dbm': (round(cmis.uw_to_dbm(tx_uw), 2)
                                 if has_tx and media else None),
                'rx_power_uw': round(rx_uw, 2) if has_rx and media else None,
                'rx_power_dbm': (round(cmis.uw_to_dbm(rx_uw), 2)
                                 if has_rx and media else None),
                'tx_bias_ma': round(bias_ma, 3) if has_b and media else None,
                # Which readings were the module's NA, and for Rx power why.
                'na': lane_na if media else [],
                'rx_power_na': rx_na if media else None,
                'datapath_state': dp_states[i],
                'datapath_state_kind': cmis.dp_state_kind(dp_states[i]),
                # 6.3.3: the Flags of this lane's monitors are assured only in
                # DPInitialized and DPActivated. A lane taken down still
                # reports a power, and colouring that by threshold announced a
                # fault on a lane that had simply been switched off - which
                # this tool's own DPDeinit does routinely.
                'dp_monitors_assured': cmis.dp_monitors_assured(dp_states[i]),
                # 11h:133 is indexed by media lane, not host lane: Table
                # 8-95 says "The signal on an Tx output media lane is declared
                # valid in the OutputStatusTx register". Its twin one byte
                # earlier is the host-lane one - "The signal on an Rx output
                # host lane is declared valid in the OutputStatusRx register".
                # So the same cell holds one fact about each side, and only
                # one of them is about a lane this module has.
                #
                # Ungated, a conformant module reporting 0 for a media lane it
                # does not have produced "Tx output is muted although the data
                # path is Activated - check Tx disable, force squelch or Rx
                # output disable" on seven lanes that are not there, while the
                # optical power beside it was correctly blank.
                'output_valid_tx': out_tx[i] if media else None,
                'output_valid_rx': out_rx[i],
                'config_status': cfg_statuses[i],
                # Whether the module accepted the configuration is a
                # property of the code, not of how its name is spelled.
                'config_rejected': cfg_codes[i] in cmis.CONFIG_STATUS_REJECTED,
                # Table 8-101 gives each rejection a different reason, and the
                # reason is what tells the operator what to change. The name
                # alone cannot carry it: three of the codes have no name.
                'config_status_code': cfg_codes[i],
            })
            # Per media lane as well (Table 8-192), so a host lane with no
            # media lane behind it has no threshold of its own - and saying
            # otherwise would put a window on a reading that does not exist.
            if media and i < len(lane_thr):
                t = lane_thr[i]
                lanes[-1].update({
                    'tx_threshold_source': '62h',
                    'tx_power_high_alarm_dbm': t['hi_alarm_dbm'],
                    'tx_power_low_alarm_dbm':  t['lo_alarm_dbm'],
                    'tx_power_high_warn_dbm':  t['hi_warn_dbm'],
                    'tx_power_low_warn_dbm':   t['lo_warn_dbm'],
                })

        _dp_state_overruns(lanes)
        monitors_present = {'tx_optical_power': has_tx_pwr,
                            'rx_optical_power': has_rx_pwr,
                            'tx_bias': has_bias}
        # Section 6.3.2.4: monitoring results "shall be within the relevant
        # accuracy requirements when the module is in the ModuleReady state",
        # and alarm and warning Flag semantics are "only assured in the
        # ModuleReady MSM state". Outside it the module still answers, so the
        # table drew a low power module's -40 dBm in alarm red - a fault the
        # module never claimed. The state travels with the readings because
        # it is the readings it qualifies; it lives one byte into lower
        # memory, so this costs no page change.
        module_state = cmis.parse_module_state(_read_lower(0x03, 1)[0])
        return _ok({'lanes': lanes,
                    'module_state': module_state,
                    'monitors_assured': module_state == 'ModuleReady',
                    # Which of the three lane monitors this module has at all,
                    # so a missing column reads as "not implemented" rather
                    # than as a poll that came back empty.
                    'monitors_present': monitors_present,
                    # Which wavelength and fibre each media lane is, where
                    # the module says. Read at connect, so it costs nothing
                    # per poll.
                    'media_lane_map': _state['caps'].get('media_lane_map', []),
                    'durations': _state['caps'].get('durations', {})})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/datapath', methods=['GET'])
def api_datapath_get():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Data Path state')
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

        # Page 10h is what was asked for; 11h is what the module is running.
        # A rejected Apply leaves the two disagreeing, and showing only the
        # staged value made a refused configuration look like the live one.
        active_app_select = []
        # The whole byte, not just the Application. Table 8-102 makes DPIDX
        # and ExplicitControl RO and Required in the Active Control Set: the
        # module states which lanes form a Data Path, and per lane whether the
        # signal integrity settings in force are the host's or its own. Both
        # were being discarded, and both were then being guessed at - the Data
        # Path grouping from Application widths, and ExplicitControl from what
        # this tool happens to write.
        active_dpconfig = []
        for _bank, raw in _read_banks(*cmis.REG_ACTIVE_APP_SELECT):
            active_app_select += cmis.unpack_appselect(raw)
            active_dpconfig += cmis.unpack_dpconfig(raw)

        # Table 8-106: the Active Control Set having been updated is not the
        # same as the hardware running it. DPInitPending says a Provision has
        # copied a staged set across but the transit through DPInit that
        # commits it "is still pending", so "the Active Control Set content
        # may deviate from the actual hardware configuration" - which is the
        # one caveat on reading 11h as what the module is doing.
        dp_init_pending = []
        for _bank, raw in _read_banks(*cmis.REG_DP_INIT_PENDING):
            dp_init_pending += cmis.parse_dp_init_pending(raw[0])

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
                'active_app_select': (active_app_select[i]
                                      if i < len(active_app_select) else 0),
                # 11h:206-213 bits 3-1 and bit 0 (Table 8-102).
                'active_dpidx': (active_dpconfig[i]['dpidx']
                                 if i < len(active_dpconfig) else None),
                'active_explicit_control': (
                    active_dpconfig[i]['explicit_control']
                    if i < len(active_dpconfig) else False),
                'dp_init_pending': (dp_init_pending[i]
                                    if i < len(dp_init_pending) else False),
                'tx_polarity_flip': bool((tx_pol_masks[b] >> bit) & 1),
                'rx_polarity_flip': bool((rx_pol_masks[b] >> bit) & 1),
            })

        # Apply commits the whole Staged Control Set, and the signal integrity
        # half of it was never read or shown - so the tool has been applying
        # settings it could not name, and ConfigRejectedInvalidSI pointed at
        # values with nowhere in the UI to look them up.
        si_adv = ((_state.get('caps') or {}).get('si')) or {}
        si = {}
        try:
            if si_adv.get('tx_adaptive_input_eq'):
                si['tx_adaptive_eq'] = _si_lane_flags(
                    cmis.REG_SCS_TX_ADAPT_EQ)
            # Table 6-5 splits these controls into an adaptive group -
            # Freeze, Store, Recall - and a non-adaptive one, the target. The
            # target was read from the start; of the adaptive group only the
            # enable bit was, so the panel knew a lane was adapting and
            # nothing else about it. Store is write-only (10h:135-136) and
            # has nothing to report.
            if si_adv.get('tx_input_eq_freeze'):
                si['tx_eq_freeze'] = _si_lane_flags(
                    cmis.REG_TX_ADAPT_EQ_FREEZE)
            if si_adv.get('tx_input_eq_recall_buffers'):
                si['tx_eq_recall'] = _si_pairs(cmis.REG_SCS_TX_EQ_RECALL)
            if si_adv.get('tx_input_eq_host_control'):
                si['tx_input_eq_target'] = _si_nibbles(
                    cmis.REG_SCS_TX_EQ_TARGET)
            # 10h:160, the byte before the Rx one, and advertised the
            # same way. The Flags panel has been reporting Tx CDR loss of
            # lock all along with no way to see whether that CDR is even in
            # circuit - a bypassed CDR cannot lock, and the flag says the
            # same thing either way.
            if cmis.cdr_host_controllable(si_adv, 'tx'):
                si['tx_cdr_enable'] = _si_lane_flags(
                    cmis.REG_SCS_TX_CDR)
            # Both bits, not just the bypass one: 10h:161 says
            # "Advertisement: 01h:162.0-1".
            if cmis.cdr_host_controllable(si_adv, 'rx'):
                si['rx_cdr_enable'] = _si_lane_flags(
                    cmis.REG_SCS_RX_CDR)
            eq = si_adv.get('rx_output_eq_control', 0)
            if eq in (1, 3):
                si['rx_eq_pre_cursor'] = _si_nibbles(
                    cmis.REG_SCS_RX_EQ_PRE)
            if eq in (2, 3):
                # With only pre-cursor advertised the post-cursor bytes carry
                # the pre-cursor target instead (Table 8-84), so the label has
                # to follow the advertisement rather than the address.
                si['rx_eq_post_cursor'] = _si_nibbles(
                    cmis.REG_SCS_RX_EQ_POST)
            if si_adv.get('rx_output_amplitude_control'):
                si['rx_output_amplitude'] = _si_nibbles(
                    cmis.REG_SCS_RX_AMPLITUDE)
        except Exception:
            # An optional control area a module does not implement is not a
            # reason to fail the whole page.
            si = {}

        # Tables 8-104/8-105: the module's own statement of what it is
        # provisioned with. With ExplicitControl clear - which is what this
        # tool writes - these "were determined by the module according to the
        # selected Application" rather than taken from the staged set, so the
        # staged numbers above are a request and these are the answer.
        si_active = {}
        try:
            if si_adv.get('tx_adaptive_input_eq'):
                si_active['tx_adaptive_eq'] = _si_lane_flags(
                    cmis.REG_ACS_TX_ADAPT_EQ)
            # 11h:215-216 is the only half of the adaptive group with an
            # Active Control Set counterpart, and it answers a different
            # question: the staged field says which buffer to recall, this
            # one says which buffer was recalled.
            if si_adv.get('tx_input_eq_recall_buffers'):
                si_active['tx_eq_recall'] = _si_pairs(
                    cmis.REG_ACS_TX_EQ_RECALLED)
            if si_adv.get('tx_input_eq_host_control'):
                si_active['tx_input_eq_target'] = _si_nibbles(
                    cmis.REG_ACS_TX_EQ_TARGET)
            if cmis.cdr_host_controllable(si_adv, 'tx'):
                si_active['tx_cdr_enable'] = _si_lane_flags(
                    cmis.REG_ACS_TX_CDR)
            if cmis.cdr_host_controllable(si_adv, 'rx'):
                si_active['rx_cdr_enable'] = _si_lane_flags(
                    cmis.REG_ACS_RX_CDR)
            eq = si_adv.get('rx_output_eq_control', 0)
            if eq in (1, 3):
                si_active['rx_eq_pre_cursor'] = _si_nibbles(
                    cmis.REG_ACS_RX_EQ_PRE)
            if eq in (2, 3):
                si_active['rx_eq_post_cursor'] = _si_nibbles(
                    cmis.REG_ACS_RX_EQ_POST)
            if si_adv.get('rx_output_amplitude_control'):
                si_active['rx_output_amplitude'] = _si_nibbles(
                    cmis.REG_ACS_RX_AMPLITUDE)
        except Exception:
            si_active = {}

        # Which lanes make up each Data Path, as lane numbers. The panel used
        # to work this out for itself from the Application width, assuming a
        # Data Path is an aligned block of that many lanes - which is not the
        # rule this server applies when it rounds a mask up to whole Data
        # Paths, and the two disagreed on most lane assignments. Publishing
        # the one the writes actually use leaves a single answer to the
        # question.
        host_lanes_by_app = {}
        _apps = []
        try:
            _apps = cmis.parse_application_descriptors(
                _read_lower(0x56, 32), _read_lower(0x55, 1)[0],
                _additional_app_descriptors(),
                _media_lane_assignments(), _flat_memory())
            for a in _apps:
                host_lanes_by_app[a['app_sel']] = a.get('host_lanes') or 1
        except Exception:
            pass
        groups = [[i + 1 for i in g]
                  for g in _datapath_groups(app_select[:_state['lanes']],
                                            host_lanes_by_app)]

        # Page 15h, read here rather than with the advertisements: 8.18
        # calls these "read-only reporting registers (not necessarily
        # static)", so they belong with the things the refresh button
        # re-reads, not with the block that is read once at connect.
        # _state['lanes'], not the `lanes` in scope here - that one is the
        # per-lane row list this handler is building, and passing it would
        # slice the latency list by a list.
        try:
            dp_latency = _read_dp_latency(_state['lanes'])
        except Exception:
            dp_latency = None
        latency_conflicts = cmis.latency_disagreements(
            groups, dp_latency) if dp_latency else []

        return _ok({
            'dp_latency': dp_latency,
            'latency_conflicts': latency_conflicts,
            'signal_integrity': si,
            'signal_integrity_active': si_active,
            'si_advertised': si_adv,
            # 01h:161.6-5 is a count and the recall code is a buffer number,
            # so the two are comparable and were never compared. The staged
            # set is where a host-written value sits; the Active Control Set
            # is the module's own report and is left to speak for itself.
            'si_recall_unadvertised': cmis.recall_buffer_violations(
                si.get('tx_eq_recall') or [],
                si_adv.get('tx_input_eq_recall_buffers') or 0),
            'tx_disable_mask': tx_disable_mask,
            # OutputDisableTx is per media lane (Table 8-79) in a table of
            # host-lane rows; this says which rows' Tx box controls a lane.
            'media_lanes_present': _media_lanes_present(),
            'dp_deinit_mask':  dp_deinit_mask,
            'tx_polarity_flip_mask': tx_pol_mask,
            'rx_polarity_flip_mask': rx_pol_mask,
            'app_select': app_select,
            'active_app_select': active_app_select,
            # The module's own grouping, by the lowest lane of each Data Path.
            # The tool infers one from Application widths because it writes
            # DPIDX zero into the staged set; this is what the module reports
            # about the set it is actually running.
            # Lane numbers, the same way datapath_groups above reports
            # them: two bases in one payload is a trap for the reader.
            'active_datapath_groups': [[i + 1 for i in g]
                                      for g in _groups_from_dpidx(active_dpconfig)],
            'explicit_control_lanes': [
                i + 1 for i, d in enumerate(active_dpconfig)
                if d['explicit_control']],
            # Where each Application may begin, and whether what is staged
            # right now breaks that. Published rather than re-derived in the
            # page: the grouping is the server's, and two answers to which
            # lanes make up a Data Path is the trap this panel already had
            # once.
            'app_lane_starts': {
                str(a['app_sel']): [b + 1 for b in range(8)
                                    if (a.get('host_lane_assign_mask') or 0)
                                    >> b & 1]
                for a in (_apps or [])
                if a.get('host_lane_assign_mask')},
            'lane_start_violations': cmis.lane_start_violations(
                groups, app_select[:_state['lanes']], _apps or []),
            # Keyed by the first host lane of each Data Path, the same way
            # datapath_groups is ordered. Nominal, and the flag beside it
            # says so: a module that can redirect media lanes reports the
            # mapping it actually committed on Page 6Dh.
            'media_lane_groups': {
                str(k): v for k, v in _nominal_media_lanes(
                    groups, app_select[:_state['lanes']],
                    _apps or []).items()},
            # True when the module can redirect media lanes: then 7.9.1's
            # allocation is what would happen by default and Page 6Dh's
            # committed mapping is what did.
            'media_lanes_are_nominal': bool(
                (_state.get('caps') or {}).get(
                    'media_lane_switching_supported')),
            'datapath_groups': groups,
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
        data = _read_lower(0x56, 32)  # the first eight, 4 bytes each
        media_type = _read_lower(0x55, 1)[0]
        apps = cmis.parse_application_descriptors(
            data, media_type, _additional_app_descriptors(),
            _media_lane_assignments(), _flat_memory())
        # 01h:175: a module with Normalized Application Descriptors keeps
        # the rest of its Applications on Page 1Ch, so this list is a prefix
        # rather than the set. Saying so beats showing fifteen of hundreds
        # as though that were all of them.
        # Page 1Ch itself, now that the panel can say more than how many
        # banks it declined to read. Bank 0 mirrors the basic list by
        # requirement (6.2.1.6.2), so the two are cross-checked rather than
        # concatenated - a module that disagrees with itself here is
        # advertising two different Applications under one AppSel code.
        try:
            nads = _read_nad_applications(media_type)
        except Exception:
            nads = None
        mirror = cmis.nad_mirror_mismatches(
            [n for n in nads if n['nad_block_index'] == 0], apps
        ) if nads else []
        return _ok({'applications': apps,
                    'nad_applications': nads,
                    'nad_mirror_mismatches': mirror,
                    'nad': _state['caps'].get('nad', {})})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/control', methods=['GET'])
def api_module_control_get():
    """Read Module Control register at lower memory byte 0x1A."""
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Lane controls')
    if err:
        return err
    try:
        ctrl_raw = _read_lower(0x1A, 1)
        # 'raw' backs the UI hover tooltips, which quote the byte a control maps to.
        # 'access' travels with the values because one of these bits is WO/SC
        # (Table 8-11) and reads back as zero whatever the module is doing -
        # showing it in the same status column as the four RW bits claims it is
        # state. The types come from here rather than being written down in the
        # page as well: two copies of a table taken from the specification is
        # how the two come to disagree.
        return _ok(dict(cmis.parse_module_control(ctrl_raw[0]),
                        raw=ctrl_raw[0],
                        access=dict(cmis.MODULE_CONTROL_ACCESS)))
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/control', methods=['POST'])
def api_module_control_set():
    """Write Module Control register (software reset / low power)."""
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Lane controls')
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        # The read side of this register names the same bits differently, so a
        # caller round-tripping what it just read changed nothing and was told
        # it worked. Both spellings are accepted; neither is guessed at.
        _ALIASES = {
            'low_pwr_request_sw':      'low_pwr',
            'low_pwr_allow_request_hw': 'allow_lp_hw',
            'squelch_method_select':   'squelch_method',
            'bank_broadcast_enable':   'bank_broadcast',
        }
        body = {_ALIASES.get(k, k): v for k, v in body.items()}
        bad = _reject_unknown(body, ('action', 'low_pwr', 'software_reset',
                                     'allow_lp_hw', 'squelch_method',
                                     'bank_broadcast'))
        if bad:
            return bad
        action = body.get('action', '')
        # 01h:155.5-4 (Table 8-51): only 11b means "Host controls the method
        # for Tx output squelching". At 01b or 10b the module squelches one
        # way and the bit is not a choice - writing it leaves the panel
        # reporting a method the module is not using.
        if 'squelch_method' in body:
            method = (_state.get('caps') or {}).get('controls', {}).get(
                'squelch_method_tx')
            if method != 3:
                names = {0: 'has no Tx output squelching',
                         1: 'squelches by reducing OMA',
                         2: 'squelches by reducing Pav'}
                return _err('This module %s and does not let the host choose '
                            'the method (01h:155.5-4 = %02db)'
                            % (names.get(method, 'does not advertise the '
                                                 'choice'),
                               int(bin(method or 0)[2:]) if method else 0), 400)

        # 01h:156.7 advertises the control (Table 8-11). Setting a bit the
        # module does not implement asks for a write policy it will not apply
        # while telling the operator it is on.
        if body.get('bank_broadcast') and not (
                _state.get('caps') or {}).get('controls', {}).get(
                    'bank_broadcast'):
            return _err('This module does not advertise bank broadcast '
                        '(01h:156.7), so the control has no effect', 400)

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

        _bus_write(cmis.REG_MODULE_CONTROL[1], bytes([val]))
        # SoftwareReset (bit 3) restarts the module, and until it is
        # manageable again - up to tMgmtInit (Table 10-2) - it may refuse
        # every access. The first read after a reset was a failure.
        if val & 0x08:
            _state['holdoff_until'] = (time.monotonic()
                                       + cmis.TIMING_SECONDS['tMgmtInit'])
        time.sleep(0.05)
        # A reset restarts the module, which restores PageMapping to its
        # default, so the page we think is selected no longer applies.
        _invalidate_page()
        # 01h:167 (Table 8-56) advertises how long this module may take over
        # ModulePwrUp and ModulePwrDn, and every shipped profile says up to
        # five seconds to power down. Fifty milliseconds here and a fixed
        # refresh in the page meant the state was read back long before the
        # module had moved, so the panel showed the state it had *before* the
        # request as the result of it. What the module said it needs travels
        # with the answer instead of being guessed at.
        return _ok({'message': f'Module control written (0x{val:02X})',
                    'value': val,
                    'transition': _control_transition(action, body)})
    except Exception as e:
        return _err(str(e), 500)


def _control_transition(action, body) -> dict:
    """What the module said it may need for the state change just requested.

    Only the two power transitions have an advertised budget. A reset ends by
    powering up, so that is the one it is measured against, but it passes
    through MgmtInit first and no target state is claimed for it: whether the
    module lands in ModuleLowPwr or ModuleReady depends on the low power
    request bits it comes back with.
    """
    dur = (_state.get('caps') or {}).get('durations') or {}
    if action == 'low_power' or body.get('low_pwr') is True:
        which, target = 'module_pwr_dn', 'ModuleLowPwr'
    elif action == 'high_power' or body.get('low_pwr') is False:
        which, target = 'module_pwr_up', 'ModuleReady'
    elif action == 'reset' or body.get('software_reset'):
        which, target = 'module_pwr_up', None
    else:
        return {}
    d = dur.get(which) or {}
    out = {'requested': action or 'fields', 'target_state': target,
           'max_seconds': d.get('max_seconds'), 'label': d.get('label'),
           'advertisement': '01h:167 %s' % which}
    if target is None:
        # Before powering up, the module has to become manageable at all:
        # tMgmtInit (Table 10-2), which 01h:167 does not include. Measured
        # against ModulePwrUp alone, the wait was over before a module
        # taking its full two seconds had answered once.
        init = cmis.TIMING_SECONDS['tMgmtInit']
        out['max_seconds'] = (None if d.get('max_seconds') is None
                              else init + d['max_seconds'])
        out['label'] = '%g s MgmtInit (Table 10-2) + %s' % (
            init, d.get('label') or 'an unadvertised ModulePwrUp')
        out['advertisement'] = 'Table 10-2 tMgmtInit + 01h:167 %s' % which
    return out


def _nominal_media_lanes(groups, app_select, apps):
    """Which media lanes each Data Path occupies, by the rule in 7.9.1.

    The host chooses host lanes and the media lanes follow from them. Nothing
    reports the result: it is not a register on a module without media lane
    switching, and CMIS does not need one because the derivation is fixed.
    This tool already reads the only input it takes.

    Where the module can redirect media lanes (Page 6Dh), the committed
    mapping there is the truth instead - the caller says which one it is
    showing rather than this function guessing.
    """
    # Eight, and only for Data Paths inside the first bank. 7.9.1 says
    # "for each Bank (i.e. group of eight host lanes) there are always eight
    # external media lanes", so the pool does not run on past lane 8 into a
    # ninth media lane - and 8.3.7 says a module wider than eight lanes
    # "cannot unambiguously declare" its media lanes at all. How the
    # allocation carries across banks is not written down, and answering it
    # here would be this tool inventing the rule.
    try:
        return cmis.media_lane_groups(
            [g for g in groups if g and g[0] <= 8], app_select, apps,
            media_lanes=8)
    except Exception:
        return {}


def _media_lane_assignments():
    """01h:176-190, the fifth descriptor byte for Applications 1-15 (Table
    8-60). "Not required for flat Memory Map modules", which have no Page 01h
    at all - there the descriptors are simply four bytes long.

    Cached for the life of the connection. It is a static advertisement, and
    the DataPath panel now needs it on every refresh - fetching it there
    would cost a page change to 01h and back on a page that already walks
    Lower memory and Pages 10h, 11h and 15h.
    """
    # Not in caps: that dict is serialised to the capabilities endpoint, and
    # raw bytes do not survive the trip - they take the whole reply with them.
    cached = _state.get('media_lane_assign')
    if cached is not None:
        return cached
    try:
        raw = _read_upper(*cmis.REG_MEDIA_LANE_ASSIGN)
    except Exception:
        raw = b''
    _state['media_lane_assign'] = raw
    return raw


def _additional_app_descriptors():
    """01h:223-250, the seven Application Descriptors that do not fit in lower
    memory (Table 8-61). Absent on a module that does not serve Page 01h, and
    harmless there: the list already ended at its FFh terminator."""
    try:
        return _read_upper(*cmis.REG_ADDITIONAL_APPS)
    except Exception:
        return b''


def _groups_from_dpidx(dpconfig: list) -> list:
    """Lanes grouped the way the module reports them, or [] if it does not.

    Table 8-102: DPIDX holds "the Data Path Index (DPIDX) of that Data Path:
    DPID (lowest numbered lane of Data Path)", so every lane of one Data Path
    carries the same value and that value names the lane it starts on. Unused
    lanes - AppSelCode 0 - are left out, because the specification says their
    DPIDX is to be ignored.

    Empty when nothing is provisioned, which is the honest answer: an
    all-zero Active Control Set is a module with no Data Paths, not one Data
    Path on lane 1.
    """
    # Keyed by bank as well as index: DPIDX is three bits and names a lane
    # within its own Bank, so bank 0's Data Path 0 and bank 1's are two
    # Data Paths. Grouping by the value alone merged them, and a 16-lane
    # module reported one Data Path across both banks.
    groups = {}
    for i, d in enumerate(dpconfig):
        if not d['app_sel'] or d['dpidx'] is None:
            continue
        groups.setdefault((i // 8, d['dpidx']), []).append(i)
    return [groups[k] for k in sorted(groups)]


def _datapath_groups(app_select: list, host_lanes_by_app: dict) -> list:
    """Split lanes into Data Paths using each Application's host lane width.

    CMIS requires Apply to be triggered on all lanes of a Data Path at once -
    a Data Path, not the module. The tool writes DataPathID 0 for every lane,
    so the grouping has to come from the Application descriptor instead: an
    Application using H host lanes occupies aligned runs of H lanes. On a
    module carrying two 400G Data Paths that is lanes 1-4 and 5-8, and
    applying to all eight takes down the one nobody touched.

    This is the staged set, where those zeros are the tool's own. What the
    module is actually running it states itself, at 11h:206-213 bits 3-1
    (Table 8-102), and _groups_from_dpidx reads that rather than deriving it.
    """
    groups, i = [], 0
    while i < len(app_select):
        sel = app_select[i]
        width = host_lanes_by_app.get(sel, 1) or 1
        run = list(range(i, min(i + width, len(app_select))))
        # A run only holds together while the Application stays the same.
        run = [j for j in run if app_select[j] == sel]
        groups.append(run or [i])
        i += len(run) or 1
    return groups


def _whole_datapaths(masks: list, app_select: list,
                     host_lanes_by_app: dict, banks: int) -> list:
    """Round a per-lane mask up so no Data Path is only partly selected."""
    lanes = set()
    for bank in range(banks):
        for bit in range(8):
            if (masks[bank] >> bit) & 1:
                lanes.add(bank * 8 + bit)
    if not lanes:
        return list(masks)
    out = set()
    for group in _datapath_groups(app_select, host_lanes_by_app):
        if lanes & set(group):
            out |= set(group)
    return [sum(1 << (l - bank * 8) for l in out
                if bank * 8 <= l < bank * 8 + 8) for bank in range(banks)]


def _lanes_needing_apply(old_sel: list, new_sel: list,
                         host_lanes_by_app: dict) -> set:
    """Lanes whose Data Path has a changed staged configuration.

    Both groupings matter: a lane leaving one Data Path disturbs the one it
    left as well as the one it joined.
    """
    changed = {i for i in range(len(new_sel))
               if i < len(old_sel) and old_sel[i] != new_sel[i]}
    if not changed:
        return set()
    out = set()
    for groups in (_datapath_groups(old_sel, host_lanes_by_app),
                   _datapath_groups(new_sel, host_lanes_by_app)):
        for g in groups:
            if changed & set(g):
                out |= set(g)
    return out


@app.route('/api/module/datapath', methods=['POST'])
def api_datapath_set():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Data Path control')
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        banks = (_state['lanes'] + 7) // 8
        bad = _reject_unknown(body, ('tx_disable_mask', 'tx_polarity_flip_mask',
                                     'rx_polarity_flip_mask', 'app_select',
                                     'dp_deinit_mask', 'apply',
                                     'apply_immediate'))
        if bad:
            return bad

        def _mask_now(reg):
            page, addr, _ = reg
            return [raw[0] for _b, raw in _read_banks(page, addr, 1)]

        # A caller changing the Application must not silently un-flip every
        # polarity it did not mention.
        tx_disable_now = _mask_now(cmis.REG_TX_OUTPUT_DIS)
        tx_disable = _keep(body, 'tx_disable_mask', tx_disable_now, banks)
        tx_pol = _keep(body, 'tx_polarity_flip_mask',
                       _mask_now(cmis.REG_TX_POL_FLIP), banks)
        rx_pol = _keep(body, 'rx_polarity_flip_mask',
                       _mask_now(cmis.REG_RX_POL_FLIP), banks)
        dp_deinit = _keep(body, 'dp_deinit_mask',
                          _mask_now(cmis.REG_DP_DEINIT), banks)
        apply = bool(body.get('apply', False))
        apply_now = bool(body.get('apply_immediate', False))

        # 8.13.1 says of the DPDeinit byte that "the module evaluates this
        # Byte only in Module State ModuleReady", and 6.3.3 that a DPSM
        # "remains in the DPDeactivated State until the Module State Machine
        # is in the ModuleReady state". So outside ModuleReady a deinit is not
        # read and no Apply can move a Data Path anywhere - both are discarded
        # in silence, and answering ok to them says the module reconfigured
        # itself while it was asleep.
        #
        # Only the parts the DPSM has to act on. Table 8-77 puts the lane
        # controls on 10h:129-142 - polarity, output disable, squelch -
        # "independent of the Data Path State machine or control sets", and
        # they take effect on the write, so they keep working here. Staging an
        # AppSelect is a write to memory and is likewise none of the DPSM's
        # business until an Apply arrives.
        wants_dpsm = [name for name, on in
                      (('DPDeinit', 'dp_deinit_mask' in body),
                       ('Apply', apply), ('ApplyImmediate', apply_now)) if on]
        if wants_dpsm:
            module_state = cmis.parse_module_state(_read_lower(0x03, 1)[0])
            if module_state != 'ModuleReady':
                return _err(
                    '%s needs the Data Path state machines, and this module '
                    'is in %s: a deinit is not evaluated and no Data Path can '
                    'leave DPDeactivated outside ModuleReady, so this would '
                    'report success and change nothing. Bring the module to '
                    'high power first'
                    % (' and '.join(wants_dpsm), module_state), 409)

        # The state each Data Path was in when the host decided to Apply, read
        # before this request writes anything. Reading it afterwards would see
        # the transient this very request had just started - a DPDeinit and an
        # Apply in one write is the release sequence 6.2.4.3 mandates, and it
        # would have refused itself.
        dp_states_before = []
        config_before = []
        if apply or apply_now:
            for _bank, raw in _read_banks(*cmis.REG_DP_STATE):
                dp_states_before += cmis.parse_dp_states(raw)
            # Read at the same moment and for the same reason: the question
            # is whether a configuration command is already running, and this
            # request's own trigger would start one.
            for _bank, raw in _read_banks(*cmis.REG_CONFIG_STATUS):
                config_before += cmis.parse_config_status_codes(raw)

        # What is staged right now, and how wide each Application is - both
        # are needed to work out which Data Paths this write actually touches.
        prev_app_select = []
        for _b, raw in _read_banks(*cmis.REG_APP_SELECT):
            prev_app_select += cmis.unpack_appselect(raw)

        # Defaulting this to AppSel 1 meant a request that only flipped a
        # polarity silently reconfigured the Application on every lane.
        app_select = body.get('app_select',
                              prev_app_select[:_state['lanes']]
                              or [1] * _state['lanes'])
        host_lanes_by_app = {}
        # Bound before the try: the lane-start check below needs the
        # descriptors, and an unbound name there would turn a module whose
        # descriptors failed to parse into a 500.
        _apps = []
        try:
            _apps = cmis.parse_application_descriptors(
                _read_lower(0x56, 32), _read_lower(0x55, 1)[0],
                _additional_app_descriptors(),
                _media_lane_assignments(), _flat_memory())
            for a in _apps:
                host_lanes_by_app[a['app_sel']] = a.get('host_lanes') or 1
        except Exception:
            pass

        refused = _refuse_unsupported((
            ('output_disable_tx', tx_disable),
            ('input_polarity_flip_tx', tx_pol),
            ('output_polarity_flip_rx', rx_pol),
        )) or _refuse_absent_media_lanes((
            ('OutputDisableTx', tx_disable, tx_disable_now),
        ))
        if refused:
            return refused

        # 6.2.3.2.1 puts an obligation on the host here - "The host must
        # assign lanes to Data Paths in accordance with the Lane Assignment
        # Options field advertised by the module" - and this endpoint
        # deliberately does not enforce it.
        #
        # The line this tool draws is whether the module answers. It refuses
        # a write the module would swallow in silence, because a control that
        # reports applied and does nothing is the one outcome an operator
        # cannot diagnose. A Data Path on the wrong boundary is the opposite:
        # the module says ConfigRejectedInvalidDataPath (4h) in
        # ConfigStatusLane, by name, and seeing what a real module does with
        # a bad allocation is a thing this tool exists to allow.
        #
        # So the warning goes on the panel before the write, and the write
        # goes through. lane_start_violations is published by the GET.

        bad = _refuse_broadcast_divergence({
            'tx_disable_mask': tx_disable,
            'tx_polarity_flip_mask': tx_pol,
            'rx_polarity_flip_mask': rx_pol,
            'dp_deinit_mask': dp_deinit,
            'app_select': [tuple(app_select[b * 8:b * 8 + 8])
                           for b in range((_state['lanes'] + 7) // 8)],
        })
        if bad:
            return bad

        # "All lanes of a Data Path must have the same value" (Table 8-78), so
        # a request that deinitialises one lane takes its whole Data Path down
        # - the alternative is a half-torn-down path the module never asked
        # for. The same grouping the Apply mask uses.
        if 'dp_deinit_mask' in body:
            dp_deinit = _whole_datapaths(dp_deinit, app_select,
                                         host_lanes_by_app, banks)

        # Every refusal below is decided before anything is written. They
        # say "this would report success and change nothing", and they were
        # decided after the lane controls, the staged Application and
        # DPDeinit had already gone down - so a refused Apply could take
        # every Data Path to DPDeactivated and turn Tx outputs off while
        # telling the operator nothing had changed. DPDeinit and 10h:129-142
        # act on the write (Tables 8-77, 8-78); there is no undoing them.
        # ApplyDPInit deinitialises and re-initialises the Data Paths whose
        # lanes are selected, so the mask decides what drops. Writing 0xFF
        # dropped every Data Path on the module, including ones the operator
        # had not touched - on a module carrying two 400G ports, reconfiguring
        # one took the other down with it.
        #
        # Page 10h:129-142 are lane controls "independent of the Data Path
        # State machine or control sets" (Table 8-77): polarity, output
        # disable and squelch take effect on the write. Only a changed staged
        # configuration needs an Apply at all.
        applied = []
        if apply and apply_now:
            return _err('Choose one Apply trigger: ApplyDPInit re-initialises '
                        'the Data Path, ApplyImmediate commits without it', 400)
        if apply_now and not (_state.get('caps') or {}).get(
                'config', {}).get('hot_reconfig', True):
            # "the module ignores any WRITE to ApplyImmediate registers" - a
            # silent no-op is the one outcome the operator cannot diagnose.
            return _err('This module does not support intervention-free hot '
                        'reconfiguration (Lower 02h), so ApplyImmediate is '
                        'ignored - use Apply', 400)
        if apply or apply_now:
            # Narrowing only when the change can be located. Pressing Apply on
            # an unchanged table is a request to re-commission, and quietly
            # doing nothing would take that away.
            need = _lanes_needing_apply(prev_app_select, app_select,
                                        host_lanes_by_app)
            if not need:
                need = set(range(_state['lanes']))
            # Section 6.2.4 names two ways an Apply is thrown away without a
            # word. Reporting which lanes were applied while the module
            # discarded the write is worse than refusing: the operator moves
            # on believing the Data Path is carrying the new configuration.
            hot = (_state.get('caps') or {}).get('config', {}).get(
                'hot_reconfig', False)

            def _named(idxs):
                return ', '.join('%d (%s)' % (i + 1, dp_states_before[i])
                                 for i in idxs)

            # Section 6.2.4.2: "When a previously triggered Provision or
            # Provision-and-Commission command is still being processed for
            # lanes of a Data Path, the module ignores new triggers for those
            # lanes", and "ignoring ... is not indicated to the host". Unlike
            # the transient-state rule below this holds whether or not
            # intervention-free reconfiguration is supported - Tables 6-3 and
            # 6-4 both start every procedure with ConfigStatus =
            # ConfigInProgress. A second Apply pressed before the first had
            # finished was reported as applied to every lane.
            busy = sorted(i for i in need
                          if i < len(config_before)
                          and config_before[i] == cmis.CONFIG_IN_PROGRESS)
            if busy:
                return _err(
                    'Lane %s still reports ConfigInProgress (11h:202-205), '
                    'and section 6.2.4.2 has the module ignore a new trigger '
                    'for those lanes without telling the host - this would '
                    'report success and change nothing. Wait for the '
                    'previous command to finish'
                    % ', '.join(str(i + 1) for i in busy), 409)
            if hot:
                stuck = sorted(i for i in need
                               if i < len(dp_states_before)
                               and cmis.dp_state_is_transient(
                                   dp_states_before[i]))
                if stuck:
                    return _err(
                        'The module silently ignores an Apply aimed at a Data '
                        'Path still in a transient state, so this would '
                        'report success and change nothing. Wait for lane %s '
                        'to settle' % _named(stuck), 409)
            if apply_now:
                unready = sorted(
                    i for i in need
                    if i < len(dp_states_before)
                    and not cmis.dp_state_takes_apply_immediate(
                        dp_states_before[i]))
                if unready:
                    return _err(
                        'ApplyImmediate is ignored outside DPInitialized and '
                        'DPActivated, so this would report success and change '
                        'nothing. Lane %s is not in either; use Apply to '
                        'bring the Data Path up' % _named(unready), 409)

        for bank in range(banks):
            _set_page(0x10, bank)
            # The Staged Control Set goes down before DPDeinit, not after.
            # Releasing a deinit hold restarts the Data Path, and the module
            # commissions whatever is staged at that moment - so writing 128
            # first brought the path back up on the *previous* Application and
            # reported ConfigSuccess for it. That is the second half of the
            # only sequence 6.2.4.3 allows for a width change, so the one
            # procedure the standard mandates was the one that did not work.
            # 129-130 are contiguous: InputPolarityFlipTx then OutputDisableTx
            _bus_write(cmis.REG_TX_POL_FLIP[1],
                                          bytes([tx_pol[bank], tx_disable[bank]]))
            _bus_write(cmis.REG_RX_POL_FLIP[1],
                                          bytes([rx_pol[bank]]))
            _bus_write(
                cmis.REG_APP_SELECT[1],
                cmis.pack_appselect(app_select[bank * 8:bank * 8 + 8]))
            _bus_write(cmis.REG_DP_DEINIT[1],
                                          bytes([dp_deinit[bank]]))

        if apply or apply_now:
            if need:
                for bank in range(banks):
                    mask = 0
                    for lane in need:
                        if bank * 8 <= lane < bank * 8 + 8:
                            mask |= 1 << (lane - bank * 8)
                    if not mask:
                        continue
                    _set_page(0x10, bank)
                    trigger = (cmis.REG_APPLY_IMM if apply_now
                               else cmis.REG_APPLY_DATAPATH)
                    _bus_write(trigger[1], bytes([mask]))
                applied = sorted(l + 1 for l in need)
                # A settle before returning, not a wait this reply depends on:
                # nothing is read back here, and the page fetches ConfigStatus
                # on its next refresh. Chapter 10 has no parameter for
                # Apply-to-ConfigStatus - Table 10-5's own note says timings
                # "may be added" for effects that depend on hardware
                # reconfiguration - so there is no number to be short of.
                time.sleep(0.1)

        return _ok({'message': 'DataPath configuration written',
                    'applied_lanes': applied,
                    'apply_immediate': bool(apply_now)})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/flags', methods=['GET'])
def api_module_flags():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Flags')
    if err:
        return err
    try:
        # 11h:134-153 are contiguous lane flag bytes; one burst read beats 20
        # page-select + 5 ms settle cycles on real hardware. It starts one byte
        # earlier than the alarm block so DPStateChangedFlag comes along for
        # nothing: it is the module's own record that a data path went down and
        # came back, which is the transient this tool exists to catch. It ends
        # one byte later for OutputStatusChangedFlagRx, which is the only
        # record that an Rx output was momentarily muted.
        first = cmis.REG_DP_STATE_CHANGED[1]
        blocks = [raw for _bank, raw in
                  _read_banks(cmis.REG_DP_STATE_CHANGED[0], first, 20)]
        # The Masks for the same twenty bytes, 10h:213-232, banked the same
        # way (Table 8-83). Same burst shape, and the byte at mask_first + k
        # masks the Flag at first + k.
        _fp, _ff, mask_page, mask_first, _n = cmis.FLAG_MASK_BLOCKS[1]
        mask_blocks = [raw for _bank, raw in
                       _read_banks(mask_page, mask_first, 20)]

        def flags(addr):
            # One bit per lane, so each bank contributes its own eight.
            out = []
            for blk in blocks:
                out += cmis.parse_lane_flags(blk[addr - first])
            return out[:_state['lanes']]

        def masked(addr):
            out = []
            for blk in mask_blocks:
                out += cmis.parse_lane_flags(blk[addr - first])
            return out[:_state['lanes']]

        dp_changed = flags(cmis.REG_DP_STATE_CHANGED[1])

        tx_fault  = flags(0x87)
        tx_los    = flags(0x88)
        tx_cdrlol = flags(0x89)
        # 11h:138 (Table 8-96), advertised in 01h:157.3. It sits inside the
        # burst above, so the read that fetched the other nineteen bytes
        # cleared this one too - dropping it did not leave it for the next
        # reader, it destroyed it. The advertisement was already being
        # published, so the reply claimed the module supports a Flag that
        # appeared on no lane.
        tx_aeq_fail = flags(cmis.REG_TX_AEQ_FAIL[1])
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
        rx_out_ch = flags(cmis.REG_RX_OUTPUT_CHANGED[1])

        # A Flag the module does not implement reads 0, the same as a healthy
        # lane. Say which ones mean anything rather than colouring them green.
        supported_flags = dict(
            ((_state.get('caps') or {}).get('flags_supported') or {}))
        # The threshold Flags are not in Table 8-52 at all; they exist only
        # where their monitor does. Without this the panel drew a green dot
        # against the Tx bias of a module that had said it does not measure
        # bias - the same "nothing wrong here" the readings used to claim.
        for flag, monitor in _THRESHOLD_FLAG_MONITOR.items():
            supported_flags[flag] = _monitor_present(monitor)

        # Which Mask byte belongs to each Flag the rows above report.
        # Kept as one table rather than repeating the addresses beside the
        # Flag reads: two lists of twenty addresses is how a Mask comes to be
        # paired with the wrong Flag. A test checks it covers every key.
        mask_of = {
            'dp_state_changed':    cmis.REG_DP_STATE_CHANGED[1],
            'tx_fault':            0x87,
            'tx_los':              0x88,
            'tx_cdr_lol':          0x89,
            'tx_adaptive_eq_fail': cmis.REG_TX_AEQ_FAIL[1],
            'tx_power_high_alarm': 0x8B,
            'tx_power_low_alarm':  0x8C,
            'tx_power_high_warn':  0x8D,
            'tx_power_low_warn':   0x8E,
            'tx_bias_high_alarm':  0x8F,
            'tx_bias_low_alarm':   0x90,
            'tx_bias_high_warn':   0x91,
            'tx_bias_low_warn':    0x92,
            'rx_los':              0x93,
            'rx_cdr_lol':          0x94,
            'rx_power_high_alarm': 0x95,
            'rx_power_low_alarm':  0x96,
            'rx_power_high_warn':  0x97,
            'rx_power_low_warn':   0x98,
            'rx_output_changed':   cmis.REG_RX_OUTPUT_CHANGED[1],
        }
        masked_by_flag = {k: masked(a) for k, a in mask_of.items()}

        lanes = []
        masks = []
        history = _state['flag_history']
        for i in range(_state['lanes']):
            lanes.append({
                'lane': i + 1,
                'dp_state_changed':   dp_changed[i],
                'tx_fault':           tx_fault[i],
                'tx_los':             tx_los[i],
                'tx_cdr_lol':         tx_cdrlol[i],
                'tx_adaptive_eq_fail': tx_aeq_fail[i],
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
                'rx_output_changed':   rx_out_ch[i],
            })
            # 11h:134-153 mixes the two sides of the module. Tables 8-96 to
            # 8-98 say which row is which, and the Tx and Rx in the names do
            # not: FailureFlagTx is "affecting media lane <i>" while LOSFlagTx
            # is "host lane <i>", and OutputStatusChangedFlagRx is a host lane
            # while every other Rx Flag is a media lane. Fifteen of the twenty
            # are about a media lane.
            #
            # A module whose media lanes are fewer than its host lanes has no
            # such lane to raise them on, and the register reads 0 there - so
            # the panel drew fifteen "checked, nothing wrong" marks per row on
            # lanes that are not there, including an Rx LOS saying a signal
            # was present on a fibre the module does not have.
            # Not published as a field of its own here: every other boolean
            # in this dict is a Flag that is set, and a caller collecting the
            # raised ones has no reason to expect an exception. The nulls
            # carry the same fact, and /api/module/monitoring states it
            # outright for anyone who wants it as a value.
            if not _media_lane_present(i + 1):
                for _key, _side in cmis.LANE_FLAG_SIDE.items():
                    if _side == 'media':
                        lanes[-1][_key] = None
            # Fold this read into what has been seen. The read just cleared
            # these bits on the module, so if this is not kept the event is
            # gone the moment the reply is rendered.
            masks.append(dict(
                {'lane': i + 1},
                **{k: v[i] for k, v in masked_by_flag.items()}))
            seen = history.setdefault(i + 1, set())
            for name, value in list(lanes[-1].items()):
                if name != 'lane' and value:
                    seen.add(name)
            lanes[-1]['seen'] = sorted(seen)
        if _state['flag_history_since'] is None:
            _state['flag_history_since'] = time.time()
        return _ok({'lanes': lanes,
                    # 10h:213-232 (Table 8-83). A Flag whose Mask is set is
                    # one the module will not assert the Interrupt line for -
                    # the panel showed the Flag and could not say the alarm
                    # behind it had been turned off.
                    'masks': masks,
                    'supported': supported_flags,
                    'history_since': _state['flag_history_since']})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/flags/clear', methods=['POST'])
def api_clear_flag_history():
    """Forget what has fired so far, and start counting from now.

    Nothing is written to the module: these are the tool's own notes on Flags
    the module already handed over and cleared. The operator who has read them
    is the only one who can say they are dealt with.
    """
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Flags')
    if err:
        return err
    _state['flag_history'] = {}
    _state['flag_history_since'] = time.time()
    return _ok({'history_since': _state['flag_history_since']})


@app.route('/api/module/thresholds', methods=['GET'])
def api_module_thresholds():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Alarm and warning thresholds')
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

        # The thresholds count the same scaled increments as the monitor.
        bias_scale = ((_state.get('caps') or {}).get('monitors')
                      or {}).get('tx_bias_scale', 1)
        txbias_ha = cmis.parse_tx_bias_ma(rd(0xB8), bias_scale)
        txbias_la = cmis.parse_tx_bias_ma(rd(0xBA), bias_scale)
        txbias_hw = cmis.parse_tx_bias_ma(rd(0xBC), bias_scale)
        txbias_lw = cmis.parse_tx_bias_ma(rd(0xBE), bias_scale)

        rxpwr_ha_uw = cmis.parse_power_uw(rd(0xC0))
        rxpwr_la_uw = cmis.parse_power_uw(rd(0xC2))
        rxpwr_hw_uw = cmis.parse_power_uw(rd(0xC4))
        rxpwr_lw_uw = cmis.parse_power_uw(rd(0xC6))

        # 144-175: the Aux monitors' own thresholds, decoded as whatever
        # 01h:145 says each monitor observes.
        _obs = _state['caps'].get('aux') or {}
        _mons = _state['caps'].get('monitors') or {}
        aux_thresholds = cmis.parse_aux_thresholds(
            _read_upper(*cmis.REG_AUX_THRESHOLDS), _obs)
        # Same gate the readings use: a monitor the module does not have has
        # no thresholds worth showing either.
        if _obs:
            aux_thresholds = {k: v for k, v in aux_thresholds.items()
                              if _mons.get(k, False)}
        return _ok({
            'aux_thresholds': aux_thresholds,
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
    err = _require_paged('Squelch controls')
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
            # The two Tx rows are per media lane (Table 8-79), the two Rx
            # rows per host lane; the page greys the Tx boxes of a media lane
            # the module does not have.
            'media_lanes_present': _media_lanes_present(),
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/squelch', methods=['POST'])
def api_squelch_set():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Squelch controls')
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        # These read back per bank, so they have to be written per bank too:
        # a 16-lane module was reading sixteen lanes of squelch state and
        # writing only the first eight, and the page's per-bank list arrived
        # here as a list where an int was expected.
        banks = (_state['lanes'] + 7) // 8
        bad = _reject_unknown(body, ('tx_squelch_disable', 'tx_squelch_force',
                                     'rx_output_disable', 'rx_squelch_disable'))
        if bad:
            return bad
        # Read first: a caller setting one control must not clear the others.
        cur_sq, cur_sf, cur_od, cur_rq = [], [], [], []
        for _b, raw in _read_banks(0x10, cmis.REG_TX_SQUELCH_DIS[1], 2):
            cur_sq.append(raw[0]); cur_sf.append(raw[1])
        for _b, raw in _read_banks(0x10, cmis.REG_RX_OUTPUT_DIS[1], 2):
            cur_od.append(raw[0]); cur_rq.append(raw[1])
        tx_sq = _keep(body, 'tx_squelch_disable', cur_sq, banks)
        tx_sf = _keep(body, 'tx_squelch_force',   cur_sf, banks)
        rx_od = _keep(body, 'rx_output_disable',  cur_od, banks)
        rx_sq = _keep(body, 'rx_squelch_disable', cur_rq, banks)

        refused = _refuse_unsupported((
            ('auto_squelch_disable_tx', tx_sq),
            ('forced_squelch_tx', tx_sf),
            ('output_disable_rx', rx_od),
            ('auto_squelch_disable_rx', rx_sq),
        )) or _refuse_absent_media_lanes((
            ('AutoSquelchDisableTx', tx_sq, cur_sq),
            ('OutputSquelchForceTx', tx_sf, cur_sf),
        ))
        if refused:
            return refused

        bad = _refuse_broadcast_divergence({
            'tx_squelch_disable': tx_sq, 'tx_squelch_force': tx_sf,
            'rx_output_disable': rx_od, 'rx_squelch_disable': rx_sq,
        })
        if bad:
            return bad

        for b in range(banks):
            _set_page(0x10, b)
            _bus_write(cmis.REG_TX_SQUELCH_DIS[1],
                                          bytes([tx_sq[b], tx_sf[b]]))
            _bus_write(cmis.REG_RX_OUTPUT_DIS[1],
                                          bytes([rx_od[b], rx_sq[b]]))
        return _ok({'message': 'Squelch/output controls written'})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/loopback', methods=['GET'])
def api_loopback_get():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Loopback')
    if err:
        return err
    err = _require_diagnostics('Loopback')
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
            'capabilities': _diag_caps()['loopback'],
            # The media rows loop media lanes (Figure 8-2); greyed on the page
            # for a media lane this module does not have.
            'media_lanes_present': _media_lanes_present(),
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
    err = _require_paged('Loopback')
    if err:
        return err
    err = _require_diagnostics('Loopback')
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        banks = (_state['lanes'] + 7) // 8
        bad = _reject_unknown(body, ('media_side_output', 'media_side_input',
                                     'host_side_output', 'host_side_input'))
        if bad:
            return bad
        _blocks = [raw for _b, raw in _read_banks(
            cmis.REG_MEDIA_OUT_LB[0], cmis.REG_MEDIA_OUT_LB[1], 4)]
        cur = [[blk[i] for blk in _blocks] for i in range(4)]
        media_out = _keep(body, 'media_side_output', cur[0], banks)
        media_in  = _keep(body, 'media_side_input',  cur[1], banks)
        host_out  = _keep(body, 'host_side_output',  cur[2], banks)
        host_in   = _keep(body, 'host_side_input',   cur[3], banks)

        caps = _diag_caps()['loopback']
        requested = (('media_side_output', media_out), ('media_side_input', media_in),
                     ('host_side_output', host_out), ('host_side_input', host_in))
        for name, masks in requested:
            if any(masks) and not caps[name]:
                return _err('This module does not support %s loopback '
                            '(13h:128 bit %d is clear)'
                            % (name.replace('_', ' '),
                               ('media_side_output', 'media_side_input',
                                'host_side_output', 'host_side_input').index(name)),
                            400)
        # The media side loops media lanes (Figure 8-2: "only one media lane
        # shown"), and a coherent module has one. Per lane only: without
        # per-lane control any bit means every lane, and that is handled below.
        if caps['per_lane_media']:
            refused = _refuse_absent_media_lanes((
                ('MediaSideOutputLoopbackEnable', media_out, cur[0]),
                ('MediaSideInputLoopbackEnable', media_in, cur[1]),
            ), 'Table 8-131')
            if refused:
                return refused
        # Table 8-131 spells out what a module without per-lane loopback
        # does: "If the Per-lane ... Loopback Supported field=1, loopback
        # control is per lane. Otherwise, if any loopback enable bit is set to
        # 1, all ... lanes are in ... loopback."
        #
        # So one lane requested on such a module is not an error - the module
        # loops all of them back. Refusing it invented a restriction the
        # module does not have, and left the operator unable to ask for
        # loopback at all without first working out that only an all-lanes
        # mask would be accepted.
        #
        # The mask is widened to what the module will actually do rather than
        # written through as sent: the register is read back into this panel,
        # and a byte reading 0x01 beside eight lanes in loopback would be the
        # tool reporting one lane looped when all eight are.
        all_lanes = (1 << min(_state['lanes'], 8)) - 1
        widened = []
        for side, names in (('media', ('media_side_output', 'media_side_input')),
                            ('host', ('host_side_output', 'host_side_input'))):
            if caps['per_lane_%s' % side]:
                continue
            for name, masks in requested:
                if name not in names or not any(masks):
                    continue
                for b, m in enumerate(masks):
                    if m not in (0, all_lanes):
                        masks[b] = all_lanes
                        if name not in widened:
                            widened.append(name)
        if not caps['simultaneous_host_and_media'] and \
                any(any(m) for n, m in requested if n.startswith('media')) and \
                any(any(m) for n, m in requested if n.startswith('host')):
            return _err('This module cannot hold a host side and a media side '
                        'loopback at the same time (13h:128 bit 6 is clear)', 400)
        # Page 13h is lane-banked like 10h, so bank broadcast turns these four
        # bytes per bank into four bytes for every bank, the last one winning.
        bad = _refuse_broadcast_divergence({
            'media_side_output': media_out, 'media_side_input': media_in,
            'host_side_output': host_out, 'host_side_input': host_in,
        })
        if bad:
            return bad

        for b in range(banks):
            _set_page(0x13, b)
            _bus_write(cmis.REG_MEDIA_OUT_LB[1], bytes([media_out[b], media_in[b],
                                                       host_out[b], host_in[b]]))
        out = {'message': 'Loopback configuration written'}
        if widened:
            out['message'] = ('Loopback configuration written; %s applied to '
                              'every lane' % ', '.join(
                                  n.replace('_', ' ') for n in widened))
            out['widened_to_all_lanes'] = widened
        return _ok(out)
    except Exception as e:
        return _err(str(e), 500)


# 01h:155-156 (Table 8-51). Read once at connect; a control the module says
# it does not implement writes a register it ignores, and the panel that
# offered it has told the operator something that is not true.
_CONTROL_NAMES = {
    'auto_squelch_disable_tx': ('automatic Tx squelching cannot be disabled', '155.2'),
    'forced_squelch_tx':       ('Tx outputs cannot be force-squelched', '155.3'),
    'output_disable_tx':       ('Tx outputs cannot be disabled', '155.1'),
    'input_polarity_flip_tx':  ('Tx input polarity cannot be flipped', '155.0'),
    'auto_squelch_disable_rx': ('automatic Rx squelching cannot be disabled', '156.2'),
    'output_disable_rx':       ('Rx outputs cannot be disabled', '156.1'),
    'output_polarity_flip_rx': ('Rx output polarity cannot be flipped', '156.0'),
}


def _refuse_unsupported(requested):
    """`requested` is (control name, per-bank masks). Returns an error response
    for the first control the module does not advertise, else None."""
    controls = (_state.get('caps') or {}).get('controls') or {}
    if not controls:
        return None            # a module that answered nothing gets no gate
    for name, masks in requested:
        if any(masks) and controls.get(name) is False:
            why, bit = _CONTROL_NAMES[name]
            return _err('This module advertises that %s (01h:%s is clear)'
                        % (why, bit), 400)
    return None


def _refuse_absent_media_lanes(requested, table='Table 8-79'):
    """`requested` is (register name, requested masks, current masks) for a
    control indexed by *media* lane - OutputDisableTx, AutoSquelchDisableTx
    and OutputSquelchForceTx (Table 8-79), the media side loopbacks
    (Table 8-131) and the media side pattern generator and checker
    (Tables 8-121, 8-125). Returns an error for the first change aimed at a
    media lane this module does not have, else None.

    The panels lay these out in host-lane rows, so on a module with fewer
    media lanes than host lanes - a coherent one has one - most of the boxes
    control nothing, and ticking one is a write the module has no lane to
    act on. Only a change is refused: the page writes whole bytes, so an
    untouched bit arrives as whatever the module already holds.
    """
    present = _media_lanes_present()
    for name, masks, current in requested:
        for lane in range(min(8, len(present))):
            if present[lane]:
                continue
            bit = 1 << lane
            if (masks[0] ^ current[0]) & bit:
                return _err('%s is set per media lane (%s), and this '
                            'module has no media lane %d (00h:210) - the '
                            'change would report success and do nothing'
                            % (name, table, lane + 1), 400)
    return None


# The five per-engine fields of a PRBS section, in the order they sit in the
# 8-byte block (13h:144+): three masks, the FEC location mask, then the
# per-lane patterns.
_PRBS_FIELDS = ('enable_mask', 'invert_mask', 'byte_swap_mask', 'fec_mask',
                'patterns')

# What one entry of /api/module/laser's `lanes` list may carry.
_LASER_LANE_FIELDS = ('lane', 'grid_code', 'channel', 'fine_offset_ghz',
                      'fine_tuning_enabled', 'target_power_dbm')


def _reject_unknown(body: dict, allowed, where: str = '') -> object:
    """Refuse a request body carrying a field this handler does not know.

    Silently ignoring an unrecognised name is how a typo in a script reports
    success and changes nothing - or worse, on the mask endpoints, clears
    every control the caller did not happen to spell correctly.
    """
    unknown = sorted(k for k in body if k not in allowed)
    if unknown:
        return _err('Unknown field%s %s; %sthis endpoint accepts %s'
                    % ('' if len(unknown) == 1 else 's',
                       ', '.join(repr(u) for u in unknown), where,
                       ', '.join(sorted(allowed))), 400)
    return None


def _keep(body: dict, key: str, current, banks: int):
    """A mask the caller did not mention keeps the value it already has.

    Byte 0x1A learned this the hard way - rebuilding it from scratch cleared
    the controls nobody had touched - and the lesson never reached the mask
    endpoints, where omitting a field silently zeroed it.
    """
    if key in body:
        return _masks_per_bank(body[key], banks)
    return list(current)


def _diag_caps() -> dict:
    """13h:128-142. What the module says it can do, which is the only thing
    that makes an option worth offering."""
    raw = _read_upper(*cmis.REG_DIAG_CAPS)
    return {
        'loopback': cmis.parse_loopback_caps(raw[0]),
        'measurement': cmis.parse_diag_meas_caps(raw[1]),
        'reporting': cmis.parse_diag_reporting_caps(raw[2]),
        # 131 is what 144/152/160/168 point at for their own advertisement:
        # an engine with both bits clear is not in the module, and one with a
        # single bit set has no choice of FEC location left to offer.
        'pattern_locations': cmis.parse_pattern_locations(raw[3]),
        'patterns': cmis.parse_pattern_caps(raw[4:12]),
        # 140 is how long a user-defined pattern this module will take. The
        # dropdown has always offered Pattern ID 15 wherever it was
        # advertised, with nowhere to say what the pattern is.
        'user_pattern_max_bytes': cmis.user_pattern_max_bytes(raw[12]),
        # 141-142 were read with the rest and thrown away, which left the
        # pattern tables offering DataInvert and SwapSymbolBits columns on
        # modules without those bytes, and eight independent rows on modules
        # where one enable covers the bank and lane 1's pattern covers them
        # all.
        'pattern_controls': cmis.parse_pattern_control_caps(raw[13], raw[14]),
    }


def _measurement_window() -> dict:
    """What window the free-running error statistics actually cover.

    13h:129 is RO and Required and was parsed and dropped; 13h:177 was never
    read. Between them they say whether the module is gating at all, over
    what period, and whether these numbers move while a measurement is still
    running - none of which a table of BERs conveys on its own.
    """
    caps = _diag_caps()['measurement']
    raw = _read_upper(*cmis.REG_CLOCK_MEAS)
    controls = cmis.parse_measurement_controls(raw[1])
    return {
        'capabilities': caps,
        'controls': controls,
        'start_stop_scope': _start_stop_scope(caps, controls),
    }


def _start_stop_scope(caps: dict, controls: dict):
    """Where 13h:177.7 StartStopIsGlobal sends a start or a stop.

    Table 8-127: set, a start/stop control written in one Bank - the reset
    at 177.5 and the checker enables at 160 and 168 - acts "across all Banks
    as if the same control value change had occurred in all supported
    Banks". Table 8-129 exempts one case, a gated measurement on the single
    global timer (13h:129.3 = 0): "the control 13h:177.7 is ignored". That
    is the only exemption; ungated, Table 8-128 gives 177.7 = 1 a row of its
    own - a reset reaching "all lanes in all Banks" - whatever 129.3 says.

    None where the bit is clear or there is one Bank, which leaves nowhere
    else for a start or stop to go.
    """
    if (_state['lanes'] + 7) // 8 < 2 or not controls.get('start_stop_is_global'):
        return None
    gated = caps.get('gating_support', 0) != 0 and controls.get('gated')
    if gated and caps.get('per_lane_gating_timers') is False:
        return 'ignored'
    return 'all_banks'


def _user_pattern() -> dict:
    """Pattern ID 15 sends whatever is in 13h:224-255 (Table 8-134).

    Only worth reading where some engine advertises ID 15: on a module that
    does not, these bytes are not a pattern anyone can select, and showing
    them would be inventing a control.

    Page 13h is banked, but the user pattern is one definition rather than a
    per-lane control, so bank 0 is what is shown and every bank is written -
    a module that keeps one copy sees the same value written twice.
    """
    caps = _diag_caps()
    # Panel order rather than alphabetical: the operator reads this against
    # the four tables above it.
    available = [role for role in ('host_gen', 'media_gen',
                                   'host_chk', 'media_chk')
                 if 15 in caps['patterns'][role]]
    if not available:
        return {'available': [], 'max_bytes': caps['user_pattern_max_bytes'],
                'pattern': []}
    raw = _read_upper(*cmis.REG_USER_PATTERN)
    return {
        'available': available,
        'max_bytes': caps['user_pattern_max_bytes'],
        'pattern': list(raw[:caps['user_pattern_max_bytes']]),
    }


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
    err = _require_paged('Pattern generation and checking')
    if err:
        return err
    err = _require_diagnostics('Pattern generation and checking')
    if err:
        return err
    try:
        banks = (_state['lanes'] + 7) // 8
        # Table 8-138. The checker pair says whether the far end has locked
        # onto the pattern; the generator pair says whether this module is
        # actually sending one. Reading only the first showed a lane as
        # generating PRBS31 while the module reported it had lost lock, and
        # the errors that followed looked like a link fault rather than a
        # source that was never transmitting properly.
        try:
            # One bit per lane, so one byte per bank of eight. Reading only
            # bank 0 gave every lane past the eighth the flag belonging to
            # the lane eight below it - lane 16's loss of lock was invisible
            # and lane 9 showed lane 1's.
            def _flag_banks(reg):
                return [raw[0] for _b, raw in _read_banks(*reg)]
            host_lol_banks = _flag_banks(cmis.REG_HOST_PRBS_LOL)
            media_lol_banks = _flag_banks(cmis.REG_MEDIA_PRBS_LOL)
            host_gen_lol_banks = _flag_banks(cmis.REG_HOST_GEN_LOL)
            media_gen_lol_banks = _flag_banks(cmis.REG_MEDIA_GEN_LOL)
            host_gate_banks = _flag_banks(cmis.REG_HOST_GATE_DONE)
            media_gate_banks = _flag_banks(cmis.REG_MEDIA_GATE_DONE)
            # 132.7, module-wide rather than per lane.
            ref_clock_lost = bool(_read_upper(*cmis.REG_REF_CLOCK_LOL)[0] & 0x80)
            # 13h:206-213, the Masks for every Flag above. 8.1.4.2: "While a
            # Flag is set, an Interrupt request is generated unless an
            # associated Mask bit is set" - and Table 8-133 states the
            # default in one line, "The default value for all Mask bits on
            # this page is 1 (masked)". So on a module nobody has configured,
            # a checker that loses lock raises a Flag the host is never told
            # about, and this panel could not say so.
            diag_mask_banks = [raw for _b, raw in
                               _read_banks(*cmis.REG_DIAG_FLAG_MASKS)]
            # Whether that matters here is a question about 13h:176 and 178:
            # a generator on the internal clock and a checker on a recovered
            # clock do not stop working because the reference clock went away.
            clk = _read_upper(*cmis.REG_CLOCK_MEAS)
            clock_sources = cmis.parse_clock_sources(clk[0], clk[2])
            # The checker enables below are start/stop controls in the sense
            # of Table 8-127, so 177.7 decides whether ticking one in this
            # Bank starts the same lane in every other.
            start_stop_scope = _start_stop_scope(
                _diag_caps()['measurement'],
                cmis.parse_measurement_controls(clk[1]))
        except Exception:
            _z = [0] * banks
            host_lol_banks = media_lol_banks = list(_z)
            host_gen_lol_banks = media_gen_lol_banks = list(_z)
            host_gate_banks = media_gate_banks = list(_z)
            diag_mask_banks = []
            ref_clock_lost = False
            clock_sources = {}
            start_stop_scope = None
        # Latched and cleared by the read that just happened, so a checker that
        # slipped for a moment mid-run leaves nothing behind unless this does.
        history = _state['flag_history']
        for masks, name in ((host_lol_banks, 'host_prbs_lol'),
                            (media_lol_banks, 'media_prbs_lol'),
                            (host_gen_lol_banks, 'host_gen_lol'),
                            (media_gen_lol_banks, 'media_gen_lol')):
            for bank, mask in enumerate(masks):
                for bit in range(8):
                    if (mask >> bit) & 1:
                        # The absolute lane, not the bit: keying by the bit
                        # alone filed bank 1's lanes under lanes 1-8 and lost
                        # which lane had actually slipped.
                        history.setdefault(bank * 8 + bit + 1,
                                           set()).add(name)
        # 14h:132.7 is RO/COR like the four above it, and it is the one that
        # was read and thrown away. The note it drives says what the checkers
        # and generators produce "cannot be relied on until it returns" - and
        # that stays true after the read that cleared the Flag, so showing it
        # for a single poll and then dropping it tells the operator the
        # reference came back when nothing said so. It is module-wide rather
        # than per lane, so it is remembered under its own key.
        if ref_clock_lost:
            history.setdefault('module', set()).add('reference_clock_lost')
        ref_clock_seen = 'reference_clock_lost' in history.get('module', ())
        if _state['flag_history_since'] is None:
            _state['flag_history_since'] = time.time()

        present = _media_lanes_present()

        def lol_seen(name):
            # The media side's are per media lane (Table 8-138, "Latched
            # per-media lane ..."): a lane the module does not have has no
            # history, and False would read as "never lost".
            media = name.startswith('media')
            return [None if media and not present[lane]
                    else bool(name in history.get(lane + 1, ()))
                    for lane in range(_state['lanes'])]

        def diag_masked(flag_addr):
            """One bit per lane, from the Mask byte that governs this Flag.

            Per lane because the Masks are: a host that cares about one lane's
            checker clears one bit, and reporting the byte would say the
            others were watched too.
            """
            if not diag_mask_banks:
                return [False] * _state['lanes']
            addr = cmis.diag_mask_addr(flag_addr)
            off = addr - cmis.REG_DIAG_FLAG_MASKS[1]
            out = []
            for blk in diag_mask_banks:
                out += cmis.parse_lane_flags(blk[off] if off < len(blk) else 0)
            return out[:_state['lanes']]

        # 14h:132.7 is module-wide, so its Mask is one bit rather than a lane
        # map: 13h:206.7 (Table 8-133).
        ref_clock_masked = bool(
            diag_mask_banks and (diag_mask_banks[0][0] & 0x80))

        return _ok({
            'pattern_capabilities': _diag_caps()['patterns'],
            # The media side engines run per media lane; the page greys the
            # rows of a media lane the module does not have.
            'media_lanes_present': present,
            # Table 8-115. The page used to carry its own list of names and it
            # stopped at ID 12, so a module advertising Custom or User Pattern
            # had them dropped from the dropdown without a word. Two lists
            # that have to agree, kept in two places.
            'pattern_names': {str(k): v for k, v in cmis.PATTERN_NAMES.items()},
            'pattern_controls': _diag_caps()['pattern_controls'],
            'pattern_locations': _diag_caps()['pattern_locations'],
            'user_pattern': _user_pattern(),
            'host_gen':  _read_prbs_block(0x90),
            'media_gen': _read_prbs_block(0x98),
            'host_chk':  _read_prbs_block(0xA0),
            'media_chk': _read_prbs_block(0xA8),
            # Bank 0 stays under the original keys because everything
            # written before banks existed reads them; the per-bank arrays
            # are what a module wider than eight lanes needs.
            'host_chk_lol_mask':  host_lol_banks[0],
            'media_chk_lol_mask': media_lol_banks[0],
            'host_chk_lol_mask_banks':  host_lol_banks,
            'media_chk_lol_mask_banks': media_lol_banks,
            'host_chk_lol_seen':  lol_seen('host_prbs_lol'),
            'media_chk_lol_seen': lol_seen('media_prbs_lol'),
            # Which of these the module will actually interrupt about. Read
            # alongside the Flags rather than cached at connect, because a
            # Mask is RW and a host may have cleared one since.
            'host_chk_lol_masked':  diag_masked(cmis.REG_HOST_PRBS_LOL[1]),
            'media_chk_lol_masked': diag_masked(cmis.REG_MEDIA_PRBS_LOL[1]),
            'host_gen_lol_masked':  diag_masked(cmis.REG_HOST_GEN_LOL[1]),
            'media_gen_lol_masked': diag_masked(cmis.REG_MEDIA_GEN_LOL[1]),
            'host_gate_done_masked':  diag_masked(cmis.REG_HOST_GATE_DONE[1]),
            'media_gate_done_masked': diag_masked(cmis.REG_MEDIA_GATE_DONE[1]),
            'reference_clock_masked': ref_clock_masked,
            'host_gen_lol_mask':  host_gen_lol_banks[0],
            'media_gen_lol_mask': media_gen_lol_banks[0],
            'host_gen_lol_mask_banks':  host_gen_lol_banks,
            'media_gen_lol_mask_banks': media_gen_lol_banks,
            'host_gen_lol_seen':  lol_seen('host_gen_lol'),
            'media_gen_lol_seen': lol_seen('media_gen_lol'),
            # Latched when a gated measurement finishes, so a gated result
            # read without it may be the previous period's.
            'host_gate_done_mask':  host_gate_banks[0],
            'media_gate_done_mask': media_gate_banks[0],
            'host_gate_done_mask_banks':  host_gate_banks,
            'media_gate_done_mask_banks': media_gate_banks,
            # 132.7 is module-wide, but it only invalidates a pattern run
            # for the engines actually clocked from the reference clock.
            'reference_clock_lost': ref_clock_lost,
            'reference_clock_lost_seen': ref_clock_seen,
            'clock_sources': clock_sources,
            'start_stop_scope': start_stop_scope,
        })
    except Exception as e:
        return _err(str(e), 500)


def _write_user_pattern(values, caps, banks):
    """13h:224-255. Returns an error response, or None once written."""
    if not any(15 in ids for ids in caps['patterns'].values()):
        return _err('This module has no user-defined pattern: no generator '
                    'or checker advertises Pattern ID 15 in 13h:132-139', 400)
    try:
        data = [int(v) & 0xFF for v in values]
    except (TypeError, ValueError):
        return _err('The user pattern must be a list of byte values', 400)
    limit = caps['user_pattern_max_bytes']
    if len(data) > limit:
        return _err('This module takes at most %d bytes of user pattern '
                    '(13h:140.3-0), and %d were given'
                    % (limit, len(data)), 400)
    # Short of the limit the remaining bytes are left as they were rather
    # than zero-filled: the module repeats the pattern it was given, and a
    # trailing run of zeros is a different pattern.
    for bank in range(banks):
        _set_page(0x13, bank)
        _write_chunked(cmis.REG_USER_PATTERN[1], bytes(data))
    return None


@app.route('/api/module/prbs', methods=['POST'])
def api_prbs_set():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Pattern generation and checking')
    if err:
        return err
    err = _require_diagnostics('Pattern generation and checking')
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        bad = _reject_unknown(body, ('host_gen', 'media_gen', 'host_chk',
                                     'media_chk', 'user_pattern'))
        if bad:
            return bad
        for _key in ('host_gen', 'media_gen', 'host_chk', 'media_chk'):
            bad = _reject_unknown(body.get(_key) or {}, _PRBS_FIELDS,
                                  where='in %s, ' % _key)
            if bad:
                return bad
        banks = (_state['lanes'] + 7) // 8
        caps = _diag_caps()
        pattern_caps = caps['patterns']
        locations = caps['pattern_locations']
        # Every write is worked out and checked before any of it is sent, the
        # way the laser endpoint already does it. This loop used to validate
        # and write one engine at a time, so a request naming four engines
        # with the fourth invalid left the first three reconfigured and
        # answered 400 - and a caller who reads an error reasonably concludes
        # that nothing moved.
        plan = []
        for key, base_addr in [
            ('host_gen',  0x90),
            ('media_gen', 0x98),
            ('host_chk',  0xA0),
            ('media_chk', 0xA8),
        ]:
            section = body.get(key, {})
            if not section:
                continue
            # The same contract the other four write endpoints keep: a field
            # the caller did not name keeps the value it has. Defaulting to
            # zero meant a request that set only the enable mask also
            # reprogrammed every lane to pattern 0 (PRBS31Q) and cleared the
            # invert, byte-swap and FEC masks - and answered "ok".
            current = _read_prbs_block(base_addr)
            supported = pattern_caps[key]
            # 13h:131 is the advertisement the Enable byte itself points at.
            # An engine with both bits clear is not in the module, so enabling
            # it writes a byte nothing reads.
            loc = locations[key]
            role = '%s side pattern %s' % (
                key.split('_')[0], 'generator' if key.endswith('_gen')
                else 'checker')
            enabled = _keep(section, 'enable_mask',
                            current['enable_mask_banks'], banks)
            fec_req = _keep(section, 'fec_mask',
                            current['fec_mask_banks'], banks)
            if not loc['present'] and any(enabled):
                return _err(
                    'This module has no %s: 13h:131 bits %s are both clear, '
                    'and 13h:%d names them as the advertisement for its own '
                    'Enable byte' % (role, loc['bits'], base_addr),
                    400)
            # PreFECEnable set means the generator runs before the encoder;
            # PostFECEnable set means the checker runs after the decoder. The
            # cleared state of each is the other location, so both values name
            # an engine that has to exist.
            is_gen = key.endswith('_gen')
            for b in range(banks):
                for bit in range(8):
                    lane = b * 8 + bit
                    if lane >= _state['lanes'] or not (enabled[b] >> bit) & 1:
                        continue
                    on = bool((fec_req[b] >> bit) & 1)
                    wants_pre = on if is_gen else not on
                    if wants_pre and not loc['pre_fec']:
                        return _err(
                            'Lane %d: the %s on this module only exists after its '
                            'FEC (13h:131 bit %s is clear), so %sFECEnable '
                            'cannot ask for the pre-FEC location'
                            % (lane + 1, role, loc['bits'].split('-')[0],
                               'Pre' if is_gen else 'Post'),
                            400)
                    if not wants_pre and not loc['post_fec']:
                        return _err(
                            'Lane %d: the %s on this module only exists before its '
                            'FEC (13h:131 bit %s is clear), so %sFECEnable '
                            'cannot ask for the post-FEC location'
                            % (lane + 1, role, loc['bits'].split('-')[1],
                               'Pre' if is_gen else 'Post'),
                            400)
            for lane, pat in enumerate(section.get('patterns', []) or []):
                if int(pat) not in supported and any(enabled):
                    return _err(
                        'Lane %d: this module\'s %s does not support pattern '
                        '%d (%s). It advertises %s in 13h:%d-%d'
                        % (lane + 1, key.replace('_', ' '), int(pat),
                           cmis.PATTERN_NAMES.get(int(pat), 'unknown'),
                           ', '.join(cmis.PATTERN_NAMES.get(i, str(i))
                                     for i in supported) or 'none',
                           132 + 2 * ('host_gen', 'media_gen', 'host_chk',
                                      'media_chk').index(key),
                           133 + 2 * ('host_gen', 'media_gen', 'host_chk',
                                      'media_chk').index(key)),
                        400)
            # "individually toggle host (13h:160) or media (13h:168) lane
            # checker enable bits to restart error counting of specific host
            # or media lanes" (8.16.11.1): the media side engines are enabled per
            # media lane. Only the enable is judged - it is the bit that makes
            # something run, and the page sends a FEC location that has one
            # legal value for every lane at once.
            if key.startswith('media_'):
                refused = _refuse_absent_media_lanes(
                    (('The %s enable' % role, enabled,
                      current['enable_mask_banks']),),
                    'Table 8-121' if is_gen else 'Table 8-125')
                if refused:
                    return refused
            en  = enabled
            inv = _keep(section, 'invert_mask',
                        current['invert_mask_banks'], banks)
            sw  = _keep(section, 'byte_swap_mask',
                        current['byte_swap_mask_banks'], banks)
            fec = fec_req
            # Patterns are one flat list over all lanes; each bank takes its
            # own eight, so lanes 9-16 are not left on whatever was there.
            # A caller who names no patterns gets the ones already programmed,
            # not lane after lane of pattern 0.
            patterns = list(section.get('patterns') or current['patterns'])
            patterns += [0] * (banks * 8 - len(patterns))
            # Each bank gets its own eight-byte block, and under bank
            # broadcast every block lands in every bank - so the last bank's
            # lanes would be what all of them run. Decided here, with the
            # rest of the plan, so a refusal still writes nothing.
            bad = _refuse_broadcast_divergence({
                '%s.enable_mask' % key: en,
                '%s.invert_mask' % key: inv,
                '%s.byte_swap_mask' % key: sw,
                '%s.fec_mask' % key: fec,
                '%s.patterns' % key: [tuple(patterns[b * 8:b * 8 + 8])
                                      for b in range(banks)],
            })
            if bad:
                return bad
            for b in range(banks):
                block = (bytes([en[b], inv[b], sw[b], fec[b]])
                         + cmis.pack_prbs_patterns(patterns[b * 8:b * 8 + 8]))
                plan.append((b, base_addr, block))

        # The user pattern is part of the same request, so it waits for the
        # same all-or-nothing decision rather than going in while an engine
        # further down the body is still capable of refusing the whole thing.
        if body.get('user_pattern') is not None:
            err = _write_user_pattern(body['user_pattern'], caps, banks)
            if err:
                return err

        for bank, base_addr, block in plan:
            _set_page(0x13, bank)
            _bus_write(base_addr, block)
        return _ok({'message': 'PRBS configuration written'})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/snr', methods=['GET'])
def api_module_snr():
    """Read per-lane SNR using diagnostic selector 0x06."""
    err = _require_connected()
    if err:
        return err
    err = _require_paged('SNR reporting')
    if err:
        return err
    err = _require_diagnostics('SNR reporting')
    if err:
        return err
    try:
        # 13h:130.5 and .4 (Table 8-113) advertise the two sides separately.
        # A module that supports neither still answers a read of the selector
        # 06h window, so the numbers in it would look like a measurement.
        rep = _diag_caps()['reporting']
        if not (rep['host_side_snr'] or rep['media_side_snr']):
            return _ok({'host_snr_db': [], 'media_snr_db': [],
                        'supported': {'host': False, 'media': False}})
        # Selector 0x06: bytes 192-207 reserved, 208-223 host SNR, 240-255 media SNR
        # Host SNR at offset 16 (bytes 208-223), media SNR at offset 48.
        # Each bank carries its own eight lanes at the same offsets - and its
        # own selector, so the window is chosen inside each bank rather than
        # once in bank 0.
        host_snr = []
        media_snr = []
        # Table 7-8: 0 is SNR's NA value - no valid sample, not 0 dB.
        na_on = bool((_state.get('caps') or {}).get('na_values'))
        host_na, media_na = [], []
        for _bank, data in _read_diag_banks(0x06):
            for i in range(8):
                for out, na_out, off in ((host_snr, host_na, 16),
                                         (media_snr, media_na, 48)):
                    raw = data[off + i*2:off + 2 + i*2]
                    na = na_on and int.from_bytes(raw, 'little') == cmis.NA_SNR
                    na_out.append(na)
                    out.append(None if na else round(cmis.parse_snr_db(raw), 3))
        host_snr = host_snr[:_state['lanes']]
        present = _media_lanes_present()
        # Table 8-139 names these MediaSideSNRLane<i> - media lanes. On a
        # module with fewer of them than host lanes the rest are registers
        # for lanes that do not exist, and the numbers in them read as a
        # measurement. Monitoring and Flags learned this; Diagnostics had not.
        media_snr = [v if present[i] else None
                     for i, v in enumerate(media_snr[:_state['lanes']])]
        media_na = [v and present[i]
                    for i, v in enumerate(media_na[:_state['lanes']])]
        return _ok({
            'host_snr_db':  host_snr if rep['host_side_snr'] else [],
            'media_snr_db': media_snr if rep['media_side_snr'] else [],
            'host_snr_na': host_na[:_state['lanes']] if rep['host_side_snr'] else [],
            'media_snr_na': media_na if rep['media_side_snr'] else [],
            'media_lanes_present': present,
            'supported': {'host': rep['host_side_snr'],
                          'media': rep['media_side_snr']},
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/ber', methods=['GET'])
def api_module_ber():
    err = _require_connected()
    if err:
        return err
    err = _require_paged('BER reporting')
    if err:
        return err
    err = _require_diagnostics('BER reporting')
    if err:
        return err
    try:
        # 13h:130.0 advertises whether selector 01h means anything here.
        if not _diag_caps()['reporting']['bit_error_ratio']:
            return _ok({'lanes': [], 'supported': False})
        # Selector 0x01 = BER F16, written into each bank that is read: the
        # page is Banked and every bank keeps its own selector.
        # Host BER at 0xC0-0xCF, Media BER at 0xD0-0xDF (8 lanes x 2B each)
        lanes = []
        present = _media_lanes_present()
        # Table 7-8: 0.5 is Pattern BER's NA value - no valid sample, not a
        # link failing every other bit.
        na_on = bool((_state.get('caps') or {}).get('na_values'))
        for _bank, ber_raw in _read_diag_banks(0x01, 32):
            for i in range(8):
                lane = len(lanes) + 1
                host = cmis.parse_f16_ber(ber_raw[i*2:(i+1)*2])
                # MediaSideBERLane<i> (Table 8-139); None for a media
                # lane this module does not have (00h:210).
                media = (cmis.parse_f16_ber(ber_raw[16 + i*2:16 + (i+1)*2])
                         if lane <= len(present) and present[lane - 1]
                         else None)
                host_na = na_on and cmis.is_na_ber(host)
                media_na = (na_on and media is not None
                            and cmis.is_na_ber(media))
                lanes.append({
                    'lane': lane,
                    'host_ber': None if host_na else host,
                    'media_ber': None if media_na else media,
                    'host_ber_na': host_na,
                    'media_ber_na': media_na,
                })
        lanes = lanes[:_state['lanes']]
        return _ok({'lanes': lanes, 'supported': True,
                    'media_lanes_present': present,
                    'measurement': _measurement_window()})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/laser', methods=['GET'])
def api_laser_get():
    """Read laser tuning capabilities (Page 04h) and current state (Page 12h)."""
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Laser tuning')
    if err:
        return err
    try:
        # 01h:155.6 is what says Pages 04h and 12h exist. Selecting a page the
        # module does not implement is not an error it reports: 8.2.4 has it
        # clear the PageSelect byte and serve Upper Page 00h instead, so the
        # reads below quietly return the identifier, vendor name and part
        # number - and this handler decoded them as a grid bitmap and a
        # programmable power range. A non-tunable module came back advertising
        # five channel grids and a power range of 123.36 to -163.28 dBm.
        #
        # The write side has always refused on this bit, and the Page checksum
        # sweep already skips Page 04h for the same reason.
        if not (_state.get('caps') or {}).get('controls', {}).get(
                'transmitter_tunable'):
            return _ok({
                'tunable': False,
                'grids_supported': [],
                'grid_300ghz_supported': False,
                'grid_300ghz_range': None,
                'relative_power_thresholds_supported': False,
                'relative_power_thresholds': {},
                'fine_tuning_supported': False,
                'fine_resolution_ghz': None,
                'fine_range_ghz': None,
                'power_range_dbm': None,
                'grid_channel_ranges': {},
                'grid_names': {str(k): v for k, v in cmis.GRID_CODES.items()},
                'lanes': [],
            })

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

        grid_channel_ranges = cmis.parse_grid_channel_ranges(
            _read_grid_ranges(grid_300_supported))
        grid_300_range = grid_channel_ranges.get(9)
        # 04h:196.6 advertises the 5.4 power-relative supervision thresholds.
        rel_supported = bool((_read_upper(*cmis.REG_REL_THR_CAP)[0] >> 6) & 1)
        rel_thresholds = (cmis.parse_relative_thresholds(
                              _read_upper(*cmis.REG_REL_THRESHOLDS))
                          if rel_supported else {})

        # Current state (Page 12h), bank by bank
        grid_spacing = _read_banked(*cmis.REG_GRID_SPACING_TX[:2], 1)
        channel_num  = _read_banked(*cmis.REG_CHANNEL_NUM_TX[:2], 2)
        fine_offset  = _read_banked(*cmis.REG_FINE_OFFSET_TX[:2], 2)
        current_freq = _read_banked_scalars(*cmis.REG_CURRENT_FREQ_TX[:2], 4)
        target_pwr   = _read_banked(*cmis.REG_TARGET_PWR_TX[:2], 2)
        tuning_status= _read_banked(*cmis.REG_TUNING_STATUS_TX[:2], 1)
        tuning_flags = _read_banked(*cmis.REG_TUNING_FLAGS_TX[:2], 1)
        # One of the four Flag/Mask pairs in the specification, and one
        # this tool had not paired. 8.2.1 defines Interrupt in one sentence -
        # it "is asserted as long as any Flag is set with its associated Mask
        # cleared" - and Table 8-109 gives every bit of 12h:239-246
        # "Default: 1", so on a module out of reset none of these Flags
        # reaches the host at all. The diagnostics Masks on Page 13h ship the
        # same way (Table 8-133); the other two blocks state no default.
        tuning_masks = _read_banked(*cmis.REG_TUNING_FLAG_MASKS[:2], 1)
        # 12h:230. Defined as exact rather than advisory - bit <n>-1 is set
        # "if and only if" a Flag is set for that lane - and the note under
        # it is the host's own procedure: read this byte to find the lane,
        # then read that lane's Flag byte. The register had a name in this
        # tool and no reader.
        # One byte per bank, not per lane: eight lanes to a byte.
        tuning_summary = [raw[0] for _bank, raw in _read_banks(
            *cmis.REG_TUNING_FLAG_SUM)]

        grid_codes = cmis.GRID_CODES

        lanes = []
        for i in range(_state['lanes']):
            # 8.15: "Each Bank of Page 12h refers to 8 media lanes", and every
            # subject area in Table 8-108 is "one ... per media lane". The
            # rows were host lanes, so a coherent module - eight host lanes
            # into one optical carrier - was offered eight tuning rows for its
            # single laser, seven of them reading registers that are not there.
            if not _media_lane_present(i + 1):
                continue
            gs = grid_spacing[i]
            gc = (gs >> 4) & 0x0F
            fine_en = bool(gs & 0x01)
            # 12h:128-135.1 (7.5.3): this lane is supervised against the
            # thresholds on Page 62h rather than the module-wide Page 02h ones.
            rel_thr_en = bool(gs & 0x02)
            ch = struct.unpack(">h", channel_num[i*2:i*2+2])[0]
            ft = struct.unpack(">h", fine_offset[i*2:i*2+2])[0]
            freq_mhz = struct.unpack(">I", current_freq[i*4:i*4+4])[0]
            freq_thz = freq_mhz / 1e6
            # Table 7-8: 0 is LaserFrequencyTx's NA value - no valid sample,
            # not a laser at 0 THz.
            freq_na = bool((_state.get('caps') or {}).get('na_values')
                           and freq_mhz == cmis.NA_LASER_FREQ)
            tgt_pwr = struct.unpack(">h", target_pwr[i*2:i*2+2])[0] * 0.01
            st = tuning_status[i]
            flags = cmis.parse_tuning_flags(tuning_flags[i])
            masks = cmis.parse_tuning_masks(tuning_masks[i])
            # Latched and clear-on-read like every other Flag, so the read that
            # reports a refused tuning is the read that erases it.
            seen = _state['flag_history'].setdefault('tuning_%d' % (i + 1), set())
            for name, value in flags.items():
                if value and name != 'tuning_complete':
                    seen.add(name)
            if _state['flag_history_since'] is None:
                _state['flag_history_since'] = time.time()

            lanes.append({
                'lane': i + 1,
                'grid': grid_codes.get(gc, f'Unknown({gc})'),
                'grid_code': gc,
                'channel': ch,
                'tuning_flags': flags,
                'tuning_flags_seen': sorted(seen),
                'tuning_masks': masks,
                # Per lane, because the summary is per lane and a host that
                # read the module-wide byte alone would still have to come
                # back here to find out whether the lane can interrupt.
                'tuning_flag_summary': cmis.tuning_summary_bit(
                    tuning_summary, i),
                'channel_range': grid_channel_ranges.get(gc),
                'fine_tuning_enabled': fine_en,
                'fine_offset_ghz': ft * 0.001,
                'frequency_thz': None if freq_na else round(freq_thz, 6),
                'frequency_na': freq_na,
                'target_power_dbm': round(tgt_pwr, 2),
                'tuning_in_progress': bool((st >> 1) & 1),
                'wavelength_locked': not bool(st & 1),
                # 5.4: when set, Page 02h's absolute Tx power thresholds stop
                # applying to this lane and the relative ones take over.
                'relative_thresholds_enabled': rel_thr_en,
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
            'tunable': True,
            # Both halves of the "if and only if" - a summary bit with no
            # Flag behind it and a Flag with no summary bit are different
            # faults, and the module is wrong either way.
            'tuning_summary_conflicts': [
                c for bank in range((_state['lanes'] + 7) // 8)
                for c in ({'lane': x['lane'] + bank * 8,
                           'summary': x['summary'], 'flags': x['flags']}
                          for x in cmis.tuning_summary_disagreements(
                              tuning_summary[bank],
                              tuning_flags[bank * 8:bank * 8 + 8]))],
            'grid_channel_ranges': grid_channel_ranges,
            # Table 8-109 names every grid code, 1111b included ("Not
            # available"). The panel kept its own copy of that table and the
            # copy stopped at 1001b, so a lane on 1111b was named "Not
            # available" in the tooltip and "15" in the dropdown beside it -
            # one register, two answers, on the same row.
            'grid_names': {str(k): v for k, v in cmis.GRID_CODES.items()},
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
    err = _require_paged('Laser tuning')
    if err:
        return err
    try:
        body = request.get_json(silent=True) or {}
        # 01h:155.6 TransmitterIsTunable (Table 8-51) is what says Pages 04h
        # and 12h exist at all. Without it these writes land on a page the
        # module does not implement: the reads that follow come back as zeros
        # and the operator is told the laser was retuned.
        if not (_state.get('caps') or {}).get('controls', {}).get(
                'transmitter_tunable'):
            return _err('This module is not tunable (01h:155.6), so it has no '
                        'Page 04h or 12h to write laser settings to', 400)
        _set_page(0x12)
        lanes = body.get('lanes', [])
        # A body this handler does not understand used to come back as
        # "parameters written" having written nothing, so a caller with the
        # wrong shape was told its tuning had been applied.
        # A misspelled field was dropped without a word while the reply said
        # the tuning had been written: `target_power` for `target_power_dbm`
        # left the laser at its old output power and reported success. The
        # handler already refuses an entry that carries nothing it knows; this
        # is the same check for an entry that carries something as well.
        #
        # Checked before "no lanes given", so a misspelled `lanes` is named as
        # the misspelling rather than reported as an empty request.
        bad = _reject_unknown(body, ('lanes',))
        if bad:
            return bad
        if not isinstance(lanes, list) or not lanes:
            return _err('No lanes given; expected {"lanes": [{"lane": 1, ...}]}', 400)
        for _i, _entry in enumerate(lanes):
            if not isinstance(_entry, dict):
                return _err('Lane entry %d is not an object; expected '
                            '{"lane": 1, ...}' % (_i + 1), 400)
            bad = _reject_unknown(_entry, _LASER_LANE_FIELDS,
                                  where='in lane entry %d, ' % (_i + 1))
            if bad:
                return bad
        # The ranges the module advertises are the only thing that says what a
        # legal request looks like, so read them before writing one.
        _set_page(0x04)
        pwr_lo = struct.unpack('>h', _read_upper(*cmis.REG_PROG_PWR_MIN))[0] * 0.01
        pwr_hi = struct.unpack('>h', _read_upper(*cmis.REG_PROG_PWR_MAX))[0] * 0.01
        fine_lo = struct.unpack('>h', _read_upper(*cmis.REG_FINE_LOW_OFFSET))[0] * 0.001
        fine_hi = struct.unpack('>h', _read_upper(*cmis.REG_FINE_HIGH_OFFSET))[0] * 0.001
        # The same gating as the GET side: without it the 300 GHz grid has no
        # advertised range here, and a channel written to it is the one channel
        # this handler never checks.
        grid_300 = bool((_read_upper(*cmis.REG_GRID_SUPPORTED)[1] >> 5) & 1)
        ch_ranges = cmis.parse_grid_channel_ranges(_read_grid_ranges(grid_300))
        _set_page(0x12)

        # Every write is worked out and checked before any of it is sent. A
        # request that fails half way used to leave the lanes it had already
        # reached retuned, and the 400 that came back named only the lane that
        # failed - so the operator had no way to tell which of the others had
        # moved. Nothing is written unless all of it can be.
        plan = []
        written = 0
        for ldata in lanes:
            if not isinstance(ldata, dict):
                return _err('Each lane entry must be an object, got %r' % (ldata,), 400)
            try:
                lane = _as_int(ldata.get('lane', 1), 'Lane') - 1
            except ValueError as e:
                return _err(str(e), 400)
            # Page 12h is banked by media lane: Bank b holds lanes
            # 8b+1..8b+8 at the same addresses (8.15). The GET side reads
            # every bank, so the tuning table offers a row per lane on a
            # module with more than eight - and this loop dropped every one of
            # them past lane 8 without a word, answering "parameters written".
            if not (0 <= lane < _state['lanes']):
                return _err(
                    'Lane %d does not exist on this module: it has %d lane%s '
                    '(01h:142.1-0)'
                    % (lane + 1, _state['lanes'],
                       '' if _state['lanes'] == 1 else 's'), 400)
            # And the lane has to be a media lane, which is the axis this page
            # is indexed by. Tuning "lane 5" on a module with one media lane
            # was accepted and written, and the panel then reported the
            # channel back from a register the module does not have.
            if not _media_lane_present(lane + 1):
                return _err(
                    'Media lane %d is not on this module (00h:210). Page 12h '
                    'is indexed by media lane - "Each Bank of Page 12h refers '
                    'to 8 media lanes" (8.15) - so there is no laser here to '
                    'tune' % (lane + 1), 400)
            bank, slot = divmod(lane, 8)
            # Counted per field, not per entry: {"lane": 3} on its own asks
            # for nothing, and reporting it as a written lane is the same
            # "parameters written" having written nothing that the shape check
            # above exists to stop.
            fields = 0
            if 'grid_code' in ldata:
                gc = int(ldata['grid_code']) & 0x0F
                fine_en = 1 if ldata.get('fine_tuning_enabled', False) else 0
                # 12h:128-135 is not only the grid. Bit 1 is
                # RelativeOutputPowerThresholdsEnableTx, which decides whether
                # the lane is supervised against Page 62h or the module-wide
                # Page 02h thresholds (7.5.3) - rebuilding the whole byte from
                # the grid silently moved a lane back to absolute supervision,
                # so a request to change a grid changed which alarm limits
                # applied to that lane.
                keep = (_read_banked(*cmis.REG_GRID_SPACING_TX[:2], 1)[lane]
                        & 0x0E)
                plan.append((bank, cmis.REG_GRID_SPACING_TX[1] + slot,
                             bytes([(gc << 4) | keep | fine_en])))
                fields += 1
            if 'channel' in ldata:
                ch = int(ldata['channel'])
                gc_now = (ldata.get('grid_code') if 'grid_code' in ldata
                          else (_read_banked(*cmis.REG_GRID_SPACING_TX[:2], 1)[lane] >> 4) & 0x0F)
                allowed = ch_ranges.get(int(gc_now))
                if allowed and not (allowed[0] <= ch <= allowed[1]):
                    return _err(
                        'Lane %d: channel %d is outside the range the module '
                        'advertises for the %s grid (%d to %d, 04h:%d-%d)'
                        % (lane + 1, ch, cmis.GRID_CODES.get(int(gc_now), gc_now),
                           allowed[0], allowed[1],
                           130 + int(gc_now) * 4, 133 + int(gc_now) * 4), 400)
                plan.append((bank, cmis.REG_CHANNEL_NUM_TX[1] + slot * 2,
                             struct.pack(">h", ch)))
                fields += 1
            if 'fine_offset_ghz' in ldata:
                off = float(ldata['fine_offset_ghz'])
                if not (fine_lo <= off <= fine_hi):
                    return _err(
                        'Lane %d: fine-tuning offset %g GHz is outside the '
                        'advertised range (%g to %g GHz, 04h:192-195)'
                        % (lane + 1, off, fine_lo, fine_hi), 400)
                ft = int(round(off / 0.001))
                plan.append((bank, cmis.REG_FINE_OFFSET_TX[1] + slot * 2,
                             struct.pack(">h", ft)))
                fields += 1
            if 'target_power_dbm' in ldata:
                tgt = float(ldata['target_power_dbm'])
                if not (pwr_lo <= tgt <= pwr_hi):
                    return _err(
                        'Lane %d: target output power %g dBm is outside the '
                        'programmable range (%g to %g dBm, 04h:198-201)'
                        % (lane + 1, tgt, pwr_lo, pwr_hi), 400)
                pwr = int(round(tgt / 0.01))
                plan.append((bank, cmis.REG_TARGET_PWR_TX[1] + slot * 2,
                             struct.pack(">h", pwr)))
                fields += 1
            written += 1 if fields else 0
        if not written:
            return _err('No lane entry carried anything to write; expected at '
                        'least one of grid_code, channel, fine_offset_ghz or '
                        'target_power_dbm', 400)
        bad = _refuse_broadcast_tuning(plan)
        if bad:
            return bad
        # Bank first, then the byte. Page 12h is banked by media lane, so a
        # write that names only the page lands in whichever bank the last read
        # happened to leave selected.
        for bank, addr, payload in plan:
            _set_page(0x12, bank)
            _bus_write(addr, payload)
        # Writing is not tuning. The module answers in the Page 12h Flags, and
        # reporting success on the strength of the write alone told the
        # operator a refused channel had been applied.
        #
        # ton_flag, Table 10-6: "Time from onset of condition or occurrence
        # of event to associated Flag bit raised", 200 ms. Fifty was a
        # quarter of it, and reading a Flag before the module has had time to
        # raise it finds it clear - which is the same answer as a request
        # that was accepted. The wait is the whole basis for saying nothing
        # was refused, so it has to be the full one.
        time.sleep(cmis.TIMING_SECONDS['ton_flag'])
        raw = _read_banked(*cmis.REG_TUNING_FLAGS_TX[:2], 1)
        refused = {}
        for i in range(_state['lanes']):
            answered = cmis.parse_tuning_flags(raw[i])
            bad = sorted(n for n, v in answered.items()
                         if v and n not in ('tuning_complete', 'wavelength_unlocked'))
            # This read clears the Flags, so the history is what the next GET
            # will still have to show.
            seen = _state['flag_history'].setdefault('tuning_%d' % (i + 1), set())
            seen.update(bad)
            if bad:
                refused[i + 1] = bad
        if refused and _state['flag_history_since'] is None:
            _state['flag_history_since'] = time.time()
        # An empty `refused` is a claim about a Flag that was not up yet as
        # much as about one that never came, so the reply carries what it
        # waited: Table 10-6 allows the module the whole of ton_flag, and
        # anything less would have been a guess reported as an answer.
        return _ok({'message': 'Laser tuning parameters written', 'lanes': written,
                    'refused': refused,
                    'flag_wait_ms': round(
                        cmis.TIMING_SECONDS['ton_flag'] * 1000)})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/counters', methods=['GET'])
def api_module_counters():
    """Read error/bit counters using diagnostic selectors 0x02-0x05."""
    err = _require_connected()
    if err:
        return err
    err = _require_paged('Acquisition counters')
    if err:
        return err
    err = _require_diagnostics('Bit and error counters')
    if err:
        return err
    try:
        # 13h:130.1 advertises whether selectors 02h-05h mean anything. The
        # spec expects modules that cannot divide 64 bits to report counts
        # instead of a ratio, so a module may have one of these and not the
        # other - they are separate bits and are checked separately.
        if not _diag_caps()['reporting']['bits_and_errors']:
            return _ok({'lanes': [], 'supported': False})
        lanes = []
        # Table 7-8: MAX(U64) is the NA value of the pattern bit error count,
        # and a ratio built on it is not a measurement either.
        na_on = bool((_state.get('caps') or {}).get('na_values'))
        for sel, lane_start, side in [
            (0x02, 0, 'host'), (0x03, 4, 'host'),
            (0x04, 0, 'media'), (0x05, 4, 'media'),
        ]:
            for bank, data in _read_diag_banks(sel):
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
                    errors_na = na_on and error_count == cmis.NA_ERROR_COUNT
                    entry[f'{side}_errors_na'] = errors_na
                    entry[f'{side}_error_count'] = (None if errors_na
                                                    else error_count)
                    entry[f'{side}_total_bits'] = total_bits
                    entry[f'{side}_psl'] = bool(psl)
                    # No bits counted is no measurement. 0.0 said "no errors",
                    # which is a result - and the page, unable to tell the two
                    # apart, printed a real zero-error run as "—" as well.
                    entry[f'{side}_ber'] = (error_count / total_bits
                                            if total_bits > 0 and not errors_na
                                            else None)

        lanes.sort(key=lambda x: x['lane'])
        # Selectors 04h/05h are "Media Lane 1-4 / 5-8 errors and bits
        # counters". A media lane the module does not have has no counters,
        # and the registers behind it are not a count of anything.
        present = _media_lanes_present()
        for entry in lanes:
            i = entry['lane'] - 1
            if i < len(present) and not present[i]:
                for field in ('error_count', 'total_bits', 'psl', 'ber'):
                    entry['media_' + field] = None
                entry['media_errors_na'] = False
        return _ok({'lanes': lanes, 'supported': True,
                    'media_lanes_present': present,
                    'measurement': _measurement_window()})
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


def _check_bank(page: int, address: int, bank: int):
    """Refuse a bank that names nothing, rather than quietly reading Bank 0.

    Three ways to ask for one that does not exist, and each used to be
    answered with Bank 0's contents under the operator's own page number:
    a bank on lower memory, which is neither paged nor banked; a bank on a
    page CMIS does not define as Banked; and a bank past what this module
    advertises in 01h:142.1-0.
    """
    if bank == 0:
        return None
    if address < 0x80:
        return _err('Lower Memory (0x00-0x7F) is not banked; bank %d names '
                    'nothing there' % bank, 400)
    if not cmis.is_banked_page(page):
        return _err('Page 0x%02X is not a Banked Page, so it has only one '
                    'bank' % page, 400)
    banks = (_state.get('caps') or {}).get('banks_supported', 1)
    if not (0 <= bank < banks):
        return _err('This module has %d bank%s (01h:142.1-0), so bank %d does '
                    'not exist' % (banks, '' if banks == 1 else 's', bank), 400)
    return None


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
            bank = _as_int(body.get('bank', 0), 'Bank')
        except ValueError as e:
            return _err(str(e))
        err = _check_bank(page, address, bank)
        if err:
            return err
        if length < 1 or length > 128:
            return _err("Length must be 1–128")
        if not (0 <= page <= 0xFF):
            return _err("Page must be 0x00–0xFF")
        if not (0 <= address <= 0xFF):
            return _err("Address must be 0x00–0xFF")
        if address + length > 0x100:
            return _err(f"Read would cross end of page (address 0x{address:02X} + length {length} > 0x100)")

        if address >= 0x80:
            data = _read_upper(page, address, length, bank)
        else:
            data = _read_lower(address, length)

        return _ok({
            'page': page,
            'address': address,
            'bank': bank,
            # So the dump can say which eight lanes it is showing. Without it
            # a page of Bank 0 and the same page of Bank 3 are the same
            # picture on screen.
            'banked': address >= 0x80 and cmis.is_banked_page(page),
            'banks': ((_state.get('caps') or {}).get('banks_supported', 1)),
            'length': length,
            'data': list(data),
            'hex': ' '.join(f'{b:02X}' for b in data),
            # Table 8-3: "All bits in a RO/COR Byte are cleared by the module
            # after the Byte value has been read". Every panel in this tool
            # that reads a latched Flag folds it into the flag history for
            # that reason; a raw read cannot, because these bytes are only
            # numbers here. So the reply says what the read just destroyed -
            # the module keeps no second copy, and the bytes above are now
            # the only record there is.
            # The page is passed as given: below 0x80 the helper knows
            # Lower Memory is mapped whatever PageSelect says, and deciding
            # it here as well would be the same rule in two places.
            'clears_on_read': cmis.clear_on_read_overlap(
                page, address, length),
            # What the module will answer in one transaction, and therefore
            # whether this read was one or several. 128 is the ceiling only
            # when full page read is advertised (section 5.2.2.1).
            'max_read': _state.get('max_read', 8),
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
            bank = _as_int(body.get('bank', 0), 'Bank')
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

        # A write longer than one WRITE goes as several (5.2.2.2) - except
        # on the CDB header, where writing 9Fh:129 is what sends the command
        # (7.2.3): split, the piece holding 129 would send it before the rest
        # of the header had arrived.
        # (Starting at 0x80 or later and longer than one WRITE, it reaches
        # byte 129 whenever it starts at or before it.)
        if (page == 0x9F and 0x80 <= address <= 129
                and len(data) > MAX_WRITE):
            return _err('A WRITE carries at most %d bytes (5.2.2.2), and on '
                        'Page 9Fh the one that includes byte 129 sends the '
                        'CDB command (7.2.3) - so this cannot be split for '
                        'you. Write the rest of the header first, then the '
                        'part with byte 129, each in %d bytes or fewer'
                        % (MAX_WRITE, MAX_WRITE))

        err = _check_bank(page, address, bank)
        if err:
            return err
        if address >= 0x80:
            _set_page(page, bank)
        writes = _write_chunked(address, data)
        # A raw write may land on the PageMapping register itself, or on the
        # control byte that resets the module - either moves the selected page
        # out from under us.
        if address <= 0x7F < address + len(data) or address < 0x80:
            _invalidate_page()

        return _ok({
            'page': page,
            'address': address,
            'bank': bank,
            'bytes_written': len(data),
            # Said, because it is not one transaction: a register array
            # written in pieces is not written atomically (5.2.5.2).
            'writes': writes,
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

    # Outside the block above on purpose - the download is finished and
    # verified by here, so a failure now is a different problem with a
    # different remedy - but still inside one: this writes a helper script and
    # spawns it, which a locked file or an antivirus can refuse. Unhandled, the
    # worker thread would die and leave the UI polling 'installing' for ever,
    # which is how an update fails without anyone being told.
    try:
        # The port this instance is on, so the helper probes the right one
        # and the relaunched build comes back where the open page is.
        updater.stage_and_swap(staged, port=_port_state['active'])
    except Exception as e:
        _fail_update(
            f'The update was downloaded and verified but could not be '
            f'installed: {e}. The unpacked files are in {staged} — close this '
            f'tool and copy them over the old ones by hand.')
        return
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
    # Set CMIS_NO_BROWSER=1 to start the server without opening a tab. Repeated
    # automated launches otherwise leave a pile of tabs behind, and after a
    # self-update the relaunched instance would open yet another one on top of
    # the page the user is already looking at.
    _open_browser = os.environ.get('CMIS_NO_BROWSER', '').strip() not in (
        '1', 'true', 'True')
    _server, _already = _start_server()
    if _server is None:
        # A second launch while the first is running: show that one rather
        # than asking the user to pick a port to get away from ourselves.
        url = f'http://127.0.0.1:{_already}/'
        print(f"CMIS Module Manager is already running at {url}")
        if _open_browser:
            webbrowser.open(url)
        sys.exit(0)
    _port_state['active'] = _server.server_port
    url = f'http://127.0.0.1:{_server.server_port}/'
    print(f"CMIS Module Manager v{__version__} starting on {url}")
    _conflict = _port_state['conflict']
    if _conflict:
        print(f"Port {_conflict['port']} could not be used "
              f"({_conflict['reason']}). Running on {_server.server_port} "
              f"for now - the page will ask which port to use from now on.")
    if _open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    _serve(_server)
