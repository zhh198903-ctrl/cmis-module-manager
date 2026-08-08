"""Comprehensive API tests for CMIS optical module management tool."""
import os
import sys
import json
import math
import struct
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
from app import app, _state


def reset_state():
    """Reset global app state between tests."""
    if _state['backend'] is not None:
        try:
            _state['backend'].disconnect()
        except Exception:
            pass
    _state['backend'] = None
    _state['connected'] = False
    _state['bus'] = None
    _state['address'] = None


def connect_mock(client):
    """Helper: connect to mock backend."""
    rv = client.post('/api/connect',
                     data=json.dumps({'backend': 'mock_dr8', 'bus': 0, 'address': 80}),
                     content_type='application/json')
    assert rv.status_code == 200, f"connect failed: {rv.data}"
    return rv


# ============================================================
# Test helpers / fixture
# ============================================================

class CMISTestCase(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        reset_state()

    def tearDown(self):
        reset_state()

    # --------------------------------------------------------
    # helper assertions
    # --------------------------------------------------------
    def assertOk(self, rv, code=200):
        self.assertEqual(rv.status_code, code,
                         f"Expected {code}, got {rv.status_code}: {rv.data}")
        body = json.loads(rv.data)
        self.assertEqual(body['status'], 'ok', f"Expected ok: {body}")
        return body

    def assertErr(self, rv, code=None):
        body = json.loads(rv.data)
        self.assertEqual(body['status'], 'error', f"Expected error: {body}")
        if code is not None:
            self.assertEqual(rv.status_code, code,
                             f"Expected HTTP {code}, got {rv.status_code}")
        return body

    def connect(self):
        return connect_mock(self.client)


# ============================================================
# 1. GET /api/backends
# ============================================================

class TestVersion(CMISTestCase):

    def test_version_endpoint(self):
        body = self.assertOk(self.client.get('/api/version'))
        self.assertEqual(body['data']['version'], app_module.__version__)

    def test_version_rendered_in_ui(self):
        """The header badge and sidebar must show the real version."""
        html = self.client.get('/').data.decode('utf-8')
        self.assertIn(f'v{app_module.__version__}', html)
        self.assertNotIn('{{ version }}', html)

    def test_version_matches_manual(self):
        """The manual ships next to the EXE; a stale version there misleads users."""
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'CMIS2Customer', 'CMIS模块管理工具操作手册.html')
        if not os.path.exists(path):
            self.skipTest('manual not present')
        text = io.open(path, encoding='utf-8').read()
        self.assertIn(app_module.__version__, text,
                      'operation manual version is out of sync with app.__version__')


class TestMonitoringPresentation(CMISTestCase):
    """Guards for values that must not be presented as trustworthy."""

    def _js(self):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'app.js')
        return io.open(path, encoding='utf-8').read()

    def test_alarm_colouring_uses_the_modules_own_thresholds(self):
        """A fixed +/-10 dBm pair contradicted the module's advertised limits,
        so the monitoring table, the thresholds card and the lane flags gave
        three different answers about one lane."""
        js = self._js()
        self.assertIn('_powerLimits()', js)
        self.assertIn('tx_power_low_alarm_dbm', js)
        self.assertIn('_moduleThresholds = d', js,
                      'thresholds are read but never fed back into colouring')

    def test_thresholds_endpoint_supplies_what_colouring_needs(self):
        self.connect()
        d = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        for k in ('tx_power_low_alarm_dbm', 'tx_power_high_alarm_dbm',
                  'rx_power_low_alarm_dbm', 'rx_power_high_alarm_dbm'):
            self.assertIn(k, d)
            self.assertIsInstance(d[k], (int, float))

    def test_counters_expose_pattern_sync_loss(self):
        """Counts taken while sync is lost are not a BER measurement."""
        self.connect()
        d = self.assertOk(self.client.get('/api/module/counters'))['data']
        self.assertTrue(d['lanes'], 'no lanes returned')
        for lane in d['lanes']:
            self.assertIn('host_psl', lane)
            self.assertIn('media_psl', lane)
        self.assertIn('no sync', self._js(),
                      'the UI does not surface pattern sync loss')

    def test_diagnostics_tab_loads_every_card(self):
        js = self._js()
        block = js.split("if (name === 'diagnostics')")[1].split('\n  }')[0]
        for fn in ('loadLoopback', 'loadPrbs', 'loadBer', 'loadSnr',
                   'loadCounters', 'loadLaser'):
            self.assertIn(fn, block, f'{fn} is not loaded when the tab opens')


class TestUpdater(unittest.TestCase):
    """Update logic, exercised without touching the network."""

    def test_version_parsing_and_ordering(self):
        import updater as u
        self.assertEqual(u.parse_version('v2.0.1'), (2, 0, 1))
        self.assertEqual(u.parse_version('2.0.1-rc1'), (2, 0, 1))
        self.assertEqual(u.parse_version(''), (0,))
        self.assertTrue(u.is_newer('2.0.2', '2.0.1'))
        self.assertTrue(u.is_newer('2.1', '2.0.9'), 'shorter version mis-padded')
        self.assertFalse(u.is_newer('2.0.1', '2.0.1'))
        self.assertFalse(u.is_newer('2.0.0', '2.0.1'), 'offered a downgrade')

    def test_release_payload_normalises(self):
        import updater as u
        rel = u.normalize_release({
            'tag_name': 'v2.1.0',
            'html_url': 'https://example.invalid/r',
            'body': 'notes',
            'assets': [
                {'name': 'source.zip', 'browser_download_url': 'https://x/1'},
                {'name': 'CMIS_dist_v2_1_0.zip', 'size': 123,
                 'digest': 'sha256:ABC', 'browser_download_url': 'https://x/2'},
            ],
        })
        self.assertEqual(rel['version'], '2.1.0')
        self.assertEqual(rel['asset_name'], 'CMIS_dist_v2_1_0.zip')
        self.assertEqual(rel['asset_url'], 'https://x/2')
        self.assertEqual(rel['sha256'], 'abc')

    def test_release_without_our_asset_is_rejected(self):
        """A release carrying only source tarballs must not look installable."""
        import updater as u
        self.assertIsNone(u.normalize_release(
            {'tag_name': 'v9.9.9', 'assets': [{'name': 'notes.txt',
                                               'browser_download_url': 'https://x'}]}))
        self.assertIsNone(u.normalize_release({'tag_name': 'v9.9.9', 'assets': []}))
        self.assertIsNone(u.normalize_release(None))

    def test_extract_rejects_paths_escaping_the_target(self):
        import tempfile, zipfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, 'evil.zip')
            with zipfile.ZipFile(zp, 'w') as zf:
                zf.writestr('../escaped.exe', b'x')
                zf.writestr(u.EXE_NAME, b'x')
            with self.assertRaises(ValueError):
                u.extract_payload(zp, os.path.join(tmp, 'out'))

    def test_extract_requires_the_executable(self):
        import tempfile, zipfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, 'partial.zip')
            with zipfile.ZipFile(zp, 'w') as zf:
                zf.writestr('manual.html', b'x')
            with self.assertRaises(ValueError):
                u.extract_payload(zp, os.path.join(tmp, 'out'))

    def test_extract_unpacks_the_whole_payload(self):
        """Manual and images travel with the exe; a new exe beside an old
        manual would document behaviour the build no longer has."""
        import tempfile, zipfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, 'ok.zip')
            with zipfile.ZipFile(zp, 'w') as zf:
                zf.writestr(u.EXE_NAME, b'exe')
                zf.writestr('manual.html', b'doc')
                zf.writestr('qrcode.jpg', b'img')
            out = os.path.join(tmp, 'out')
            names = u.extract_payload(zp, out)
            self.assertCountEqual(names, [u.EXE_NAME, 'manual.html', 'qrcode.jpg'])
            self.assertTrue(os.path.isfile(os.path.join(out, u.EXE_NAME)))

    def test_checksum_absent_is_accepted_but_mismatch_is_not(self):
        import tempfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'f.bin')
            with open(p, 'wb') as fh:
                fh.write(b'hello')
            self.assertTrue(u.verify_sha256(p, None))
            self.assertFalse(u.verify_sha256(p, '00' * 32))

    def test_swap_script_is_bounded_and_quotes_paths(self):
        """An unbounded retry loop would leave a hidden cmd.exe spinning."""
        import updater as u
        bat = u.build_swap_bat(r'C:\stage dir', r'C:\install dir')
        self.assertIn('if %tries% gtr 150 goto cleanup', bat)
        self.assertIn(r'"C:\install dir\CMIS_Module_Manager.exe"', bat)
        self.assertIn('del "%~f0"', bat)

    def test_self_update_refused_when_running_from_source(self):
        import updater as u
        self.assertFalse(u.is_frozen(), 'tests should not run frozen')
        with self.assertRaises(RuntimeError):
            u.stage_and_swap('anywhere')


class TestUpdateRoutes(CMISTestCase):

    def test_apply_refused_from_source(self):
        rv = self.client.post('/api/update/apply')
        self.assertErr(rv, 400)
        self.assertIn('git pull', json.loads(rv.data)['message'])

    def test_check_reports_unreachable_rather_than_up_to_date(self):
        """A blocked network must never be reported as being current."""
        import updater as u
        real = u.fetch_latest_release
        u.fetch_latest_release = lambda *a, **k: None
        try:
            rv = self.client.get('/api/update/check')
            self.assertErr(rv, 502)
            self.assertIn('Could not reach GitHub', json.loads(rv.data)['message'])
        finally:
            u.fetch_latest_release = real

    def test_check_compares_against_the_running_version(self):
        import updater as u
        import app as app_mod
        real = u.fetch_latest_release
        u.fetch_latest_release = lambda *a, **k: {
            'version': '9.9.9', 'tag': 'v9.9.9', 'asset_name': 'CMIS_dist_v9_9_9.zip',
            'asset_url': 'https://x', 'asset_size': 10, 'sha256': None,
            'html_url': 'https://x', 'notes': '', 'published_at': '',
        }
        try:
            body = self.assertOk(self.client.get('/api/update/check'))['data']
            self.assertTrue(body['update_available'])
            self.assertEqual(body['current_version'], app_mod.__version__)
            self.assertFalse(body['can_self_update'], 'source run offered a self-update')
        finally:
            u.fetch_latest_release = real


class TestModuleControlBits(CMISTestCase):
    """Byte 0x1A packs unrelated controls; touching one must not move others."""

    def _set_raw(self, value):
        self.client.post('/api/register/write',
                         data=json.dumps({'page': 0, 'address': 0x1A, 'data': [value]}),
                         content_type='application/json')

    def _raw(self):
        return self.assertOk(self.client.get('/api/module/control'))['data']['raw']

    def test_low_power_toggle_preserves_other_bits(self):
        """Regression: Exit LowPwr used to clear SquelchMethodSelect."""
        self.connect()
        self._set_raw(0xA0)          # BankBroadcast + SquelchMethodSelect(Pav)
        for action in ('low_power', 'high_power'):
            self.client.post('/api/module/control',
                             data=json.dumps({'action': action}),
                             content_type='application/json')
            raw = self._raw()
            self.assertTrue(raw & (1 << 7), f'{action} cleared BankBroadcastEnable')
            self.assertTrue(raw & (1 << 5), f'{action} cleared SquelchMethodSelect')
        self.assertFalse(self._raw() & (1 << 4), 'high_power left LowPwrRequestSW set')

    def test_low_power_actually_toggles_its_own_bit(self):
        self.connect()
        self._set_raw(0x00)
        self.client.post('/api/module/control', data=json.dumps({'action': 'low_power'}),
                         content_type='application/json')
        self.assertTrue(self._raw() & (1 << 4))

    def test_partial_field_set_leaves_unnamed_fields_alone(self):
        self.connect()
        self._set_raw(0xA0)
        self.client.post('/api/module/control',
                         data=json.dumps({'low_pwr': True}),
                         content_type='application/json')
        self.assertEqual(self._raw(), 0xB0, 'a partial set rebuilt the whole byte')

    def test_software_reset_is_self_clearing(self):
        """CMIS marks SoftwareReset WO/SC; reading it back as 1 would leave the
        UI claiming a reset is still in progress forever."""
        self.connect()
        self.client.post('/api/module/control', data=json.dumps({'action': 'reset'}),
                         content_type='application/json')
        self.assertFalse(self._raw() & (1 << 3),
                         'SoftwareReset stayed set after the write')


class TestRawWriteGuards(CMISTestCase):

    def test_write_through_page_select_is_refused(self):
        """A multi-byte write across 0x7F would reprogram the page mid-transfer
        and scatter the rest into whatever page that byte named."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x7C,
                                               'data': [0, 0, 0, 0x12, 0x34]}),
                              content_type='application/json')
        self.assertErr(rv, 400)
        self.assertEqual(_state['backend']._current_page, 0x00,
                         'the refused write still moved the page')

    def test_single_byte_page_select_still_allowed(self):
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x7F, 'data': [0x11]}),
                              content_type='application/json')
        self.assertOk(rv)


class TestPageSelection(CMISTestCase):
    """Page selection is cached; a stale cache silently reads the wrong page."""

    def _page_writes(self, fn):
        """Run fn and return the pages written to the PageMapping register."""
        backend = _state['backend']
        real = backend.write_bytes
        seen = []

        def spy(addr, data):
            if addr == 0x7F:
                seen.append(data[0])
            return real(addr, data)

        backend.write_bytes = spy
        try:
            fn()
        finally:
            backend.write_bytes = real
        return seen

    def test_page_change_waits_the_spec_hold_off(self):
        """CMIS 5.3 gives tBPC, max Bank/Page Change time, as 10 ms.

        Reading sooner can return the previous page's contents on a slow
        module - an intermittent fault that looks like corrupt data.
        """
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
        src = io.open(path, encoding='utf-8').read()
        body = src.split('def _set_page(')[1].split('\ndef ')[0]
        self.assertIn('time.sleep(0.010)', body,
                      'page-change hold-off is shorter than tBPC = 10 ms')

    def test_repeated_reads_of_one_page_select_once(self):
        self.connect()
        pages = self._page_writes(lambda: self.client.get('/api/module/thresholds'))
        self.assertEqual(pages, [0x02],
                         f'thresholds re-selected its page: {pages}')

    def test_alternating_pages_reselect_each_time(self):
        """Caching must not skip a genuine page change."""
        self.connect()
        def alternate():
            for _ in range(2):
                self.client.post('/api/register/read',
                                 data=json.dumps({'page': 0x11, 'address': 0x80, 'length': 1}),
                                 content_type='application/json')
                self.client.post('/api/register/read',
                                 data=json.dumps({'page': 0x10, 'address': 0x80, 'length': 1}),
                                 content_type='application/json')
        self.assertEqual(self._page_writes(alternate), [0x11, 0x10, 0x11, 0x10])

    def test_reconnect_forgets_the_cached_page(self):
        self.connect()
        self.client.get('/api/module/thresholds')       # leaves page 02h selected
        self.connect()                                   # fresh module, page unknown
        pages = self._page_writes(lambda: self.client.get('/api/module/thresholds'))
        self.assertEqual(pages, [0x02], 'reconnect trusted a stale page cache')

    def test_raw_write_to_page_register_forgets_the_cache(self):
        """Writing 0x7F by hand moves the page out from under the cache."""
        self.connect()
        self.client.get('/api/module/thresholds')       # page 02h cached
        self.client.post('/api/register/write',
                         data=json.dumps({'page': 0, 'address': 0x7F, 'data': [0x11]}),
                         content_type='application/json')
        pages = self._page_writes(lambda: self.client.get('/api/module/thresholds'))
        self.assertEqual(pages, [0x02], 'cache survived a raw write to 0x7F')

    def test_module_reset_forgets_the_cached_page(self):
        self.connect()
        self.client.get('/api/module/thresholds')
        self.client.post('/api/module/control',
                         data=json.dumps({'action': 'reset'}),
                         content_type='application/json')
        pages = self._page_writes(lambda: self.client.get('/api/module/thresholds'))
        self.assertEqual(pages, [0x02], 'cache survived a module reset')

    def test_values_still_come_from_the_right_page(self):
        """End-to-end guard: caching must not cross-contaminate pages."""
        self.connect()
        self.client.post('/api/module/squelch',
                         data=json.dumps({'tx_squelch_disable': 0x11,
                                          'tx_squelch_force': 0x22,
                                          'rx_output_disable': 0x33,
                                          'rx_squelch_disable': 0x44}),
                         content_type='application/json')
        # Interleave reads of three different pages, then re-check page 10h.
        self.client.get('/api/module/thresholds')   # 02h
        self.client.get('/api/module/monitoring')   # 11h
        self.client.get('/api/module/loopback')     # 13h
        body = self.assertOk(self.client.get('/api/module/squelch'))
        self.assertEqual(body['data']['tx_squelch_disable'], 0x11)
        self.assertEqual(body['data']['rx_squelch_disable'], 0x44)


class TestRegisterTooltips(CMISTestCase):
    """The UI hover tooltips quote CMIS field names and addresses at the user.

    A wrong tooltip is worse than none, so pin the strings that app.js emits to
    the names and byte addresses in OIF CMIS 5.3. Sources: Table 8-69
    (lane-specific controls, Page 10h), Table 8-72 (Staged Control Set 0),
    Tables 8-109/8-111/8-113/8-115 (pattern gen/check, Page 13h), Table 8-121
    (loopback controls), Table 8-99 (tunable laser, Page 12h) and the Module
    Control byte in Lower Memory.
    """

    def _js(self):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'app.js')
        return io.open(path, encoding='utf-8').read()

    def test_page10h_tooltip_fields(self):
        js = self._js()
        for field, addr in [('InputPolarityFlipTx', '0x81'), ('OutputDisableTx', '0x82'),
                            ('AutoSquelchDisableTx', '0x83'), ('OutputSquelchForceTx', '0x84'),
                            ('OutputPolarityFlipRx', '0x89'), ('OutputDisableRx', '0x8A'),
                            ('AutoSquelchDisableRx', '0x8B')]:
            self.assertIn(field, js, f'{field} missing from tooltips')
            self.assertIn(addr, js, f'{addr} missing from tooltips')
        self.assertIn('DPDeinitLane', js)
        self.assertIn('DPConfigLane', js)

    def test_page13h_tooltip_fields(self):
        """Pattern controls are named <Side>Side<Role><Field>Lane<n>."""
        js = self._js()
        for part in ['Side${role}', 'PatternSelectLane', 'SwapSymbolBits',
                     'DataInvert', 'PreFECEnable', 'PostFECEnable']:
            self.assertIn(part, js, f'{part} missing from PRBS tooltips')
        for field in ['MediaSideOutputLoopbackEnable', 'MediaSideInputLoopbackEnable',
                      'HostSideOutputLoopbackEnable', 'HostSideInputLoopbackEnable']:
            self.assertIn(field, js, f'{field} missing from loopback tooltips')

    def test_page12h_tooltip_fields(self):
        js = self._js()
        for field in ['GridSpacingTx', 'FineTuningEnableTx', 'ChannelNumberTx',
                      'FineTuningOffsetTx', 'CurrentLaserFrequencyTx',
                      'TargetOutputPowerTx', 'TuningInProgressTx', 'WavelengthUnlockedTx']:
            self.assertIn(field, js, f'{field} missing from laser tooltips')

    def test_module_control_tooltip_fields(self):
        js = self._js()
        for field in ['SoftwareReset', 'LowPwrRequestSW',
                      'LowPwrAllowRequestHW', 'SquelchMethodSelect']:
            self.assertIn(field, js, f'{field} missing from Module Control tooltips')

    def _html(self):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        return io.open(path, encoding='utf-8').read()

    def test_static_column_labels_match_register_map(self):
        """Column headers hard-code addresses; keep them tied to cmis_registers.

        These labels drifted once already: after the Page 10h map was corrected
        the DataPath headers still advertised the old bytes, so the header and
        the hover tooltip disagreed about the same control.
        """
        import cmis_registers as c
        html = self._html()
        for label, reg in [('AppSelect', c.REG_APP_SELECT),
                           ('TX Enable', c.REG_TX_OUTPUT_DIS),
                           ('TX Pol Flip', c.REG_TX_POL_FLIP),
                           ('RX Pol Flip', c.REG_RX_POL_FLIP),
                           ('DP Deinit', c.REG_DP_DEINIT)]:
            head = html.split(f'<th>{label}<span class="reg-meta">')[1].split('</span>')[0]
            page, addr = reg[0], reg[1]
            self.assertIn(f'{page:02X}h', head,
                          f'{label} header names the wrong page: {head}')
            self.assertIn(f'0x{addr:02X}', head,
                          f'{label} header names the wrong address: {head}')

    @staticmethod
    def _register_ranges():
        """Every (page, first_byte, last_byte) defined in cmis_registers."""
        import cmis_registers as c
        out = []
        for name in dir(c):
            if not name.startswith('REG_'):
                continue
            val = getattr(c, name)
            if isinstance(val, tuple) and len(val) == 3:
                page, addr, length = val
                out.append((name, page, addr, addr + length - 1))
        return out

    def _resolve(self, page, addr):
        return [n for n, p, lo, hi in self._register_ranges()
                if p == page and lo <= addr <= hi]

    def test_every_displayed_address_exists_in_the_register_map(self):
        """Static Page/address labels are only "executed" when a human reads them.

        Nothing else exercises the strings baked into the column headers and
        the Module Info rows, which is how the FW Revision row came to
        advertise Page 01h 0x84-0x85 for a field the code reads from Lower
        Memory 0x27. Require every address shown to resolve to a real entry in
        cmis_registers, so an invented or stale one fails here.
        """
        import re
        html, js = self._html(), self._js()
        shown = []

        for m in re.finditer(r'class="reg-(?:meta|badge)">([^<]+)<', html):
            label = m.group(1)
            if 'sel=' in label:
                continue  # a diagnostics selector value, not an address
            pm = re.search(r'\b([0-9A-Fa-f]{2})h', label)
            am = re.search(r'0x([0-9A-Fa-f]{2})', label)
            if pm and am:
                shown.append((int(pm.group(1), 16), int(am.group(1), 16),
                              f'index.html header "{label.strip()}"'))

        rows = js.split('const rows = [')[1].split('\n  ];')[0]
        for pg_s, ad_s in re.findall(r"'(Lower|[0-9A-Fa-f]{2}h)',\s*'0x([0-9A-Fa-f]{2})", rows):
            page = None if pg_s == 'Lower' else int(pg_s[:2], 16)
            shown.append((page, int(ad_s, 16),
                          f'Module Info row ({pg_s}, 0x{ad_s})'))

        self.assertGreater(len(shown), 50, 'address extraction stopped working')
        unknown = [(p, a, w) for p, a, w in shown if not self._resolve(p, a)]
        self.assertEqual(unknown, [], 'addresses shown to the user with no '
                                      'matching entry in cmis_registers')

    def test_fw_revision_row_points_at_lower_memory(self):
        """Regression: this row named a Page 01h fibre-length byte."""
        import cmis_registers as c
        rows = self._js().split('const rows = [')[1].split('\n  ];')[0]
        fw = [ln for ln in rows.splitlines() if "'FW Revision'" in ln][0]
        self.assertIn("'Lower'", fw)
        self.assertIn(f'0x{c.REG_FW_ACTIVE_MAJOR[1]:02X}', fw)

    def test_output_status_tx_rx_not_swapped(self):
        """CMIS 5.3 8.10.2: OutputStatusRx is 11h:132, OutputStatusTx is 11h:133."""
        import cmis_registers as c
        self.assertEqual(c.REG_OUTPUT_STATUS_RX[:2], (0x11, 0x84))
        self.assertEqual(c.REG_OUTPUT_STATUS_TX[:2], (0x11, 0x85))

    def test_values_state_their_radix(self):
        """A bare number in a register tool is ambiguous; mark hex and binary."""
        js = self._js()
        self.assertIn('hex = ', js, 'tooltips do not label the hex form')
        self.assertIn('bin = ', js, 'tooltips do not label the binary form')
        self.assertIn('dec', js, 'tooltips do not label the decimal form')
        # The lane-assignment bitmap must not render as bare digits.
        self.assertIn("'0b' + a.host_lane_assign_mask.toString(2)", js)

    def test_apply_handlers_reload_after_write(self):
        """Every Apply must re-read, or the panel and its tooltips go stale.

        The tooltips quote the current register byte, so an Apply that only
        writes leaves the user looking at the value from before their change -
        and hides a module that rejected or clamped the write.
        """
        js = self._js()
        # These three go through the shared write-then-reload helper.
        for fn in ['applySquelch', 'applyLoopback', 'applyPrbs']:
            body = js.split(f'async function {fn}(')[1].split('\nasync function')[0]
            self.assertIn('applyAndReload', body,
                          f'{fn} writes without re-reading the module')
        # applyDatapath waits for ApplyDPInit before re-reading, so it reloads
        # explicitly rather than via the helper.
        dp = js.split('async function applyDatapath(')[1].split('\nasync function')[0]
        self.assertIn('loadDatapath()', dp, 'applyDatapath does not re-read')

    def test_control_endpoint_exposes_raw_byte(self):
        """Tooltips quote the current byte, so the API must return it."""
        self.connect()
        body = self.assertOk(self.client.get('/api/module/control'))
        self.assertIn('raw', body['data'])
        self.assertIsInstance(body['data']['raw'], int)


class TestBackends(CMISTestCase):

    def test_backends_ok(self):
        """Should return list with at least mock backend."""
        rv = self.client.get('/api/backends')
        body = self.assertOk(rv)
        backends = body['data']
        self.assertIsInstance(backends, list)
        self.assertTrue(len(backends) >= 1)
        names = [b['name'] for b in backends]
        for profile in ('mock_coherent', 'mock_dr8', 'mock_sr8', 'mock_fr4x2'):
            self.assertIn(profile, names)

    def test_backends_mock_available(self):
        """All mock profiles must be marked available."""
        rv = self.client.get('/api/backends')
        body = self.assertOk(rv)
        by_name = {b['name']: b for b in body['data']}
        for profile in ('mock_coherent', 'mock_dr8', 'mock_sr8', 'mock_fr4x2'):
            self.assertTrue(by_name[profile]['available'])


# ============================================================
# 2. POST /api/connect
# ============================================================

class TestConnect(CMISTestCase):

    def test_connect_mock_defaults(self):
        """Connect with mock backend, default bus/address."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8'}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['backend'], 'mock_dr8')
        self.assertTrue(_state['connected'])

    def test_connect_explicit_params(self):
        """Connect with explicit bus and integer address."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8', 'bus': 1, 'address': 90}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['bus'], 1)
        self.assertEqual(body['data']['address'], 90)

    def test_connect_hex_address(self):
        """Address may be supplied as hex string."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8', 'bus': 0, 'address': '0x50'}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['address'], 0x50)

    def test_connect_unknown_backend(self):
        """Unknown backend name must return error."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'nonexistent'}),
                              content_type='application/json')
        self.assertErr(rv, 400)
        self.assertFalse(_state['connected'])

    def test_connect_invalid_address_string(self):
        """Non-numeric address string must return error (not crash)."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8', 'address': 'bad_addr'}),
                              content_type='application/json')
        # Should return 400/500 error, not 500 unhandled exception
        self.assertIn(rv.status_code, (400, 500))
        body = json.loads(rv.data)
        self.assertEqual(body['status'], 'error')

    def test_connect_no_body(self):
        """Empty body → defaults to mock backend, should succeed."""
        rv = self.client.post('/api/connect',
                              data=b'',
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['backend'], 'mock_dr8')

    def test_reconnect_replaces_backend(self):
        """Second connect call should replace first backend."""
        self.connect()
        first_backend = _state['backend']
        self.connect()
        second_backend = _state['backend']
        # Different object instances
        self.assertIsNot(first_backend, second_backend)
        self.assertTrue(_state['connected'])


# ============================================================
# 3. GET /api/disconnect
# ============================================================

class TestDisconnect(CMISTestCase):

    def test_disconnect_when_connected(self):
        """Normal disconnect after connection."""
        self.connect()
        rv = self.client.get('/api/disconnect')
        body = self.assertOk(rv)
        self.assertFalse(_state['connected'])
        self.assertIsNone(_state['backend'])

    def test_disconnect_when_not_connected(self):
        """Disconnect without prior connect should still succeed."""
        rv = self.client.get('/api/disconnect')
        self.assertOk(rv)
        self.assertFalse(_state['connected'])

    def test_disconnect_via_post(self):
        """Disconnect also accepts POST."""
        self.connect()
        rv = self.client.post('/api/disconnect')
        self.assertOk(rv)
        self.assertFalse(_state['connected'])


# ============================================================
# 4. GET /api/module/info
# ============================================================

class TestModuleInfo(CMISTestCase):

    def test_info_not_connected(self):
        """Should return 503 when not connected."""
        rv = self.client.get('/api/module/info')
        self.assertErr(rv, 503)

    def test_info_ok(self):
        """Should return valid module info fields."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        d = body['data']
        self.assertIn('module_id', d)
        self.assertIn('module_type', d)
        self.assertIn('cmis_revision', d)
        self.assertIn('vendor_name', d)
        self.assertIn('vendor_pn', d)
        self.assertIn('vendor_sn', d)
        self.assertIn('date_code', d)
        self.assertIn('host_lanes', d)
        self.assertIn('media_lanes', d)

    def test_info_module_id_qsfpdd(self):
        """Mock registers QSFP-DD (0x1E)."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['module_id'], 0x1E)
        self.assertIn('QSFP-DD', body['data']['module_type'])

    def test_info_cmis_revision(self):
        """CMIS revision should be '5.3' in mock_dr8."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['cmis_revision'], '5.3')

    def test_info_vendor_name(self):
        """Vendor name should be OPENCMIS DEMO (stripped)."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['vendor_name'], 'OPENCMIS DEMO')

    def test_info_num_lanes(self):
        """mock_dr8 is 8 host lanes / 8 media lanes."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['host_lanes'], 8)
        self.assertEqual(body['data']['media_lanes'], 8)


# ============================================================
# 5. GET /api/module/status
# ============================================================

class TestModuleStatus(CMISTestCase):

    def test_status_not_connected(self):
        rv = self.client.get('/api/module/status')
        self.assertErr(rv, 503)

    def test_status_ok(self):
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        d = body['data']
        self.assertIn('module_state', d)
        self.assertIn('temperature_c', d)
        self.assertIn('voltage_v', d)
        self.assertIn('interrupt_asserted', d)
        self.assertIn('alarm_active', d)

    def test_status_temperature_range(self):
        """Temperature should be in a sane range for mock (near 45°C)."""
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        temp = body['data']['temperature_c']
        self.assertIsInstance(temp, float)
        self.assertGreater(temp, 0.0)
        self.assertLess(temp, 100.0)

    def test_status_voltage_range(self):
        """Voltage should be near 3.3V in mock."""
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        volt = body['data']['voltage_v']
        self.assertAlmostEqual(volt, 3.3, delta=0.1)

    def test_status_module_state_value(self):
        """Mock lower[0x01] = 0x04 → bits[3:1] = 0x04 & 0x0E = 0x04 → ModuleReady."""
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        # state byte 0x04 → bits[3:1] = 0x04 & 0x0E = 0x04 → 'ModuleReady'
        self.assertEqual(body['data']['module_state'], 'ModuleReady')

    def test_status_alarm_active_type(self):
        """alarm_active should be a boolean."""
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        self.assertIsInstance(body['data']['alarm_active'], bool)


# ============================================================
# 6. GET /api/module/monitoring
# ============================================================

class TestModuleMonitoring(CMISTestCase):

    def test_monitoring_not_connected(self):
        rv = self.client.get('/api/module/monitoring')
        self.assertErr(rv, 503)

    def test_monitoring_ok(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        self.assertIn('lanes', body['data'])

    def test_monitoring_8_lanes(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        self.assertEqual(len(body['data']['lanes']), 8)

    def test_monitoring_lane_fields(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        lane = body['data']['lanes'][0]
        for key in ('lane', 'tx_power_uw', 'tx_power_dbm', 'rx_power_uw',
                    'rx_power_dbm', 'tx_bias_ma', 'datapath_state'):
            self.assertIn(key, lane, f"Missing key: {key}")

    def test_monitoring_lane_numbers(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        lane_nums = [l['lane'] for l in body['data']['lanes']]
        self.assertEqual(lane_nums, list(range(1, 9)))

    def test_monitoring_power_positive(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        for lane in body['data']['lanes']:
            self.assertGreater(lane['tx_power_uw'], 0)
            self.assertGreater(lane['rx_power_uw'], 0)

    def test_monitoring_dbm_conversion(self):
        """dBm should correspond to µW conversion."""
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        for lane in body['data']['lanes']:
            expected_dbm = round(10 * math.log10(lane['tx_power_uw'] / 1000.0), 2)
            self.assertAlmostEqual(lane['tx_power_dbm'], expected_dbm, places=1)

    def test_monitoring_datapath_state_valid(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        valid_states = {'Deactivated', 'Init', 'TxTurnOn', 'Activated'}
        for lane in body['data']['lanes']:
            state = lane['datapath_state']
            # Either a known state or Unknown(...)
            self.assertTrue(state in valid_states or state.startswith('Unknown'),
                            f"Unexpected state: {state}")


# ============================================================
# 7. GET /api/module/datapath
# ============================================================

class TestDatapathGet(CMISTestCase):

    def test_datapath_not_connected(self):
        rv = self.client.get('/api/module/datapath')
        self.assertErr(rv, 503)

    def test_datapath_get_ok(self):
        self.connect()
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        d = body['data']
        self.assertIn('tx_disable_mask', d)
        self.assertIn('dp_deinit_mask', d)
        self.assertIn('app_select', d)
        self.assertIn('lanes', d)

    def test_datapath_get_8_lanes(self):
        self.connect()
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        self.assertEqual(len(body['data']['lanes']), 8)

    def test_datapath_default_tx_enabled(self):
        """Mock default TX disable = 0 → all lanes tx_enable=True."""
        self.connect()
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        for lane in body['data']['lanes']:
            self.assertTrue(lane['tx_enable'],
                            f"Lane {lane['lane']} should have tx_enable=True")

    def test_datapath_app_select_default(self):
        """Mock default app_select = 1 for all lanes."""
        self.connect()
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        for v in body['data']['app_select']:
            self.assertEqual(v, 1)


# ============================================================
# 8. POST /api/module/datapath
# ============================================================

class TestDatapathSet(CMISTestCase):

    def test_datapath_set_not_connected(self):
        rv = self.client.post('/api/module/datapath',
                              data=json.dumps({'tx_disable_mask': 0}),
                              content_type='application/json')
        self.assertErr(rv, 503)

    def test_datapath_set_ok(self):
        self.connect()
        rv = self.client.post('/api/module/datapath',
                              data=json.dumps({
                                  'tx_disable_mask': 0xAA,
                                  'app_select': [1, 2, 1, 2, 1, 2, 1, 2],
                                  'apply': False
                              }),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertIn('message', body['data'])

    def test_datapath_set_then_get(self):
        """Write tx_disable_mask, then read back and verify."""
        self.connect()
        mask = 0xAA
        self.client.post('/api/module/datapath',
                         data=json.dumps({'tx_disable_mask': mask, 'apply': False}),
                         content_type='application/json')
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['tx_disable_mask'], mask)

    def test_datapath_set_apply(self):
        """apply=True should not cause an error."""
        self.connect()
        rv = self.client.post('/api/module/datapath',
                              data=json.dumps({
                                  'tx_disable_mask': 0,
                                  'app_select': [1] * 8,
                                  'apply': True
                              }),
                              content_type='application/json')
        self.assertOk(rv)

    def test_datapath_set_mask_clamp(self):
        """tx_disable_mask > 0xFF should be clamped to 8 bits."""
        self.connect()
        rv = self.client.post('/api/module/datapath',
                              data=json.dumps({'tx_disable_mask': 0x1FF}),
                              content_type='application/json')
        self.assertOk(rv)
        rv2 = self.client.get('/api/module/datapath')
        body = self.assertOk(rv2)
        # 0x1FF & 0xFF = 0xFF
        self.assertEqual(body['data']['tx_disable_mask'], 0xFF)

    def test_page10h_addresses_match_spec(self):
        """Pin the Page 10h control map to OIF CMIS 5.3 Tables 8-67/8-69/8-70/8-72.

        These addresses were previously off by several bytes, which on real
        hardware silently flipped Tx polarity instead of disabling Tx and
        triggered ApplyDPInit when writing the Rx polarity mask.
        """
        import cmis_registers as c
        self.assertEqual(c.REG_DP_DEINIT[:2],        (0x10, 0x80))  # 128
        self.assertEqual(c.REG_TX_POL_FLIP[:2],      (0x10, 0x81))  # 129
        self.assertEqual(c.REG_TX_OUTPUT_DIS[:2],    (0x10, 0x82))  # 130
        self.assertEqual(c.REG_TX_SQUELCH_DIS[:2],   (0x10, 0x83))  # 131
        self.assertEqual(c.REG_TX_FORCE_SQUELCH[:2], (0x10, 0x84))  # 132
        self.assertEqual(c.REG_RX_POL_FLIP[:2],      (0x10, 0x89))  # 137
        self.assertEqual(c.REG_RX_OUTPUT_DIS[:2],    (0x10, 0x8A))  # 138
        self.assertEqual(c.REG_RX_SQUELCH_DIS[:2],   (0x10, 0x8B))  # 139
        self.assertEqual(c.REG_APPLY_DATAPATH[:2],   (0x10, 0x8F))  # 143
        self.assertEqual(c.REG_APPLY_IMM[:2],        (0x10, 0x90))  # 144
        self.assertEqual(c.REG_APP_SELECT,           (0x10, 0x91, 8))  # 145-152

    def test_datapath_write_lands_on_spec_registers(self):
        """tx_disable must reach OutputDisableTx (130), not a polarity register."""
        self.connect()
        self.client.post('/api/module/datapath',
                         data=json.dumps({'tx_disable_mask': 0xA5,
                                          'tx_polarity_flip_mask': 0x0F,
                                          'rx_polarity_flip_mask': 0x33,
                                          'apply': False}),
                         content_type='application/json')
        page10 = _state['backend']._registers[0x10]
        self.assertEqual(page10[0x82], 0xA5)  # OutputDisableTx
        self.assertEqual(page10[0x81], 0x0F)  # InputPolarityFlipTx
        self.assertEqual(page10[0x89], 0x33)  # OutputPolarityFlipRx
        self.assertEqual(page10[0x8F], 0x00)  # ApplyDPInit must stay untouched

    def test_squelch_roundtrip_all_four_controls(self):
        self.connect()
        payload = {'tx_squelch_disable': 0x11, 'tx_squelch_force': 0x22,
                   'rx_output_disable': 0x33, 'rx_squelch_disable': 0x44}
        self.assertOk(self.client.post('/api/module/squelch',
                                       data=json.dumps(payload),
                                       content_type='application/json'))
        body = self.assertOk(self.client.get('/api/module/squelch'))
        for k, v in payload.items():
            self.assertEqual(body['data'][k], v, f'{k} did not round-trip')
        page10 = _state['backend']._registers[0x10]
        self.assertEqual(page10[0x83], 0x11)
        self.assertEqual(page10[0x84], 0x22)
        self.assertEqual(page10[0x8A], 0x33)
        self.assertEqual(page10[0x8B], 0x44)

    def test_datapath_set_empty_body(self):
        """Empty body should use defaults without crashing."""
        self.connect()
        rv = self.client.post('/api/module/datapath',
                              data=b'',
                              content_type='application/json')
        self.assertOk(rv)

    def test_datapath_set_app_select_roundtrip(self):
        """pack/unpack roundtrip for app_select values."""
        self.connect()
        app_sel = [1, 2, 3, 4, 1, 2, 3, 4]
        self.client.post('/api/module/datapath',
                         data=json.dumps({'tx_disable_mask': 0, 'app_select': app_sel}),
                         content_type='application/json')
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['app_select'], app_sel)


# ============================================================
# 9. POST /api/register/read
# ============================================================

class TestRegisterRead(CMISTestCase):

    def test_register_read_not_connected(self):
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0, 'length': 1}),
                              content_type='application/json')
        self.assertErr(rv, 503)

    def test_register_read_lower_page(self):
        """Read from lower page (address < 0x80)."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['data'], [0x1E])  # Module ID = QSFP-DD

    def test_register_read_upper_page(self):
        """Read from upper page (address >= 0x80)."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x81, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['data'], [0x4F])  # vendor name starts 'O' (OPENCMIS DEMO)

    def test_register_read_multiple_bytes(self):
        """Read multiple bytes at once."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x00, 'address': 0x81, 'length': 16}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['length'], 16)
        self.assertEqual(len(body['data']['data']), 16)
        # Vendor name field 00h:129-144, space padded
        text = bytes(body['data']['data']).decode('ascii').rstrip()
        self.assertEqual(text, 'OPENCMIS DEMO')

    def test_register_read_hex_string_address(self):
        """Address as hex string."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': '0x00', 'length': 1}),
                              content_type='application/json')
        self.assertOk(rv)

    def test_register_read_max_length(self):
        """Length = 128 (boundary, should pass)."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 128}),
                              content_type='application/json')
        self.assertOk(rv)

    def test_register_read_length_zero(self):
        """Length = 0 should return error."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 0}),
                              content_type='application/json')
        self.assertErr(rv, 400)

    def test_register_read_length_too_large(self):
        """Length > 128 should return error."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 129}),
                              content_type='application/json')
        self.assertErr(rv, 400)

    def test_register_read_response_fields(self):
        """Response must contain page, address, length, data, hex."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        d = body['data']
        for k in ('page', 'address', 'length', 'data', 'hex'):
            self.assertIn(k, d)

    def test_register_read_hex_format(self):
        """Hex field should be space-separated uppercase hex."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        hex_str = body['data']['hex']
        # Should match pattern like "1E"
        self.assertRegex(hex_str, r'^[0-9A-F]{2}( [0-9A-F]{2})*$')

    def test_register_read_negative_length(self):
        """Negative length should return error."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': -1}),
                              content_type='application/json')
        self.assertErr(rv, 400)


# ============================================================
# 10. POST /api/register/write
# ============================================================

class TestRegisterWrite(CMISTestCase):

    def test_register_write_not_connected(self):
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x81, 'data': [0x00]}),
                              content_type='application/json')
        self.assertErr(rv, 503)

    def test_register_write_ok(self):
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0x10, 'address': 0x81, 'data': [0xFF]}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['bytes_written'], 1)

    def test_register_write_read_roundtrip(self):
        """Write a value and read it back."""
        self.connect()
        self.client.post('/api/register/write',
                         data=json.dumps({'page': 0x10, 'address': 0x81, 'data': [0xAB]}),
                         content_type='application/json')
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x10, 'address': 0x81, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['data'], [0xAB])

    def test_register_write_no_data(self):
        """Missing data field should return error."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x80}),
                              content_type='application/json')
        self.assertErr(rv, 400)

    def test_register_write_empty_data(self):
        """Empty data list should return error."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x80, 'data': []}),
                              content_type='application/json')
        self.assertErr(rv, 400)

    def test_register_write_multi_byte(self):
        """Write multiple bytes."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0x10, 'address': 0x86, 'data': [0x11, 0x22, 0x33]}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['bytes_written'], 3)

    def test_register_write_byte_clamping(self):
        """Values > 255 should be clamped via & 0xFF."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0x10, 'address': 0x81, 'data': [0x1FF]}),
                              content_type='application/json')
        # Should succeed (0x1FF & 0xFF = 0xFF)
        body = self.assertOk(rv)
        self.assertEqual(body['data']['bytes_written'], 1)

    def test_register_write_lower_page(self):
        """Write to lower page address (< 0x80) should not set page."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x02, 'data': [0x01]}),
                              content_type='application/json')
        self.assertOk(rv)
        # Read it back
        rv2 = self.client.post('/api/register/read',
                               data=json.dumps({'page': 0, 'address': 0x02, 'length': 1}),
                               content_type='application/json')
        body = self.assertOk(rv2)
        self.assertEqual(body['data']['data'], [0x01])


# ============================================================
# 11. Boundary / edge cases
# ============================================================

class TestEdgeCases(CMISTestCase):

    def test_all_endpoints_require_connection(self):
        """All module endpoints should return 503 when not connected."""
        endpoints = [
            ('GET', '/api/module/info'),
            ('GET', '/api/module/status'),
            ('GET', '/api/module/monitoring'),
            ('GET', '/api/module/datapath'),
        ]
        for method, path in endpoints:
            rv = self.client.open(path, method=method)
            self.assertEqual(rv.status_code, 503,
                             f"{method} {path} should be 503 when disconnected")

    def test_post_endpoints_require_connection(self):
        for path, payload in [
            ('/api/module/datapath', {'tx_disable_mask': 0}),
            ('/api/register/read', {'page': 0, 'address': 0, 'length': 1}),
            ('/api/register/write', {'page': 0, 'address': 0x80, 'data': [0]}),
        ]:
            rv = self.client.post(path, data=json.dumps(payload),
                                  content_type='application/json')
            self.assertEqual(rv.status_code, 503,
                             f"POST {path} should be 503 when disconnected")

    def test_connect_then_disconnect_then_reconnect(self):
        """Full lifecycle should work cleanly."""
        self.connect()
        self.assertTrue(_state['connected'])
        self.client.get('/api/disconnect')
        self.assertFalse(_state['connected'])
        self.connect()
        self.assertTrue(_state['connected'])

    def test_module_info_after_reconnect(self):
        """After reconnect, module info should still work."""
        self.connect()
        self.client.get('/api/disconnect')
        self.connect()
        rv = self.client.get('/api/module/info')
        self.assertOk(rv)

    def test_register_read_address_boundary_0x7F(self):
        """Address 0x7F is < 0x80, so reads from lower page."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x7F, 'length': 1}),
                              content_type='application/json')
        self.assertOk(rv)

    def test_register_read_address_boundary_0x80(self):
        """Address 0x80 is >= 0x80, so reads from upper page."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x11, 'address': 0x80, 'length': 1}),
                              content_type='application/json')
        self.assertOk(rv)

    def test_connect_missing_json_content_type(self):
        """POST without Content-Type: json should still work (force=True)."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8'}))
        self.assertOk(rv)

    def test_monitoring_after_datapath_write(self):
        """Monitoring should remain functional after datapath write."""
        self.connect()
        self.client.post('/api/module/datapath',
                         data=json.dumps({'tx_disable_mask': 0xFF}),
                         content_type='application/json')
        rv = self.client.get('/api/module/monitoring')
        self.assertOk(rv)

    def test_status_after_multiple_reads(self):
        """Multiple status reads should all succeed."""
        self.connect()
        for _ in range(5):
            rv = self.client.get('/api/module/status')
            self.assertOk(rv)


# ============================================================
# 12. cmis_registers unit tests
# ============================================================

class TestCmisRegisters(unittest.TestCase):

    def test_parse_temperature_positive(self):
        from cmis_registers import parse_temperature
        raw = struct.pack(">h", int(45.0 * 256))
        self.assertAlmostEqual(parse_temperature(raw), 45.0, places=3)

    def test_parse_temperature_negative(self):
        from cmis_registers import parse_temperature
        raw = struct.pack(">h", int(-10.0 * 256))
        self.assertAlmostEqual(parse_temperature(raw), -10.0, places=3)

    def test_parse_voltage(self):
        from cmis_registers import parse_voltage
        # 3.3V → 33000
        raw = struct.pack(">H", 33000)
        self.assertAlmostEqual(parse_voltage(raw), 3.3, places=4)

    def test_parse_power_uw(self):
        from cmis_registers import parse_power_uw
        # 5000 * 0.1 = 500µW
        raw = struct.pack(">H", 5000)
        self.assertAlmostEqual(parse_power_uw(raw), 500.0, places=3)

    def test_uw_to_dbm_zero(self):
        from cmis_registers import uw_to_dbm
        self.assertEqual(uw_to_dbm(0), -40.0)

    def test_uw_to_dbm_negative(self):
        from cmis_registers import uw_to_dbm
        self.assertEqual(uw_to_dbm(-1), -40.0)

    def test_uw_to_dbm_1000uw_is_0dbm(self):
        from cmis_registers import uw_to_dbm
        self.assertAlmostEqual(uw_to_dbm(1000.0), 0.0, places=5)

    def test_parse_tx_bias_ma(self):
        from cmis_registers import parse_tx_bias_ma
        # 17500 * 0.002 = 35mA
        raw = struct.pack(">H", 17500)
        self.assertAlmostEqual(parse_tx_bias_ma(raw), 35.0, places=3)

    def test_parse_ascii(self):
        from cmis_registers import parse_ascii
        raw = b"INNOLIGHT       "
        self.assertEqual(parse_ascii(raw), "INNOLIGHT")

    def test_parse_ascii_null_terminated(self):
        from cmis_registers import parse_ascii
        raw = b"TEST\x00\x00\x00"
        self.assertEqual(parse_ascii(raw), "TEST")

    def test_parse_dp_states_all_activated(self):
        from cmis_registers import parse_dp_states
        # 4 bits/lane, 2 lanes/byte; 0x4 = DPActivated
        raw = bytes([0x44] * 4)
        states = parse_dp_states(raw)
        self.assertEqual(len(states), 8)
        self.assertTrue(all(s == 'Activated' for s in states))

    def test_parse_dp_states_all_deactivated(self):
        from cmis_registers import parse_dp_states
        # 0x1 = DPDeactivated
        raw = bytes([0x11] * 4)
        states = parse_dp_states(raw)
        self.assertTrue(all(s == 'Deactivated' for s in states))

    def test_unpack_appselect_all_ones(self):
        from cmis_registers import unpack_appselect
        # 1 byte/lane, AppSel in bits[7:4]: 0x11 → AppSel 1
        data = bytes([0x11] * 8)
        lanes = unpack_appselect(data)
        self.assertEqual(lanes, [1, 1, 1, 1, 1, 1, 1, 1])

    def test_pack_unpack_appselect_roundtrip(self):
        from cmis_registers import pack_appselect, unpack_appselect
        original = [1, 2, 3, 4, 5, 6, 7, 8]
        packed = pack_appselect(original)
        # pack only stores 4 bits per lane (nibble), so values must be 0-15
        unpacked = unpack_appselect(packed)
        self.assertEqual(unpacked, [v & 0x0F for v in original])

    def test_module_id_name_known(self):
        from cmis_registers import module_id_name
        self.assertEqual(module_id_name(0x1E), "QSFP-DD CMIS")

    def test_module_id_name_unknown(self):
        from cmis_registers import module_id_name
        result = module_id_name(0xFF)
        self.assertIn('Unknown', result)

    def test_cmis_revision_str(self):
        from cmis_registers import cmis_revision_str
        self.assertEqual(cmis_revision_str(0x50), '5.0')
        self.assertEqual(cmis_revision_str(0x53), '5.3')

    def test_pack_appselect_length(self):
        from cmis_registers import pack_appselect
        # Always 8 bytes (one per lane, zero-padded)
        result = pack_appselect([1] * 8)
        self.assertEqual(len(result), 8)

    def test_pack_appselect_nibble_boundary(self):
        """Values > 15 should be masked to nibble."""
        from cmis_registers import pack_appselect, unpack_appselect
        packed = pack_appselect([0xFF] * 8)
        unpacked = unpack_appselect(packed)
        # 0xFF & 0x0F = 0xF = 15
        self.assertTrue(all(v == 15 for v in unpacked))


# ============================================================
# Run
# ============================================================

if __name__ == '__main__':
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]))
    print(f"\n{'='*60}")
    print(f"Tests run: {result.testsRun}")
    print(f"Failures:  {len(result.failures)}")
    print(f"Errors:    {len(result.errors)}")
    if result.failures:
        print("\nFAILURES:")
        for test, tb in result.failures:
            print(f"  {test}: {tb.splitlines()[-1]}")
    if result.errors:
        print("\nERRORS:")
        for test, tb in result.errors:
            print(f"  {test}: {tb.splitlines()[-1]}")
