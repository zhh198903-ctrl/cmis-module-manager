"""Comprehensive API tests for CMIS optical module management tool."""
import os
import re
import sys
import json
import math
import struct
import time
import shutil
import tempfile
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
import updater as updater_module
from app import app, _state

# Shape of a real release-asset URL. The updater refuses anything that is not
# https on a GitHub host, so fixtures have to look genuine or they would pass
# for the wrong reason.
_GH_URL = ('https://github.com/zhh198903-ctrl/cmis-module-manager/releases/'
           'download/v2.1.0/CMIS_dist_v2_1_0.zip')


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


def poke(page, addr, value):
    """Write a register behind the API's back without desynchronising it.

    Writing 0x7E directly leaves app._set_page believing the module is still
    on whatever page it last selected, so the next read silently lands on the
    wrong page and returns plausible nonsense - the exact hazard the project
    documents for real hardware, reproduced inside the test suite.
    """
    app_module._set_page(page)
    _state['backend'].write_bytes(addr, bytes([value]))
    app_module._invalidate_page()


def deactivated(client, lanes=0xFF):
    """Stop the Data Paths so a reconfiguration is legal.

    6.2.4.3 allows freeing a lane, or moving a Data Path to an Application of
    a different width, "only while in the DPDeactivated state". Every shipped
    profile advertises Applications of differing widths, so on these modules
    any change of Application needs this first - which is why so many tests
    below take the path down before they reconfigure it. Doing it in one Apply
    earns ConfigRejectedLanesInUse, exactly as a real module answers.
    """
    rv = client.post('/api/module/datapath',
                     data=json.dumps({'dp_deinit_mask': lanes, 'apply': True}),
                     content_type='application/json')
    assert rv.status_code == 200, rv.data
    time.sleep(0.5)


def reconfigure(client, app_select, **body):
    """The two-step procedure: stop the path, then stage the new Application
    and release the hold in one Apply."""
    deactivated(client)
    body = dict(body, app_select=app_select, dp_deinit_mask=0x00, apply=True)
    rv = client.post('/api/module/datapath', data=json.dumps(body),
                     content_type='application/json')
    assert rv.status_code == 200, rv.data
    return rv


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
        # The condition, not the call: the helper gained a lane argument, and
        # asserting its old spelling proved nothing about the colouring.
        body = js[js.index('function _powerLimits('):]
        body = body[:body.index(chr(10) + '}')]
        self.assertRegex(body, r'num\(t\.tx_power_low_alarm_dbm',
                         'the module-wide low alarm is not consulted')
        self.assertRegex(body, r'num\(t\.rx_power_high_alarm_dbm',
                         'the module-wide high alarm is not consulted')
        self.assertRegex(js, r'const lim = _powerLimits\(',
                         'the monitoring row computes no limits at all')
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

    def test_zero_ber_is_not_dressed_up_as_a_measured_bound(self):
        """A zero F16 word is a zero count. Rendering it as "< 1e-15" invented
        a figure with no basis - the format bottoms out at 1e-24 - and that is
        the sort of number that gets quoted in a test report."""
        js = self._js()
        self.assertNotIn('1e-15', js)
        body = js.split('function formatBer(')[1].split('\n}')[0]
        self.assertIn('not a', body, 'the zero case is unexplained')

    def test_reconnect_clears_every_panel(self):
        """Checkbox cells and the summary line are not plain tables, so a
        sweep of table bodies leaves the previous module's squelch settings and
        temperature on screen."""
        js = self._js()
        body = js.split('function clearTabContent(')[1].split('\n}\n')[0]
        for marker in ("'sq', 'sf', 'od', 'rd'", "'mso', 'msi', 'hso', 'hsi'",
                       'monitor-summary'):
            self.assertIn(marker, body, f'{marker} survives a reconnect')

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
                {'name': 'source.zip',
                 'browser_download_url': 'https://github.com/o/r/a/source.zip'},
                {'name': 'CMIS_dist_v2_1_0.zip', 'size': 123,
                 'digest': 'sha256:ABC',
                 'browser_download_url': _GH_URL},
            ],
        })
        self.assertEqual(rel['version'], '2.1.0')
        self.assertEqual(rel['asset_name'], 'CMIS_dist_v2_1_0.zip')
        self.assertEqual(rel['asset_url'], _GH_URL)
        self.assertEqual(rel['sha256'], 'abc')

    def test_release_without_our_asset_is_rejected(self):
        """A release carrying only source tarballs must not look installable."""
        import updater as u
        # A GitHub-hosted URL, so this can only be rejected on the asset name.
        self.assertIsNone(u.normalize_release(
            {'tag_name': 'v9.9.9', 'assets': [{'name': 'notes.txt',
                                               'browser_download_url': _GH_URL}]}))
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

    def _download_harness(self, responses):
        """Drive download_asset against scripted responses, no network.

        Returns (fake_open, requests) - requests records each urllib Request so
        a test can assert which byte range was actually asked for.
        """
        import io as _io
        requests = []

        class _Resp:
            def __init__(self, body, code=200, content_length=None):
                self._buf = _io.BytesIO(body)
                self._code = code
                n = len(body) if content_length is None else content_length
                self.headers = {'Content-Length': str(n)}

            def getcode(self):
                return self._code

            def read(self, n=-1):
                return self._buf.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        scripted = list(responses)

        def fake_open(req, timeout):
            requests.append(req)
            item = scripted.pop(0)
            if isinstance(item, Exception):
                raise item
            return _Resp(*item)

        return fake_open, requests

    def _with_fake_open(self, fake_open):
        import updater as u
        original = u._open
        u._open = fake_open
        self.addCleanup(lambda: setattr(u, '_open', original))

    def test_a_dropped_connection_resumes_instead_of_starting_over(self):
        """Restarting from zero can never finish on a link that drops more
        often than a full download takes - which is what a 16 MB asset over a
        slow proxy looks like."""
        import tempfile
        import updater as u
        payload = bytes(range(256)) * 8          # 2048 bytes
        fake, reqs = self._download_harness([
            (payload[:800], 200, len(payload)),  # closes early
            (payload[800:], 206, None),          # honours Range
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset(_GH_URL, dest, total_hint=len(payload), retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
            self.assertFalse(os.path.exists(dest + '.part'))
        self.assertIsNone(reqs[0].get_header('Range'))
        self.assertEqual(reqs[1].get_header('Range'), 'bytes=800-',
                         'the retry must ask only for the missing bytes')

    def test_a_server_ignoring_range_restarts_rather_than_corrupting(self):
        """Appending a full body onto a partial file would produce a plausible
        archive of the wrong length; the checksum would catch it, but only
        after another full download."""
        import tempfile
        import updater as u
        payload = bytes(range(256)) * 8
        fake, _ = self._download_harness([
            (payload[:800], 200, len(payload)),
            (payload, 200, None),                # ignores Range, sends it all
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset(_GH_URL, dest, total_hint=len(payload), retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)

    def test_a_download_that_gets_nowhere_gives_up_and_leaves_nothing(self):
        """`attempts` bounds consecutive attempts that add nothing. A .part
        left next to the staged files would be mistaken for the asset."""
        import tempfile
        import updater as u
        fake, reqs = self._download_harness([IOError('refused')] * 3)
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            with self.assertRaises(Exception):
                u.download_asset(_GH_URL, dest, total_hint=2048,
                                 attempts=3, retry_wait=0)
            self.assertFalse(os.path.exists(dest))
            self.assertFalse(os.path.exists(dest + '.part'))
        self.assertEqual(len(reqs), 3, 'it must stop after that many dead rounds')

    def test_a_download_still_inching_forward_is_not_given_up_on(self):
        """A real 16 MB download here ran 44 minutes and died at 58% with six
        attempts spent - every one of which had transferred megabytes. The
        budget was protecting against "slow"; the only thing worth abandoning
        is a transfer that has stopped moving.
        """
        import tempfile
        import updater as u
        payload = bytes(range(250)) * 4          # 1000 bytes
        steps = [(payload[:200], 200, len(payload))]
        for a in range(200, 1000, 200):
            steps.append((payload[a:a + 200], 206, None))
        fake, reqs = self._download_harness(steps)
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset(_GH_URL, dest, total_hint=len(payload),
                             attempts=2, retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
        self.assertEqual(len(reqs), 5, 'each partial delivery must buy another round')

    def test_a_source_dribbling_forever_still_hits_a_ceiling(self):
        """Progress resetting the budget must not become "never give up"."""
        import tempfile
        import updater as u
        fake, reqs = self._download_harness([(b'x', 200, 10_000)] +
                                            [(b'x', 206, None)] * 40)
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            with self.assertRaises(Exception):
                u.download_asset(_GH_URL, dest, total_hint=10_000,
                                 attempts=3, retry_wait=0, max_rounds=6)
        self.assertEqual(len(reqs), 6, 'max_rounds is the backstop')

    def test_a_definitive_http_error_is_not_retried(self):
        """A 404, or the handler's refusal to follow a redirect off GitHub,
        will not fix itself - retrying only delays the error."""
        import tempfile
        import urllib.error
        import updater as u
        err = urllib.error.HTTPError(_GH_URL, 404, 'Not Found', {}, None)
        fake, reqs = self._download_harness([err, err, err])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(urllib.error.HTTPError):
                u.download_asset(_GH_URL, os.path.join(tmp, 'a.zip'),
                                 attempts=3, retry_wait=0)
        self.assertEqual(len(reqs), 1)

    def test_the_mirror_may_carry_bytes_but_never_metadata(self):
        """The download site publishes no version list and no digest, so it is
        never asked what the newest release is - only for a copy of an asset
        GitHub already named and hashed. That split is what makes plain http
        acceptable for it."""
        import updater as u
        mirror = u.mirror_url('2.2.3', 'CMIS_dist_v2_2_3.zip')
        self.assertTrue(u.is_allowed_source(mirror))
        self.assertFalse(u.is_trusted_url(mirror),
                         'the mirror must not pass the metadata check')
        self.assertIsNone(u.normalize_release({
            'tag_name': 'v2.2.3',
            'assets': [{'name': 'CMIS_dist_v2_2_3.zip', 'size': 1,
                        'browser_download_url': mirror}],
        }), 'a release pointing at the mirror is not installable')

    def test_widening_the_source_check_did_not_open_it_to_anyone(self):
        import updater as u
        for bad in ('http://attacker.example/p.zip',
                    'https://attacker.example/p.zip',
                    'http://106.14.76.130.evil.example/p.zip',
                    'https://106.14.76.130.evil.example/p.zip'):
            self.assertFalse(u.is_allowed_source(bad), bad)
        with self.assertRaises(ValueError):
            u.download_asset(['http://attacker.example/p.zip'], 'unused.zip')

    def test_the_faster_source_is_tried_first(self):
        """Picking wrong costs minutes on a 16 MB asset, and which one wins
        changes by the hour, so it is measured rather than assumed."""
        import updater as u
        slow = _GH_URL
        fast = u.mirror_url('9.9.9', 'CMIS_dist_v9_9_9.zip')
        fake, _ = self._download_harness([(b'x' * 200, 206, None),
                                          (b'y' * 400000, 206, None)])
        self._with_fake_open(fake)
        ranked = u.order_sources([slow, fast], seconds=0.05)
        self.assertEqual([url for url, _ in ranked], [fast, slow])
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_an_unreachable_source_scores_zero_but_is_still_offered(self):
        """Four seconds of silence is not proof the host is gone, and refusing
        to try it would strand the update when both probes happen to fail."""
        import updater as u
        fake, _ = self._download_harness([IOError('refused'), IOError('refused')])
        self._with_fake_open(fake)
        ranked = u.order_sources([_GH_URL, u.mirror_url('9.9.9', 'a.zip')],
                                 seconds=0.05)
        self.assertEqual(len(ranked), 2)
        self.assertEqual([rate for _, rate in ranked], [0.0, 0.0])

    def test_a_source_that_does_not_carry_the_release_falls_back(self):
        """The mirror is filled by hand, so it routinely lags a release by
        hours - a 404 there must not abort an update GitHub can serve."""
        import tempfile
        import urllib.error
        import updater as u
        payload = b'p' * 1500
        mirror = u.mirror_url('9.9.9', 'CMIS_dist_v9_9_9.zip')
        fake, reqs = self._download_harness([
            urllib.error.HTTPError(mirror, 404, 'Not Found', {}, None),
            (payload, 200, None),
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset([mirror, _GH_URL], dest,
                             total_hint=len(payload), attempts=3, retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
        self.assertEqual(reqs[0].full_url, mirror)
        self.assertEqual(reqs[1].full_url, _GH_URL, 'it must switch source')

    def test_switching_source_keeps_the_bytes_already_fetched(self):
        """Every mirror serves the identical asset - that is what the shared
        SHA-256 asserts - so a dead source's progress is still good."""
        import tempfile
        import updater as u
        payload = bytes(range(256)) * 8
        mirror = u.mirror_url('9.9.9', 'CMIS_dist_v9_9_9.zip')
        fake, reqs = self._download_harness([
            (payload[:700], 200, len(payload)),   # dies partway
            (payload[700:], 206, None),           # other source finishes it
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset([mirror, _GH_URL], dest,
                             total_hint=len(payload), attempts=3, retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
        self.assertEqual(reqs[1].full_url, _GH_URL)
        self.assertEqual(reqs[1].get_header('Range'), 'bytes=700-',
                         'the second source must continue, not restart')

    def test_a_truncated_mirror_cannot_end_the_download_early(self):
        """The size came from the release metadata over TLS. A mirror holding
        a half-uploaded copy must not be able to talk the download into
        calling that complete."""
        import tempfile
        import updater as u
        payload = b'w' * 2000
        mirror = u.mirror_url('9.9.9', 'CMIS_dist_v9_9_9.zip')
        fake, _ = self._download_harness([
            (payload[:500], 200, 500),        # mirror claims the asset is 500 B
            (payload[500:], 206, None),
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset([mirror, _GH_URL], dest,
                             total_hint=len(payload), attempts=3, retry_wait=0)
            self.assertEqual(os.path.getsize(dest), len(payload))

    def test_a_kept_partial_lets_a_later_run_carry_on(self):
        """Retries alone still lose everything once they run out. On a link
        that drops this often, the bytes already fetched are the only thing
        that makes the next attempt shorter than the last."""
        import tempfile
        import updater as u
        payload = bytes(range(256)) * 8
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            part = os.path.join(tmp, 'keep', 'a.zip.part')

            fake, _ = self._download_harness([(payload[:900], 200, len(payload))])
            self._with_fake_open(fake)
            with self.assertRaises(Exception):
                u.download_asset(_GH_URL, dest, total_hint=len(payload),
                                 attempts=1, retry_wait=0,
                                 part_path=part, keep_partial=True)
            self.assertTrue(os.path.exists(part), 'the progress must survive')
            self.assertEqual(os.path.getsize(part), 900)

            fake2, reqs2 = self._download_harness([(payload[900:], 206, None)])
            self._with_fake_open(fake2)
            u.download_asset(_GH_URL, dest, total_hint=len(payload),
                             attempts=1, retry_wait=0,
                             part_path=part, keep_partial=True)
            self.assertEqual(reqs2[0].get_header('Range'), 'bytes=900-')
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
            self.assertFalse(os.path.exists(part))

    def test_a_partial_longer_than_the_asset_is_thrown_away(self):
        """A release rebuilt under the same name leaves a partial that is not
        a prefix of anything; resuming past it would fail the checksum on
        every future attempt, which is a loop the user cannot get out of."""
        import tempfile
        import updater as u
        payload = b'z' * 1000
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            part = os.path.join(tmp, 'a.zip.part')
            with open(part, 'wb') as fh:
                fh.write(b'Q' * 4000)
            fake, reqs = self._download_harness([(payload, 200, None)])
            self._with_fake_open(fake)
            u.download_asset(_GH_URL, dest, total_hint=len(payload),
                             attempts=2, retry_wait=0, part_path=part)
            self.assertIsNone(reqs[0].get_header('Range'),
                              'it must not resume from a bogus offset')
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)

    def test_partials_for_other_versions_are_dropped(self):
        """Otherwise every abandoned upgrade parks 16 MB on disk for good."""
        import tempfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            original = u.download_dir
            u.download_dir = lambda: tmp
            self.addCleanup(lambda: setattr(u, 'download_dir', original))
            for name in ('CMIS_dist_v2_2_1.zip.part', 'CMIS_dist_v2_2_2.zip.part'):
                open(os.path.join(tmp, name), 'wb').close()
            removed = u.discard_stale_partials('CMIS_dist_v2_2_2.zip')
            self.assertEqual(removed, ['CMIS_dist_v2_2_1.zip.part'])
            self.assertEqual(os.listdir(tmp), ['CMIS_dist_v2_2_2.zip.part'])

    def test_the_partial_is_not_kept_where_staging_gets_wiped(self):
        """api_update_apply rmtree's the staging directory before every
        download; a partial in there could never survive to be resumed."""
        import updater as u
        staged = os.path.normcase(os.path.abspath(u.staging_dir()))
        part = os.path.normcase(os.path.abspath(u.partial_path('CMIS_dist_v9_9_9.zip')))
        self.assertFalse(part.startswith(staged + os.sep))

    def test_a_wrapped_payload_still_stages_flat(self):
        """The layout v2.1.0 and v2.2.0 actually shipped.

        Those zips hold a CMIS2Customer/ folder rather than the four files.
        Staging kept the folder, so the swap helper found no exe where it
        looks, retried for 75 s and quit before ever relaunching - the update
        silently did nothing on every real upgrade into those two versions.
        """
        import tempfile, zipfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, 'wrapped.zip')
            with zipfile.ZipFile(zp, 'w') as zf:
                zf.writestr('CMIS2Customer/' + u.EXE_NAME, b'exe')
                zf.writestr('CMIS2Customer/manual.html', b'doc')
                zf.writestr('CMIS2Customer/qrcode.jpg', b'img')
            out = os.path.join(tmp, 'out')
            names = u.extract_payload(zp, out)
            self.assertCountEqual(names, [u.EXE_NAME, 'manual.html', 'qrcode.jpg'])
            self.assertTrue(os.path.isfile(os.path.join(out, u.EXE_NAME)),
                            'the helper looks for the exe in the staging root')
            self.assertFalse(os.path.isdir(os.path.join(out, 'CMIS2Customer')))

    def test_extract_refuses_a_payload_it_cannot_stage_flat(self):
        """Failing loudly beats the 75 s silent stall the helper used to hit."""
        import tempfile, zipfile
        import updater as u
        for label, entries in [
            ('two levels deep', ['a/b/' + u.EXE_NAME, 'a/b/manual.html']),
            ('only some nested', [u.EXE_NAME, 'sub/manual.html']),
            ('same name twice', ['a/' + u.EXE_NAME, 'b/' + u.EXE_NAME]),
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                zp = os.path.join(tmp, 'odd.zip')
                with zipfile.ZipFile(zp, 'w') as zf:
                    for n in entries:
                        zf.writestr(n, b'x')
                with self.assertRaises(ValueError, msg=label):
                    u.extract_payload(zp, os.path.join(tmp, 'out'))

    def test_an_unverifiable_download_is_refused_like_a_failed_one(self):
        """This test used to assert the opposite, and its old name said so.

        Accepting a download with no digest to check against meant an attacker
        who could strip one field got an unverified install with nothing shown
        to the user - while the manual told that user the tool refuses exactly
        this case. Every release asset this project has published carries a
        digest, so nothing that works today is refused.
        """
        import tempfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'f.bin')
            with open(p, 'wb') as fh:
                fh.write(b'hello')
            digest = '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
            self.assertFalse(u.verify_sha256(p, None), 'no digest must not pass')
            self.assertFalse(u.verify_sha256(p, ''), 'empty digest must not pass')
            self.assertFalse(u.verify_sha256(p, '00' * 32))
            self.assertTrue(u.verify_sha256(p, digest), 'a real match must pass')

    def test_swap_script_is_bounded_and_quotes_paths(self):
        """Unbounded waits would leave a hidden PowerShell spinning forever."""
        import updater as u
        ps = u.build_swap_script(r'C:\stage dir', r'C:\install dir')
        self.assertIn('-lt 150', ps, 'the unlock wait is unbounded')
        self.assertIn('-le 2', ps, 'the relaunch is retried without limit')
        self.assertIn('-lt 15', ps, 'the health wait is unbounded')
        # Single-quoted, so a path with spaces survives and one containing
        # $(...) is not evaluated - see test_install_path_cannot_inject_powershell.
        self.assertIn(r"'C:\install dir\CMIS_Module_Manager.exe'", ps)

    def test_swap_script_verifies_the_app_came_back(self):
        """Moving the files is not the same as the app running again; the
        helper must check rather than assume, and retry when it has not."""
        import updater as u
        ps = u.build_swap_script(r'C:\s', r'C:\t')
        self.assertIn('Invoke-WebRequest', ps)
        self.assertIn(u.HEALTH_URL, ps)

    def test_relaunch_avoids_shellexecute(self):
        """Start-Process goes through ShellExecute, which needs a usable window
        station. Spawned from an exiting console app it created nothing while
        reporting no error, so the app never came back."""
        import updater as u
        ps = u.build_swap_script(r'C:\s', r'C:\t')
        self.assertIn('[System.Diagnostics.Process]::Start', ps)
        self.assertIn('UseShellExecute = $false', ps)
        self.assertNotIn('Start-Process', ps)

    def test_swap_script_leaves_a_log(self):
        """A failed update is otherwise invisible - the app is simply gone."""
        import updater as u
        ps = u.build_swap_script(r'C:\s', r'C:\t')
        self.assertIn(r'C:\t\update.log', ps)
        for moment in ('update helper started', 'executable replaced',
                       'relaunch attempt', 'started pid',
                       'the files are updated'):
            self.assertIn(moment, ps, f'the log never records "{moment}"')

    def test_relaunch_suppresses_the_browser(self):
        """The user is already looking at a page that reloads itself; opening
        another tab on every update is how tabs pile up."""
        import updater as u
        self.assertIn('CMIS_NO_BROWSER', u.build_swap_script(r'C:\s', r'C:\t'))

    def test_no_relaunch_leaves_the_health_check_out(self):
        import updater as u
        ps = u.build_swap_script(r'C:\s', r'C:\t', relaunch=False)
        self.assertNotIn('Start-Process', ps)

    def test_helper_keeps_a_console_so_it_can_relaunch(self):
        """Regression, found by actually upgrading a real 2.0.1 build.

        With DETACHED_PROCESS the helper has no console, so its `start` cannot
        allocate one for the console-mode exe: the files swapped correctly and
        the app simply never came back. Nothing errors, so only this assertion
        catches a reintroduction.
        """
        import io
        import updater as u
        self.assertFalse(hasattr(u, '_DETACHED_PROCESS'),
                         'DETACHED_PROCESS leaves the helper unable to relaunch')
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'updater.py')
        src = io.open(path, encoding='utf-8').read()
        body = src.split('def stage_and_swap(')[1]
        self.assertIn('creationflags=_CREATE_NO_WINDOW', body)
        self.assertNotIn('0x00000008', body)

    def test_self_update_refused_when_running_from_source(self):
        import updater as u
        self.assertFalse(u.is_frozen(), 'tests should not run frozen')
        with self.assertRaises(RuntimeError):
            u.stage_and_swap('anywhere')


class TestUpdateRoutes(CMISTestCase):

    def test_nothing_checks_or_updates_without_a_click(self):
        """The tool is used on isolated lab networks and on modules that are
        mid-measurement. It must never reach out to GitHub, and must never
        replace itself, unless the operator asked for it.
        """
        import io
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'static', 'app.js')
        js = io.open(js_path, encoding='utf-8').read()

        # checkForUpdate may only be reachable from the button.
        callers = [ln.strip() for ln in js.splitlines()
                   if 'checkForUpdate' in ln and 'function checkForUpdate' not in ln]
        self.assertEqual(len(callers), 1, f'unexpected callers: {callers}')
        self.assertIn("getElementById('btn-check-update')", callers[0])

        # No timer or load hook may drive it, and apply is only ever called
        # from inside checkForUpdate, after the user confirms.
        for pattern in ('setInterval(checkForUpdate', 'setTimeout(checkForUpdate',
                        "addEventListener('load'", 'checkForUpdate()'):
            if pattern == 'checkForUpdate()':
                continue
            self.assertNotIn(pattern, js, f'{pattern} would update unprompted')
        body = js.split('async function checkForUpdate(')[1].split('\n}')[0]
        self.assertIn('confirm(', body, 'apply runs without asking the user')
        self.assertEqual(js.count("apiPost('/api/update/apply'"), 1)

    def test_completion_screen_says_what_to_do_immediately(self):
        """Spinning first and only then admitting a restart is needed reads as
        a hang. The files are already in place by that point, so the
        instruction comes first and the reconnect poll runs behind it.
        """
        import io
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'static', 'app.js')
        js = io.open(js_path, encoding='utf-8').read()
        body = js.split('async function _waitForNewVersion(')[1].split('\n}\n')[0]
        instruction = body.index('CMIS_Module_Manager.exe')
        poll = body.index("fetch('/api/version'")
        self.assertLess(instruction, poll,
                        'the restart instruction is shown only after polling')
        self.assertIn('update.log', body, 'no pointer to the update record')

    def test_apply_refused_from_source(self):
        rv = self.client.post('/api/update/apply')
        self.assertErr(rv, 400)
        self.assertIn('git pull', json.loads(rv.data)['message'])

    def _arm_fake_update(self, download):
        """Point the update machinery at temp dirs and a scripted download.

        Never let a test reach the end of _run_update: it hands over to the
        swap helper and then os._exit()s the process, which here is the test
        runner. Every case below ends in the error branch.
        """
        import tempfile
        import updater as u
        tmp = tempfile.mkdtemp()
        rel = {'version': '9.9.9', 'asset_name': 'CMIS_dist_v9_9_9.zip',
               'asset_url': _GH_URL, 'asset_size': 10240, 'sha256': 'ab' * 32,
               'tag': 'v9.9.9', 'html_url': '', 'notes': '', 'published_at': ''}
        saved = {name: getattr(u, name) for name in
                 ('is_frozen', 'fetch_latest_release', 'order_sources',
                  'discard_stale_partials', 'staging_dir', 'partial_path',
                  'download_asset')}
        u.is_frozen = lambda: True
        u.fetch_latest_release = lambda *a, **k: dict(rel)
        u.order_sources = lambda urls, **k: [(urls[0], 1.0)]
        u.discard_stale_partials = lambda name: []
        u.staging_dir = lambda: os.path.join(tmp, '_cmis_update')
        u.partial_path = lambda name: os.path.join(tmp, 'parts', name + '.part')
        u.download_asset = download

        def restore():
            for name, fn in saved.items():
                setattr(u, name, fn)
            app_module._update.update(state='idle', version='', done=0,
                                      total=0, source='', message='')
        self.addCleanup(restore)
        return rel

    def _await_update_state(self, wanted, timeout=5.0):
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            if app_module._update['state'] in wanted:
                return app_module._update['state']
            _t.sleep(0.02)
        return app_module._update['state']

    def test_apply_returns_at_once_and_reports_progress_separately(self):
        """Downloading inside the request froze every other endpoint for as
        long as the transfer took - forty minutes on a slow link, with nothing
        on screen. The request loop has to stay free to answer the poll."""
        import threading as _th
        gate = _th.Event()

        def download(urls, dest, progress_cb=None, **kw):
            progress_cb(4096, 10240)
            gate.wait(5)
            raise IOError('link died')

        self.addCleanup(gate.set)
        self._arm_fake_update(download)

        rv = self.client.post('/api/update/apply')
        body = self.assertOk(rv)['data']
        self.assertEqual(body['version'], '9.9.9')

        self.assertEqual(self._await_update_state(('downloading',)), 'downloading')
        prog = self.assertOk(self.client.get('/api/update/progress'))['data']
        self.assertEqual(prog['state'], 'downloading')
        self.assertEqual((prog['done'], prog['total']), (4096, 10240))

        gate.set()
        self.assertEqual(self._await_update_state(('error',)), 'error')
        prog = self.assertOk(self.client.get('/api/update/progress'))['data']
        self.assertIn('link died', prog['message'])

    def test_a_second_apply_is_refused_while_one_is_running(self):
        """Two downloads into one staging directory would delete each other's
        files halfway through."""
        import threading as _th
        gate = _th.Event()

        def download(urls, dest, progress_cb=None, **kw):
            progress_cb(1, 10240)
            gate.wait(5)
            raise IOError('stopped')

        self.addCleanup(gate.set)
        self._arm_fake_update(download)
        self.assertOk(self.client.post('/api/update/apply'))
        self._await_update_state(('downloading',))
        self.assertErr(self.client.post('/api/update/apply'), 409)
        gate.set()

    def test_the_page_puts_the_download_percentage_on_screen(self):
        """A number moving is the difference between "slow" and "hung", and
        the user's response to the second one is to kill the tool."""
        import io
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'static', 'app.js')
        js = io.open(js_path, encoding='utf-8').read()
        self.assertIn("fetch('/api/update/progress'", js)
        body = js.split('async function _followUpdateProgress(')[1].split('\n}\n')[0]
        self.assertIn('done / p.total', body.replace('p.done', 'done'))
        self.assertIn('%', body)

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


class TestMultiBankLanes(CMISTestCase):
    """Modules wider than eight lanes, which CMIS 5.4 raised the ceiling for.

    Verified against the mock only - no 16-lane hardware was available - so
    what these pin is the lane arithmetic and the bank switching, not the
    behaviour of any particular module.
    """

    def _connect16(self):
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_1600g_16lane',
                                               'bus': 0, 'address': 80}),
                              content_type='application/json')
        return self.assertOk(rv)['data']

    def test_interface_ids_decode_to_their_sff_8024_names(self):
        """CMIS stores a number and points at SFF-8024 for the meaning, so
        without these tables the UI can only show hex. The two anchors this
        project already used independently confirm the transcription."""
        import cmis_registers as c
        self.assertEqual(c.host_interface_name(0x51), '800GAUI-8 S C2M')
        self.assertEqual(c.host_interface_name(0x4F), '400GAUI-4-S C2M')
        self.assertEqual(c.host_interface_name(0x83), '1.6TAUI-8 C2M')
        self.assertEqual(c.media_interface_name(0x56), '800GBASE-DR8')
        self.assertEqual(c.media_interface_name(0x1C), '400GBASE-DR4')
        self.assertEqual(c.media_interface_name(0x7F), '1.6TBASE-DR8')
        # The same code is a different interface on multimode fibre.
        self.assertEqual(c.media_interface_name(0x12, 0x01), '800GBASE-SR8')
        self.assertNotEqual(c.media_interface_name(0x12, 0x01),
                            c.media_interface_name(0x12, 0x02))

    def test_an_unknown_interface_code_is_shown_not_blanked(self):
        """A module using a code newer than this table is worth seeing; an
        empty cell reads as though the module said nothing."""
        import cmis_registers as c
        self.assertIn('0xEE', c.host_interface_name(0xEE))
        self.assertIn('Unknown', c.host_interface_name(0xEE))

    def test_the_1_6t_mock_uses_the_real_1_6t_codes(self):
        """It advertised invented codes before SFF-8024 was to hand; a
        simulator that reports a code no module would is worse than useless
        for anyone checking their decode against it."""
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_1600g_dr8', 'bus': 0, 'address': 80}),
            content_type='application/json'))
        apps = self.assertOk(self.client.get('/api/module/applications'))['data']['applications']
        self.assertEqual(apps[0]['host_if_id'], 0x83)
        self.assertEqual(apps[0]['media_if_id'], 0x7F)
        self.assertEqual(apps[0]['host_if_name'], '1.6TAUI-8 C2M')
        self.assertEqual(apps[0]['media_if_name'], '1.6TBASE-DR8')

    def test_heatsink_and_fiber_face_decode_to_names(self):
        import cmis_registers as c
        self.assertEqual(c.HEATSINK_TYPES[1], 'RHS — Riding Heatsink')
        self.assertEqual(c.FIBER_FACE_TYPES[2], 'APC (Angled Physical Contact)')

    def test_every_masked_control_writes_as_many_banks_as_it_reads(self):
        """Reading sixteen lanes and writing eight is the failure this change
        kept producing: the panel shows the module, Apply configures half of
        it, and nothing reports a problem. Squelch got as far as a 500,
        because the page sent a list where an int was expected.
        """
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_1600g_16lane', 'bus': 0, 'address': 80}),
            content_type='application/json'))

        def post(path, body):
            return self.assertOk(self.client.post(
                path, data=json.dumps(body), content_type='application/json'))

        post('/api/module/squelch', {'tx_squelch_disable': [0x0F, 0xF0]})
        got = self.assertOk(self.client.get('/api/module/squelch'))['data']
        self.assertEqual(got['tx_squelch_disable_banks'], [0x0F, 0xF0])

        post('/api/module/loopback', {'media_side_output': [0x03, 0x0C]})
        got = self.assertOk(self.client.get('/api/module/loopback'))['data']
        self.assertEqual(got['media_side_output_banks'], [0x03, 0x0C])

        # Distinct patterns per bank: identical ones cannot tell "split across
        # banks" from "bank 0 written twice". Both have to be patterns this
        # module advertises in 13h:132-133, or the write is refused outright.
        post('/api/module/prbs', {'host_gen': {'enable_mask': [0xFF, 0x0F],
                                               'patterns': [1] * 8 + [7] * 8}})
        got = self.assertOk(self.client.get('/api/module/prbs'))['data']['host_gen']
        self.assertEqual(got['enable_mask_banks'], [0xFF, 0x0F])
        self.assertEqual(len(got['patterns']), 16)
        self.assertEqual(got['patterns'][0], 1)
        self.assertEqual(got['patterns'][15], 7,
                         "bank 1 was given bank 0's patterns")

    def test_the_masked_controls_still_take_a_plain_byte(self):
        """Every caller written before banks existed sends one, and an
        eight-lane module is still the common case."""
        self.connect()
        self.assertOk(self.client.post(
            '/api/module/squelch',
            data=json.dumps({'tx_squelch_disable': 0x0F}),
            content_type='application/json'))
        got = self.assertOk(self.client.get('/api/module/squelch'))['data']
        self.assertEqual(got['tx_squelch_disable'], 0x0F)
        self.assertEqual(got['tx_squelch_disable_banks'], [0x0F])

    def test_both_1_6t_shapes_are_modelled(self):
        """The two 1.6T layouts in the market exercise different code here:
        8x200G fits one bank, 16x100G needs two."""
        for backend, lanes, banks in (('mock_1600g_dr8', 8, 1),
                                      ('mock_1600g_16lane', 16, 2)):
            self.assertOk(self.client.post(
                '/api/connect',
                data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
                content_type='application/json'))
            caps = self.assertOk(self.client.get('/api/module/capabilities'))['data']
            self.assertEqual((caps['max_lanes'], caps['banks_supported']),
                             (lanes, banks), backend)
            self.assertEqual(caps['cmis_revision'], '5.4', backend)
            info = self.assertOk(self.client.get('/api/module/info'))['data']
            self.assertEqual(info['power_class'], 8, f'{backend} draws 1.6T power')
            self.assertGreater(info['max_power_w'], 20, backend)

    def test_the_200g_per_lane_ber_is_modelled_as_the_spec_expects(self):
        """A healthy pre-FEC BER at 200G/lane sits around 1e-4 - orders worse
        than an 800G module and not a fault. A mock that copied the 800G
        figure would teach the opposite."""
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_1600g_dr8', 'bus': 0, 'address': 80}),
            content_type='application/json'))
        ber = self.assertOk(self.client.get('/api/module/ber'))['data']['lanes']
        self.assertGreater(ber[0]['media_ber'], 1e-5)
        self.assertLess(ber[0]['media_ber'], 1e-3)

    def test_an_application_never_claims_more_than_eight_lanes(self):
        """CMIS 5.4 section 6.4.1 caps one Application at eight lanes, so a
        16-lane module advertises Applications that fit a lane group rather
        than one 16-lane Application."""
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_1600g_16lane', 'bus': 0, 'address': 80}),
            content_type='application/json'))
        apps = self.assertOk(self.client.get('/api/module/applications'))['data']['applications']
        self.assertTrue(apps)
        for a in apps:
            self.assertLessEqual(a['host_lanes'], 8, 'an Application exceeded 8 lanes')
            self.assertLessEqual(a['media_lanes'], 8)

    def test_lane_count_comes_from_the_advertisement(self):
        d = self._connect16()
        self.assertEqual(d['lanes'], 16)
        self.assertEqual(d['cmis_revision'], '5.4')
        caps = self.assertOk(self.client.get('/api/module/capabilities'))['data']
        self.assertEqual(caps['banks_supported'], 2)
        self.assertEqual(caps['max_lanes'], 16)

    def test_every_lane_panel_covers_all_the_lanes(self):
        """Sizing a panel to eight would silently hide half the module."""
        self._connect16()
        for path, key in (('/api/module/monitoring', 'lanes'),
                          ('/api/module/datapath', 'lanes'),
                          ('/api/module/flags', 'lanes'),
                          ('/api/module/ber', 'lanes')):
            body = self.assertOk(self.client.get(path))['data']
            self.assertEqual(len(body[key]), 16, path)
        snr = self.assertOk(self.client.get('/api/module/snr'))['data']
        self.assertEqual(len(snr['host_snr_db']), 16)

    def test_the_second_bank_is_actually_read(self):
        """Serving bank 0 for every bank yields plausible numbers for lanes
        that were never looked at - the failure this is here to catch."""
        self._connect16()
        lanes = self.assertOk(self.client.get('/api/module/monitoring'))['data']['lanes']
        self.assertEqual([l['lane'] for l in lanes], list(range(1, 17)))
        self.assertNotEqual(lanes[0]['tx_power_uw'], lanes[8]['tx_power_uw'],
                            'lane 9 repeated lane 1: the bank never changed')

    def test_the_escape_code_is_what_reaches_past_thirty_two_lanes(self):
        """01h:142's two-bit field tops out at 32 lanes. CMIS 5.4 gave it the
        value 11b meaning "the real count is in 01h:174", which is the only
        route to the 256 lanes the revision allows. A module that does not use
        the escape must not have 01h:174 read into its lane count - the byte
        is not required to exist and may be anything.
        """
        import cmis_registers as c
        self.assertEqual(c.parse_supported_pages(0b00)['max_lanes'], 8)
        self.assertEqual(c.parse_supported_pages(0b01)['max_lanes'], 16)
        self.assertEqual(c.parse_supported_pages(0b10)['max_lanes'], 32)
        # Escape set: 174.4-0 = n means (n+1) banks of eight.
        self.assertEqual(c.parse_supported_pages(0b11, bytes([0, 31]))['max_lanes'], 256)
        self.assertEqual(c.parse_supported_pages(0b11, bytes([0, 0]))['max_lanes'], 8)
        # Escape clear: the same byte must be ignored entirely.
        self.assertEqual(c.parse_supported_pages(0b01, bytes([0, 31]))['max_lanes'], 16)
        self.assertIsNone(c.parse_supported_pages(0b01, bytes([0, 31]))['extra_lane_banks'])

    def test_an_eight_lane_module_is_unaffected(self):
        self.connect()
        caps = self.assertOk(self.client.get('/api/module/capabilities'))['data']
        self.assertEqual(caps['max_lanes'], 8)
        self.assertEqual(caps['banks_supported'], 1)
        lanes = self.assertOk(self.client.get('/api/module/monitoring'))['data']['lanes']
        self.assertEqual(len(lanes), 8)

    def test_the_new_in_5_4_list_is_served_not_retyped(self):
        """The UI badge and the manual both read this list, so it has to come
        from the decoder rather than being written out again beside them."""
        import cmis_registers as c
        self._connect16()
        caps = self.assertOk(self.client.get('/api/module/capabilities'))['data']
        self.assertEqual(set(caps['new_in_5_4']), set(c.NEW_IN_5_4))
        self.assertIn('max_lanes', caps['new_in_5_4'])


class TestTheManualsMarkupHolds(CMISTestCase):
    """The manual is one hand-edited file that ships to customers, and a
    callout wraps its text in a span. Putting a table in one renders today
    only because .callout is display:flex; any change to that puts flow
    content inside an inline box and the layout collapses. This has been
    written twice now."""

    def _manual(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'CMIS2Customer', 'CMIS模块管理工具操作手册.html')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_no_table_sits_inside_a_span(self):
        manual = self._manual()
        offenders = [manual[:m.start()].count('\n') + 1
                     for m in re.finditer(r'<span[^>]*>(.*?)</span>', manual, re.S)
                     if '<table' in m.group(1)]
        self.assertEqual(offenders, [],
                         'a table is nested in a span at line(s) %s' % offenders)

    def test_the_tags_balance(self):
        manual = self._manual()
        for tag in ('div', 'table', 'tbody', 'thead'):
            self.assertEqual(manual.count('<%s' % tag), manual.count('</%s>' % tag),
                             '<%s> tags do not balance' % tag)

    def test_the_raw_register_chapter_warns_what_a_write_can_do(self):
        """That tab writes any byte on any page by design - it exists to do
        what the guarded panels will not. On a live module that includes
        resetting it. Every other disruptive action in this tool is called
        out; this one was not documented at all."""
        manual = self._manual()
        start = manual.index('id="s11"')
        chapter = manual[start:manual.index('id="s12b"')]
        self.assertIn('不设任何护栏', chapter,
                      'the raw register chapter does not say it is unguarded')
        for addr in ('0x1A', '0x8F', '0x7E'):
            self.assertIn(addr, chapter,
                          '%s can be written from that tab and is not mentioned' % addr)
        self.assertIn('软复位', chapter,
                      'the chapter never says a raw write can reset the module')


class TestTheFirstScreenExplainsItself(CMISTestCase):
    """What someone sees before they have pressed anything. The backend list
    already carries a description of each entry - including why an adapter is
    unavailable - and withholding it until after Connect meant the only way to
    read it was to try and fail."""

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_backend_list_carries_a_description_for_every_entry(self):
        rows = self.assertOk(self.client.get('/api/backends'))['data']
        for row in rows:
            self.assertTrue(row.get('description'),
                            '%s has nothing to say for itself' % row['name'])
            self.assertIn('available', row)

    def test_an_unavailable_adapter_says_what_is_missing(self):
        """A user without the adapter should learn that from the panel, not
        from a failed connection."""
        rows = {r['name']: r for r in
                self.assertOk(self.client.get('/api/backends'))['data']}
        for name in ('ch341', 'ch347', 'ftdi'):
            row = rows.get(name)
            if row and not row['available']:
                self.assertGreater(len(row['description']), 10,
                                   '%s is unavailable without saying why' % name)

    def test_the_page_describes_the_selection_before_connecting(self):
        js = self._js()
        self.assertIn("getElementById('sel-backend')?.addEventListener('change'", js,
                      'changing the backend does not update its description')
        idx = js.index("getElementById('sel-backend')?.addEventListener('change'")
        handler = js[idx:idx + 400]
        self.assertIn('updateBackendInfoArea', handler)
        self.assertIn('AppState.connected', handler,
                      'the description would be overwritten while connected')
        # and the default selection is described as soon as the list arrives
        load = js[js.index('async function loadBackends('):]
        load = load[:load.index('\n}')]
        self.assertIn('updateBackendInfoArea', load,
                      'the first screen says nothing about the default backend')

    def test_connecting_with_the_defaults_works(self):
        """The whole first-run path: press Connect without touching anything."""
        rows = self.assertOk(self.client.get('/api/backends'))['data']
        first_available = next(r['name'] for r in rows if r['available'])
        self.assertTrue(first_available.startswith('mock'),
                        'the first working entry is not a simulation, so a user '
                        'without hardware cannot get started')
        body = self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': first_available, 'bus': 0, 'address': 0x50}),
            content_type='application/json'))
        self.assertEqual(body['data']['backend'], first_available)
        info = self.assertOk(self.client.get('/api/module/info'))['data']
        self.assertTrue(info.get('vendor_name'),
                        'a fresh connection shows no module identity')


class TestAPulledModuleRecoversHonestly(CMISTestCase):
    """Pulling a module mid-session is routine in a lab. The tool stops the
    refresh so a dead bus cannot spam, and marks the readings stale so nobody
    reads minutes-old power as current. The recovery path has to restore both
    facts together, or the page ends up clear-bannered and frozen - which is
    the exact hazard the stale marking was written to prevent."""

    def test_the_api_recovers_without_a_reconnect(self):
        self.connect()
        backend = _state['backend']
        real_read, real_write = backend.read_bytes, backend.write_bytes

        def gone(*a, **k):
            raise IOError('no ACK from 0x50')

        backend.read_bytes = backend.write_bytes = gone
        try:
            for path in ('/api/module/monitoring', '/api/module/status',
                         '/api/module/flags'):
                body = self.assertErr(self.client.get(path), 500)
                self.assertIn('ACK', body['message'],
                              '%s hid what the adapter said' % path)
            self.assertTrue(_state['connected'],
                            'a read failure dropped the connection; a pulled '
                            'module may be back in a second')
        finally:
            backend.read_bytes, backend.write_bytes = real_read, real_write

        self.assertOk(self.client.get('/api/module/monitoring'))

    def test_the_banner_says_the_refresh_stopped_and_how_to_restart_it(self):
        js = self._js()
        idx = js.index('function markMonitoringStale(')
        body = js[idx:idx + 500]
        self.assertIn('STALE', body)
        self.assertIn('Auto-refresh is stopped', body,
                      'the banner does not say the page stopped updating')
        self.assertIn('Now', body,
                      'the banner names no way to resume')

    def test_a_recovered_read_starts_the_refresh_again(self):
        """The banner clears on the read that succeeds. If the interval is not
        restarted with it, the page looks live and never updates again."""
        js = self._js()
        self.assertIn('_monitoringHaltedByError', js,
                      'nothing records that a failure stopped the refresh')
        idx = js.index('async function _loadMonitoringOnce(')
        body = js[idx:idx + 3600]
        clear_at = body.index('clearMonitoringStale();')
        resume = re.search(r'_monitoringHaltedByError\s*&&[^)]*\)\s*\{[^}]*setInterval',
                           body[clear_at:], re.S)
        self.assertIsNotNone(
            resume, 'the success path clears the stale banner without restarting '
                    'the refresh, leaving a frozen page that looks live')
        # and a user who chose Manual must not have it turned back on for them
        self.assertIn('AppState.monitoringManual', body[clear_at:clear_at + 400],
                      'manual mode would be overridden by the recovery')

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()


class TestTheDataPathPassesThroughItsStates(CMISTestCase):
    """Applying a configuration is not instant on a real module: the lanes go
    DPInit, DPTxTurnOn, then DataPathActivated, and the UI shows each. The
    suite only ever checked where they ended up, so a regression that jumped
    straight to Activated - or stalled in the middle - would show as the tool
    never displaying a transition, which nobody would notice."""

    def _states(self):
        d = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        return [l['datapath_state'] for l in d['lanes']]

    def test_the_lanes_move_through_init_and_turn_on(self):
        self.connect()
        self.assertEqual(set(self._states()), {'Activated'})

        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [1] * 8, 'apply': True}),
            content_type='application/json'))

        seen = set()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            seen.update(self._states())
            if 'Activated' in seen and len(seen) > 1:
                break
            time.sleep(0.05)

        self.assertIn('Activated', seen, 'the lanes never came up: %s' % sorted(seen))
        # Both steps, not merely "something happened": Init and TxTurnOn are
        # different failures to be stuck in, and the tool shows which.
        self.assertIn('Init', seen,
                      'the lanes never reported Init, so a data path stuck '
                      'there would look like one that never started: %s' % sorted(seen))
        self.assertIn('TxTurnOn', seen,
                      'the lanes never reported TxTurnOn, so a laser that fails '
                      'to come up is indistinguishable from a config that was '
                      'rejected: %s' % sorted(seen))

    def test_a_disabled_lane_ends_deactivated_not_activated(self):
        """Tx disabled means that lane has nothing to bring up. Reporting it
        Activated alongside the others hides which lanes are actually carrying
        traffic."""
        self.connect()
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [1] * 8, 'tx_disable_mask': 0b00000101,
                             'apply': True}),
            content_type='application/json'))
        deadline = time.time() + 3.0
        while time.time() < deadline:
            states = self._states()
            if states[0] != 'Activated' and states[1] == 'Activated':
                break
            time.sleep(0.05)
        states = self._states()
        self.assertEqual(states[0], 'Deactivated', 'lane 1 was disabled')
        self.assertEqual(states[2], 'Deactivated', 'lane 3 was disabled')
        self.assertEqual(states[1], 'Activated', 'lane 2 was not disabled')

    def test_a_reset_takes_the_module_down_and_brings_it_back(self):
        """The module state machine has intermediate steps too, and the tool
        shows them; only the endpoints were ever asserted."""
        self.connect()
        start = self.assertOk(self.client.get('/api/module/status'))['data']
        self.assertEqual(start['module_state'], 'ModuleReady')

        self.assertOk(self.client.post(
            '/api/module/control',
            data=json.dumps({'software_reset': True}),
            content_type='application/json'))

        seen = set()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            seen.add(self.assertOk(
                self.client.get('/api/module/status'))['data']['module_state'])
            if 'ModuleReady' in seen and len(seen) > 1:
                break
            time.sleep(0.05)
        self.assertTrue(seen - {'ModuleReady'},
                        'the module never left ModuleReady, so the reset did '
                        'nothing observable: %s' % sorted(seen))
        self.assertIn('ModuleReady', seen, 'the module never came back up')


class TestModuleLevelEventsAreRememberedToo(CMISTestCase):
    """The per-lane flags got a history; the module-level ones are latched in
    exactly the same way and did not have one. A module that reset and came
    back between two polls was indistinguishable from one that never moved."""

    def test_the_mock_flags_a_state_change_at_all(self):
        """6.3.2 has the module set ModuleStateChangedFlag on entering a new
        state. It never did, so neither the tool's handling nor the demo could
        show one."""
        self.connect()
        self.client.get('/api/module/status')          # clear what connecting set
        self.assertOk(self.client.post(
            '/api/module/control',
            data=json.dumps({'software_reset': True}),
            content_type='application/json'))
        deadline = time.time() + 4.0
        saw = False
        while time.time() < deadline and not saw:
            saw = self.assertOk(
                self.client.get('/api/module/status'))['data']['module_state_changed']
            time.sleep(0.05)
        self.assertTrue(saw, 'a reset walked the module through three states '
                             'without flagging any of them')

    def test_a_state_change_survives_the_read_that_reported_it(self):
        self.connect()
        self.client.get('/api/module/status')
        self.assertOk(self.client.post(
            '/api/module/control',
            data=json.dumps({'software_reset': True}),
            content_type='application/json'))
        time.sleep(2.0)
        for _ in range(3):
            d = self.assertOk(self.client.get('/api/module/status'))['data']
            self.assertIn('module_state_changed', d['seen'],
                          'the only record that the module restarted was lost')

    def test_a_deliberate_reset_is_not_called_an_alarm(self):
        """The indicator people glance at first should mean something is out
        of spec. Lighting it because somebody pressed reset teaches them to
        stop reading it."""
        self.connect()
        self.assertOk(self.client.post(
            '/api/module/control',
            data=json.dumps({'software_reset': True}),
            content_type='application/json'))
        deadline = time.time() + 4.0
        while time.time() < deadline:
            d = self.assertOk(self.client.get('/api/module/status'))['data']
            if d['module_state_changed']:
                self.assertFalse(d['alarm_active'],
                                 'a state change on its own raised the alarm '
                                 'indicator at %.1f C' % d['temperature_c'])
            time.sleep(0.05)

    def test_the_restart_reaches_the_screen(self):
        """The history was recorded and then not shown anywhere. A record the
        operator cannot see is the same as no record."""
        js = self._read('static', 'app.js')
        self.assertRegex(js, r"includes\('module_state_changed'\)",
                         'the restart history never reaches the render')
        idx = js.index('function moduleRestartCell(')
        body = js[idx:idx + 700]
        self.assertIn('flag-was', body,
                      'a module that restarted looks like one that did not')
        header = js[js.index('function renderHealthIndicator('):]
        header = header[:header.index('\n}')]
        self.assertIn('module_state_changed', header,
                      'the always-visible header stays silent about a restart')

    def _read(self, *parts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_module_history_clears_with_the_rest(self):
        self.connect()
        self.assertOk(self.client.post(
            '/api/module/control',
            data=json.dumps({'software_reset': True}),
            content_type='application/json'))
        time.sleep(2.0)
        self.assertIn('module_state_changed',
                      self.assertOk(self.client.get('/api/module/status'))['data']['seen'])
        self.assertOk(self.client.post('/api/module/flags/clear'))
        self.assertEqual(
            self.assertOk(self.client.get('/api/module/status'))['data']['seen'], [])


class TestTheDistributionPayloadStaysFlat(CMISTestCase):
    """v2.1.0 and v2.2.0 shipped the containing folder instead of its contents.
    The swap helper looks for the exe in the staging root with a non-recursive
    listing, found none, retried for 75 s and gave up - silently, for every
    user, twice. Existing installs carry that logic frozen inside their own
    exe, so a nested payload cannot be rescued by fixing the updater later."""

    def _script(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'packaging', 'make_dist_zip.py')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_updater_refuses_a_nested_payload(self):
        import updater
        with self.assertRaises(ValueError):
            updater._payload_members(['CMIS_Module_Manager.exe',
                                      'skill/SKILL.md'])

    def test_a_flat_payload_with_the_skill_is_accepted(self):
        import updater
        staged = [rel for _o, rel in updater._payload_members(
            ['CMIS_Module_Manager.exe', 'manual.html', 'SKILL.md'])]
        self.assertEqual(staged,
                         ['CMIS_Module_Manager.exe', 'manual.html', 'SKILL.md'])

    def test_the_packaging_script_ships_the_skill_flat(self):
        """The skill has to reach users who never open the download page."""
        src = self._script()
        self.assertIn("'SKILL.md'", src,
                      'the distribution package no longer carries the skill')
        self.assertNotIn("'skill/SKILL.md'", src,
                         'a nested skill path would make every existing '
                         'install refuse the update')

    def test_the_packaging_script_checks_before_it_ships(self):
        src = self._script()
        self.assertIn('assert_updater_accepts', src,
                      'nothing verifies the payload against the updater that '
                      'has to unpack it')
        self.assertIn('_payload_members', src,
                      'the check does not use the real updater logic')

    def test_the_version_comes_from_one_place(self):
        """A hand-typed file name and a built exe drift apart quietly."""
        src = self._script()
        self.assertIn('__version__', src,
                      'the archive name is not derived from app.py')

    def test_the_script_keeps_no_machine_specific_path(self):
        """packaging/ is in the public repo."""
        src = self._script()
        self.assertNotIn('D:\\\\claude', src)
        self.assertNotIn('D:/claude', src)


class TestADataPathCanBeTakenOutOfService(CMISTestCase):
    """10h:128 is RW and Required (Table 8-78): 1b deinitialises the Data Path
    of that lane. It is the only way to stop one deliberately. The tool read
    the byte and showed it, the mock stored it at build and never looked at
    it again, and nothing could write it - so the column said Active or
    Deinit about a control neither side could operate."""

    def _connect(self, backend='mock_fr4x2'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, body):
        return self.assertOk(self.client.post(
            '/api/module/datapath', data=json.dumps(body),
            content_type='application/json'))

    def _states(self):
        return [l['datapath_state'] for l in self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']]

    def _mask(self):
        return self.assertOk(
            self.client.get('/api/module/datapath'))['data']['dp_deinit_mask']

    def _running(self):
        # App 1 advertises lane 1 as its only starting lane and App 2 lane 5,
        # so this - not eight lanes of App 1 - is what this module can run.
        self._post({'app_select': [1, 1, 1, 1, 2, 2, 2, 2], 'apply': True})
        time.sleep(1.2)
        self.assertEqual(set(self._states()), {'Activated'})

    def test_deinitialising_a_lane_takes_its_whole_data_path(self):
        """Table 8-78: all lanes of a Data Path must carry the same value, so
        asking for one lane asks for its path."""
        self._connect()
        self._running()
        self._post({'dp_deinit_mask': 0x10})          # lane 5 alone
        time.sleep(0.6)
        self.assertEqual(self._mask(), 0xF0,
                         'one lane was deinitialised on its own')

    def test_the_other_data_path_keeps_running(self):
        self._connect()
        self._running()
        self._post({'dp_deinit_mask': 0x10})
        time.sleep(0.6)
        self.assertEqual(self._states(),
                         ['Activated'] * 4 + ['Deactivated'] * 4,
                         'taking one Data Path down disturbed the other')

    def test_releasing_it_brings_the_path_back(self):
        self._connect()
        self._running()
        self._post({'dp_deinit_mask': 0xF0})
        time.sleep(0.6)
        self._post({'dp_deinit_mask': 0x00})
        time.sleep(1.2)
        self.assertEqual(set(self._states()), {'Activated'},
                         'a released Data Path never came back')

    def test_a_released_path_walks_back_up_rather_than_snapping(self):
        """It goes through DPInit, so DPStateChangedFlag records it - the same
        evidence any other re-initialisation leaves."""
        self._connect()
        self._running()
        self._post({'dp_deinit_mask': 0xF0})
        time.sleep(0.6)
        self.client.get('/api/module/flags')
        self.assertOk(self.client.post('/api/module/flags/clear'))
        self._post({'dp_deinit_mask': 0x00})
        time.sleep(1.2)
        lanes = self.assertOk(self.client.get('/api/module/flags'))['data']['lanes']
        bounced = [l['lane'] for l in lanes
                   if l['dp_state_changed'] or 'dp_state_changed' in l['seen']]
        self.assertEqual(bounced, [5, 6, 7, 8])

    def test_a_held_lane_is_not_lifted_by_an_apply(self):
        """Deinit outranks Apply: a path held down must stay down."""
        self._connect()
        self._running()
        self._post({'dp_deinit_mask': 0xF0})
        time.sleep(0.6)
        self._post({'app_select': [1, 1, 1, 1, 2, 2, 2, 2], 'apply': True})
        time.sleep(1.2)
        self.assertEqual(self._states()[4:], ['Deactivated'] * 4,
                         'an Apply re-initialised a Data Path being held '
                         'deinitialised')

    def test_the_mask_is_left_alone_when_not_mentioned(self):
        self._connect()
        self._running()
        self._post({'dp_deinit_mask': 0xF0})
        time.sleep(0.6)
        self._post({'tx_polarity_flip_mask': 0x0F})
        self.assertEqual(self._mask(), 0xF0,
                         'a polarity change released a Data Path')

    def test_the_column_is_a_control_now(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        self.assertIn('id="dp-deinit-', js,
                      'the column still only reports a control nobody can use')
        body = js[js.index('async function applyDatapath('):]
        body = body[:body.index('\n}')]
        # The name also appears where the mask is built, so pin the
        # posted object rather than the identifier.
        posted = body[body.index("apiPost('/api/module/datapath'"):]
        self.assertRegex(posted, r'(?m)^\s*dp_deinit_mask,\s*$',
                         'Apply never sends what the boxes say')

    def test_the_boxes_move_as_a_data_path(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        self.assertIn('_appHostLanes(lane.app_select)', js,
                      'the boxes can be ticked one at a time, asking for a '
                      'half-torn-down Data Path')


class TestAWriteOnlyChangesWhatItNames(CMISTestCase):
    """Omitting a field made it default to zero, so a request that set one
    control silently cleared the others and reported success. Byte 0x1A
    already learned this - its handler reads before writing, and the comment
    says why - and the lesson never reached the endpoints that write masks."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body),
                                content_type='application/json')

    def test_setting_one_squelch_control_leaves_the_others(self):
        self._connect()
        self.assertOk(self._post('/api/module/squelch',
                                 {'tx_squelch_disable': 0xFF,
                                  'rx_output_disable': 0x0F}))
        self.assertOk(self._post('/api/module/squelch',
                                 {'tx_squelch_force': 0x03}))
        got = self.assertOk(self.client.get('/api/module/squelch'))['data']
        self.assertEqual(got['tx_squelch_disable'], 0xFF,
                         'a request that never mentioned this control '
                         'cleared it')
        self.assertEqual(got['rx_output_disable'], 0x0F)
        self.assertEqual(got['tx_squelch_force'], 0x03)

    def test_setting_a_polarity_does_not_reconfigure_every_lane(self):
        """app_select defaulted to AppSel 1, so a polarity change quietly
        re-provisioned the whole module."""
        self._connect()
        self.assertOk(self._post('/api/module/datapath',
                                 {'app_select': [2] * 8, 'apply': False}))
        self.assertOk(self._post('/api/module/datapath',
                                 {'tx_polarity_flip_mask': 0x0F,
                                  'apply': False}))
        got = self.assertOk(self.client.get('/api/module/datapath'))['data']
        self.assertEqual(got['app_select'], [2] * 8,
                         'a polarity change reconfigured the Application on '
                         'every lane')
        self.assertEqual(got['tx_polarity_flip_mask'], 0x0F)

    def test_setting_an_application_does_not_unflip_polarity(self):
        self._connect()
        self.assertOk(self._post('/api/module/datapath',
                                 {'tx_polarity_flip_mask': 0xFF,
                                  'apply': False}))
        self.assertOk(self._post('/api/module/datapath',
                                 {'app_select': [1] * 8, 'apply': False}))
        got = self.assertOk(self.client.get('/api/module/datapath'))['data']
        self.assertEqual(got['tx_polarity_flip_mask'], 0xFF)

    def test_setting_one_loopback_leaves_the_others(self):
        self._connect()
        self.assertOk(self._post('/api/module/loopback',
                                 {'media_side_output': 0xFF}))
        self.assertOk(self._post('/api/module/loopback',
                                 {'host_side_input': 0x0F}))
        got = self.assertOk(self.client.get('/api/module/loopback'))['data']
        self.assertEqual(got['media_side_output'], 0xFF,
                         'setting one loopback type cleared another')
        self.assertEqual(got['host_side_input'], 0x0F)


class TestAMisspelledFieldIsNotSuccess(CMISTestCase):
    """A name the handler does not know used to be ignored and the request
    reported ok - having changed nothing, or on the mask endpoints having
    cleared every control the caller did not spell correctly."""

    def _connect(self):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_dr8', 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body),
                                content_type='application/json')

    def test_a_typo_is_refused_and_changes_nothing(self):
        self._connect()
        self.assertOk(self._post('/api/module/squelch',
                                 {'tx_squelch_disable': 0xFF}))
        rv = self._post('/api/module/squelch', {'tx_squelch_disabled': 0xFF})
        self.assertEqual(rv.status_code, 400,
                         'a misspelled field was accepted')
        msg = json.loads(rv.data)['message']
        self.assertIn('tx_squelch_disabled', msg,
                      'the refusal does not say which field it did not know')
        # Not the field they mistyped: 'tx_squelch_disabled' contains
        # 'tx_squelch_disable', so naming that one proves nothing.
        self.assertIn('rx_output_disable', msg,
                      'the refusal does not say what is accepted')
        got = self.assertOk(self.client.get('/api/module/squelch'))['data']
        self.assertEqual(got['tx_squelch_disable'], 0xFF,
                         'the refused request cleared the control anyway')

    def test_every_write_endpoint_refuses_a_stray_field(self):
        self._connect()
        for path in ('/api/module/squelch', '/api/module/loopback',
                     '/api/module/datapath', '/api/module/control'):
            with self.subTest(path=path):
                rv = self._post(path, {'definitely_not_a_field': 1})
                self.assertEqual(rv.status_code, 400,
                                 '%s accepted a field it does not know' % path)

    def test_the_control_register_accepts_the_names_it_reports(self):
        """The read side named the same bits differently, so a caller feeding
        back what it had just read changed nothing and was told it worked."""
        self._connect()
        before = self.assertOk(
            self.client.get('/api/module/control'))['data']
        self.assertIn('low_pwr_request_sw', before)
        self.assertOk(self._post('/api/module/control',
                                 {'low_pwr_request_sw': True}))
        after = self.assertOk(self.client.get('/api/module/control'))['data']
        self.assertTrue(after['low_pwr_request_sw'],
                        'the register reports a name its own writer rejects')

    def test_the_module_actually_enters_low_power(self):
        """And the request has to reach the module, not just the byte."""
        self._connect()
        self.assertOk(self._post('/api/module/control',
                                 {'low_pwr_request_sw': True}))
        time.sleep(0.5)
        state = self.assertOk(
            self.client.get('/api/module/status'))['data']['module_state']
        self.assertEqual(state, 'ModuleLowPwr')


class TestApplyOnlyTouchesTheDataPathsThatChanged(CMISTestCase):
    """CMIS 6.2.3.3.1: Apply must be triggered on all lanes of a Data Path at
    once - a Data Path, not the module. The tool wrote 0xFF every time, so
    reconfiguring one 400G port on a module carrying two took the other one
    down with it."""

    def _connect(self, backend='mock_fr4x2'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _apply(self, **body):
        body.setdefault('apply', True)
        return self.assertOk(self.client.post(
            '/api/module/datapath', data=json.dumps(body),
            content_type='application/json'))['data']

    def _bounced(self):
        lanes = self.assertOk(self.client.get('/api/module/flags'))['data']['lanes']
        return [l['lane'] for l in lanes
                if l['dp_state_changed'] or 'dp_state_changed' in l['seen']]

    def _settle(self):
        time.sleep(1.2)
        self.client.get('/api/module/flags')
        self.assertOk(self.client.post('/api/module/flags/clear'))

    def test_two_data_paths_are_independent(self):
        self._connect()
        apps = self.assertOk(
            self.client.get('/api/module/applications'))['data']['applications']
        self.assertEqual(apps[0]['host_lanes'], 4,
                         'this profile is meant to carry two 4-lane paths')
        self._apply(app_select=[1] * 8)
        self._settle()
        self._apply(app_select=[1, 1, 1, 1, 2, 2, 2, 2])
        time.sleep(1.2)
        self.assertEqual(self._bounced(), [5, 6, 7, 8],
                         'changing one Data Path re-initialised the other')

    def test_the_response_says_what_it_applied(self):
        self._connect()
        self._apply(app_select=[1] * 8)
        self._settle()
        body = self._apply(app_select=[1, 1, 1, 1, 2, 2, 2, 2])
        self.assertEqual(body['applied_lanes'], [5, 6, 7, 8])

    def test_an_unchanged_table_still_re_commissions(self):
        """Pressing Apply with nothing edited is a request to re-commission,
        and quietly doing nothing would take that away."""
        self._connect()
        self._apply(app_select=[1] * 8)
        self._settle()
        body = self._apply(app_select=[1] * 8)
        self.assertEqual(body['applied_lanes'], [1, 2, 3, 4, 5, 6, 7, 8])

    def test_the_grouping_follows_the_application_width(self):
        """The tool writes DataPathID 0 for every lane, so the grouping has to
        come from the Application descriptor."""
        eight = {1: 8}
        self.assertEqual(app_module._datapath_groups([1] * 8, eight),
                         [[0, 1, 2, 3, 4, 5, 6, 7]])
        four = {1: 4, 2: 4}
        self.assertEqual(app_module._datapath_groups([1, 1, 1, 1, 2, 2, 2, 2], four),
                         [[0, 1, 2, 3], [4, 5, 6, 7]])

    def test_a_lane_leaving_a_path_disturbs_both(self):
        """It has to be applied on the path it left as well as the one it
        joined, or the abandoned path keeps a lane it no longer owns."""
        four = {1: 4, 2: 4}
        need = app_module._lanes_needing_apply([1, 1, 1, 1, 1, 1, 1, 1],
                                        [1, 1, 1, 1, 2, 2, 2, 2], four)
        self.assertEqual(sorted(need), [4, 5, 6, 7])

    def test_a_run_stops_where_the_application_changes(self):
        """A width of four does not make four lanes one Data Path: the run
        only holds while the Application does. Uniform test data hides this,
        because every run happens to be whole."""
        four = {1: 4, 2: 4}
        self.assertEqual(
            app_module._datapath_groups([1, 1, 2, 2, 1, 1, 1, 1], four),
            [[0, 1], [2, 3], [4, 5, 6, 7]])

    def test_nothing_changed_means_no_narrowing(self):
        four = {1: 4, 2: 4}
        self.assertEqual(app_module._lanes_needing_apply([1] * 8, [1] * 8, four), set())


class TestTheMockHonoursTheApplyMask(CMISTestCase):
    """ApplyDPInit is a per-lane bit mask, not a switch. The mock accepted
    only 0xFF - the one value the tool happened to write - so the tool's habit
    of re-initialising every Data Path could not show up here."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def test_a_partial_mask_moves_only_those_lanes(self):
        self._connect()
        backend = _state['backend']
        # App 2 is four lanes wide, so lanes 1-4 are a Data Path in their own
        # right and a trigger naming just them is a whole command. Half of the
        # eight-lane App 1 would be ConfigRejectedPartialDataPath instead.
        # Getting there is a width change, so the path stops first (6.2.4.3).
        reconfigure(self.client, [2] * 8)
        time.sleep(1.2)
        self.client.get('/api/module/flags')
        self.assertOk(self.client.post('/api/module/flags/clear'))

        poke(0x10, 0x8F, 0x0F)                        # lanes 1-4 only
        time.sleep(1.2)
        lanes = self.assertOk(self.client.get('/api/module/flags'))['data']['lanes']
        moved = [l['lane'] for l in lanes
                 if l['dp_state_changed'] or 'dp_state_changed' in l['seen']]
        self.assertEqual(moved, [1, 2, 3, 4],
                         'the mock treats ApplyDPInit as all-or-nothing')

    def test_an_unselected_lane_keeps_the_configuration_it_had(self):
        """The lanes an Apply does not select must not be commissioned by it.
        Staging a different Application on them and then applying elsewhere is
        the only way to tell - with the same value staged everywhere,
        committing them anyway looks identical."""
        self._connect()
        reconfigure(self.client, [2] * 8)
        time.sleep(1.2)
        # Free lanes 5-8 in the staged set without applying it.
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [2, 2, 2, 2, 0, 0, 0, 0],
                             'apply': False}),
            content_type='application/json'))
        poke(0x10, 0x8F, 0x0F)                        # apply lanes 1-4 only
        time.sleep(1.2)
        dp = self.assertOk(self.client.get('/api/module/datapath'))['data']
        self.assertEqual(dp['active_app_select'], [2] * 8,
                         'an Apply that did not select lanes 5-8 commissioned '
                         'them anyway')

    def test_an_unselected_lane_keeps_its_config_status(self):
        """Same again for the status: a rejected lane must not be quietly
        marked successful by an Apply aimed at other lanes."""
        self._connect()
        # Reach the four-lane Application first, so that re-applying it later
        # is neither a width change nor a lane being freed - the partial
        # trigger has to be the only thing this test varies.
        reconfigure(self.client, [2] * 8)
        time.sleep(1.2)
        # Every lane refused next, so the status to preserve differs from
        # what a fresh validation would produce. With the same staged set
        # everywhere, re-validating the unselected lanes lands on the value
        # they already had and overwriting them looks identical.
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [14] * 8, 'apply': True}),
            content_type='application/json'))
        time.sleep(1.2)
        mon = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        self.assertTrue(all(l['config_rejected'] for l in mon['lanes']),
                        'every lane should be sitting on a refused Application')

        # Stage something legal everywhere, then apply it to lanes 1-4 only.
        # App 2 is four lanes wide, so that trigger names a whole Data Path.
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [2] * 8, 'apply': False}),
            content_type='application/json'))
        poke(0x10, 0x8F, 0x0F)
        time.sleep(1.2)
        mon = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        self.assertFalse(any(l['config_rejected'] for l in mon['lanes'][:4]),
                         'lanes 1-4 were applied and should have cleared')
        self.assertTrue(all(l['config_rejected'] for l in mon['lanes'][4:]),
                        'an Apply aimed at lanes 1-4 cleared the refusal on '
                        'lanes it never selected')

    def test_unselected_lanes_keep_their_config_status(self):
        self._connect()
        backend = _state['backend']
        reconfigure(self.client, [2] * 8)
        time.sleep(1.2)
        poke(0x10, 0x8F, 0x0F)
        time.sleep(1.2)
        mon = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        for lane in mon['lanes']:
            self.assertEqual(lane['config_status'], 'ConfigSuccess',
                             'lane %d lost its status to an Apply that did '
                             'not select it' % lane['lane'])


class TestTheStagedSetHasASignalIntegrityHalf(CMISTestCase):
    """Apply commits the whole Staged Control Set, and the tool only ever read
    the AppSel and mask part of it. The signal integrity half - adaptive Tx
    equalization, host-controlled targets, the Rx CDR bypass, the output
    equalizer cursors, the output amplitude - was never read or shown, so the
    tool applied settings it could not name and ConfigRejectedInvalidSI (5h)
    named a fault with nowhere in the interface to look it up."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _dp(self):
        return self.assertOk(self.client.get('/api/module/datapath'))['data']

    def test_the_advertisement_is_read(self):
        self._connect()
        si = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']['si']
        self.assertIn('rx_output_eq_control_name', si)
        self.assertIn('tx_input_eq_max', si)

    def test_the_staged_si_controls_are_read(self):
        self._connect()
        si = self._dp()['signal_integrity']
        for key in ('tx_adaptive_eq', 'tx_input_eq_target', 'rx_cdr_enable',
                    'rx_eq_pre_cursor', 'rx_eq_post_cursor',
                    'rx_output_amplitude'):
            self.assertIn(key, si, '%s is committed by Apply and never read'
                                   % key)
            self.assertEqual(len(si[key]), 8)

    def test_only_advertised_controls_come_back(self):
        """A module without host-controlled Tx EQ has no such target to show,
        and a zero there would read as a real setting."""
        self._connect('mock_sr8')
        si = self._dp()['signal_integrity']
        self.assertIn('tx_adaptive_eq', si)
        self.assertNotIn('tx_input_eq_target', si)
        self.assertNotIn('rx_output_amplitude', si)
        self.assertNotIn('rx_eq_post_cursor', si,
                         'this module advertises pre-cursor control only')

    def test_a_retimed_module_does_not_ship_with_its_cdrs_bypassed(self):
        """CDREnableRx clear means bypassed. Left unset in the mock the whole
        byte reads zero, which says every Rx CDR is off on a module that
        advertises having one."""
        for backend in ('mock_dr8', 'mock_sr8', 'mock_coherent'):
            with self.subTest(backend=backend):
                self._connect(backend)
                self.assertTrue(all(self._dp()['signal_integrity']['rx_cdr_enable']),
                                '%s ships with every Rx CDR bypassed' % backend)

    def test_the_nibbles_are_unpacked_lane_one_first(self):
        """Lane 1 is the low nibble of the first byte. Reading it the other
        way round swaps every pair of lanes and looks entirely plausible."""
        import cmis_registers as c
        self.assertEqual(c.unpack_nibbles(bytes([0x21, 0x43, 0x65, 0x87])),
                         [1, 2, 3, 4, 5, 6, 7, 8])

    def test_the_maxima_decode(self):
        import cmis_registers as c
        m = c.parse_si_maxima(bytes([0xF7, 0x35]))
        self.assertEqual(m['rx_output_levels'], [0, 1, 2, 3])
        self.assertEqual(m['tx_input_eq_max'], 7)
        self.assertEqual(m['rx_output_eq_post_cursor_max'], 3)
        self.assertEqual(m['rx_output_eq_pre_cursor_max'], 5)
        self.assertEqual(c.parse_si_maxima(bytes([0x00, 0x00]))
                         ['rx_output_levels'], [])

    def test_the_eq_control_modes_are_named(self):
        import cmis_registers as c
        names = [c.parse_si_controls_adv(bytes([0, m << 3]))
                 ['rx_output_eq_control_name'] for m in range(4)]
        self.assertEqual(names, ['Not supported', 'Pre-cursor only',
                                 'Post-cursor only', 'Pre- and post-cursor'])


class TestTheSignalIntegrityTableFollowsTheAdvertisement(CMISTestCase):
    """Its columns depend on what the module says it has, so the header and
    the cells are built together - a fixed header would go out of step the
    moment a module advertises a different set."""

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_columns_come_from_the_data(self):
        js = self._js()
        body = js[js.index('function renderSignalIntegrity('):]
        body = body[:body.index('\nasync function applyDatapath(')]
        self.assertRegex(body, r'SI_COLUMNS\.filter\(',
                         'every column is shown whatever the module has')
        self.assertIn('head.innerHTML', body,
                      'the header is not built alongside the cells')

    def test_a_bypassed_cdr_does_not_read_as_off(self):
        """CDREnableRx clear means the CDR is bypassed, which is a different
        statement from a control being switched off."""
        js = self._js()
        self.assertIn('Bypassed', js,
                      'a bypassed Rx CDR is labelled like any other disabled '
                      'control')

    def test_the_pre_cursor_only_case_is_called_out(self):
        """With only pre-cursor advertised the post-cursor bytes carry the
        pre-cursor target, so the address alone is misleading."""
        js = self._js()
        self.assertIn('rx_output_eq_control === 1', js,
                      'nothing warns that the post-cursor bytes hold the '
                      'pre-cursor target on such a module')

    def test_the_table_exists_and_is_empty_by_default(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('id="tbl-si-head"', html)
        self.assertIn('id="tbl-si-body"', html)


class TestWhatTheRxPowerNumberActuallyIs(CMISTestCase):
    """01h:151.4 decides whether the Rx power monitor reports OMA or average
    power. They are different quantities, several dB apart on a modulated
    signal, and a receiver limit is written for one or the other. The register
    was never read, so the column showed a dBm figure that could not be
    compared against anything with confidence."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))
        return self.assertOk(
            self.client.get('/api/module/capabilities'))['data']['rx_tx']

    def test_the_characteristics_byte_is_read(self):
        rx = self._connect()
        self.assertIn('rx_power_type', rx,
                      'nothing says whether the Rx power reading is OMA or '
                      'average power')

    def test_every_bit_decodes_independently(self):
        import cmis_registers as c
        off = c.parse_rx_tx_characteristics(0x00)
        self.assertEqual(off['detector_type'], 'PIN')
        self.assertEqual(off['rx_power_type'], 'OMA')
        self.assertEqual(off['rx_los_type'], 'OMA')
        self.assertFalse(off['tx_disable_module_wide'])
        on = c.parse_rx_tx_characteristics(0xFF)
        self.assertEqual(on['detector_type'], 'APD')
        self.assertEqual(on['rx_power_type'], 'Average power')
        self.assertEqual(on['rx_los_type'], 'Pav')
        self.assertTrue(on['tx_disable_module_wide'])
        self.assertTrue(on['rx_los_is_fast'])
        self.assertTrue(on['tx_disable_is_fast'])

    def test_the_output_eq_type_is_named(self):
        import cmis_registers as c
        self.assertEqual(c.parse_rx_tx_characteristics(0x20)['rx_output_eq_type'], 1)
        self.assertEqual(c.parse_rx_tx_characteristics(0x60)['rx_output_eq_name'],
                         'Reserved')

    def test_the_mock_does_not_claim_oma(self):
        """Left at zero the byte says the module reports OMA and its Rx LOS
        responds to OMA, which is not what any shipped profile models."""
        for backend in ('mock_dr8', 'mock_sr8', 'mock_coherent',
                        'mock_fr4x2', 'mock_coherent_zr'):
            with self.subTest(backend=backend):
                rx = self._connect(backend)
                self.assertEqual(rx['rx_power_type'], 'Average power',
                                 '%s reports a power reading it does not '
                                 'describe' % backend)

    def test_the_column_says_which_one_it_is(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        self.assertIn('th-rx-power-type', js,
                      'the Rx power column never says what it is measuring')
        # The identifier appearing is not the label being written: pin the
        # assignment that actually puts the quantity in the heading.
        self.assertRegex(
            js, r'rxHead\.textContent = t\.rx_power_type \?',
            'the column heading no longer carries the measurement type')


class TestAModuleWideTxDisable(CMISTestCase):
    """01h:151.0: any OutputDisableTx takes every Tx lane down. The panel
    offers eight independent checkboxes, which says the opposite - and on a
    live link the difference is seven other lanes."""

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))
        return self.assertOk(
            self.client.get('/api/module/capabilities'))['data']['rx_tx']

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_a_profile_advertises_it(self):
        """Otherwise the branch is unreachable and untestable."""
        self.assertTrue(self._connect('mock_fr4x2')['tx_disable_module_wide'],
                        'no shipped profile has a module-wide Tx disable, so '
                        'the handling is never exercised')

    def test_per_lane_modules_are_unaffected(self):
        for backend in ('mock_dr8', 'mock_sr8', 'mock_coherent_zr'):
            with self.subTest(backend=backend):
                self.assertFalse(
                    self._connect(backend)['tx_disable_module_wide'])

    def test_the_boxes_move_together_when_it_is_module_wide(self):
        js = self._js()
        body = js[js.index('const moduleWide = rxtx.tx_disable_module_wide'):]
        body = body[:body.index('\n  }).join')] if '\n  }).join' in body else body[:4000]
        self.assertRegex(body, r'if \(moduleWide\)',
                         'the module-wide advertisement changes nothing')
        self.assertIn('o.checked = el.checked', body,
                      'clearing one lane leaves the other seven looking '
                      'enabled on a module that just disabled them all')

    def test_the_panel_warns_before_the_click(self):
        js = self._js()
        self.assertIn('datapath-txdisable-note', js,
                      'nothing warns that the per-lane boxes are not per lane')
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        with open(path, encoding='utf-8') as f:
            self.assertIn('id="datapath-txdisable-note"', f.read())


class TestTheAuxMonitorsMeanSomething(CMISTestCase):
    """Lower Memory 18-23 are three plain S16 registers whose meaning is chosen
    by 01h:145 (Table 8-50): Aux2 is degrees Celsius or a percentage of the
    maximum TEC current depending on one bit. The tool read all three and
    handed them out as aux1_raw..aux3_raw with no unit, and nothing displayed
    them - a cooled module's laser temperature and TEC current were simply
    thrown away."""

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _aux(self):
        return self.assertOk(self.client.get('/api/module/status'))['data']['aux']

    def test_the_observable_advertisement_is_read(self):
        self._connect('mock_coherent_zr')
        caps = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']
        self.assertIn('aux', caps, 'nothing reads what the Aux monitors measure')
        self.assertTrue(caps['aux']['cooled_transmitter'])

    def test_each_monitor_is_named_and_carries_its_unit(self):
        self._connect('mock_coherent_zr')
        got = {a['index']: (a['observable'], a['unit']) for a in self._aux()}
        self.assertEqual(got[1], ('tec_current', '%'))
        self.assertEqual(got[2], ('laser_temperature', 'degC'))
        self.assertEqual(got[3], ('vcc2', 'V'))

    def test_the_values_round_trip_in_their_own_units(self):
        self._connect('mock_coherent_zr')
        by = {a['index']: a['value'] for a in self._aux()}
        self.assertAlmostEqual(by[1], -38.0, delta=0.01)   # cooling
        self.assertAlmostEqual(by[2], 45.0, delta=0.01)    # degC
        self.assertAlmostEqual(by[3], 1.8, delta=0.001)    # V

    def test_a_cooling_tec_reads_negative(self):
        """+100 % is full heating and -100 % full cooling; dropping the sign
        would turn a module working hard to cool into one that is heating."""
        self._connect('mock_coherent_zr')
        tec = [a for a in self._aux() if a['observable'] == 'tec_current'][0]
        self.assertLess(tec['value'], 0)

    def test_an_uncooled_module_reports_no_aux_monitor(self):
        """01h:159.4-2 clear means there is no such monitor. Showing a zero
        would read as 0 degC or a TEC doing nothing."""
        for backend in ('mock_dr8', 'mock_sr8', 'mock_fr4x2'):
            with self.subTest(backend=backend):
                self._connect(backend)
                self.assertEqual(self._aux(), [],
                                 '%s offers an Aux reading it never '
                                 'advertised' % backend)

    def test_the_mock_encodes_what_it_advertises(self):
        """A module saying Aux2 is a laser temperature and then encoding a TEC
        percentage there is the same contradiction, one level down."""
        import cmis_registers as c
        self._connect('mock_coherent_zr')
        obs = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']['aux']
        raw = self.assertOk(self.client.get('/api/module/status'))['data']
        for idx in (1, 2, 3):
            observable = obs['aux%d' % idx]
            value, _unit = c.parse_aux_value(
                struct.pack('>h', raw['aux%d_raw' % idx]), observable)
            live = [a for a in raw['aux'] if a['index'] == idx][0]
            self.assertAlmostEqual(live['value'], value, places=4)

    def test_each_bit_selects_its_own_observable(self):
        """The shipped profiles happen to exercise one branch per monitor, so
        pin all three bits directly - otherwise hard-coding an observable
        looks correct against whatever the mocks chose."""
        import cmis_registers as c
        for bits, expect in (
                (0x00, ('custom', 'laser_temperature', 'laser_temperature')),
                (0x01, ('tec_current', 'laser_temperature', 'laser_temperature')),
                (0x02, ('custom', 'tec_current', 'laser_temperature')),
                (0x04, ('custom', 'laser_temperature', 'vcc2')),
                (0x07, ('tec_current', 'tec_current', 'vcc2'))):
            got = c.parse_aux_observables(bits)
            self.assertEqual((got['aux1'], got['aux2'], got['aux3']), expect,
                             '01h:145 = 0x%02X' % bits)
        self.assertFalse(c.parse_aux_observables(0x00)['cooled_transmitter'])
        self.assertTrue(c.parse_aux_observables(0x80)['cooled_transmitter'])

    def test_a_reserved_or_custom_aux_claims_no_unit(self):
        """Aux1 with 145.0 clear is vendor defined; inventing a unit for it
        would be worse than showing the raw number."""
        import cmis_registers as c
        value, unit = c.parse_aux_value(struct.pack('>h', 1234), 'custom')
        self.assertEqual((value, unit), (1234, ''))

    def test_the_panel_shows_them(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        self.assertRegex(js, r'for \(const a of s\.aux \|\| \[\]\)',
                         'the Aux monitors are read and never displayed')
        self.assertIn('01h:145', js,
                      'the row does not say where its meaning comes from')


class TestTxBiasIsScaledTheWayTheModuleSaid(CMISTestCase):
    """01h:160.4-3 (Table 8-53) multiplies the 2 uA bias increment by 1, 2 or
    4. The decoder hard-coded 2 uA, so a module using x2 or x4 had every bias
    reading and every bias threshold understated by that factor."""

    X1_CEILING_MA = 0xFFFF * 0.002        # 131.07 mA

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def test_the_scaling_factor_is_read(self):
        self._connect('mock_coherent_zr')
        mon = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']['monitors']
        self.assertEqual(mon['tx_bias_scale'], 2,
                         'the scaling factor is not read from 01h:160')

    def test_a_bias_above_the_x1_ceiling_is_reported(self):
        """65535 increments of 2 uA stop at 131 mA. A module reading above
        that is proof the factor is being applied."""
        self._connect('mock_coherent_zr')
        lane = self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes'][0]
        self.assertGreater(lane['tx_bias_ma'], self.X1_CEILING_MA,
                           'the bias reads below what x1 scaling can express, '
                           'so the factor is being ignored')

    def test_the_thresholds_are_scaled_with_it(self):
        """A scaled monitor against unscaled thresholds alarms on a healthy
        laser, or stays silent on a dying one."""
        self._connect('mock_coherent_zr')
        thr = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        self.assertGreater(thr['tx_bias_high_alarm_ma'], self.X1_CEILING_MA)
        lane = self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes'][0]
        self.assertLess(lane['tx_bias_ma'], thr['tx_bias_high_alarm_ma'])
        self.assertGreater(lane['tx_bias_ma'], thr['tx_bias_low_alarm_ma'])

    def test_an_unscaled_module_is_unchanged(self):
        self._connect('mock_dr8')
        mon = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']['monitors']
        self.assertEqual(mon['tx_bias_scale'], 1)
        lane = self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes'][0]
        self.assertLess(lane['tx_bias_ma'], self.X1_CEILING_MA)

    def test_the_reserved_code_does_not_quadruple_anything(self):
        """11b is reserved. Treating it as a multiplier would be inventing one."""
        import cmis_registers as c
        self.assertEqual(c.parse_supported_monitors(bytes([0, 0x18]))
                         ['tx_bias_scale'], 1)
        self.assertEqual(c.parse_supported_monitors(bytes([0, 0x10]))
                         ['tx_bias_scale'], 4)
        self.assertEqual(c.parse_supported_monitors(bytes([0, 0x08]))
                         ['tx_bias_scale'], 2)


class TestAFlagTheModuleDoesNotImplement(CMISTestCase):
    """01h:157-158 (Table 8-52) says which Flags the module has. One it does
    not implement reads 0 - exactly what a healthy lane reads - so the table
    coloured it green and said "no fault" about a lane nobody was watching."""

    def _connect(self, backend='mock_sr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def test_the_advertisement_is_read(self):
        self._connect('mock_dr8')
        caps = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']
        self.assertIn('flags_supported', caps,
                      'nothing reads which Flags the module implements')

    def test_the_flags_endpoint_says_which_are_real(self):
        self._connect()
        d = self.assertOk(self.client.get('/api/module/flags'))['data']
        self.assertIn('supported', d)
        self.assertFalse(d['supported']['rx_cdr_lol'],
                         'this profile is meant to lack the Rx CDR LOL Flag')
        self.assertTrue(d['supported']['rx_los'])

    def test_a_module_that_reports_flags_advertises_them(self):
        for backend in ('mock_dr8', 'mock_coherent', 'mock_fr4x2',
                        'mock_coherent_zr'):
            with self.subTest(backend=backend):
                self._connect(backend)
                sup = self.assertOk(
                    self.client.get('/api/module/flags'))['data']['supported']
                for name in ('tx_fault', 'tx_los', 'rx_los'):
                    self.assertTrue(sup[name],
                                    '%s reports %s while advertising that it '
                                    'has no such Flag' % (backend, name))

    def test_the_table_does_not_call_it_healthy(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('function renderFlags('):]
        body = body[:body.index('\nfunction ', 1)]
        self.assertRegex(body, r'implemented === false',
                         'an unimplemented Flag renders as a healthy one')
        for name in ('tx_fault', 'tx_los', 'tx_cdr_lol', 'rx_los', 'rx_cdr_lol'):
            self.assertIn("has('%s')" % name, body,
                          '%s is coloured without asking whether the module '
                          'has it' % name)


class TestTheMockAdvertisesThePagesItServes(CMISTestCase):
    """142.5 DiagnosticPagesSupported says Pages 13h-14h are there. Every
    profile builds and serves both while advertising that it has neither."""

    def test_every_profile_admits_to_its_diagnostic_pages(self):
        for backend in ('mock_dr8', 'mock_coherent', 'mock_sr8',
                        'mock_fr4x2', 'mock_coherent_zr'):
            with self.subTest(backend=backend):
                self.assertOk(self.client.post(
                    '/api/connect',
                    data=json.dumps({'backend': backend, 'bus': 0,
                                     'address': 80}),
                    content_type='application/json'))
                caps = self.assertOk(
                    self.client.get('/api/module/capabilities'))['data']
                self.assertTrue(caps['diagnostic_pages_supported'],
                                '%s serves PRBS and BER from Pages 13h-14h '
                                'while advertising that it has neither'
                                % backend)
                # And the pages really do answer.
                self.assertOk(self.client.get('/api/module/prbs'))
                self.assertOk(self.client.get('/api/module/ber'))


class TestTheControlsTheModuleSaysItHas(CMISTestCase):
    """01h:155-156 (Table 8-51) is the Supported Controls Advertisement: which
    of the lane controls the panels offer the module actually implements. No
    mock set it and nothing read it, so every module read as implementing
    none of them while accepting every write."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))
        return self.assertOk(
            self.client.get('/api/module/capabilities'))['data']['controls']

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body),
                                content_type='application/json')

    def test_a_module_that_takes_these_writes_advertises_them(self):
        for backend in ('mock_dr8', 'mock_coherent', 'mock_sr8',
                        'mock_fr4x2', 'mock_coherent_zr'):
            with self.subTest(backend=backend):
                ctl = self._connect(backend)
                self.assertTrue(ctl['output_disable_tx'],
                                '%s accepts OutputDisableTx while advertising '
                                'that it cannot' % backend)
                self.assertTrue(ctl['tx_squelch_supported'],
                                '%s squelches while saying it has no Tx '
                                'squelching at all' % backend)

    def test_tunability_matches_the_advertisement(self):
        """155.6 says Pages 04h and 12h are there. A module cannot be tunable
        in one place and not the other."""
        for backend, tunable in (('mock_coherent_zr', True),
                                 ('mock_coherent', False),
                                 ('mock_dr8', False)):
            with self.subTest(backend=backend):
                ctl = self._connect(backend)
                self.assertEqual(ctl['transmitter_tunable'], tunable,
                                 '%s disagrees with itself about tunability'
                                 % backend)

    def test_the_squelch_method_is_named(self):
        """00b means no Tx output squelching at all, and the three other
        values say whether squelching cuts OMA or Pav - which changes how the
        Tx power reading should be read."""
        ctl = self._connect()
        self.assertEqual(ctl['squelch_method_tx_name'], 'Reduces OMA')
        import cmis_registers as c
        self.assertEqual(c.parse_supported_controls(bytes([0x00, 0x00]))
                         ['squelch_method_tx_name'], 'Not supported')
        self.assertEqual(c.parse_supported_controls(bytes([0x30, 0x00]))
                         ['squelch_method_tx_name'], 'Host selects OMA or Pav')

    def test_a_control_the_module_lacks_is_refused(self):
        ctl = self._connect('mock_fr4x2')
        self.assertFalse(ctl['forced_squelch_tx'],
                         'this profile is meant to lack forced Tx squelch')
        rv = self._post('/api/module/squelch', {'tx_squelch_force': 0xFF})
        self.assertEqual(rv.status_code, 400,
                         'a control the module says it does not have was '
                         'written anyway')
        self.assertIn('01h:155.3', json.loads(rv.data)['message'])

    def test_the_datapath_controls_are_gated_too(self):
        ctl = self._connect('mock_fr4x2')
        self.assertFalse(ctl['output_polarity_flip_rx'])
        rv = self._post('/api/module/datapath', {'rx_polarity_flip_mask': 0xFF})
        self.assertEqual(rv.status_code, 400)
        self.assertIn('01h:156.0', json.loads(rv.data)['message'])

    def test_a_control_the_module_has_still_works(self):
        self._connect('mock_fr4x2')
        self.assertOk(self._post('/api/module/squelch',
                                 {'tx_squelch_disable': 0xFF}))
        self.assertOk(self._post('/api/module/datapath',
                                 {'tx_polarity_flip_mask': 0xFF}))

    def test_clearing_a_control_is_never_refused(self):
        """Writing zero asks for nothing, so it cannot be unsupported - and
        refusing it would leave a lane stuck in whatever it was."""
        self._connect('mock_fr4x2')
        self.assertOk(self._post('/api/module/squelch',
                                 {'tx_squelch_force': 0}))
        self.assertOk(self._post('/api/module/datapath',
                                 {'rx_polarity_flip_mask': 0}))


class TestThePanelsHideControlsTheModuleLacks(CMISTestCase):
    """A box that writes a register the module ignores is worse than no box:
    it is ticked, Apply reports success, and nothing happens."""

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_squelch_boxes_are_gated(self):
        js = self._js()
        body = js[js.index('async function loadSquelch('):]
        body = body[:body.index('\nasync function applySquelch(')]
        # The call existing is not the gate: pin each row to the capability
        # that decides it, or `true` passes just as well.
        for prefix, control in (("'sq'", 'auto_squelch_disable_tx'),
                                ("'sf'", 'forced_squelch_tx'),
                                ("'od'", 'output_disable_rx'),
                                ("'rd'", 'auto_squelch_disable_rx')):
            self.assertRegex(
                body,
                r'_gateBitmaskRow\(%s,\s*ctl\.%s !== false' % (prefix, control),
                'the %s row is offered whatever the module says' % prefix)

    def test_the_datapath_boxes_are_gated(self):
        js = self._js()
        body = js[js.index('async function loadDatapath('):]
        body = body[:body.index('\nasync function applyDatapath(')]
        for control in ('output_disable_tx', 'input_polarity_flip_tx',
                        'output_polarity_flip_rx'):
            self.assertIn(control, body,
                          '%s is offered whatever the module says' % control)

    def test_a_gated_box_is_actually_disabled(self):
        js = self._js()
        body = js[js.index('function _gateControl('):]
        body = body[:body.index('\n}')]
        self.assertRegex(body, r'el\.disabled = !supported',
                         'the gate does not disable anything')

    def test_a_disabled_tx_enable_box_is_not_read_as_disable(self):
        """An unticked Tx enable box means "disable this output". A box the
        module will not let you change must not be read that way."""
        js = self._js()
        body = js[js.index('async function applyDatapath('):]
        body = body[:body.index('\n}')]
        self.assertIn('txEn.disabled', body,
                      'a greyed-out Tx enable box sends a disable request')

    def test_the_panel_says_what_the_squelching_does(self):
        js = self._js()
        self.assertIn('squelch_method_tx_name', js,
                      'the panel never says whether squelching cuts OMA or Pav')
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        with open(path, encoding='utf-8') as f:
            self.assertIn('id="squelch-caps"', f.read())


class TestTheModuleSaysWhatDiagnosticsItHas(CMISTestCase):
    """13h:128-142 is the diagnostic advertisement: which loopbacks exist, how
    a measurement can be gated, and which patterns each generator and checker
    supports. No mock advertised any of it while accepting everything, and the
    tool read none of it while offering everything."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _prbs(self):
        return self.assertOk(self.client.get('/api/module/prbs'))['data']

    def _loopback(self):
        return self.assertOk(self.client.get('/api/module/loopback'))['data']

    def test_a_module_that_accepts_everything_advertises_something(self):
        for backend in ('mock_dr8', 'mock_coherent', 'mock_sr8',
                        'mock_fr4x2', 'mock_coherent_zr'):
            with self.subTest(backend=backend):
                self._connect(backend)
                caps = self._loopback()['capabilities']
                self.assertTrue(any(caps.values()),
                                '%s advertises no diagnostics at all while '
                                'accepting every loopback set on it' % backend)

    def test_the_supported_patterns_are_a_real_subset(self):
        """All sixteen would make the advertisement pointless to read."""
        self._connect()
        caps = self._prbs()['pattern_capabilities']
        for role in ('host_gen', 'media_gen', 'host_chk', 'media_chk'):
            self.assertTrue(caps[role], '%s supports no pattern at all' % role)
            self.assertNotIn(2, caps[role],
                             '%s claims every pattern, so nothing is gated'
                             % role)

    def test_a_pattern_the_generator_does_not_have_is_refused(self):
        self._connect()
        rv = self.client.post(
            '/api/module/prbs',
            data=json.dumps({'host_gen': {'enable_mask': 0xFF,
                                          'patterns': [2] * 8}}),
            content_type='application/json')
        self.assertEqual(rv.status_code, 400,
                         'the module was told to generate a pattern it does '
                         'not have')
        msg = json.loads(rv.data)['message']
        self.assertIn('PRBS23Q', msg)
        self.assertIn('13h:132', msg, 'the refusal does not say what it is '
                                      'going by')

    def test_a_pattern_it_does_have_still_works(self):
        self._connect()
        caps = self._prbs()['pattern_capabilities']['host_gen']
        self.assertOk(self.client.post(
            '/api/module/prbs',
            data=json.dumps({'host_gen': {'enable_mask': 0xFF,
                                          'patterns': [caps[0]] * 8}}),
            content_type='application/json'))

    def test_a_loopback_type_the_module_lacks_is_refused(self):
        """No shipped profile drops a type, so drive the advertisement
        directly - a real module that lacks one is common enough."""
        self._connect()
        # Writing the register dict needs no page select, and doing one here
        # would leave the API's page cache pointing somewhere else.
        backend = _state['backend']
        backend._registers[0x13][0x80] = 0x0E      # media side output cleared
        rv = self.client.post(
            '/api/module/loopback',
            data=json.dumps({'media_side_output': 0xFF}),
            content_type='application/json')
        self.assertEqual(rv.status_code, 400)
        self.assertIn('13h:128', json.loads(rv.data)['message'])

    def test_a_module_without_per_lane_loopback_takes_all_or_none(self):
        self._connect('mock_sr8')
        caps = self._loopback()['capabilities']
        self.assertFalse(caps['per_lane_host'],
                         'this profile is meant to be the less capable one')
        rv = self.client.post(
            '/api/module/loopback',
            data=json.dumps({'host_side_input': 0x0F}),
            content_type='application/json')
        self.assertEqual(rv.status_code, 400,
                         'four of eight lanes were accepted by a module that '
                         'moves them together')
        self.assertOk(self.client.post(
            '/api/module/loopback',
            data=json.dumps({'host_side_input': 0xFF}),
            content_type='application/json'))

    def test_host_and_media_together_are_refused_when_unsupported(self):
        self._connect('mock_sr8')
        rv = self.client.post(
            '/api/module/loopback',
            data=json.dumps({'host_side_input': 0xFF,
                             'media_side_output': 0xFF}),
            content_type='application/json')
        self.assertEqual(rv.status_code, 400)
        self.assertIn('same time', json.loads(rv.data)['message'])

    def test_the_measurement_capabilities_decode(self):
        """13h:129 carries the gating support the diagnostics rely on."""
        import cmis_registers as c
        caps = c.parse_diag_meas_caps(0x7C)
        self.assertEqual(caps['gating_support'], 1)
        self.assertTrue(caps['per_lane_gating_timers'])
        self.assertFalse(c.parse_diag_meas_caps(0x00)['gating_results'])


class TestTheDiagnosticsPanelOffersWhatTheModuleHas(CMISTestCase):
    """The pattern dropdown listed all thirteen names whatever the module
    said, and every loopback box was tickable on every module."""

    def _read(self, *parts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_pattern_list_comes_from_the_module(self):
        js = self._read('static', 'app.js')
        body = js[js.index('function _renderPrbsTable('):]
        body = body[:body.index('\nfunction ', 1)]
        self.assertRegex(body, r'const ids = supported && supported\.length',
                         'the dropdown is not built from what the module '
                         'advertises')

    def test_an_unadvertised_pattern_in_use_stays_visible(self):
        """Snapping the box to another value would quietly change what the
        module is being asked to do."""
        js = self._read('static', 'app.js')
        body = js[js.index('function _renderPrbsTable('):]
        body = body[:body.index('\nfunction ', 1)]
        self.assertIn('not advertised', body,
                      'a lane sitting on a pattern the module no longer '
                      'advertises is silently retargeted')

    def test_an_unsupported_loopback_box_cannot_be_ticked(self):
        js = self._read('static', 'app.js')
        body = js[js.index('function _populateLoopbackRow('):]
        body = body[:body.index('\n}')]
        self.assertIn('cb.disabled = true', body,
                      'a loopback the module does not have is still offered')

    def test_without_per_lane_the_boxes_move_together(self):
        js = self._read('static', 'app.js')
        body = js[js.index('function _populateLoopbackRow('):]
        body = body[:body.index('\n}')]
        # The name appearing is not the branch existing: pin the condition
        # and the loop that actually moves the other boxes.
        self.assertRegex(body, r'else if \(!perLane\)',
                         'per-lane boxes are shown for a module that engages '
                         'a whole side at once')
        branch = body[body.index('else if (!perLane)'):]
        branch = branch[:branch.index('});')]
        self.assertIn('other.checked = cb.checked', branch,
                      'ticking one box leaves the rest behind on a module '
                      'that moves a whole side together')

    def test_the_panel_says_what_the_module_will_not_do(self):
        html = self._read('templates', 'index.html')
        js = self._read('static', 'app.js')
        self.assertIn('id="loopback-caps"', html)
        self.assertIn("getElementById('loopback-caps')", js,
                      'the note element is never filled in')


class TestATuningRequestTheLaserCannotServe(CMISTestCase):
    """CMIS 5.4 Table 8-109: the module answers a tuning request in the Page
    12h Flags - InvalidChannelNumberFlagTx, TargetOutputPowerOORFlagTx and the
    rest, all RO/COR. The register was in the map and nothing read it, so the
    tool wrote whatever it was given and called it tuned."""

    def _connect_tunable(self, backend='mock_coherent_zr'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))
        return self.assertOk(self.client.get('/api/module/laser'))['data']

    def _tune(self, **fields):
        fields.setdefault('lane', 1)
        return self.client.post(
            '/api/module/laser',
            data=json.dumps({'lanes': [fields]}),
            content_type='application/json')

    def test_the_advertised_channel_range_is_read_at_all(self):
        """04h:130-165 says which channel numbers are legal on each grid. The
        register was defined and never read, so nothing could say."""
        d = self._connect_tunable()
        self.assertTrue(d['grid_channel_ranges'],
                        'the module advertises channel ranges and the tool '
                        'never reads them')

    def test_every_grid_the_module_offers_has_a_range(self):
        import cmis_registers as c
        d = self._connect_tunable()
        names = {c.GRID_CODES[int(code)]
                 for code in d['grid_channel_ranges']}
        self.assertEqual(set(d['grids_supported']), names,
                         'a grid is offered with no channel plan behind it')

    def test_a_channel_the_module_will_not_take_is_refused(self):
        self._connect_tunable()
        rv = self._tune(channel=9999)
        self.assertEqual(rv.status_code, 400,
                         'an impossible channel was written and reported as '
                         'tuned')
        msg = json.loads(rv.data)['message']
        self.assertIn('9999', msg)
        self.assertIn('04h:', msg, 'the message does not say which '
                                   'advertisement it is quoting')

    def test_an_out_of_range_offset_is_not_a_server_error(self):
        """50 GHz of fine tuning does not fit the S16 register, and the raw
        struct error came back as a 500."""
        self._connect_tunable()
        rv = self._tune(fine_tuning_enabled=True, fine_offset_ghz=50.0)
        self.assertEqual(rv.status_code, 400)
        self.assertNotIn('format requires', json.loads(rv.data)['message'],
                         'the operator is shown a struct error')

    def test_a_power_beyond_the_programmable_range_is_refused(self):
        self._connect_tunable()
        rv = self._tune(target_power_dbm=99.0)
        self.assertEqual(rv.status_code, 400)
        self.assertIn('99', json.loads(rv.data)['message'])

    def test_a_request_inside_the_range_still_works(self):
        d = self._connect_tunable()
        low, high = d['grid_channel_ranges'][str(d['lanes'][0]['grid_code'])]
        self.assertOk(self._tune(channel=min(10, high),
                                 target_power_dbm=d['power_range_dbm'][0]))
        time.sleep(0.3)
        lane = self.assertOk(
            self.client.get('/api/module/laser'))['data']['lanes'][0]
        self.assertEqual(lane['channel'], min(10, high))
        self.assertFalse([k for k, v in lane['tuning_flags'].items()
                          if v and k != 'tuning_complete'],
                         'a legal request raised a Flag')

    def test_the_module_refuses_what_slips_past_the_host(self):
        """The host checks what the module advertises; the Flags are the
        module's own answer, and the backstop when the two disagree."""
        self._connect_tunable()
        # Grid 0 has no advertised channel plan, so there is no host-side range
        # to check against - the module has to be the one to say no.
        self.assertOk(self._tune(grid_code=0, channel=7))
        time.sleep(0.3)
        lane = self.assertOk(
            self.client.get('/api/module/laser'))['data']['lanes'][0]
        self.assertIn('tuning_not_accepted', lane['tuning_flags_seen'],
                      'the module took a grid it has no channel plan for')

    def test_the_laser_does_not_move_on_a_refused_request(self):
        d = self._connect_tunable()
        before = d['lanes'][0]['frequency_thz']
        self.assertOk(self._tune(grid_code=0, channel=7))
        time.sleep(0.3)
        after = self.assertOk(
            self.client.get('/api/module/laser'))['data']['lanes'][0]
        self.assertEqual(after['frequency_thz'], before,
                         'the module reported tuning to a channel it refused')

    def test_a_tuning_flag_survives_the_read_that_reported_it(self):
        self._connect_tunable()
        self.assertOk(self._tune(grid_code=0, channel=7))
        time.sleep(0.3)
        for _ in range(3):
            lane = self.assertOk(
                self.client.get('/api/module/laser'))['data']['lanes'][0]
            self.assertIn('tuning_not_accepted', lane['tuning_flags_seen'],
                          'the only record of a refused tuning was lost')

    def test_the_write_says_what_the_module_made_of_it(self):
        self._connect_tunable()
        body = self.assertOk(self._tune(grid_code=0, channel=7))['data']
        self.assertIn('refused', body,
                      'the write reports success without asking the module')
        self.assertIn('tuning_not_accepted', body['refused'].get('1', []))


class TestTheTuningFlagsAreReadOnTheirOwn(CMISTestCase):
    """The write path reads the Flags too, which hides whether the read path
    does. Raise one behind the tool's back and ask only the GET."""

    def _connect_and_raise(self, byte_val=0x04):   # InvalidChannelNumberFlagTx
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_coherent_zr', 'bus': 0,
                             'address': 80}),
            content_type='application/json'))
        self.assertOk(self.client.get('/api/module/laser'))   # start clean
        self.assertOk(self.client.post('/api/module/flags/clear'))
        backend = _state['backend']
        backend._registers[0x12][0xE7] = byte_val
        backend._registers[0x12][0xE6] = 0x01

    def _lane1(self):
        return self.assertOk(
            self.client.get('/api/module/laser'))['data']['lanes'][0]

    def test_the_read_path_reports_a_live_flag(self):
        self._connect_and_raise()
        self.assertTrue(self._lane1()['tuning_flags']['invalid_channel_number'],
                        'the laser read never looks at the Flag register')

    def test_the_read_that_reports_it_clears_it(self):
        """Table 8-109 makes these RO/COR, which is the whole reason the
        history beside them has to exist."""
        self._connect_and_raise()
        self.assertTrue(self._lane1()['tuning_flags']['invalid_channel_number'])
        self.assertFalse(self._lane1()['tuning_flags']['invalid_channel_number'],
                         'the Flag survived the read that reported it')

    def test_the_read_path_remembers_it(self):
        self._connect_and_raise()
        self._lane1()                       # this read clears it on the module
        for _ in range(3):
            self.assertIn('invalid_channel_number',
                          self._lane1()['tuning_flags_seen'],
                          'the only record of a refused channel was lost')

    def test_clearing_the_history_clears_these_too(self):
        self._connect_and_raise()
        self._lane1()
        self.assertOk(self.client.post('/api/module/flags/clear'))
        self.assertEqual(self._lane1()['tuning_flags_seen'], [])


class TestTheTuningPanelOffersWhatTheModuleHas(CMISTestCase):
    """The grid dropdown listed all nine grid codes whatever the module said,
    and the channel box had no bounds at all."""

    def _read(self, *parts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_only_advertised_grids_are_offered(self):
        js = self._read('static', 'app.js')
        body = js[js.index('  const gridOpts = ('):]
        body = body[:body.index('\n  };')]
        self.assertRegex(body, r'const opts = advertised\.map\(',
                         'the dropdown is not built from what the module '
                         'advertises')
        self.assertNotIn("[5, '100 GHz'], [4, '50 GHz']", body,
                         'the hard-coded list of all nine grids is back')

    def test_the_channel_box_carries_the_advertised_bounds(self):
        js = self._read('static', 'app.js')
        idx = js.index('id="laser-ch-')
        cell = js[idx:idx + 500]
        self.assertIn('channel_range', cell,
                      'the channel box accepts any number with no hint of '
                      'what the module will take')
        self.assertIn('min=', cell)

    def test_the_laser_table_has_a_column_for_every_cell(self):
        html = self._read('templates', 'index.html')
        js = self._read('static', 'app.js')

        idx = html.index('id="tbl-laser"')
        head = html[html.rindex('<thead>', 0, idx):idx]
        columns = head.count('<th>')

        body = js[js.index('  tbody.innerHTML = d.lanes.map(l => {'):]
        row = body[body.index('return `<tr>'):body.index('</tr>`')]
        cells = row.count('<td>') + len(re.findall(r'<td\s', row))
        self.assertEqual(cells, columns,
                         'the laser table has %d headings and renders %d cells'
                         % (columns, cells))

        for placeholder in re.findall(r'colspan="(\d+)" class="placeholder-text"',
                                      js[js.index('async function loadLaser('):
                                         js.index('async function applyLaser(')]):
            self.assertEqual(int(placeholder), columns,
                             'an empty-state row spans the wrong width')


class TestAConfigurationTheModuleRefused(CMISTestCase):
    """CMIS 8.13.3 runs an Apply as acceptance, validation, execution and
    result feedback. A module only accepts an AppSelCode it advertises, and on
    a validation failure it "skips the following command execution step" - so
    the lane keeps running what it was running. No shipped mock ever refused
    anything, which left the whole rejection path unexercised."""

    def _apply(self, sel, backend='mock_dr8', connect=True):
        # Reconnecting rebuilds the module, so a test that cares what was
        # running before this Apply has to keep the session it set up.
        if connect:
            self.assertOk(self.client.post(
                '/api/connect',
                data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
                content_type='application/json'))
        reconfigure(self.client, sel)
        time.sleep(1.0)
        return (self.assertOk(self.client.get('/api/module/monitoring'))['data'],
                self.assertOk(self.client.get('/api/module/datapath'))['data'])

    def test_an_application_the_module_never_advertised_is_refused(self):
        self.connect()
        apps = self.assertOk(
            self.client.get('/api/module/applications'))['data']['applications']
        self.assertLess(len(apps), 14, 'pick a code beyond what this mock has')
        mon, _ = self._apply([14] * 8)
        for lane in mon['lanes']:
            self.assertEqual(lane['config_status'], 'ConfigRejectedInvalidAppSel',
                             'lane %d accepted an Application that does not '
                             'exist on this module' % lane['lane'])

    def test_deprovisioning_a_lane_is_always_allowed(self):
        """AppSelCode 0 means "no application", not "App 0"."""
        mon, _ = self._apply([0] * 8)
        for lane in mon['lanes']:
            self.assertFalse(lane['config_rejected'],
                             'lane %d refused to be deprovisioned'
                             % lane['lane'])

    def test_a_refused_lane_keeps_running_what_it_had(self):
        self.connect()
        reconfigure(self.client, [1] * 8)
        time.sleep(1.0)
        mon, dp = self._apply([2, 2, 2, 2, 14, 14, 14, 14], connect=False)

        for lane in dp['lanes'][:4]:
            self.assertEqual(lane['active_app_select'], 2)
        for lane in dp['lanes'][4:]:
            self.assertEqual(lane['app_select'], 14,
                             'the staged set should still hold the request')
            self.assertEqual(lane['active_app_select'], 1,
                             'a rejected lane was reconfigured anyway')

    def test_a_refused_lane_does_not_restart_its_data_path(self):
        """Execution is skipped, so the path never goes through DPInit - and
        DPStateChangedFlag is the evidence either way."""
        self.connect()
        # Stopping the path is itself a state change, so it has to happen
        # before the flags are cleared or every lane looks like it bounced.
        deactivated(self.client)
        self.client.get('/api/module/flags')          # start from a clean slate
        self.assertOk(self.client.post('/api/module/flags/clear'))
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [2, 2, 2, 2, 14, 14, 14, 14],
                             'dp_deinit_mask': 0x00, 'apply': True}),
            content_type='application/json'))
        time.sleep(1.0)
        lanes = self.assertOk(self.client.get('/api/module/flags'))['data']['lanes']
        bounced = [l['lane'] for l in lanes
                   if l['dp_state_changed'] or 'dp_state_changed' in l['seen']]
        self.assertEqual(bounced, [1, 2, 3, 4],
                         'the lanes the module refused restarted anyway')

    def test_an_accepted_apply_leaves_staged_and_active_agreeing(self):
        _mon, dp = self._apply([2] * 8)
        for lane in dp['lanes']:
            self.assertEqual(lane['app_select'], lane['active_app_select'],
                             'lane %d: the module accepted the configuration '
                             'but is not running it' % lane['lane'])

    def test_every_profile_refuses_the_same_way(self):
        for backend in ('mock_dr8', 'mock_coherent', 'mock_sr8',
                        'mock_fr4x2', 'mock_coherent_zr'):
            with self.subTest(backend=backend):
                mon, _ = self._apply([15] * 8, backend)
                self.assertTrue(all(l['config_rejected'] for l in mon['lanes']),
                                '%s accepted App 15' % backend)


class TestTheApplyProtocolRunsOnTheModulesOwnClock(CMISTestCase):
    """CMIS 8.13.3: acceptance, parameter validation, execution, result
    feedback. Both validation and execution act on the Staged Control Set as
    it stood when the Apply arrived, and the module runs that sequence on its
    own clock - not when the host happens to read."""

    def _stage_and_apply(self, sel):
        # Carries the release, so it works from a stopped Data Path as well as
        # a running one - these tests are about when the module acts, not
        # about what it will accept.
        return self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': sel, 'dp_deinit_mask': 0x00,
                             'apply': True}),
            content_type='application/json'))

    def _active(self):
        return self.assertOk(
            self.client.get('/api/module/datapath'))['data']['active_app_select']

    def test_a_second_apply_does_not_erase_the_first(self):
        """The state machine used to advance only inside read_bytes, so two
        Applies with no read between them lost the first one outright."""
        self.connect()
        reconfigure(self.client, [2] * 8)
        time.sleep(1.0)                       # deliberately no read here
        self._stage_and_apply([14] * 8)       # refused, so it changes nothing
        time.sleep(1.0)
        self.assertEqual(self._active(), [2] * 8,
                         'the first Apply never reached its result step')

    def test_an_intervening_read_changes_nothing(self):
        self.connect()
        reconfigure(self.client, [2] * 8)
        time.sleep(1.0)
        self.client.get('/api/module/datapath')
        self._stage_and_apply([14] * 8)
        time.sleep(1.0)
        self.assertEqual(self._active(), [2] * 8,
                         'the outcome depends on whether anyone was looking')

    def test_an_apply_during_one_already_running_is_ignored(self):
        """Step (1): if any relevant lane still reads ConfigInProgress the
        module "aborts all further command handling steps for the relevant
        Data Path silently (without feedback)"."""
        self.connect()
        # From DPDeactivated both commands would be accepted on their own, so
        # what is being tested is the readiness check and not the width rule.
        deactivated(self.client)
        self._stage_and_apply([2] * 8)
        self._stage_and_apply([1] * 8)        # arrives while still in progress
        time.sleep(1.2)
        self.assertEqual(self._active(), [2] * 8,
                         'a command sent during ConfigInProgress took effect')

    def test_the_apply_uses_what_was_staged_when_it_arrived(self):
        """Committing whatever 10h holds at the completion step let a late
        Apply install a configuration nobody had validated."""
        self.connect()
        reconfigure(self.client, [2] * 8)
        time.sleep(1.0)
        # Stage something invalid but do not apply it.
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [14] * 8, 'apply': False}),
            content_type='application/json'))
        time.sleep(0.6)
        self.assertEqual(self._active(), [2] * 8,
                         'staging alone changed what the module is running')

    def test_the_ui_waits_for_the_result_before_calling_it_a_success(self):
        """ConfigInProgress is not a rejection, so reading ConfigStatus once
        and immediately reported an unfinished Apply as applied."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('async function applyDatapath('):]
        body = body[:body.index('\n}')]
        self.assertIn('ConfigInProgress', body,
                      'the apply path never looks at whether the module has '
                      'finished')
        # `while (` on its own is satisfied by `while (false)`. Pin what the
        # loop actually tests: still in progress, and not yet out of time.
        loop = body[body.index('while ('):]
        loop = loop[:loop.index('{')]
        self.assertIn('ConfigInProgress', loop,
                      'it checks once instead of waiting for the result')
        self.assertIn('deadline', loop,
                      'a module stuck in ConfigInProgress hangs the wait')


class TestTheWholeNegativeRangeCountsAsRejection(CMISTestCase):
    """Table 8-101 labels 2h-Bh and Dh-Fh "Negative Result Status" as one
    block. Deciding by whether the decoded name begins with "ConfigRejected"
    missed 8h - which the tool did not even name - and every reserved and
    custom code, so a refused configuration got a success toast."""

    def test_the_spec_codes_are_all_named(self):
        import cmis_registers as c
        for code, name in ((0x1, 'ConfigSuccess'),
                           (0x3, 'ConfigRejectedInvalidAppSel'),
                           (0x6, 'ConfigRejectedLanesInUse'),
                           (0x8, 'ConfigRejectedNoEmulation'),
                           (0xC, 'ConfigInProgress')):
            self.assertEqual(c.CONFIG_STATUS_NAMES.get(code), name,
                             'code 0x%X' % code)

    def test_every_negative_code_is_treated_as_a_rejection(self):
        import cmis_registers as c
        for code in list(range(0x2, 0xC)) + list(range(0xD, 0x10)):
            self.assertIn(code, c.CONFIG_STATUS_REJECTED,
                          '0x%X is in the negative range of Table 8-101' % code)

    def test_success_and_progress_are_not_rejections(self):
        import cmis_registers as c
        for code in (0x0, 0x1, 0xC):
            self.assertNotIn(code, c.CONFIG_STATUS_REJECTED,
                             '0x%X is not a negative result status' % code)

    def test_the_ui_asks_the_api_not_the_spelling(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('async function applyDatapath('):]
        body = body[:body.index('\n}')]
        self.assertIn('config_rejected', body,
                      'the apply path decides by name prefix again')
        self.assertNotIn("startsWith('ConfigRejected')", body,
                         'a code the tool cannot name reads as accepted')

    def test_the_lane_row_says_what_the_module_is_actually_running(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('async function loadDatapath('):]
        self.assertIn('active_app_select', body,
                      'the page shows the request and never the reality')
        self.assertIn('appsel-mismatch', body,
                      'a refused Application looks exactly like a live one')
        # The marker existing is not the same as it being reachable: pin the
        # comparison that decides whether a lane gets one. This assertion used
        # to carry the `active &&` guard with it, which is how the guard
        # survived: AppSelCode 0 is falsy, so the one lane state the dropdown
        # cannot show on its own was also the one with no warning under it.
        self.assertRegex(
            body, r'const stale = active !== lane\.app_select',
            'the mismatch marker is never actually chosen')
        self.assertNotRegex(
            body, r'const stale = active &&',
            'a lane the module is running nothing on is skipped')


class TestTheWatchTellsTheTruthWhenItCannotRead(CMISTestCase):
    """The header chips are the only signal on every tab but Monitoring, and
    the poll that feeds them had no failure path at all: a dead server left
    the last chips sitting there, and an empty slot reads as "all clear"."""

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def _poll_body(self):
        js = self._js()
        body = js[js.index('async function pollHealth('):]
        return body[:body.index('\n}')]

    def test_a_failed_read_repaints_the_header(self):
        body = self._poll_body()
        fail = body[body.index("status.status !== 'ok'"):]
        self.assertIn('_paintChips(', fail,
                      'a module nobody can reach keeps the chips it had when '
                      'contact was lost')

    def test_it_says_so_once_and_not_every_five_seconds(self):
        """loadMonitoring stops its own auto-refresh to avoid exactly this."""
        body = self._poll_body()
        self.assertIn('_healthLost', body,
                      'the watch has no way to tell a new failure from the '
                      'same one continuing, so it toasts on every poll')
        fail = body[body.index("status.status !== 'ok'"):]
        toast_at = fail.index('toast(')
        self.assertIn('if (!_healthLost)', fail[:toast_at],
                      'the lost-contact toast is not guarded by the transition')

    def test_a_slow_module_does_not_stack_polls(self):
        """The monitoring loop already needed this: a wide module over a slow
        adapter reads for longer than the interval."""
        body = self._poll_body()
        # The assignment alone is not the guard: pin the early return.
        self.assertRegex(body, r'if \(_healthInFlight\)\s*return',
                         'polls pile up on a module slower than the interval')
        self.assertIn('_healthInFlight = false', body,
                      'one slow read wedges the watch for good')

    def test_a_fresh_connection_does_not_inherit_the_last_failure(self):
        js = self._js()
        body = js[js.index('function startHealthWatch('):]
        body = body[:body.index('\n}')]
        self.assertIn('_healthLost = false', body,
                      'connecting a working module still reports lost contact')


class TestAServerThatForgotTheModule(CMISTestCase):
    """The built-in updater restarts the server on purpose, and the page stays
    open across it. The new process has no connection, so every endpoint
    answers 503 while the indicator still reads Connected and each button
    fails on its own."""

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_server_really_answers_503(self):
        """_sessionGone keys off the status code, so pin the contract it
        depends on rather than trusting the wording of a message."""
        rv = self.client.get('/api/module/status')
        self.assertEqual(rv.status_code, 503)

    def test_the_client_can_see_the_status_code(self):
        js = self._js()
        body = js[js.index('async function apiFetch('):]
        body = body[:body.index('\n}')]
        self.assertIn('resp.status', body,
                      'the only thing distinguishing a lost session from a '
                      'read error is discarded before anyone can look at it')

    def test_both_loops_end_the_session(self):
        js = self._js()
        for start, what in ((js.index('async function pollHealth('),
                             'the background watch'),
                            (js.index('async function _loadMonitoringOnce('),
                             'the monitoring loop')):
            body = js[start:]
            body = body[:body.index('\n}')]
            self.assertIn('_sessionGone(', body,
                          '%s keeps reporting on a module the server has '
                          'forgotten' % what)
            self.assertIn('_endSession(', body,
                          '%s leaves the UI claiming Connected' % what)

    def test_ending_the_session_does_not_ask_the_server(self):
        """The server has already forgotten us; a round trip to confirm it can
        only fail, and would leave the UI wrong if it did."""
        js = self._js()
        body = js[js.index('function _endSession('):]
        body = body[:body.index('\n}')]
        self.assertNotIn('apiGet(', body,
                         'the teardown calls the server it just decided is gone')
        for expected in ('updateConnectionUI(false', 'stopHealthWatch()',
                         'stopMonitoring()'):
            self.assertIn(expected, body,
                          'the teardown leaves %s undone' % expected)


class TestAConnectedModuleIsNeverLeftUnwatched(CMISTestCase):
    """The flag history answers "what happened while I was away". Switching
    tabs is a way of being away, and it used to stop every poll: on any tab
    but Monitoring the tool read nothing, so the history recorded nothing."""

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_leaving_the_monitoring_tab_hands_the_poll_over(self):
        js = self._js()
        body = js[js.index('function switchTab('):]
        body = body[:body.index("if (name === 'info')")]
        stop = body.index('stopMonitoring()')
        self.assertIn('startHealthWatch()', body[stop:stop + 200],
                      'leaving Monitoring stops polling and starts nothing')

    def test_the_watch_reads_the_flags_not_just_the_status(self):
        """Reading /status alone leaves the lane history frozen: the server
        accumulates it from the flags read."""
        body = self._js()
        body = body[body.index('async function pollHealth('):]
        body = body[:body.index('\n}')]
        self.assertIn('/api/module/flags', body,
                      'the background watch never reads the lane flags, so a '
                      'bounce off-tab is lost')

    def test_a_disconnect_stops_it(self):
        js = self._js()
        body = js[js.index('async function disconnectModule('):]
        body = body[:body.index('\n}')]
        # The teardown is shared with the lost-session path; either route
        # has to stop the watch, and TestAServerThatForgotTheModule pins
        # that _endSession itself does.
        self.assertIn('_endSession(', body,
                      'the watch keeps polling a module that is gone')
        teardown = js[js.index('function _endSession('):]
        teardown = teardown[:teardown.index("\n}")]
        self.assertIn('stopHealthWatch()', teardown,
                      'the watch keeps polling a module that is gone')

    def test_the_two_loops_do_not_both_run(self):
        """Double-polling doubles the I2C traffic on real hardware."""
        js = self._js()
        body = js[js.index('function switchTab('):]
        body = body[:body.index("if (name === 'info')")]
        self.assertIn('stopHealthWatch()', body,
                      'entering Monitoring leaves the background watch running '
                      'alongside it')

    def test_the_chips_lead_to_the_detail(self):
        """An indicator that says something happened without saying where is
        worse than none: it starts a hunt across four tabs."""
        js = self._js()
        body = js[js.index('function renderHealthIndicator('):]
        body = body[:body.index("\n}")]
        self.assertIn('_paintChips(', body, 'the chips are never painted')
        painter = js[js.index('function _paintChips('):]
        painter = painter[:painter.index("\n}")]
        self.assertIn("switchTab('monitoring')", painter,
                      'the header alert is a dead end')

    def test_clearing_the_history_blanks_the_chips_at_once(self):
        """The poll is 5 s. Leaving the chips lit that long after the button
        is pressed reads as the button not working."""
        js = self._js()
        body = js[js.index("btn-clear-flag-history"):]
        body = body[:body.index('loadMonitoring();')]
        self.assertIn('renderHealthIndicator(', body,
                      'Clear flag history leaves the header lit until the '
                      'next poll comes round')

    def test_the_header_slot_exists_to_render_into(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        head = html[html.index('<header>'):html.index('</header>')]
        self.assertIn('id="header-alert"', head,
                      'the only always-visible alert slot is not in the header')


class TestAPatternCheckerThatSlipped(CMISTestCase):
    """Table 8-138 makes the Page 14h diagnostic flags RO/COR. A checker that
    lost lock part-way through a long run is the whole reason for running one,
    and it reads as locked again one refresh later."""

    def _slip(self, lanes_mask):
        backend = _state['backend']
        backend._registers[0x14][0x8A] = lanes_mask

    def test_lock_loss_is_latched_and_then_remembered(self):
        self.connect()
        self.client.get('/api/module/prbs')            # start from clean
        self._slip(0b00000101)                         # lanes 1 and 3

        d = self.assertOk(self.client.get('/api/module/prbs'))['data']
        self.assertEqual(d['host_chk_lol_mask'], 0b00000101)
        self.assertEqual(d['host_chk_lol_seen'][:4], [True, False, True, False])

        d = self.assertOk(self.client.get('/api/module/prbs'))['data']
        self.assertEqual(d['host_chk_lol_mask'], 0,
                         'the flag survived the read that reported it')
        self.assertEqual(d['host_chk_lol_seen'][:4], [True, False, True, False],
                         'a checker that slipped now reads as if it never had')

    def test_the_checker_row_marks_a_lane_that_slipped(self):
        js = self._read('static', 'app.js')
        idx = js.index('function _renderPrbsTable(')
        body = js[idx:js.index('\nfunction ', idx + 1)]
        self.assertIn('lolSeen', body, 'the render ignores the slip history')
        self.assertIn('flag-was', body,
                      'a checker that slipped looks like one that never did')
        # Nothing here runs the script, so pin the expression that decides it:
        # the marker existing is not the same as it being reachable.
        self.assertRegex(body, r'slipped\s*=\s*!!\(\s*lolSeen\s*&&\s*lolSeen\[',
                         'the slip marker is never actually chosen')

    def _read(self, *parts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()


class TestABouncedDataPathIsNotSilent(CMISTestCase):
    """CMIS 6.3.3: the module sets DPStateChangedFlag when a data path reaches
    a lasting steady state through a real transition. It is the module's own
    record that a link went down and came back - and since both ends read
    Activated, it is the only sign anything happened. The tool defined the
    register at 11h:134 and never read it."""

    def _lane_flags(self):
        return self.assertOk(self.client.get('/api/module/flags'))['data']['lanes']

    def test_the_flag_is_read_at_all(self):
        self.connect()
        lane = self._lane_flags()[0]
        self.assertIn('dp_state_changed', lane,
                      'the register the module records a bounce in is not read')

    def test_a_bounce_is_reported_and_then_remembered(self):
        self.connect()
        self.assertFalse(any(l['dp_state_changed'] for l in self._lane_flags()),
                         'a settled module claims its paths just changed')

        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [1] * 8, 'apply': True}),
            content_type='application/json'))
        deadline = time.time() + 3.0
        while time.time() < deadline:
            lanes = self._lane_flags()
            if any(l['dp_state_changed'] for l in lanes):
                break
            time.sleep(0.05)
        self.assertTrue(any(l['dp_state_changed'] for l in lanes),
                        'the path went through DPInit and came back, unremarked')

        # It is a Flag: the read that reported it cleared it on the module, and
        # the state now reads Activated at both ends. The history is all that
        # is left of the event.
        after = self._lane_flags()
        self.assertFalse(any(l['dp_state_changed'] for l in after),
                         'the flag survived the read that reported it')
        self.assertIn('dp_state_changed', after[0]['seen'],
                      'the only record that the path bounced was discarded')

    def test_a_reset_marks_every_lane(self):
        """Every path came back through DPInit, so every lane has changed."""
        self.connect()
        self.client.get('/api/module/flags')          # clear what connecting set
        self.assertOk(self.client.post(
            '/api/module/control',
            data=json.dumps({'software_reset': True}),
            content_type='application/json'))
        deadline = time.time() + 4.0
        marked = []
        while time.time() < deadline:
            lanes = self._lane_flags()
            marked = [l['lane'] for l in lanes if l['dp_state_changed']]
            if len(marked) == len(lanes):
                break
            time.sleep(0.05)
        self.assertEqual(len(marked), 8,
                         'a reset restarted every path but marked %s' % marked)

    def test_the_table_has_a_column_for_every_cell_it_renders(self):
        """The renderer emits one cell per column by hand. Adding a cell
        without a heading shifts every reading one column left, which is
        exactly as wrong as a bad value and much harder to notice."""
        html = self._read('templates', 'index.html')
        js = self._read('static', 'app.js')

        idx = html.index('id="tbl-flags"')
        head = html[html.rindex('<thead>', 0, idx):idx]
        columns = head.count('<th>')

        body = js[js.index('function renderFlags('):]
        row = body[body.index('return `<tr>'):body.index('</tr>`')]
        cells = row.count('<td>') + len(re.findall(r'<td\$\{', row))
        self.assertEqual(cells, columns,
                         'the flags table has %d headings and renders %d cells'
                         % (columns, cells))

        placeholder = re.search(r'colspan="(\d+)" class="placeholder-text"',
                                html[idx:idx + 300])
        self.assertEqual(int(placeholder.group(1)), columns,
                         'the empty-state row spans the wrong number of columns')

    def _read(self, *parts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()


class TestLatchedFlagsSurviveBeingRead(CMISTestCase):
    """CMIS 5.4: "a Flag bit remains set (latched) until cleared by a READ of
    the Byte containing the Flag". Polling therefore consumes them. A fault
    that came and went between two refreshes lands in exactly one reply, and
    if nothing remembers it, it is gone - which is the opposite of what a tool
    for chasing intermittent links is for."""

    def _blip_backend(self):
        import copy
        import i2c_interface
        from i2c_backends import mock

        class Blip(mock.MockBackend):
            PROFILE = copy.deepcopy(mock._DR8_800G)
            dark = False

            def _update_dynamic_values(self):
                super()._update_dynamic_values()
                if self.dark:
                    self._registers[0x11][0xBA] = 0x00
                    self._registers[0x11][0xBB] = 0x14      # 2 uW, far under
                    self._set_lane_flags(0, self.PROFILE['tx_power_uw_nom'],
                                         self.PROFILE['tx_bias_ma_nom'], 2.0)

        i2c_interface._BACKENDS['blip'] = Blip
        self.addCleanup(i2c_interface._BACKENDS.pop, 'blip', None)
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'blip', 'bus': 0, 'address': 80}),
            content_type='application/json'))
        return _state['backend']

    def _lane1(self):
        d = self.assertOk(self.client.get('/api/module/flags'))['data']
        return d['lanes'][0]

    def _now(self, lane):
        return {k for k, v in lane.items() if v is True}

    def test_the_module_holds_a_fault_until_somebody_reads_it(self):
        """The mock has to latch, or the tool's handling of latched flags can
        never be exercised and the demo cannot show a transient at all."""
        backend = self._blip_backend()
        self.assertEqual(self._now(self._lane1()), set())

        backend.dark = True
        self.client.get('/api/module/monitoring')     # the fault happens
        backend.dark = False
        self.client.get('/api/module/monitoring')     # and it is over

        held = self._now(self._lane1())
        # Both kinds: loss of signal comes from its own branch, the power
        # alarms from the threshold comparison, and either could stop latching
        # on its own.
        self.assertIn('rx_los', held,
                      'the module forgot a fault nobody had read yet')
        self.assertIn('rx_power_low_alarm', held,
                      'the threshold flags stopped latching')

    def test_a_read_clears_it_on_the_module_the_way_the_spec_says(self):
        backend = self._blip_backend()
        backend.dark = True
        self.client.get('/api/module/monitoring')
        backend.dark = False
        self.client.get('/api/module/monitoring')

        self.assertIn('rx_los', self._now(self._lane1()))
        self.assertNotIn('rx_los', self._now(self._lane1()),
                         'the flag survived the read that reported it')

    def test_the_tool_remembers_what_the_read_destroyed(self):
        backend = self._blip_backend()
        backend.dark = True
        self.client.get('/api/module/monitoring')
        backend.dark = False
        self.client.get('/api/module/monitoring')

        first = self._lane1()
        self.assertIn('rx_los', first['seen'])
        for _ in range(3):
            later = self._lane1()
            self.assertEqual(self._now(later), set(), 'the fault is over')
            self.assertIn('rx_los', later['seen'],
                          'the only record of the fault was thrown away')

    def test_the_operator_can_start_the_history_again(self):
        backend = self._blip_backend()
        backend.dark = True
        self.client.get('/api/module/monitoring')
        backend.dark = False
        self.client.get('/api/module/monitoring')
        self.assertIn('rx_los', self._lane1()['seen'])

        self.assertOk(self.client.post('/api/module/flags/clear'))
        self.assertEqual(self._lane1()['seen'], [],
                         'clearing the history left something behind')

    def test_the_next_module_starts_with_a_clean_sheet(self):
        backend = self._blip_backend()
        backend.dark = True
        self.client.get('/api/module/monitoring')
        backend.dark = False
        self.assertIn('rx_los', self._lane1()['seen'])

        self.client.post('/api/disconnect')
        self.connect()
        self.assertEqual(self._lane1()['seen'], [],
                         "one module's history followed another")

    def test_the_page_shows_fired_earlier_differently_from_set_now(self):
        """Not set now and never happened look identical in the register. They
        are not the same thing to anyone chasing an intermittent link."""
        js = self._read('static', 'app.js')
        idx = js.index('function renderFlags(')
        body = js[idx:idx + 2000]
        self.assertIn('lane.seen', body, 'the render ignores the history')
        self.assertIn('flag-was', body,
                      'a flag that fired earlier looks like one that never did')
        # The marker existing is not the same as it being reachable: pin the
        # condition that produces it, since nothing here executes the script.
        self.assertRegex(body, r'seen\.has\(\s*name\s*\)',
                         'the fired-earlier marker is never actually chosen')
        css = self._read('static', 'style.css')
        self.assertIn('.flag-was', css, 'the marker has no styling')
        html = self._read('templates', 'index.html')
        self.assertIn('btn-clear-flag-history', html,
                      'there is no way to start the history again')
        self.assertIn('latched', html,
                      'nothing on the page explains why a flag vanishes')

    def _read(self, *parts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()


class TestFlagsFollowTheReadings(CMISTestCase):
    """A module that reports a value outside its own limits and raises nothing
    contradicts itself: the display colours the cell from the threshold while
    the module insists it is fine. Whichever the reader believes, they learn
    that the flags are not worth reading."""

    def _fixture(self, name, **overrides):
        import copy
        import i2c_interface
        from i2c_backends import mock
        profile = copy.deepcopy(mock._DR8_800G)
        profile.update(overrides)
        profile['display'] = name
        i2c_interface._BACKENDS[name] = type(
            'Fx', (mock.MockBackend,), {'PROFILE': profile})
        self.addCleanup(i2c_interface._BACKENDS.pop, name, None)
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': name, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _lane_flags(self):
        d = self.assertOk(self.client.get('/api/module/flags'))['data']
        return {k for lane in d['lanes'] for k, v in lane.items() if v is True}

    def test_every_shipped_profile_is_quiet_when_healthy(self):
        """Three profiles had their Rx nominal sitting exactly on the generic
        high warning, so a healthy module flickered a warning. The earlier
        sweep only compared against alarms, which is how that survived."""
        import i2c_interface
        import i2c_backends            # noqa: F401
        for name in sorted(n for n in i2c_interface._BACKENDS if n.startswith('mock')):
            self.assertOk(self.client.post(
                '/api/connect',
                data=json.dumps({'backend': name, 'bus': 0, 'address': 80}),
                content_type='application/json'))
            self.assertEqual(self._lane_flags(), set(),
                             '%s raises a flag with nothing wrong' % name)
            status = self.assertOk(self.client.get('/api/module/status'))['data']
            self.assertFalse(status['alarm_active'],
                             '%s announces a module alarm while healthy' % name)
            self.client.post('/api/disconnect')

    def test_a_reading_under_its_low_alarm_raises_the_flag(self):
        self._fixture('flags_dark_rx', rx_power_uw_nom=5)      # about -23 dBm
        t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        m = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        self.assertLess(m['lanes'][0]['rx_power_dbm'], t['rx_power_low_alarm_dbm'],
                        'the fixture is not actually below its alarm')
        flags = self._lane_flags()
        self.assertIn('rx_power_low_alarm', flags)
        self.assertIn('rx_los', flags, 'a receiver with no light is not in LOS')

    def test_a_reading_over_its_high_alarm_raises_the_flag(self):
        # Above the 120 mA alarm but inside what the register can hold:
        # the two are only about 5 mA apart.
        self._fixture('flags_hot_bias', tx_bias_ma_nom=122.0)
        t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        m = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        self.assertGreater(m['lanes'][0]['tx_bias_ma'], t['tx_bias_high_alarm_ma'])
        self.assertIn('tx_bias_high_alarm', self._lane_flags())

    def test_a_nominal_too_big_for_its_register_is_refused(self):
        """The monitor registers are 16 bits. A nominal that does not fit used
        to wrap and report a smaller, entirely plausible number - 200 mA of
        bias came back as 68.9 - which is worse than not starting."""
        import copy
        import i2c_interface
        from i2c_backends import mock
        for key, value in (('tx_bias_ma_nom', 200.0),
                           ('tx_power_uw_nom', 20000),
                           ('rx_power_uw_nom', 20000)):
            profile = copy.deepcopy(mock._DR8_800G)
            profile[key] = value
            cls = type('Overflow', (mock.MockBackend,), {'PROFILE': profile})
            with self.assertRaises(ValueError, msg='%s = %g was accepted' % (key, value)):
                cls().connect(0, 0x50)

    def test_the_module_alarm_is_not_a_timer(self):
        """The summary byte used to flip every thirty seconds regardless, so a
        module at 55 C in a 0-80 C window announced a temperature alarm twice a
        minute. It has to come from the reading."""
        self._fixture('flags_hot_case', temperature_c_nom=95.0)
        s = self.assertOk(self.client.get('/api/module/status'))['data']
        self.assertTrue(s['temp_high_alarm'], 'a module at 95 C reports no alarm')
        self.assertTrue(s['alarm_active'])
        self.client.post('/api/disconnect')

        self.connect()                                  # healthy again
        for _ in range(4):
            s = self.assertOk(self.client.get('/api/module/status'))['data']
            self.assertFalse(s['alarm_active'],
                             'a healthy module announced an alarm at %.1f C'
                             % s['temperature_c'])


class TestVeryWideModules(CMISTestCase):
    """CMIS 5.4 allows 256 lanes and the widest shipped mock is 16, so the
    banked path past two banks had never been driven. These register throwaway
    profiles rather than shipping ones nobody would pick from the dropdown."""

    WIDTHS = (24, 32, 256)

    @classmethod
    def setUpClass(cls):
        import copy
        import i2c_interface
        from i2c_backends import mock
        for lanes in cls.WIDTHS:
            profile = copy.deepcopy(mock._XD16_1600G)
            profile['lanes'] = lanes
            profile['display'] = '%d-lane fixture' % lanes
            i2c_interface._BACKENDS['test_%dlane' % lanes] = type(
                'Fixture%d' % lanes, (mock.MockBackend,), {'PROFILE': profile})

    @classmethod
    def tearDownClass(cls):
        import i2c_interface
        for lanes in cls.WIDTHS:
            i2c_interface._BACKENDS.pop('test_%dlane' % lanes, None)

    def _connect(self, lanes):
        rv = self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'test_%dlane' % lanes, 'bus': 0,
                             'address': 80}),
            content_type='application/json'))
        return rv

    def test_a_lane_count_the_legacy_field_cannot_spell_uses_the_escape(self):
        """01h:142 encodes one, two or four banks and nothing else. A 24-lane
        module rounded up to 32 there, advertising eight lanes that do not
        exist; every table then sized itself to the wrong module."""
        rv = self._connect(24)
        self.assertEqual(rv['data']['lanes'], 24)
        d = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        self.assertEqual([l['lane'] for l in d['lanes']], list(range(1, 25)))

    def test_every_lane_is_read_and_written_at_any_width(self):
        for lanes in self.WIDTHS:
            self._connect(lanes)
            for path, key in (('/api/module/monitoring', 'lanes'),
                              ('/api/module/flags', 'lanes'),
                              ('/api/module/ber', 'lanes')):
                d = self.assertOk(self.client.get(path))['data']
                self.assertEqual(len(d[key]), lanes,
                                 '%s at %d lanes' % (path, lanes))

            # A different value per bank, so a mask folded onto bank 0 cannot
            # read back looking correct.
            banks = (lanes + 7) // 8
            want = [(b * 7 + 1) & 0xFF for b in range(banks)]
            self.assertOk(self.client.post(
                '/api/module/squelch',
                data=json.dumps({'tx_squelch_disable': want}),
                content_type='application/json'))
            got = self.assertOk(self.client.get('/api/module/squelch'))['data']
            self.assertEqual(got['tx_squelch_disable_banks'], want,
                             'squelch at %d lanes' % lanes)
            self.client.post('/api/disconnect')

    def test_the_last_lane_exists_and_the_one_past_it_does_not(self):
        for lanes in self.WIDTHS:
            self._connect(lanes)
            self.assertOk(self.client.post(
                '/api/module/acq_counters/reset',
                data=json.dumps({'lanes': [lanes]}),
                content_type='application/json'))
            self.assertErr(self.client.post(
                '/api/module/acq_counters/reset',
                data=json.dumps({'lanes': [lanes + 1]}),
                content_type='application/json'), 400)
            self.client.post('/api/disconnect')

    def test_a_lane_count_that_is_not_a_group_of_eight_is_refused(self):
        """CMIS counts lanes in groups of eight. A profile saying 20 would be
        advertised as 16 and quietly lose four."""
        import copy
        from i2c_backends import mock
        profile = copy.deepcopy(mock._XD16_1600G)
        profile['lanes'] = 20
        cls = type('Bad20', (mock.MockBackend,), {'PROFILE': profile})
        # The register map is built up front, so the refusal lands there
        # rather than waiting for a read to notice.
        with self.assertRaises(ValueError):
            cls().connect(0, 0x50)

    def test_the_refresh_loop_does_not_queue_reads_it_cannot_finish(self):
        """A 256-lane read is 32 bank changes, each owing the spec 10 ms, so it
        outlasts the refresh interval. Ticking regardless would stack requests
        the server takes one at a time - the display falls further behind every
        tick and the module is hammered."""
        js = self._read('static', 'app.js')
        self.assertIn('_monitoringInFlight', js,
                      'the refresh loop has no in-flight guard')
        idx = js.index('async function loadMonitoring(')
        head = js[idx:idx + 400]
        self.assertIn('if (_monitoringInFlight) return;', head,
                      'loadMonitoring does not skip a tick while one is out')
        # The guard has to be released in a finally, and released there: a
        # finally that no longer clears it leaves monitoring dead after the
        # first read that throws, with no error to show for it.
        released = re.search(r'finally\s*\{[^}]*_monitoringInFlight\s*=\s*false',
                             head)
        self.assertIsNotNone(
            released, 'the in-flight guard is not cleared in a finally, so one '
                      'failed read would stop the refresh for good')

    def _read(self, *parts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()


class TestCmis54Pages(CMISTestCase):
    """The optional pages CMIS 5.4 added. Mock-only: no module that carries
    them was available."""

    def _connect54(self):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_1600g_16lane', 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def test_the_new_pages_are_read_when_advertised(self):
        self._connect54()
        d = self.assertOk(self.client.get('/api/module/ext54'))['data']
        self.assertEqual(set(d['available']), {'0Ch', '60h', '61h', '62h', '6Dh'})
        self.assertEqual(len(d['polarity_status']), 16)
        self.assertEqual(len(d['acquisition_counters']), 16)
        self.assertEqual(len(d['lane_power_thresholds']), 16)
        self.assertTrue(d['consolidated_pm']['supported'])
        self.assertEqual(d['consolidated_pm']['defined_in'], '5.4')

    def test_a_module_without_them_exposes_nothing(self):
        """An unsupported page is not required to answer meaningfully, so
        reading it anyway would turn whatever was left on the bus into
        per-lane numbers that look like readings."""
        self.connect()
        d = self.assertOk(self.client.get('/api/module/ext54'))['data']
        self.assertEqual(d['available'], {})
        self.assertNotIn('acquisition_counters', d)
        self.assertErr(self.client.post(
            '/api/module/acq_counters/reset',
            data=json.dumps({'lanes': [1]}), content_type='application/json'), 400)

    def test_counter_reset_masks_are_grouped_by_bank(self):
        """Lane 9 is bit 0 of bank 1, not bit 8 of anything. Folding it into
        bank 0's mask would clear lane 1's counter and leave lane 9 running -
        the reset would look like it worked and the wrong counter would move.
        """
        self._connect54()
        backend = _state['backend']
        real = backend.write_bytes
        seen = []

        def spy(addr, data):
            if addr in (0x7E, 0xC0, 0xC1):
                seen.append((addr, bytes(data), backend._current_bank))
            return real(addr, data)

        backend.write_bytes = spy
        try:
            self.assertOk(self.client.post(
                '/api/module/acq_counters/reset',
                data=json.dumps({'lanes': [1, 3, 9], 'side': 'rx'}),
                content_type='application/json'))
        finally:
            backend.write_bytes = real

        writes = [(bank, data[0]) for addr, data, bank in seen if addr == 0xC0]
        self.assertIn((0, 0b00000101), writes, 'lanes 1 and 3 belong to bank 0')
        self.assertIn((1, 0b00000001), writes, 'lane 9 is bit 0 of bank 1')

        self.assertErr(self.client.post(
            '/api/module/acq_counters/reset',
            data=json.dumps({'lanes': []}), content_type='application/json'), 400)

    def test_the_reset_actually_zeroes_the_lanes_it_names(self):
        """The masks were right while the counters never moved, because the
        mock stored the command instead of acting on it. A reset that reports
        success and changes nothing is the one failure a demo cannot show."""
        self._connect54()

        def counts():
            d = self.assertOk(self.client.get('/api/module/ext54'))['data']
            return {l['lane']: (l['acq_rx'], l['acq_tx'])
                    for l in d['acquisition_counters']}

        before = counts()
        self.assertTrue(any(v != (0, 0) for v in before.values()),
                        'nothing to clear, so the test proves nothing')
        self.assertOk(self.client.post(
            '/api/module/acq_counters/reset',
            data=json.dumps({'lanes': [1, 3, 9]}),
            content_type='application/json'))
        after = counts()
        for lane in (1, 3, 9):
            self.assertEqual(after[lane], (0, 0), 'lane %d not cleared' % lane)
        for lane in (2, 4, 10):
            self.assertEqual(after[lane], before[lane],
                             'lane %d was cleared without being asked' % lane)

    def test_a_redirection_that_is_not_a_permutation_is_refused(self):
        """The spec requires a permutation; a module validates the command and
        rejects it, but only after the host was told the write went through."""
        self._connect54()
        rv = self.client.post('/api/module/media_lane_switching',
                              data=json.dumps({'redirection': [1, 1, 3, 4, 5, 6, 7, 8]}),
                              content_type='application/json')
        self.assertErr(rv, 400)
        self.assertIn('permutation', json.loads(rv.data)['message'])

    def test_a_valid_redirection_is_staged_and_reads_back(self):
        self._connect54()
        self.assertOk(self.client.post(
            '/api/module/media_lane_switching',
            data=json.dumps({'redirection': [2, 1, 4, 3, 5, 6, 7, 8],
                             'enable': True, 'commit': True}),
            content_type='application/json'))
        m = self.assertOk(self.client.get('/api/module/ext54'))['data']['media_lane_switching']
        self.assertEqual([l['redirected_to'] for l in m['lanes']],
                         [2, 1, 4, 3, 5, 6, 7, 8])
        self.assertTrue(m['enabled'])
        self.assertTrue(m['is_permutation'])

    def test_a_broken_mapping_is_reported_not_tidied(self):
        """Silently sorting it would hide the one thing worth seeing."""
        import cmis_registers as c
        d = c.parse_media_lane_switching(0, bytes([1, 1, 3, 4, 5, 6, 7, 8]), 1, bytes(8))
        self.assertFalse(d['is_permutation'])
        self.assertEqual([l['redirected_to'] for l in d['lanes']][:2], [1, 1])


class TestBadInputIsNotAServerError(CMISTestCase):
    """A 500 is what a failed I2C transfer looks like. Answering one to a
    malformed request tells the user their module is broken when the request
    was, and buries the reason in a traceback nobody sees."""

    GARBAGE = [
        {},
        {'lanes': 'nope'},
        {'lanes': [999]},
        {'lanes': [{'lane': 'x'}]},
        {'appsel': 99},
        {'redirection': [1, 1, 1, 1, 1, 1, 1, 1]},
        {'page': 'x', 'address': -5},
        {'page': 1, 'address': 2, 'data': ['zz']},
        {'address': 0x7F, 'data': [0]},
    ]

    def _routes(self, method):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py'),
                   encoding='utf-8').read()
        out = []
        for route, methods in re.findall(
                r"@app\.route\('(/api/[^']+)', methods=\[([^\]]+)\]\)", src):
            if "'%s'" % method in methods:
                out.append(route)
        return out

    def test_every_module_endpoint_answers_503_while_disconnected(self):
        for route in self._routes('GET') + self._routes('POST'):
            if '/api/module' not in route and '/api/diagnostics' not in route:
                continue
            for call in (self.client.get, lambda r: self.client.post(r, json={})):
                rv = call(route)
                if rv.status_code == 405:
                    continue          # that verb is not offered on this route
                self.assertEqual(rv.status_code, 503, '%s while disconnected' % route)
                self.assertTrue(rv.headers['Content-Type'].startswith('application/json'),
                                '%s answered with something other than JSON' % route)

    def test_no_post_endpoint_turns_bad_input_into_a_500(self):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_1600g_16lane', 'bus': 0, 'address': 80}),
            content_type='application/json'))
        for route in self._routes('POST'):
            if route in ('/api/connect', '/api/disconnect'):
                continue
            for body in self.GARBAGE:
                rv = self.client.post(route, data=json.dumps(body),
                                      content_type='application/json')
                self.assertLess(rv.status_code, 500,
                                '%s answered %d to %s' % (route, rv.status_code, body))
                self.assertTrue(
                    rv.headers['Content-Type'].startswith('application/json'),
                    '%s answered with something other than JSON' % route)
            # a body that is not JSON at all
            rv = self.client.post(route, data=b'not json',
                                  content_type='application/json')
            self.assertLess(rv.status_code, 500, '%s on unparseable body' % route)

    def test_the_lane_numbers_a_module_does_not_have_are_refused(self):
        """Eight-lane module, lane 9: the mask would fold onto lane 1 and reset
        a counter nobody asked about."""
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_1600g_dr8', 'bus': 0, 'address': 80}),
            content_type='application/json'))
        # This module does advertise Page 60h, so a refusal here can only come
        # from the lane check. mock_dr8 would have been refused for not having
        # the page at all, proving nothing.
        self.assertErr(self.client.post(
            '/api/module/acq_counters/reset',
            data=json.dumps({'lanes': [9]}), content_type='application/json'), 400)
        self.assertOk(self.client.post(
            '/api/module/acq_counters/reset',
            data=json.dumps({'lanes': [8]}), content_type='application/json'))


class TestManualCountsWhatIsActuallyRegistered(CMISTestCase):
    """The manual ships beside the exe and is what a customer trusts when the
    UI and their expectation disagree. Every number in it that the code also
    knows should be asked of the code, not retyped."""

    def _manual_text(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'CMIS2Customer', 'CMIS模块管理工具操作手册.html')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def _mocks(self):
        import i2c_interface
        import i2c_backends            # noqa: F401 - triggers registration
        return {n for n in i2c_interface._BACKENDS if n.startswith('mock')}

    def test_every_stated_profile_count_matches_the_registry(self):
        """One of these said "4 种 Mock" long after there were seven, and it
        described the coherent one as long-haul after it became coherent lite."""
        manual = self._manual_text()
        n = len(self._mocks())
        for m in re.finditer(r'(\d+)\s*种\s*(?:Mock|profile)', manual):
            self.assertEqual(int(m.group(1)), n,
                             'the manual states %s profiles; %d are registered'
                             % (m.group(1), n))

    def test_every_mock_the_manual_names_exists_and_none_is_missing(self):
        manual = self._manual_text()
        named = set(re.findall(r'<code>(mock_[a-z0-9_]+)</code>', manual))
        registered = self._mocks()
        self.assertEqual(named - registered, set(),
                         'the manual names backends that are not registered')
        self.assertEqual(registered - named, set(),
                         'a registered backend the manual never mentions')

    def test_the_manual_documents_no_endpoint_the_app_does_not_serve(self):
        manual = self._manual_text()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py'),
                  encoding='utf-8') as f:
            app_src = f.read()
        for route in sorted(set(re.findall(r'<code>(/api/[\w/]+)</code>', manual))):
            self.assertIn("'%s'" % route, app_src,
                          'the manual documents %s, which is not a route' % route)


class TestAShortReadSaysWhatWasShort(CMISTestCase):
    """An adapter that NAKs partway through returns fewer bytes than were
    asked for. Every decoder downstream unpacks a fixed width, so without a
    check at the read itself the user gets "unpack requires a buffer of 2
    bytes" and no idea which register, or that the cause is the bus."""

    def _truncate_by(self, n):
        backend = _state['backend']
        real = backend.read_bytes
        backend.read_bytes = lambda addr, length: real(addr, length)[:max(0, length - n)]
        return real

    def test_the_error_names_the_register_and_the_shortfall(self):
        self.connect()
        real = self._truncate_by(1)
        try:
            rv = self.client.get('/api/module/monitoring')
            body = self.assertErr(rv, 500)
        finally:
            _state['backend'].read_bytes = real
        msg = body['message']
        self.assertIn('short read', msg)
        self.assertRegex(msg, r'\b[0-9A-F]{2}h:0x[0-9A-F]{2}',
                         'the message does not name the register: %r' % msg)
        self.assertNotIn('unpack requires', msg,
                         'the struct error is still what reaches the user')

    def test_a_short_read_does_not_poison_the_next_one(self):
        """The failure has to be transient: a flaky bus recovers, and the tool
        must recover with it rather than needing a reconnect."""
        self.connect()
        real = self._truncate_by(1)
        try:
            self.assertErr(self.client.get('/api/module/monitoring'), 500)
        finally:
            _state['backend'].read_bytes = real
        self.assertOk(self.client.get('/api/module/monitoring'))

    def test_a_full_length_read_is_untouched(self):
        """The check must not cost anything when the bus is behaving."""
        self.connect()
        d = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        self.assertEqual(len(d['lanes']), 8)


class TestTheLauncherFindsItsOwnFiles(unittest.TestCase):
    """启动.bat is what someone runs when they have the source rather than the
    exe. It used to run `python app.py` against whatever the current directory
    happened to be, so a desktop shortcut, a taskbar pin or a terminal sitting
    anywhere else failed with "can't open file 'C:\\Windows\\app.py'"."""

    def _script(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '启动.bat')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_it_moves_to_its_own_directory_first(self):
        s = self._script()
        self.assertIn('cd /d "%~dp0"', s,
                      'the launcher does not move to its own folder, so it only '
                      'works when the current directory already happens to be right')
        # and it has to happen before the line that actually opens the file,
        # not merely before some mention of it
        self.assertLess(s.index('cd /d "%~dp0"'), s.index('%PYTHON% app.py'),
                        'the directory change comes after the file it protects')

    def test_it_does_not_call_a_failed_start_a_stopped_server(self):
        """Printing "Server stopped" after python could not even open app.py
        tells the reader the run was fine."""
        s = self._script()
        self.assertIn('%errorlevel%', s.lower(),
                      'the launcher never looks at whether the server failed')

    def test_it_still_prefers_the_py_launcher(self):
        """py resolves a real installation; python.exe on a stock Windows is an
        App Execution Alias that opens the Microsoft Store instead."""
        s = self._script()
        self.assertLess(s.index('where py '), s.index('where python '),
                        'python is probed before py')

    def test_the_file_keeps_the_line_endings_cmd_needs(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '启动.bat')
        with open(path, 'rb') as f:
            raw = f.read()
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''),
                         'a bare LF in a batch file breaks label and goto parsing')


class TestAskingGitHubSurvivesAFlakyLink(unittest.TestCase):
    """Every other test replaces fetch_latest_release outright, so its own
    retry loop had never run. On the link this tool is used over, the first
    request failing and the second succeeding is the common case, not the
    exception."""

    def setUp(self):
        self._real_open = updater_module._open
        self.calls = []

    def tearDown(self):
        updater_module._open = self._real_open

    class _Resp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return self._body

    def _payload(self):
        return json.dumps({
            'tag_name': 'v9.9.9',
            'html_url': 'https://github.com/o/r/releases/tag/v9.9.9',
            'body': 'notes', 'published_at': '2026-01-01T00:00:00Z',
            'assets': [{
                'name': 'CMIS_dist_v9_9_9.zip', 'size': 123,
                'digest': 'sha256:' + 'ab' * 32,
                'browser_download_url':
                    'https://github.com/o/r/releases/download/v9.9.9/'
                    'CMIS_dist_v9_9_9.zip',
            }],
        }).encode()

    def test_a_first_attempt_that_fails_does_not_end_the_check(self):
        def fake_open(req, timeout):
            self.calls.append(req.full_url)
            if len(self.calls) < 3:
                raise IOError('connection reset by peer')
            return self._Resp(self._payload())
        updater_module._open = fake_open

        rel = updater_module.fetch_latest_release(attempts=3, retry_wait=0)

        self.assertIsNotNone(rel, 'gave up with attempts still in hand')
        self.assertEqual(rel['version'], '9.9.9')
        self.assertEqual(len(self.calls), 3)

    def test_running_out_of_attempts_answers_none_not_up_to_date(self):
        """None means "could not ask". Reporting it as "you are current" would
        leave someone on a version with a known fault believing otherwise."""
        def fake_open(req, timeout):
            self.calls.append(req.full_url)
            raise IOError('no route to host')
        updater_module._open = fake_open

        self.assertIsNone(
            updater_module.fetch_latest_release(attempts=2, retry_wait=0))
        self.assertEqual(len(self.calls), 2, 'the attempt budget was not spent')

    def test_a_reply_that_is_not_json_is_a_failure_not_a_crash(self):
        def fake_open(req, timeout):
            self.calls.append(req.full_url)
            return self._Resp(b'<html>proxy login page</html>')
        updater_module._open = fake_open

        # A captive portal or proxy answering HTML is exactly what this sees on
        # a lab network, and it must not escape as an exception.
        self.assertIsNone(
            updater_module.fetch_latest_release(attempts=2, retry_wait=0))


class TestTheUpdateWorkerHandlesItsFailures(unittest.TestCase):
    """The worker that downloads and installs a release has never been run by
    the suite - only the pure pieces it calls have. Its own job is the
    orchestration: which state it reports, which message a user gets, and
    whether it leaves a half-unpacked staging directory behind."""

    def setUp(self):
        self.staged = tempfile.mkdtemp()
        self._saved = {
            'download': updater_module.download_asset,
            'verify': updater_module.verify_sha256,
            'extract': updater_module.extract_payload,
            'staging': updater_module.staging_dir,
            'partial': updater_module.partial_path,
            'order': updater_module.order_sources,
        }
        updater_module.staging_dir = lambda: self.staged
        updater_module.partial_path = lambda name: os.path.join(self.staged, name + '.part')
        updater_module.order_sources = lambda urls, **kw: [(u, 1.0) for u in urls]
        app_module._update.update(state='idle', message='', done=0, total=0)

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(updater_module, {
                'download': 'download_asset', 'verify': 'verify_sha256',
                'extract': 'extract_payload', 'staging': 'staging_dir',
                'partial': 'partial_path', 'order': 'order_sources'}[key], value)
        shutil.rmtree(self.staged, ignore_errors=True)
        app_module._update.update(state='idle', message='', done=0, total=0)

    def _release(self, sha='ab' * 32):
        return {'version': '9.9.9', 'tag': 'v9.9.9',
                'asset_name': 'CMIS_dist_v9_9_9.zip',
                'asset_url': 'https://github.com/o/r/releases/download/v9.9.9/CMIS_dist_v9_9_9.zip',
                'asset_size': 1234, 'sha256': sha, 'notes': '', 'html_url': ''}

    def _wrote_archive(self, urls, dest, **kw):
        with open(dest, 'wb') as fh:
            fh.write(b'not really a zip')

    def test_a_digest_mismatch_says_so_and_installs_nothing(self):
        updater_module.download_asset = self._wrote_archive
        updater_module.verify_sha256 = lambda path, expected: False
        updater_module.extract_payload = lambda *a: self.fail('installed a bad download')

        app_module._run_update(self._release())

        self.assertEqual(app_module._update['state'], 'error')
        self.assertIn('checksum', app_module._update['message'].lower())
        self.assertFalse(os.path.isdir(self.staged),
                         'a failed verification left the staging directory behind')

    def test_a_release_with_no_digest_is_a_different_message(self):
        """"We checked and it was wrong" and "there was nothing to check
        against" call for different reactions, and the second is not the
        user's fault."""
        updater_module.download_asset = self._wrote_archive
        updater_module.verify_sha256 = lambda path, expected: False
        updater_module.extract_payload = lambda *a: self.fail('installed unverified')

        app_module._run_update(self._release(sha=None))

        self.assertEqual(app_module._update['state'], 'error')
        msg = app_module._update['message']
        self.assertIn('SHA-256', msg)
        self.assertIn('release page', msg,
                      'the user is told it failed but not what they can do')
        self.assertNotIn('failed its checksum', msg,
                         'a missing digest is reported as a mismatch')

    def test_a_download_that_raises_is_reported_not_swallowed(self):
        def explode(*a, **kw):
            raise IOError('connection reset')
        updater_module.download_asset = explode

        app_module._run_update(self._release())

        self.assertEqual(app_module._update['state'], 'error')
        self.assertIn('connection reset', app_module._update['message'])
        self.assertFalse(os.path.isdir(self.staged))

    def test_a_swap_that_cannot_run_is_reported_not_left_hanging(self):
        """The download is finished and verified by then, so the thread dying
        here would leave the UI polling 'installing' for ever - an update that
        fails without anyone being told, which this project has shipped once
        before."""
        updater_module.download_asset = self._wrote_archive
        updater_module.verify_sha256 = lambda path, expected: True
        updater_module.extract_payload = lambda *a: ['CMIS_Module_Manager.exe']

        # Running from source, the swap refuses outright, which is the same
        # shape as a locked file or an antivirus blocking the helper.
        app_module._run_update(self._release())

        self.assertEqual(app_module._update['state'], 'error',
                         'the worker left the update stuck at %r'
                         % app_module._update['state'])
        msg = app_module._update['message']
        self.assertIn('downloaded and verified', msg,
                      'the message does not say the payload is fine')
        self.assertIn(self.staged, msg,
                      'the user is not told where the unpacked files are')

    def test_a_good_download_reaches_the_installing_state(self):
        seen = []
        updater_module.download_asset = self._wrote_archive
        updater_module.verify_sha256 = lambda path, expected: True
        def extract(archive, dest):
            seen.append(('extract', os.path.basename(archive)))
            return ['CMIS_Module_Manager.exe']
        updater_module.extract_payload = extract

        app_module._run_update(self._release())

        # The swap itself cannot run from source, so this stops at the step
        # before it: the payload was fetched, verified and unpacked, and the
        # archive cleaned up.
        self.assertEqual(seen, [('extract', 'CMIS_dist_v9_9_9.zip')])
        self.assertFalse(
            os.path.exists(os.path.join(self.staged, 'CMIS_dist_v9_9_9.zip')),
            'the archive was left in the staging directory after unpacking')


class TestTheServerStaysSingleThreaded(unittest.TestCase):
    """One I2C connection and one cached page selection cannot survive
    interleaved requests, and nothing but this stops someone turning threading
    on to "make it faster".

    Measured, not assumed: the same app run with threaded=True, hammered with
    three endpoints that each need a different page, returned 40 of 90 reads
    from the wrong page - vendor name arriving as raw register bytes, alarm
    limits inverted. Single-threaded, 0 of 90.
    """

    def test_app_run_is_not_threaded(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py'),
                  encoding='utf-8') as f:
            src = f.read()
        m = re.search(r'app\.run\(([^)]*)\)', src)
        self.assertIsNotNone(m, 'app.run() is gone; the serving model changed')
        args = m.group(1)
        self.assertIn('threaded=False', args,
                      'the server must stay single-threaded: with threading on, '
                      'reads come back from whatever page another request '
                      'selected in between')
        self.assertNotIn('debug=True', args,
                         "the reloader would run two copies, each holding the "
                         "same adapter open")

    def test_the_page_helper_still_assumes_it(self):
        """_set_page caches what it wrote. That is only sound while no other
        request can write the page register between the cache and the read."""
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py'),
                  encoding='utf-8') as f:
            src = f.read()
        idx = src.index('def _set_page(')
        body = src[idx:idx + 1400]
        self.assertIn("_state['page'] = page", body,
                      'the page cache is gone; if that was deliberate, this '
                      'test and the threading constraint should go together')


class TestRegisterMapIsTheOnlySourceOfAddresses(unittest.TestCase):
    """Page and address belong in cmis_registers.py. A call site that spells
    them out drifts from the map silently - which is how v2.0.0 shipped Page
    10h controls one byte off, flipping TX Disable polarity on real modules."""

    def _src(self, name):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
                  encoding='utf-8') as f:
            return f.read()

    def test_no_call_site_spells_out_an_address_the_map_defines(self):
        app_src = self._src('app.py')
        reg_src = self._src('cmis_registers.py')
        # Keyed by page as well as address: 0x87 means one thing on Page 01h
        # and another on Page 11h, so matching on the address alone invents
        # collisions that are not there.
        known, lower = {}, {}
        for name, page, addr, _ln in re.findall(
                r'^(REG_\w+)\s*=\s*\((None|0x[0-9A-Fa-f]+),\s*(0x[0-9A-Fa-f]+),\s*(\d+)\)',
                reg_src, re.M):
            if page == 'None':
                lower.setdefault(int(addr, 16), []).append(name)
            else:
                known.setdefault((int(page, 16), int(addr, 16)), []).append(name)

        offenders = []
        # Reads that name both a page and an address outright.
        for m in re.finditer(r'(_read_upper|_read_banked|_read_banks)\('
                             r'\s*(0x[0-9A-Fa-f]+)\s*,\s*(0x[0-9A-Fa-f]+)', app_src):
            key = (int(m.group(2), 16), int(m.group(3), 16))
            if key in known:
                offenders.append('%s reads %02Xh:0x%02X, which %s defines'
                                 % (m.group(1), key[0], key[1], known[key][0]))
        # Writes always land on the page already selected, so only the lower
        # page's own registers can be matched without tracking that state.
        for m in re.finditer(r'write_bytes\(\s*(0x[0-9A-Fa-f]+)', app_src):
            addr = int(m.group(1), 16)
            if addr in lower:
                offenders.append('write_bytes to 0x%02X, which %s defines'
                                 % (addr, lower[addr][0]))
        self.assertEqual(offenders, [],
                         'call sites bypassing the register map: ' + '; '.join(offenders))


class TestEveryProfileIsHealthyOnConnect(CMISTestCase):
    """A demo module that alarms the moment it connects reads as broken
    hardware. Each profile's own thresholds have to contain its own nominals."""

    PROFILES = ['mock_coherent', 'mock_coherent_zr', 'mock_dr8', 'mock_sr8',
                'mock_fr4x2', 'mock_1600g_dr8', 'mock_1600g_16lane']

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def test_no_monitored_value_sits_outside_its_own_alarms(self):
        for backend in self.PROFILES:
            self._connect(backend)
            t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
            m = self.assertOk(self.client.get('/api/module/monitoring'))['data']
            for lane in m['lanes']:
                for kind in ('tx', 'rx'):
                    v = lane.get('%s_power_dbm' % kind)
                    if v is None:
                        continue
                    self.assertLess(v, t['%s_power_high_alarm_dbm' % kind],
                                    '%s lane %d %s power' % (backend, lane['lane'], kind))
                    self.assertGreater(v, t['%s_power_low_alarm_dbm' % kind],
                                       '%s lane %d %s power' % (backend, lane['lane'], kind))
                bias = lane.get('tx_bias_ma')
                if bias is not None and t.get('tx_bias_high_alarm_ma') is not None:
                    self.assertLess(bias, t['tx_bias_high_alarm_ma'],
                                    '%s lane %d bias' % (backend, lane['lane']))
                    self.assertGreater(bias, t['tx_bias_low_alarm_ma'],
                                       '%s lane %d bias: a VCSEL runs at a fraction '
                                       'of an EML current, so one generic window '
                                       'cannot serve both' % (backend, lane['lane']))
            self.client.post('/api/disconnect')

    def test_every_application_describes_a_module_that_could_exist(self):
        """Lane counts are not free text: an interface name fixes how many
        lanes it has, and a descriptor that disagrees describes no module."""
        host_width = {'1.6TAUI-16': 16, '1.6TAUI-8': 8, '800GAUI-8': 8,
                      '800GAUI-4': 4, '400GAUI-8': 8, '400GAUI-4': 4,
                      '400GAUI-2': 2, '200GAUI-2': 2, '200GAUI-1': 1}
        media_width = {'1.6TBASE-DR8': 8, '800GBASE-DR8': 8, '800GBASE-DR4': 4,
                       '800GBASE-LR1': 1, '800GBASE-SR8': 8, '400GBASE-DR4': 4,
                       '400GBASE-SR8': 8, '400GBASE-SR4': 4, '400GBASE-FR4': 1,
                       '400GBASE-DR2': 2, '200GBASE-ER4': 1}

        def width(name, table):
            for key in sorted(table, key=len, reverse=True):
                if name.startswith(key):
                    return table[key]
            return None

        for backend in self.PROFILES:
            self._connect(backend)
            apps = self.assertOk(
                self.client.get('/api/module/applications'))['data']['applications']
            for i, a in enumerate(apps, 1):
                for key in ('host_if_name', 'media_if_name'):
                    self.assertNotIn('Unknown', str(a[key]),
                                     '%s AppSel %d %s' % (backend, i, key))
                hw = width(a['host_if_name'], host_width)
                if hw is not None:
                    self.assertEqual(a['host_lanes'], hw,
                                     '%s AppSel %d: %s is %d lanes wide'
                                     % (backend, i, a['host_if_name'], hw))
                mw = width(a['media_if_name'], media_width)
                if mw is not None:
                    self.assertEqual(a['media_lanes'], mw,
                                     '%s AppSel %d: %s is %d lanes wide'
                                     % (backend, i, a['media_if_name'], mw))
                self.assertLessEqual(a['host_lanes'], 8, '%s AppSel %d' % (backend, i))
                self.assertLessEqual(a['media_lanes'], 8, '%s AppSel %d' % (backend, i))
            self.client.post('/api/disconnect')

    def test_bias_thresholds_follow_the_profile_that_names_them(self):
        """The VCSEL module and the EML modules cannot share one window."""
        self._connect('mock_sr8')
        vcsel = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        self.client.post('/api/disconnect')
        self._connect('mock_dr8')
        eml = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        self.assertNotEqual(vcsel['tx_bias_low_alarm_ma'], eml['tx_bias_low_alarm_ma'],
                            'both modules alarm at the same bias current')
        self.assertLess(vcsel['tx_bias_high_alarm_ma'], eml['tx_bias_high_alarm_ma'])


class TestCoherentProfiles(CMISTestCase):
    """The coherent mock models IEEE P802.3dj/D3.1 Clause 185 800GBASE-LR1,
    the datacenter coherent-lite PMD, and the tunable C-band module it used to
    be lives on beside it because nothing else exercises laser tuning."""

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def test_the_coherent_mock_is_the_802_3dj_lr1_pmd(self):
        """One wavelength, one media lane, whichever host width drives it.
        The old profile advertised a 200G ZR media code under an 800G host,
        which is not a combination any module can have."""
        self._connect('mock_coherent')
        apps = self.assertOk(
            self.client.get('/api/module/applications'))['data']['applications']
        for a in apps:
            self.assertEqual(a['media_if_name'], '800GBASE-LR1')
            self.assertEqual(a['media_lanes'], 1, 'LR1 is a single wavelength')
        self.assertEqual([a['host_if_name'] for a in apps],
                         ['800GAUI-8 S C2M', '800GAUI-4 C2M'])

    def test_its_power_windows_come_from_tables_185_5_and_185_6(self):
        """Table 185-5 bounds launch power at -11.2 to -6 dBm and Table 185-6
        bounds average receive power tolerance at -17.5 to -4 dBm. Coherent
        lite launches far below a DR8, so borrowing DR8 limits would put the
        module in alarm from the moment it connects."""
        self._connect('mock_coherent')
        t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        self.assertEqual(t['tx_power_high_alarm_dbm'], -6.0)
        self.assertEqual(t['tx_power_low_alarm_dbm'], -11.2)
        self.assertEqual(t['rx_power_high_alarm_dbm'], -4.0)
        self.assertEqual(t['rx_power_low_alarm_dbm'], -17.5)
        m = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        for lane in m['lanes']:
            self.assertLess(lane['tx_power_dbm'], t['tx_power_high_warn_dbm'])
            self.assertGreater(lane['tx_power_dbm'], t['tx_power_low_warn_dbm'])
            self.assertLess(lane['rx_power_dbm'], t['rx_power_high_warn_dbm'])
            self.assertGreater(lane['rx_power_dbm'], t['rx_power_low_warn_dbm'])

    def test_lr1_advertises_no_tuning_grid(self):
        """Clause 185 gives one carrier frequency with a tolerance, not a grid.
        A module that offered grids here would be claiming to be a ZR."""
        self._connect('mock_coherent')
        d = self.assertOk(self.client.get('/api/module/laser'))['data']
        self.assertEqual(d['grids_supported'], [])

    def test_the_tunable_profile_still_tunes(self):
        """Laser tuning had no test at all, so moving the only tunable profile
        to its own name could have taken the feature with it silently."""
        self._connect('mock_coherent_zr')
        d = self.assertOk(self.client.get('/api/module/laser'))['data']
        self.assertIn('100 GHz', d['grids_supported'])
        self.assertOk(self.client.post(
            '/api/module/laser',
            data=json.dumps({'lanes': [{'lane': 1, 'grid_code': 5, 'channel': 20,
                                        'fine_offset_ghz': 2.5,
                                        'target_power_dbm': -1.5}]}),
            content_type='application/json'))
        lane = self.assertOk(
            self.client.get('/api/module/laser'))['data']['lanes'][0]
        self.assertEqual(lane['channel'], 20)
        self.assertAlmostEqual(lane['frequency_thz'], 195.1, places=3)
        self.assertAlmostEqual(lane['fine_offset_ghz'], 2.5, places=3)
        self.assertAlmostEqual(lane['target_power_dbm'], -1.5, places=2)

    def test_a_tuning_request_it_cannot_read_is_refused(self):
        """A body in the wrong shape came back as "parameters written" having
        written nothing, so the caller believed the laser had been retuned."""
        self._connect('mock_coherent_zr')
        # 'oops' is iterable, so without the type check the loop would walk its
        # characters and fail with a 500 instead of saying what was wrong.
        for body in ({}, {'lanes': []}, {'lane': 1, 'channel': 20},
                     {'lanes': 'oops'}, {'lanes': [{'lane': 99, 'channel': 20}]}):
            self.assertErr(self.client.post(
                '/api/module/laser', data=json.dumps(body),
                content_type='application/json'), 400)


class TestFiveFourCardsLiveWithTheirFunction(CMISTestCase):
    """The 5.4 optional-page cards were moved out of a revision-named tab and
    into the tab for the job each one belongs to. Nothing else in the page
    records where they went, so this does."""

    def _read(self, *parts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()

    def _html(self):
        return self._read('templates', 'index.html')

    def _js(self):
        return self._read('static', 'app.js')

    def _manual(self):
        return self._read('CMIS2Customer', 'CMIS模块管理工具操作手册.html')

    # card id -> (tab panel it belongs in, tab name the manual must give)
    PLACEMENT = {
        'card-pagemap':  ('tab-info',        'Module Info'),
        'card-lanethr':  ('tab-monitoring',  'Monitoring'),
        'card-polarity': ('tab-datapath',    'DataPath Config'),
        'card-mls':      ('tab-datapath',    'DataPath Config'),
        'card-acq':      ('tab-diagnostics', 'Diagnostics'),
    }

    def _panels(self, html):
        """Map each tab panel id to its own markup, by balancing <div>s."""
        panels = {}
        for m in re.finditer(r'<div class="tab-panel[^"]*" id="(tab-[a-z0-9]+)">', html):
            depth, start = 0, m.start()
            for d in re.finditer(r'</?div', html[start:]):
                depth += 1 if d.group(0) == '<div' else -1
                if depth == 0:
                    end = html.index('>', start + d.end()) + 1
                    panels[m.group(1)] = html[start:end]
                    break
        return panels

    def test_each_card_sits_in_the_tab_for_its_job(self):
        html = self._html()
        self.assertNotIn('data-tab="ext54"', html,
                         'the revision-named tab is back')
        panels = self._panels(html)
        for card, (panel, _tab) in self.PLACEMENT.items():
            holders = [p for p, markup in panels.items()
                       if 'id="%s"' % card in markup]
            self.assertEqual(holders, [panel],
                             '%s should live in %s, found in %s'
                             % (card, panel, holders or 'no panel'))

    def test_the_manual_sends_the_reader_to_the_same_tab(self):
        """A card in one tab and a manual pointing at another is worse than
        either being wrong on its own: the reader trusts the manual and
        concludes the module does not support the feature."""
        manual = self._manual()
        for card, (_panel, tab) in self.PLACEMENT.items():
            self.assertIn(tab, manual,
                          'the manual never names the %s tab, where %s lives'
                          % (tab, card))
        self.assertNotIn('CMIS 5.4 扩展页', manual,
                         'the manual still describes a separate 5.4 tab')

    def test_a_module_without_the_pages_shows_none_of_the_cards(self):
        """The cards now sit among cards that are always there, so an
        unadvertised page must leave nothing behind that looks like a reading
        from this module."""
        self.connect()                       # mock_dr8: CMIS 5.3, no 5.4 pages
        d = self.assertOk(self.client.get('/api/module/ext54'))['data']
        self.assertEqual(d['available'], {})
        for key in ('polarity_status', 'acquisition_counters',
                    'lane_power_thresholds', 'media_lane_switching',
                    'supported_pages'):
            self.assertNotIn(key, d)

    def test_disconnect_empties_them_rather_than_leaving_them_up(self):
        """Every card the JS hides on disconnect has to be named there; one
        left out keeps the previous module's table under the next module's
        heading."""
        js = self._js()
        idx = js.index('function clearTabContent(')
        end = js.index('\nfunction ', idx + 1)
        body = js[idx:end]
        for card in self.PLACEMENT:
            self.assertIn("'%s'" % card, body,
                          '%s is never hidden on disconnect' % card)
        for table in ('tbl-polarity', 'tbl-acq', 'tbl-lanethr', 'tbl-mls'):
            self.assertIn("'%s'" % table, body,
                          '%s keeps its rows across a disconnect' % table)


class TestReadmeCountsTheSuiteItDescribes(unittest.TestCase):

    def test_the_readme_test_count_is_the_real_one(self):
        """The README number was stale the moment it was last hand-edited.
        Counting the loaded suite means it can only ever be right or fail."""
        readme = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'README.md')
        with open(readme, encoding='utf-8') as f:
            text = f.read()
        stated = re.search(r'(\d+) end-to-end API tests', text)
        self.assertIsNotNone(stated, 'README no longer states a test count')
        actual = unittest.TestLoader().loadTestsFromModule(
            sys.modules[__name__]).countTestCases()
        self.assertEqual(int(stated.group(1)), actual,
                         'README says %s tests, the suite has %d'
                         % (stated.group(1), actual))


class TestAdvertisedRevisionIsSingleSourced(CMISTestCase):

    def test_the_footer_says_what_the_api_says(self):
        """The footer sat at "OIF CMIS 5.3" for the whole 5.4 release because it
        was typed into the page while the API was updated in app.py. Whoever
        reads the corner of the window is entitled to the same answer."""
        api = self.assertOk(self.client.get('/api/version'))['data']
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('OIF CMIS %s' % api['cmis_revision_supported'], page)
        self.assertNotIn('OIF CMIS 5.3', page)


class TestDj1600GAlignment(CMISTestCase):
    """The 1.6T DR8 profile models a PMD that IEEE P802.3dj/D3.1 specifies, so
    its optical numbers are checked against the standard rather than against
    whatever the profile happens to say. Clause 180 covers 200GBASE-DR1,
    400GBASE-DR2, 800GBASE-DR4 and 1.6TBASE-DR8 -- all 200G per lane, 500 m."""

    # 802.3dj D3.1, 174A.6: above this pre-correction BER the RS-FEC of a
    # 1.6TBASE-R PHY can no longer hold the frame loss ratio.
    PRE_FEC_BER_LIMIT = 2.921e-4
    # Table 174A-1 divides that budget: this much to the PMD-to-PMD link, the
    # rest to the AUIs either side. Not to be confused with the 6.4e-5 BERadded
    # of 180.2, which is the allocation for everything EXCEPT the PMD.
    PMD_BER_ALLOCATION = 2.28e-4

    def _connect_dr8(self):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': 'mock_1600g_dr8', 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def test_it_advertises_the_interfaces_802_3dj_names(self):
        """1.6TBASE-DR8 is the spec's own spelling; 1600GBASE-DR8 is not a PHY
        type anyone defines. The breakout Application has to stay on 200G media
        lanes too -- a DR8 optic has no 100G lasers to offer."""
        self._connect_dr8()
        apps = self.assertOk(
            self.client.get('/api/module/applications'))['data']['applications']
        pairs = [(a['host_if_name'], a['media_if_name'],
                  a['host_lanes'], a['media_lanes']) for a in apps]
        self.assertEqual(pairs[0], ('1.6TAUI-8 C2M', '1.6TBASE-DR8', 8, 8))
        self.assertEqual(pairs[1], ('800GAUI-4 C2M', '800GBASE-DR4', 4, 4))

    def test_alarm_levels_come_from_tables_180_7_and_180_8(self):
        """Table 180-7 bounds launch power per lane at -3.1 to +4 dBm and
        Table 180-8 bounds average receive power at -6.1 to +4 dBm. A module
        that alarms somewhere else is not modelling this PMD.

        The warning levels are checked too, but they are a demo choice: neither
        table has a warning level, and 802.3dj has no such concept."""
        self._connect_dr8()
        t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        self.assertEqual(t['tx_power_high_alarm_dbm'], 4.0)
        self.assertEqual(t['tx_power_low_alarm_dbm'], -3.1)
        self.assertEqual(t['rx_power_high_alarm_dbm'], 4.0)
        self.assertEqual(t['rx_power_low_alarm_dbm'], -6.1)
        self.assertEqual(t['tx_power_high_warn_dbm'], 3.5)
        self.assertEqual(t['tx_power_low_warn_dbm'], -2.6)
        self.assertEqual(t['rx_power_high_warn_dbm'], 3.5)
        self.assertEqual(t['rx_power_low_warn_dbm'], -5.6)

    def test_the_5_4_per_lane_thresholds_agree_with_the_module_wide_ones(self):
        """Page 62h carries the Tx limits per lane in 0.01 dBm; once a lane
        moves to power-relative supervision (5.4 section 7.5.3) they supersede
        Page 02h. No lane here has, so the two must agree -- disagreeing would
        send a user chasing a lane that is not actually out of spec."""
        self._connect_dr8()
        t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        d = self.assertOk(self.client.get('/api/module/ext54'))['data']
        for lane in d['lane_power_thresholds']:
            self.assertEqual(lane['hi_alarm_dbm'], t['tx_power_high_alarm_dbm'])
            self.assertEqual(lane['lo_alarm_dbm'], t['tx_power_low_alarm_dbm'])
            self.assertEqual(lane['hi_warn_dbm'], t['tx_power_high_warn_dbm'])
            self.assertEqual(lane['lo_warn_dbm'], t['tx_power_low_warn_dbm'])

    def test_the_modelled_ber_sits_where_802_3dj_allocates_it(self):
        """A 200G/lane link runs at a pre-FEC BER that would be a fault on an
        800G module, so the demo has to show that -- but a conformant module
        stays inside the share Table 174A-1 gives the PMD, which is stricter
        than the whole path's 2.921e-4."""
        self._connect_dr8()
        lanes = self.assertOk(self.client.get('/api/module/ber'))['data']['lanes']
        self.assertLess(self.PMD_BER_ALLOCATION, self.PRE_FEC_BER_LIMIT)
        for lane in lanes:
            for key in ('host_ber', 'media_ber'):
                self.assertLess(lane[key], self.PMD_BER_ALLOCATION)
                self.assertGreater(lane[key], 1e-5)

    def test_the_demo_never_alarms_against_its_own_limits(self):
        """Nominal powers drift a few percent. If a threshold edit ever pushes
        that drift past an alarm, every lane lights up red on connect and the
        demo looks broken rather than tight."""
        self._connect_dr8()
        t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        m = self.assertOk(self.client.get('/api/module/monitoring'))['data']
        for lane in m['lanes']:
            self.assertLess(lane['tx_power_dbm'], t['tx_power_high_warn_dbm'])
            self.assertGreater(lane['tx_power_dbm'], t['tx_power_low_warn_dbm'])
            self.assertLess(lane['rx_power_dbm'], t['rx_power_high_warn_dbm'])
            self.assertGreater(lane['rx_power_dbm'], t['rx_power_low_warn_dbm'])

    def test_every_profile_that_serves_page_62h_agrees_with_its_page_02h(self):
        """The 16-lane profile also advertises Page 62h but names no PMD, so it
        took the fallback quad while its Page 02h kept the generic one --
        the same contradiction, on the profile the other test never connects."""
        for backend in ('mock_1600g_dr8', 'mock_1600g_16lane'):
            self.assertOk(self.client.post(
                '/api/connect',
                data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
                content_type='application/json'))
            t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
            d = self.assertOk(self.client.get('/api/module/ext54'))['data']
            for lane in d['lane_power_thresholds']:
                self.assertAlmostEqual(lane['hi_alarm_dbm'],
                                       t['tx_power_high_alarm_dbm'], places=1,
                                       msg='%s lane %d' % (backend, lane['lane']))
                self.assertAlmostEqual(lane['lo_alarm_dbm'],
                                       t['tx_power_low_alarm_dbm'], places=1,
                                       msg='%s lane %d' % (backend, lane['lane']))
            self.client.post('/api/disconnect')

    def test_the_page_map_lists_only_pages_the_module_answers(self):
        """Page 0Ch exists to end the disagreement between scattered page
        advertisements. A map built from a fixed list claimed the laser pages
        04h and 12h on a module with no tunable laser."""
        self._connect_dr8()
        d = self.assertOk(self.client.get('/api/module/ext54'))['data']
        pages = set(d['supported_pages'])
        self.assertIn(0x0C, pages)
        self.assertIn(0x62, pages)
        for absent in (0x04, 0x12, 0x0D):
            self.assertNotIn(absent, pages,
                             'page %02Xh advertised but never answered' % absent)

    def test_the_acquisition_counter_advertisement_uses_the_top_nibble(self):
        """Table 8-188 puts the four supported bits at 60h:130.7-4 and reserves
        3-0. Advertising 0x0F said no category was supported while Page 61h was
        full of counters, and set four reserved bits doing it."""
        self._connect_dr8()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x60, 'address': 130,
                                               'length': 1}),
                              content_type='application/json')
        byte = self.assertOk(rv)['data']['data'][0]
        self.assertEqual(byte >> 4, 0x0F, 'four categories should be advertised')
        self.assertEqual(byte & 0x0F, 0, 'bits 3-0 are reserved')

    def test_acquisition_counters_keep_rx_and_tx_on_the_right_sides(self):
        """Table 8-191 orders Page 61h Rx first: 128-143 per Rx media lane,
        144-159 per Tx host lane, then the same order for the Data Path pair.
        Reading it Tx-first points the user at the host electrical side while
        the fiber receiver is the one losing lock."""
        self._connect_dr8()
        d = self.assertOk(self.client.get('/api/module/ext54'))['data']
        lane1 = d['acquisition_counters'][0]
        self.assertEqual(lane1['acq_rx'], 1)
        self.assertEqual(lane1['acq_tx'], 2)
        self.assertEqual(lane1['dp_acq_rx'], 3)
        self.assertEqual(lane1['dp_acq_tx'], 4)

    def test_a_staged_redirection_is_not_reported_as_the_active_one(self):
        """Table 8-196 keeps the staged mapping (136-143, RW) apart from what
        the switch is doing (184-191, RO), and says enabling alone does not
        commit. Reporting the staged one as the module's mapping would show a
        lane assignment the hardware is not using."""
        self._connect_dr8()
        staged = [2, 1, 3, 4, 5, 6, 7, 8]
        self.assertOk(self.client.post(
            '/api/module/media_lane_switching',
            data=json.dumps({'redirection': staged, 'enable': True}),
            content_type='application/json'))
        d = self.assertOk(self.client.get('/api/module/ext54'))['data']
        mls = d['media_lane_switching']
        self.assertEqual([l['redirected_to'] for l in mls['lanes']], staged)
        self.assertEqual([l['active_target'] for l in mls['lanes']],
                         [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertFalse(mls['committed'])

        self.assertOk(self.client.post(
            '/api/module/media_lane_switching',
            data=json.dumps({'commit': True}),
            content_type='application/json'))
        mls = self.assertOk(
            self.client.get('/api/module/ext54'))['data']['media_lane_switching']
        self.assertEqual([l['active_target'] for l in mls['lanes']], staged)
        self.assertTrue(mls['committed'])
        self.assertEqual(mls['lanes'][0]['commit_result_name'], 'Success')

    def test_a_finished_commit_is_not_called_in_progress(self):
        """Table 8-196: 1 is success and 2 is in progress. Swapped, a commit
        still running reads as done and the operator moves traffic onto a
        switch configuration that is not in effect."""
        import cmis_registers as c
        self.assertEqual(c.MLS_RESULT_NAMES[1], 'Success')
        self.assertEqual(c.MLS_RESULT_NAMES[2], 'In progress')
        for rejected in (3, 4, 5, 6):
            self.assertIn('Rejected', c.MLS_RESULT_NAMES[rejected])

    def test_the_breakout_application_says_where_its_media_lanes_start(self):
        """CMIS 5.4 Table 8-60 keeps MediaLaneAssignmentOptions on Page 01h,
        apart from the four descriptor bytes, and a paged module has to supply
        it. Without it the host has no permissible media lane for the second
        DR4 -- exactly the Application that needs one, being a breakout."""
        self._connect_dr8()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x01, 'address': 176,
                                               'length': 2}),
                              content_type='application/json')
        opts = self.assertOk(rv)['data']['data']
        self.assertEqual(opts[0], 0x01, 'the 8-lane Application starts at lane 1')
        self.assertEqual(opts[1], 0x11, 'the 4-lane one starts at lane 1 or 5')

    def test_the_advertised_reach_is_the_500_m_of_table_180_6(self):
        """Byte 132 counts 0.1 km per step under multiplier 00b, so 500 m is 5.
        Writing 1 there advertises 100 m, which is what this used to say."""
        self._connect_dr8()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x01, 'address': 132,
                                               'length': 1}),
                              content_type='application/json')
        byte = self.assertOk(rv)['data']['data'][0]
        self.assertEqual(byte >> 6, 0b00)
        self.assertEqual((byte & 0x3F) * 0.1, 0.5)

    def test_profiles_without_a_modelled_pmd_keep_generic_thresholds(self):
        """Only a profile that names the standard it follows gets the standard's
        limits. Applying 1.6T numbers to every mock would make the 800G ones
        wrong instead."""
        self.connect()
        t = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        self.assertEqual(t['tx_power_high_alarm_dbm'], 5.0)
        self.assertEqual(t['rx_power_high_alarm_dbm'], 0.0)


class TestRawWriteGuards(CMISTestCase):

    def test_write_through_page_select_is_refused(self):
        """A multi-byte write across 0x7F would reprogram the page mid-transfer
        and scatter the rest into whatever page that byte named."""
        self.connect()
        before = _state['backend']._current_page
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x7C,
                                               'data': [0, 0, 0, 0x12, 0x34]}),
                              content_type='application/json')
        self.assertErr(rv, 400)
        # Compared against where the page actually was, not against zero:
        # connecting now reads the capability block and legitimately leaves
        # another page selected. What must hold is that the refusal moved it
        # nowhere, whatever it was.
        self.assertEqual(_state['backend']._current_page, before,
                         'the refused write still moved the page')

    def test_single_byte_page_select_still_allowed(self):
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x7F, 'data': [0x11]}),
                              content_type='application/json')
        self.assertOk(rv)


class TestPageSelection(CMISTestCase):
    """Page selection is cached; a stale cache silently reads the wrong page."""

    def test_bank_and_page_are_written_together_bank_first(self):
        """CMIS 8.2.15: the module holds off acting on BankSelect until
        PageSelect is written. Writing the bank on its own would leave the
        change pending and the next read would come from the old bank - which
        looks like correct data from the wrong lanes, not like an error.
        """
        import app as app_module
        self.connect()
        backend = _state['backend']
        real = backend.write_bytes
        seen = []

        def spy(addr, data):
            seen.append((addr, bytes(data)))
            return real(addr, data)

        backend.write_bytes = spy
        try:
            app_module._invalidate_page()
            app_module._set_page(0x11, bank=1)
        finally:
            backend.write_bytes = real

        selects = [w for w in seen if w[0] <= 0x7F < w[0] + len(w[1])]
        self.assertEqual(len(selects), 1, 'bank and page must be one transfer')
        addr, data = selects[0]
        self.assertEqual(addr, 0x7E, 'the transfer starts at BankSelect')
        self.assertEqual(data[0], 1, 'bank byte comes first')
        self.assertEqual(data[1], 0x11, 'page byte follows it')

    def test_a_bank_change_alone_still_reselects(self):
        """Same page, different bank is a different set of lanes."""
        import app as app_module
        self.connect()
        app_module._invalidate_page()
        app_module._set_page(0x11, bank=0)
        app_module._set_page(0x11, bank=1)
        self.assertEqual(_state['bank'], 1)
        self.assertEqual(_state['page'], 0x11)

    def _page_writes(self, fn):
        """Run fn and return the pages written to the PageMapping register.

        Page selection writes BankSelect and PageSelect together in one
        transfer starting at 0x7E, because the module defers acting on the bank
        until the page byte lands. The page is therefore the second byte.
        """
        backend = _state['backend']
        real = backend.write_bytes
        seen = []

        def spy(addr, data):
            if addr <= 0x7F < addr + len(data):
                seen.append(data[0x7F - addr])
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


class TestPageCacheNeverOutlivesItsWrite(CMISTestCase):
    """Two ways the cached page can end up claiming something that is not true.
    Neither shows up as an error: the next read simply comes from the wrong
    page, which is a plausible-looking number rather than a failure."""

    def test_a_page_write_that_fails_leaves_no_cached_page(self):
        """The write is what makes the cache true. If it raises after the bank
        byte has gone out, the module may be on neither page, so believing the
        old one reads the wrong page for as long as the session lasts."""
        self.connect()
        self.client.get('/api/module/thresholds')       # settle on Page 02h
        backend = _state['backend']
        real = backend.write_bytes

        def explode(addr, data):
            if addr == 0x7E:
                raise IOError('bus wedged mid-write')
            return real(addr, data)

        backend.write_bytes = explode
        try:
            self.client.get('/api/module/monitoring')    # wants Page 11h, cannot get there
        except Exception:
            pass
        finally:
            backend.write_bytes = real

        self.assertIsNone(app_module._state['page'],
                          'a failed page write left a page cached')
        self.assertIsNone(app_module._state['bank'],
                          'a failed page write left a bank cached')

        # And the next read must actually re-select rather than trust the cache.
        seen = []

        def spy(addr, data):
            if addr == 0x7E:
                seen.append(bytes(data))
            return real(addr, data)

        backend.write_bytes = spy
        try:
            self.assertOk(self.client.get('/api/module/monitoring'))
        finally:
            backend.write_bytes = real
        self.assertTrue(seen, 'the next read trusted a cache that had no write behind it')

    def test_disconnect_leaves_no_page_cached_for_the_next_module(self):
        """Connect invalidates too, so this is the second lock on the same
        door - but it is the one that holds if the first is ever removed, and
        the failure it prevents is silent."""
        self.connect()
        self.client.get('/api/module/thresholds')
        self.assertIsNotNone(app_module._state['page'])
        self.assertOk(self.client.get('/api/disconnect'))
        self.assertIsNone(app_module._state['page'],
                          'the page selected on the last module is still cached')
        self.assertIsNone(app_module._state['bank'])


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
        """A POST that is not declared as JSON must be refused.

        This used to be accepted (get_json(force=True)), which is precisely
        what made the API reachable from any other website: text/plain is a
        CORS-simple content type, so a foreign page could post JSON here with
        no preflight and no consent. Requiring application/json forces the
        preflight that such a page cannot satisfy.
        """
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8'}),
                              content_type='text/plain')
        self.assertEqual(rv.status_code, 415)

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
# Request provenance (anti-CSRF / anti-DNS-rebinding)
# ============================================================

class TestForeignRequestsRefused(CMISTestCase):
    """Any other website the user opens must not be able to drive this API.

    Serving no CORS headers does not achieve that: it withholds the response
    from the attacker but the request still executes. Before this guard, a
    text/plain POST from any page wrote to the attached module and returned
    200 - a real risk of damaging expensive hardware, not just a nuisance.
    """

    WRITE = {'page': 0x10, 'address': 130, 'data': [0xFF]}

    def test_the_original_attack_is_refused(self):
        """Verbatim replay of what used to succeed: a CORS-simple POST."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps(self.WRITE),
                              content_type='text/plain',
                              headers={'Origin': 'https://evil.example',
                                       'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(rv.status_code, 403)

    def test_cross_site_fetch_metadata_is_refused(self):
        self.connect()
        rv = self.client.post('/api/register/write', json=self.WRITE,
                              headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(rv.status_code, 403)

    def test_foreign_origin_is_refused(self):
        self.connect()
        rv = self.client.post('/api/register/write', json=self.WRITE,
                              headers={'Origin': 'https://evil.example'})
        self.assertEqual(rv.status_code, 403)

    def test_state_changing_get_is_refused(self):
        """A bare <img src> GET carries no Origin, so Sec-Fetch-Site is the
        only thing standing between a foreign page and this route."""
        self.connect()
        rv = self.client.get('/api/disconnect',
                             headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(rv.status_code, 403)

    def test_dns_rebinding_is_refused(self):
        """With a name the attacker owns pointed at 127.0.0.1 the browser
        calls their page same-origin and would read the replies too. The Host
        header is what gives it away."""
        self.connect()
        rv = self.client.get('/api/module/info',
                             headers={'Host': 'evil.example'})
        self.assertEqual(rv.status_code, 403)

    def test_form_encoded_post_is_refused(self):
        """The layer that holds even if a browser sends no metadata at all."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data='page=16&address=130&data=255',
                              content_type='application/x-www-form-urlencoded')
        self.assertEqual(rv.status_code, 415)

    def test_the_real_ui_still_works(self):
        """The guard is worthless if it also blocks the page it protects."""
        self.connect()
        rv = self.client.post('/api/register/write', json=self.WRITE,
                              headers={'Origin': 'http://127.0.0.1:5000',
                                       'Sec-Fetch-Site': 'same-origin',
                                       'Host': '127.0.0.1:5000'})
        self.assertOk(rv)

    def test_address_bar_and_localhost_still_work(self):
        """Sec-Fetch-Site: none is what a browser sends for a typed URL."""
        rv = self.client.get('/api/version',
                             headers={'Sec-Fetch-Site': 'none',
                                      'Host': 'localhost:5000'})
        self.assertOk(rv)


# ============================================================
# Update-path hardening
# ============================================================

class TestUpdateTrustBoundary(unittest.TestCase):

    def test_install_path_cannot_inject_powershell(self):
        """A Windows directory may legally be named `$(...)`, and inside a
        double-quoted PowerShell string that runs before the cmdlet does -
        under -ExecutionPolicy Bypass. Paths must be single-quoted literals."""
        import updater as u
        evil = r'D:\tools\$(Start-Process calc.exe)\CMIS'
        ps = u.build_swap_script(evil + r'\_cmis_update', evil)
        self.assertNotIn(f'"{evil}', ps, 'path landed in an expanding string')
        self.assertIn(f"'{evil}", ps)

    def test_embedded_quote_is_escaped(self):
        import updater as u
        self.assertEqual(u.ps_literal(r"D:\it's here"), r"'D:\it''s here'")

    def test_asset_url_must_be_https_on_github(self):
        import updater as u
        for bad in ('http://attacker.example/p.zip',
                    'https://attacker.example/p.zip',
                    'https://github.com.evil.example/p.zip',
                    'http://github.com/o/r/p.zip'):
            self.assertFalse(u.is_trusted_url(bad), bad)
        for good in (_GH_URL, 'https://objects.githubusercontent.com/x'):
            self.assertTrue(u.is_trusted_url(good), good)

    def test_release_pointing_off_github_is_not_installable(self):
        import updater as u
        self.assertIsNone(u.normalize_release({
            'tag_name': 'v9.9.9',
            'assets': [{'name': 'CMIS_dist_v9_9_9.zip', 'size': 1,
                        'browser_download_url': 'https://attacker.example/p.zip'}],
        }))

    def test_download_refuses_an_untrusted_url(self):
        """Belt and braces: the check does not rely on normalize_release
        having been the only way the URL was chosen."""
        import updater as u
        with self.assertRaises(ValueError):
            u.download_asset('http://attacker.example/p.zip', 'unused.zip')

    def test_redirects_off_github_are_refused(self):
        """urllib follows redirects on its own; without this handler a 302
        could walk the download onto plain http or another host entirely."""
        import updater as u
        h = u._TrustedRedirectHandler()
        with self.assertRaises(Exception):
            h.redirect_request(None, None, 302, 'Found', {},
                               'http://attacker.example/p.zip')


PREFS_KEY = 'cmis.ui'


class TestDisplayPreferences(CMISTestCase):
    """The UI is themeable and scalable, which only holds while nothing
    hard-codes a colour or a size behind the variable system's back.

    These read the three frontend files as text. Coarse, but the alternative is
    a browser in the test loop, and what they catch is exactly the silent kind
    of breakage: a stray hex that looks right in the theme it was picked for, a
    breakpoint that quietly stops matching once the user changes scale.
    """

    def _read(self, *parts):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        return io.open(path, encoding='utf-8').read()

    def _css(self):
        return self._read('static', 'style.css')

    def _js(self):
        return self._read('static', 'app.js')

    def _html(self):
        return self._read('templates', 'index.html')

    def _palettes(self):
        """Token names defined by each theme block, keyed by theme name."""
        pattern = r':root(?:,\s*\n:root)?\[data-theme="(\w+)"\]\s*\{(.*?)\n\}'
        return {m.group(1): set(re.findall(r'(--[\w-]+)\s*:', m.group(2)))
                for m in re.finditer(pattern, self._css(), re.S)}

    def _rules(self):
        """The stylesheet with the palette blocks removed."""
        return self._css().split('* {\n  box-sizing', 1)[1]

    def test_every_theme_defines_the_same_tokens(self):
        """A theme that omits a token silently inherits the default palette's.

        That is how a light theme ends up drawing pale-on-white alarm text: it
        looks finished until the one value it forgot lands on the wrong ground.
        """
        palettes = self._palettes()
        self.assertGreaterEqual(len(palettes), 4, f'themes: {sorted(palettes)}')
        self.assertIn('midnight', palettes)
        base = palettes['midnight']
        self.assertGreater(len(base), 25, 'the default palette looks truncated')
        for name, tokens in palettes.items():
            self.assertEqual(base - tokens, set(), f'theme {name} is missing tokens')
            self.assertEqual(tokens - base, set(), f'theme {name} defines extra tokens')

    def test_every_theme_declares_a_colour_scheme(self):
        """Dropdown popups and scrollbars are drawn by the OS, not by CSS, so a
        light theme without color-scheme gets black popups."""
        self.assertEqual(len(re.findall(r'color-scheme:', self._css())),
                         len(self._palettes()))

    def test_no_colour_literals_outside_the_palettes(self):
        """A colour belongs to a theme. A literal belongs to whichever theme
        its author happened to be looking at."""
        offenders = [line.strip() for line in self._rules().splitlines()
                     if re.search(r':[^;]*#[0-9a-fA-F]{3,8}\b', line)
                     or re.search(r':[^;]*\brgba?\(', line)]
        self.assertEqual(offenders, [])

    def test_no_colour_literals_in_the_frontend_scripts(self):
        """The post-update screen used to hard-code pale green on a dark
        ground. On a light theme that is near-white on white, and it is the one
        screen that tells the user how to finish updating."""
        for name, text in (('app.js', self._js()), ('index.html', self._html())):
            self.assertEqual(re.findall(r'(?<!&)#[0-9a-fA-F]{3,8}\b', text), [],
                             f'{name} hard-codes a colour')

    def test_font_sizes_all_use_tokens(self):
        """One step has to move every size together, so no size may opt out."""
        for name, text in (('style.css', self._css()), ('index.html', self._html()),
                           ('app.js', self._js())):
            self.assertEqual(re.findall(r'font-size:\s*\d+px', text), [],
                             f'{name} sets a font-size outside the scale')

    def test_font_step_is_written_with_a_unit(self):
        """calc(10px + 2) is invalid at computed-value time, and an invalid
        font-size falls back to the inherited one - so a missing 'px' would
        collapse every label in the app at once."""
        for text in (self._js(), self._html()):
            for m in re.finditer(r"setProperty\(\s*'--fs-step'\s*,\s*([^)]+)\)", text):
                self.assertIn("'px'", m.group(1), f'unitless --fs-step: {m.group(1)}')

    def test_monospace_survives_the_font_setting(self):
        """Hex dumps and register columns must keep their digits aligned
        whatever family the user picks, so no preset reassigns --font-mono."""
        css = self._css()
        self.assertIn('font-family: var(--font-mono);', css)
        for m in re.finditer(r':root\[data-font-sans="\w+"\]\s*\{([^}]*)\}', css):
            self.assertNotIn('--font-mono:', m.group(1))

    def test_no_magic_viewport_arithmetic(self):
        """The shell used to subtract a hard-coded header height. It was
        already a pixel out, and it multiplies wrongly once the UI can scale."""
        css = self._css()
        self.assertNotIn('calc(100vh', css)
        self.assertIn('inset: 0;', css)

    def test_scale_uses_zoom_not_transform(self):
        """transform: scale() rasterises then resamples - blurry text - and
        does not reflow, so nothing inside would adapt to the new width."""
        css = self._css()
        self.assertIn('zoom: var(--ui-zoom)', css)
        self.assertNotIn('transform: scale(', css)

    def test_breakpoints_use_container_queries(self):
        """@media measures the unzoomed viewport. At 150% on a 1920 screen the
        content is effectively 1280 wide while every media query still reports
        1920, so the breakpoints stop firing exactly when they are needed."""
        css = self._css()
        self.assertGreaterEqual(len(re.findall(r'@container\s', css)), 4)
        self.assertEqual(re.findall(r'@media\s*\(', css), [])
        self.assertIn('container-type: inline-size', css)

    def test_every_table_is_scroll_contained(self):
        """The counters table passes 1300px once total_bits reaches 18 digits,
        which is minutes into any run. With no scroll box it draws outside its
        own card and reads as a rendering fault."""
        html = self._html()
        self.assertEqual(html.count('<table'), html.count('class="table-scroll"'))
        self.assertNotIn('float:right', html)

    def test_theme_is_applied_before_the_first_paint(self):
        """Applying the saved theme from app.js would paint the default palette
        first and then swap, which reads as a flash on every launch."""
        head = self._html().split('</head>', 1)[0]
        self.assertIn(PREFS_KEY, head)
        self.assertLess(head.index(PREFS_KEY), head.index('style.css'))
        self.assertIn('documentElement', head)
        self.assertIn('data-theme', head)

    def test_preferences_survive_the_body_being_replaced(self):
        """_waitForNewVersion() assigns document.body.innerHTML, so anything
        parked on <body> goes with it."""
        block = self._js().split('function applyPrefs', 1)[1].split('\n}', 1)[0]
        self.assertIn('documentElement', block)
        self.assertNotIn('document.body', block)

    def test_preferences_key_agrees_between_bootstrap_and_app(self):
        """The key is necessarily spelled out twice. If the two drift the saved
        settings are silently discarded on every reload."""
        self.assertIn(PREFS_KEY, self._html())
        self.assertIn(PREFS_KEY, self._js())

    def test_auto_scale_never_shrinks(self):
        """innerWidth is CSS pixels, so a 1366x768 laptop at 125% reports about
        1093 - a small workspace, not a dense one. Scaling down there would
        shrink the text that is already hardest to read."""
        block = self._js().split('function autoScale', 1)[1].split('\n}', 1)[0]
        factors = [float(x) for x in re.findall(r'return\s+([\d.]+);', block)]
        self.assertTrue(factors, 'autoScale returns nothing')
        self.assertEqual(min(factors), 1, f'auto scale goes below 100%: {factors}')
        self.assertNotIn('devicePixelRatio', block)

    def test_display_settings_reachable_without_a_connection(self):
        """updateConnectionUI disables every tab button until a module is
        connected, and someone whose UI is unreadable has to fix that first."""
        html = self._html()
        self.assertIn('id="btn-settings"', html)
        tabs = html.split('<nav class="tabs">', 1)[1].split('</nav>', 1)[0]
        self.assertNotIn('btn-settings', tabs)
        button = re.search(r'<button[^>]*id="btn-settings"[^>]*>', html).group(0)
        self.assertNotIn('disabled', button)

    def test_manual_documents_the_display_settings(self):
        """CLAUDE.md requires the manual to track user-visible behaviour."""
        manual = self._read('CMIS2Customer', 'CMIS模块管理工具操作手册.html')
        for phrase in ('显示设置', '主题', '界面缩放'):
            self.assertIn(phrase, manual, f'the manual never mentions {phrase}')


class TestManualMatchesBehaviour(CMISTestCase):
    """The manual ships to customers as the only description of the product.

    These pin the claims that were found wrong once already, so a rewrite or a
    careless version bump cannot quietly put them back.
    """

    def _manual(self):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'CMIS2Customer', 'CMIS模块管理工具操作手册.html')
        return io.open(path, encoding='utf-8').read()

    def test_the_page_marks_5_4_fields_and_reads_the_list_from_the_server(self):
        """A reader has to be able to tell a 5.4 field from one that has always
        been there. The badge is styling only - which fields get it comes from
        /api/module/capabilities, so the list cannot drift from the decoder."""
        import io as _io
        js = _io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'static', 'app.js'), encoding='utf-8').read()
        self.assertIn('badge-new54', js)
        self.assertIn('>5.4<', js, 'the badge no longer says what it marks')
        self.assertIn("apiGet('/api/module/capabilities')", js)
        css = _io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'static', 'style.css'), encoding='utf-8').read()
        self.assertIn('.badge-new54', css, 'the badge has no styling')

    def test_the_masked_control_rows_are_rebuilt_too_not_just_headers(self):
        """Output Controls and Loopback carry one static cell per lane in the
        markup. Widening only their headings would leave checkboxes for eight
        lanes under sixteen headings - the control for lane 9 would simply not
        exist, while the header claimed it did.
        """
        import io as _io
        base = os.path.dirname(os.path.abspath(__file__))
        js = _io.open(os.path.join(base, 'static', 'app.js'), encoding='utf-8').read()
        idx = js.index('function rebuildLaneColumns(')
        body = js[idx:idx + 1600]
        for prefix in ("'sq'", "'lb-mso'"):
            self.assertIn(prefix, body, f'{prefix} row is never rebuilt')
        self.assertIn('insertCell', body, 'no cells are created for extra lanes')
        # And the renderers must size from the module, not from eight.
        for fn in ('_populateBitmaskRow', '_populateLoopbackRow'):
            i = js.index(f'function {fn}(')
            self.assertIn('AppState.lanes', js[i:i + 500],
                          f'{fn} still renders a fixed eight lanes')

    def test_lane_columns_are_rebuilt_for_wide_modules(self):
        """Five tables put lanes across the top with L1..L8 written into the
        HTML. Sixteen data cells under eight headings puts every reading past
        the eighth under the wrong column - wrong, and quietly so."""
        import io as _io
        base = os.path.dirname(os.path.abspath(__file__))
        html = _io.open(os.path.join(base, 'templates', 'index.html'), encoding='utf-8').read()
        js = _io.open(os.path.join(base, 'static', 'app.js'), encoding='utf-8').read()
        # Counted from the markup rather than pinned to a number: every header
        # row that lays lanes out as columns needs the marker, and a new table
        # is exactly when one gets forgotten.
        import re as _re
        rows = _re.findall(r'<tr[^>]*>.*?</tr>', html, _re.S)
        lane_rows = [r for r in rows if '<th>L1</th>' in r.replace(' ', '')
                     or '<th>L1</th>' in r]
        self.assertTrue(lane_rows, 'no lane-column headers found at all')
        for r in lane_rows:
            self.assertIn('data-lane-cols="1"', r,
                          'a lane-column header lost its marker')
        self.assertIn('rebuildLaneColumns', js)
        idx = js.index('function rebuildLaneColumns(')
        body = js[idx:idx + 600]
        self.assertIn('AppState.lanes', body,
                      'headers are not sized from the module')

    def test_the_manual_documents_the_5_4_pages_and_their_traps(self):
        """Each of these is a place the tool deliberately does something the
        reader would not guess, so the manual has to say which and why."""
        manual = self._manual()
        for reg in ('60h:128', '61h:128', '62h:128', '6Dh:136', '0Ch:128'):
            self.assertIn(reg, manual, f'{reg} panel is undocumented')
        # The reasons, not just the addresses.
        self.assertIn('可以合法地不一致', manual, '60h vs 01h polarity')
        self.assertIn('必须是通道的一个置换', manual, 'the redirection rule')
        self.assertIn('都印成了字节 195', manual, 'the spec typo behind the missing reset')
        self.assertIn('没有广告的页本工具不会去读', manual, 'why unadvertised pages are skipped')

    def test_the_manual_says_which_cmis_revision_the_tool_decodes(self):
        """It ships as the only description of the product, and "which
        revision" is the first thing a reader checks against their module."""
        manual = self._manual()
        self.assertIn('CMIS 5.4', manual)
        self.assertIn('OIF-CMIS-05.4.pdf', manual)
        self.assertIn('向后兼容', manual, 'a 5.3 module must not look unsupported')

    def test_the_manual_marks_what_5_4_actually_added(self):
        """The badge in the UI and this list are the same claim; a reader who
        sees "5.4 新增" beside a field has to be able to look it up."""
        manual = self._manual()
        self.assertIn('5.4 新增', manual)
        for reg in ('01h:171', '01h:173', '00h:61', '01h:252', '04h:196', '12h:216'):
            self.assertIn(reg, manual, f'{reg} is a 5.4 addition the tool reads')

    def test_the_manual_does_not_claim_1_6t_support_that_5_4_lacks(self):
        """5.4 never mentions 1.6T and still references SFF-8024 rev 4.10.
        What it raised is the lane ceiling and the interface code space, and
        one application is still capped at eight lanes - saying otherwise
        would send someone looking for a feature that is not there.
        """
        manual = self._manual()
        self.assertIn('256', manual, 'the lane ceiling 5.4 actually raised')
        self.assertIn('SFF-8024 rev 4.10', manual)
        self.assertIn('8 条通道', manual, 'the per-application limit still applies')

    def test_the_manual_admits_multi_bank_was_only_mocked(self):
        """No 16-lane hardware was available. Claiming otherwise is the kind
        of thing this manual has already been corrected for once."""
        manual = self._manual()
        self.assertIn('仅经 Mock 验证', manual)

    def test_the_alarm_fallback_is_not_described_as_symmetric(self):
        """ALARM_FALLBACK is -10/+3 dBm; '±10' was never a value in the code."""
        manual = self._manual()
        self.assertNotIn('±10', manual)
        self.assertIn('−10 / +3', manual)

    def test_the_guard_paragraph_keeps_its_version_and_its_scope(self):
        """Bumping the version with a global search-and-replace rewrote history
        here once: the cross-site guard shipped in v2.1.0, not v2.2.0. The
        scope matters just as much - the guard covers /api/, so a reader must
        not conclude the page failing to load is what keeps them safe.
        """
        manual = self._manual()
        self.assertIn('自 <b>v2.1.0</b> 起', manual)
        self.assertIn('/api/', manual)
        for name in ('127.0.0.1', 'localhost', '[::1]'):
            self.assertIn(name, manual, f'{name} also reaches the API')

    def test_the_datapath_apply_carries_a_traffic_warning(self):
        """Apply restarts all eight lanes including the untouched ones, which
        is the easiest way in the whole UI to drop live traffic by accident."""
        manual = self._manual()
        self.assertIn('Apply 会重启全部 8 条 lane', manual)
        section = manual.split('9.3 DataPath 配置表', 1)[1].split('9.4', 1)[0]
        self.assertIn('callout-warn', section)
        self.assertIn('中断', section)

    def test_the_appendix_lists_the_buttons_that_disrupt_traffic(self):
        """It calls itself a button reference; omitting the dangerous ones is
        how a reader concludes none of them are dangerous."""
        appendix = self._manual().split('13. 附录', 1)[1]
        for button in ('Reset Module', 'Enter LowPwr', 'Exit LowPwr', 'Write'):
            self.assertIn(button, appendix, f'{button} is missing')

    def test_a_missing_digest_reads_differently_from_a_bad_one(self):
        """"We checked and it was wrong" and "there was nothing to check
        against" call for different reactions from the user."""
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
        src = io.open(path, encoding='utf-8').read()
        self.assertIn('publishes no SHA-256 digest', src)
        self.assertIn('failed its checksum', src)


class TestRequestGuardScope(CMISTestCase):
    """The provenance guard covers the API, not the page."""

    def test_the_page_and_its_assets_are_not_gated(self):
        """Serving the HTML and the stylesheet grants no capability - every way
        to reach the module is under /api/ - so refusing a cross-site
        navigation only meant that following a link to this tool from a wiki or
        a chat message landed the user on a 403 instead of the UI.
        """
        for path in ('/', '/static/style.css', '/static/app.js'):
            r = self.client.get(path, headers={'Sec-Fetch-Site': 'cross-site'})
            self.assertEqual(r.status_code, 200, path)

    def test_the_api_is_still_gated(self):
        for path in ('/api/version', '/api/backends', '/api/disconnect'):
            r = self.client.get(path, headers={'Sec-Fetch-Site': 'cross-site'})
            self.assertEqual(r.status_code, 403, path)


# ============================================================
# Run
# ============================================================

class TestWhetherAnOutputIsActuallyOn(CMISTestCase):
    """11h:132-133 (Table 8-95) are RO and Required, and 8.14.2 says they
    report output validity "independent of the state of the DPSM instances
    associated with those output lanes". Four controls in this tool mute an
    output without touching the DataPath State, so a lane sending nothing read
    Activated and green, and the operator who had just ticked one of those
    boxes had no way to see it take effect."""

    def _connect(self, backend='mock_fr4x2'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _lanes(self):
        return self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']

    def _tx(self):
        return [l['output_valid_tx'] for l in self._lanes()]

    def _rx(self):
        return [l['output_valid_rx'] for l in self._lanes()]

    def _squelch(self, body):
        self.assertOk(self.client.post('/api/module/squelch',
                                       data=json.dumps(body),
                                       content_type='application/json'))

    def _datapath(self, body):
        self.assertOk(self.client.post('/api/module/datapath',
                                       data=json.dumps(body),
                                       content_type='application/json'))

    def _flags(self):
        return self.assertOk(
            self.client.get('/api/module/flags'))['data']['lanes']

    def test_a_running_lane_reports_a_valid_output(self):
        self._connect()
        self.assertEqual(self._tx(), [True] * 8)
        self.assertEqual(self._rx(), [True] * 8)

    def test_disabling_tx_mutes_the_tx_output_only(self):
        self._connect()
        self._datapath({'tx_disable_mask': 0x0F})
        self.assertEqual(self._tx(), [False] * 4 + [True] * 4,
                         'OutputDisableTx did not show up in 11h:133')
        self.assertEqual(self._rx(), [True] * 8,
                         'disabling the Tx output muted the Rx output too')

    def test_force_squelching_tx_mutes_the_tx_output(self):
        """10h:132 is a lane-specific control that needs no Apply, so the only
        confirmation it ever gets is the output status."""
        # 01h:155.3 gates this control and mock_fr4x2 clears it on purpose,
        # so the force-squelch path has to be exercised on a module that
        # actually advertises it.
        self._connect('mock_dr8')
        self._squelch({'tx_squelch_force': 0x03})
        self.assertEqual(self._tx(), [False, False] + [True] * 6)
        self.assertEqual(self._rx(), [True] * 8)

    def test_disabling_the_rx_output_mutes_the_rx_output_only(self):
        self._connect()
        self._squelch({'rx_output_disable': 0xF0})
        self.assertEqual(self._rx(), [True] * 4 + [False] * 4,
                         'OutputDisableRx did not show up in 11h:132')
        self.assertEqual(self._tx(), [True] * 8,
                         'disabling the Rx output muted the Tx output too')

    def test_a_muted_lane_still_reads_activated(self):
        """The point of the register: the DataPath State cannot say this."""
        self._connect()
        self._datapath({'tx_disable_mask': 0xFF})
        lanes = self._lanes()
        self.assertEqual({l['datapath_state'] for l in lanes}, {'Activated'})
        self.assertEqual([l['output_valid_tx'] for l in lanes], [False] * 8)

    def test_a_deinitialised_path_reports_no_output(self):
        self._connect()
        self._datapath({'dp_deinit_mask': 0xF0})
        time.sleep(0.6)
        self.assertEqual(self._tx(), [True] * 4 + [False] * 4)
        self.assertEqual(self._rx(), [True] * 4 + [False] * 4)

    def test_a_squelch_that_came_and_went_is_still_recorded(self):
        """8.14.2 gives the Rx side a latched Flag at 11h:153 precisely so a
        momentary mute survives to the next poll. There is deliberately no Tx
        equivalent."""
        self._connect()
        self._flags()                       # start from a cleared page
        self._squelch({'rx_output_disable': 0x01})
        self._squelch({'rx_output_disable': 0x00})
        self.assertEqual(self._rx()[0], True, 'the mute did not lift')
        lanes = self._flags()
        self.assertEqual([l['lane'] for l in lanes if l['rx_output_changed']],
                         [1])

    def test_the_flag_clears_on_read_but_the_history_does_not(self):
        self._connect()
        self._flags()
        self._squelch({'rx_output_disable': 0x02})
        self.assertTrue(self._flags()[1]['rx_output_changed'])
        again = self._flags()[1]
        self.assertFalse(again['rx_output_changed'],
                         '11h:153 is RO/COR and must not survive its own read')
        self.assertIn('rx_output_changed', again['seen'])

    def test_the_flag_does_not_fire_on_a_module_left_alone(self):
        """A Flag that sets itself on every poll is worse than none."""
        self._connect()
        self._flags()
        for _ in range(3):
            self.assertEqual([l['rx_output_changed'] for l in self._flags()],
                             [False] * 8)

    def test_the_monitoring_table_shows_it(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        # Anchored on the table this is about rather than on one line inside
        # the renderer: that line has already been rewritten once for an
        # unrelated reason and took this test with it.
        # Anchored on the renderer's own tbody rather than on one line inside
        # it: that line has already been rewritten once for an unrelated
        # reason and took this test with it. The first mention of the table id
        # belongs to the stale-marking helper, so match the assignment.
        row = js[js.index("const tbody = document.getElementById('tbl-monitoring')"):]
        row = row[row.index('return `<tr>'):]
        row = row[:row.index('}).join')]
        self.assertIn('outputCell(lane)', row,
                      'the monitoring table never renders the output status')
        body = js[js.index('function outputCell('):]
        body = body[:body.index('\n}')]
        for field in ('output_valid_tx', 'output_valid_rx'):
            self.assertIn(field, body,
                          'the cell cannot be showing ' + field)

    def test_a_banked_module_reports_all_of_its_lanes(self):
        """Page 11h repeats per bank, so lanes 9-16 read bank 1's copy. A
        status written only into bank 0 leaves the second half of a 16-lane
        module permanently muted on screen."""
        self._connect('mock_1600g_16lane')
        self.assertEqual(self._tx(), [True] * 16)
        self.assertEqual(self._rx(), [True] * 16)

    def test_the_cell_reads_differently_when_the_output_is_muted(self):
        """A cell that renders the same thing either way is the same defect as
        having no column at all, and much harder to notice."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('function outputCell('):]
        body = body[:body.index(chr(10) + '}')]
        picked = re.search(r'const dot = \([^)]*\) =>\s*(\S+)', body)
        self.assertEqual(picked.group(1), 'valid',
                         'the chip is not chosen by the output validity')
        for cls in ('flag-ok', 'flag-warn'):
            self.assertIn(cls, body,
                          'both outcomes must be distinguishable: ' + cls)

    def test_the_flags_table_shows_the_latched_one(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        self.assertRegex(js, r'lane\.rx_output_changed\s*\n?\s*\?',
                         'the flags table does not branch on 11h:153')


class TestHowFarTheModuleReaches(CMISTestCase):
    """01h:128-137 are ten contiguous RO/Required bytes and the tool read two
    of them. Table 8-45 says how far the module reaches on each fibre type;
    Table 8-44 says which firmware is sitting in the standby bank. The only
    length on screen was 00h:202, which by specification an ordinary
    transceiver populates with zeroes - so the field said "- (transceiver)"
    for precisely the modules whose reach the module was publishing."""

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _info(self):
        return self.assertOk(self.client.get('/api/module/info'))['data']

    def test_a_single_mode_module_reports_its_reach(self):
        self._connect('mock_dr8')
        self.assertEqual(self._info()['link_lengths'],
                         [{'media': 'SMF', 'km': 0.5}])

    def test_the_multimode_module_reports_the_fibre_it_runs_on(self):
        """An SR module's reach is on OM4, not on SMF - reading only the SMF
        byte would report nothing at all for it."""
        self._connect('mock_sr8')
        self.assertEqual(self._info()['link_lengths'],
                         [{'media': 'OM4', 'm': 100}])

    def test_the_multiplier_is_applied(self):
        """01h:132 is base x multiplier, and mock_coherent uses the 1 km
        multiplier while mock_dr8 uses 0.1 - reading the base alone would make
        a 10 km module and a 500 m one both read 5."""
        self._connect('mock_coherent')
        self.assertEqual(self._info()['link_lengths'],
                         [{'media': 'SMF', 'km': 10.0}])

    def test_a_module_that_advertises_nothing_says_so(self):
        self._connect('mock_coherent_zr')
        self.assertEqual(self._info()['link_lengths'], [])

    def test_the_escape_multiplier_is_read_from_137(self):
        """01h:132[7:6] = 11b means the multiplier lives in 01h:137, which is
        why the burst has to run to the end of the table."""
        self._connect('mock_dr8')
        poke(0x01, 0x84, 0xC0 | 40)
        poke(0x01, 0x89, 0xC0)              # 11b -> x500 km
        self.assertEqual(self._info()['link_lengths'],
                         [{'media': 'SMF', 'km': 20000.0}])

    def test_every_advertised_fibre_type_is_listed(self):
        self._connect('mock_sr8')
        poke(0x01, 0x85, 30)                # OM5 60 m
        poke(0x01, 0x87, 35)                # OM3 70 m
        poke(0x01, 0x88, 20)                # OM2 20 m, single-metre units
        self.assertEqual(self._info()['link_lengths'],
                         [{'media': 'OM5', 'm': 60}, {'media': 'OM4', 'm': 100},
                          {'media': 'OM3', 'm': 70}, {'media': 'OM2', 'm': 20}])

    def test_the_inactive_firmware_revision_is_reported(self):
        """Table 8-44: modules carry two firmware images, and the standby one
        is what says whether an update landed in the bank you meant."""
        self._connect('mock_dr8')
        d = self._info()
        self.assertEqual(d['fw_inactive_revision'], '1.0')
        self.assertNotEqual(d['fw_inactive_revision'], d['fw_revision'])

    def test_the_hardware_revision_still_comes_from_its_own_bytes(self):
        """The three fields now share one burst; an off-by-one in the slicing
        would hand one field's bytes to another."""
        self._connect('mock_dr8')
        poke(0x01, 0x82, 9)
        poke(0x01, 0x83, 7)
        d = self._info()
        self.assertEqual(d['hw_revision'], '9.7')
        self.assertEqual(d['fw_inactive_revision'], '1.0')

    def test_the_panel_shows_both(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        self.assertIn('linkLengthSummary(d.link_lengths)', js,
                      'the reach is read and then never shown')
        self.assertIn('d.fw_inactive_revision', js)
        body = js[js.index('function linkLengthSummary('):]
        body = body[:body.index(chr(10) + '}')]
        self.assertRegex(body, r'x\.km !== undefined',
                         'kilometres and metres cannot be told apart')
        self.assertRegex(body, r'!list \|\| !list\.length',
                         'an unadvertised reach renders as an empty string')

    def test_the_cable_length_row_points_at_the_other_one(self):
        """A transceiver reads 0 there by specification, and the row used to
        stop at that."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        row = re.search(r"\['Cable Length',[^\n]*", js).group(0)
        self.assertIn('see Link Length', row)


class TestCommittingWithoutTearingTheLinkDown(CMISTestCase):
    """8.13.3.1 defines two Apply triggers. ApplyDPInit (10h:143) walks the
    Data Path back through DPInit; ApplyImmediate (10h:144) commits the same
    staged set into hardware with the path staying where it is. The tool had
    only the first, so every change cost a re-initialisation - and Lower 02h,
    which says which of the two the module honours, was read for one bit."""

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, body, code=200):
        rv = self.client.post('/api/module/datapath', data=json.dumps(body),
                              content_type='application/json')
        self.assertEqual(rv.status_code, code, rv.data)
        return json.loads(rv.data)

    def _lanes(self):
        return self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']

    def _info(self):
        return self.assertOk(self.client.get('/api/module/info'))['data']

    def _poke_lower_02(self, value):
        # Lower memory is visible whatever page is selected, so this needs no
        # page select and cannot desynchronise the API's page cache.
        _state['backend'].write_bytes(0x02, bytes([value]))

    def _running(self):
        self._post({'app_select': [1] * 8, 'apply': True})
        time.sleep(1.2)
        self.assertEqual({l['datapath_state'] for l in self._lanes()},
                         {'Activated'})

    def test_the_data_path_never_leaves_activated(self):
        """The whole point: no transient, so no traffic hit."""
        self._connect('mock_coherent')
        self._running()
        self._post({'app_select': [1] * 8, 'apply_immediate': True})
        seen = set()
        for _ in range(6):
            time.sleep(0.15)
            seen |= {l['datapath_state'] for l in self._lanes()}
        self.assertEqual(seen, {'Activated'})

    def test_it_still_reports_a_result(self):
        self._connect('mock_coherent')
        self._running()
        self._post({'app_select': [1] * 8, 'apply_immediate': True})
        time.sleep(0.9)
        self.assertEqual({l['config_status'] for l in self._lanes()},
                         {'ConfigSuccess'})

    def test_it_leaves_no_bounce_behind(self):
        """6.3.3 sets DPStateChangedFlag on a steady state reached through a
        transient. There was none, so claiming one would send whoever is
        chasing an intermittent link after a change they made themselves."""
        self._connect('mock_coherent')
        self._running()
        self.client.get('/api/module/flags')
        self.assertOk(self.client.post('/api/module/flags/clear'))
        self._post({'app_select': [1] * 8, 'apply_immediate': True})
        time.sleep(0.9)
        lanes = self.assertOk(self.client.get('/api/module/flags'))['data']['lanes']
        self.assertEqual([l['lane'] for l in lanes
                          if l['dp_state_changed']
                          or 'dp_state_changed' in l['seen']],
                         [])

    def test_apply_dpinit_still_bounces_the_path(self):
        """The contrast is the point; if both triggers behaved the same the
        new one would be decoration."""
        self._connect('mock_coherent')
        self._running()
        self.client.get('/api/module/flags')
        self.assertOk(self.client.post('/api/module/flags/clear'))
        self._post({'app_select': [1] * 8, 'apply': True})
        time.sleep(1.2)
        lanes = self.assertOk(self.client.get('/api/module/flags'))['data']['lanes']
        self.assertEqual([l['lane'] for l in lanes
                          if l['dp_state_changed']
                          or 'dp_state_changed' in l['seen']],
                         list(range(1, 9)))

    def test_a_stepped_only_module_refuses_rather_than_ignores(self):
        """The module ignores any WRITE to ApplyImmediate registers, and a
        silent no-op is the one outcome the operator cannot diagnose."""
        self._connect('mock_dr8')
        body = self._post({'app_select': [1] * 8, 'apply_immediate': True}, 400)
        self.assertIn('Lower 02h', body['message'])

    def test_a_stepped_only_module_still_takes_the_other_trigger(self):
        self._connect('mock_dr8')
        self._post({'app_select': [1] * 8, 'apply': True})
        time.sleep(1.2)
        self.assertEqual({l['datapath_state'] for l in self._lanes()},
                         {'Activated'})

    def test_asking_for_both_triggers_is_refused(self):
        self._connect('mock_coherent')
        body = self._post({'app_select': [1] * 8, 'apply': True,
                           'apply_immediate': True}, 400)
        self.assertIn('one Apply trigger', body['message'])

    def _active(self):
        return [l['active_app_select'] for l in self.assertOk(
            self.client.get('/api/module/datapath'))['data']['lanes']]

    def test_the_mock_ignores_the_write_when_it_says_it_will(self):
        """Written behind the API's back, so what is tested is the module's
        own promise and not the guard standing in front of it.

        Something different has to be staged first: an ApplyImmediate that was
        wrongly honoured while the staged and active sets already agreed would
        commit the same values again and leave no trace of having run.
        """
        self._connect('mock_dr8')
        # Park two lanes free, so the staged change is one ApplyImmediate is
        # allowed to commit on a running path: bringing unused lanes into a
        # Data Path is neither a width change nor a lane being freed.
        reconfigure(self.client, [2, 2, 2, 2, 0, 0, 0, 0])
        time.sleep(1.2)
        self.assertEqual(self._active(), [2, 2, 2, 2, 0, 0, 0, 0])
        self._post({'app_select': [2] * 8})       # staged, deliberately not applied
        poke(0x10, 0x90, 0xFF)
        time.sleep(0.7)
        self.assertEqual(self._active(), [2, 2, 2, 2, 0, 0, 0, 0],
                         'the module committed a configuration through a '
                         'trigger it advertises that it ignores')

    def test_the_same_write_is_honoured_where_it_is_advertised(self):
        """Otherwise the test above would pass on a mock that ignores every
        write to 10h:144, advertised or not.

        Same module and same staged change as above - only Lower 02h differs,
        so the advertisement is the single variable."""
        self._connect('mock_dr8')
        reconfigure(self.client, [2, 2, 2, 2, 0, 0, 0, 0])
        time.sleep(1.2)
        self._poke_lower_02(0x00)                 # legacy default: both supported
        self._post({'app_select': [2] * 8})
        self.assertEqual(self._active(), [2, 2, 2, 2, 0, 0, 0, 0])
        poke(0x10, 0x90, 0xFF)
        time.sleep(0.7)
        self.assertEqual(self._active(), [2] * 8,
                         'a module advertising hot reconfiguration ignored '
                         'the trigger anyway')

    def test_lower_02h_decodes_all_four_fields(self):
        self._connect('mock_1600g_16lane')
        cc = self._info()['config_capabilities']
        self.assertEqual(cc['memory_model'], 'Paged')
        self.assertTrue(cc['stepped_config_only'])
        self.assertFalse(cc['hot_reconfig'])
        self.assertTrue(cc['regular_reconfig'])
        self.assertEqual(cc['mci_max_speed_i2c'], '1 MHz')

    def test_the_legacy_default_advertises_both(self):
        self._connect('mock_coherent')
        cc = self._info()['config_capabilities']
        self.assertFalse(cc['stepped_config_only'])
        self.assertTrue(cc['hot_reconfig'])
        self.assertTrue(cc['regular_reconfig'])

    def test_neither_procedure_leaves_the_button_off(self):
        """SteppedConfigOnly with AutoCommissioning 00b means neither."""
        self._connect('mock_dr8')
        self._poke_lower_02(0x40)
        cc = self._info()['config_capabilities']
        self.assertFalse(cc['hot_reconfig'])
        self.assertFalse(cc['regular_reconfig'])

    def test_a_reserved_speed_code_is_not_invented(self):
        """3-15 are Reserved on the I2C scale; guessing a number for one would
        be worse than showing the code."""
        self._connect('mock_dr8')
        self._poke_lower_02(0x41 | (7 << 2))
        cc = self._info()['config_capabilities']
        self.assertIsNone(cc['mci_max_speed_i2c'])
        self.assertEqual(cc['mci_max_speed_code'], 7)

    def test_the_capabilities_endpoint_carries_what_the_gate_needs(self):
        """The button is gated on AppState.caps.config, which is only ever
        filled from this endpoint - a source-level check of the gate proves
        nothing if the value never arrives."""
        self._connect('mock_dr8')
        caps = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']
        self.assertIs(caps['config']['hot_reconfig'], False)
        self._connect('mock_coherent')
        caps = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']
        self.assertIs(caps['config']['hot_reconfig'], True)

    def test_the_button_exists_and_is_gated_on_the_advertisement(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'templates', 'index.html'),
                  encoding='utf-8') as f:
            html = f.read()
        with open(os.path.join(here, 'static', 'app.js'), encoding='utf-8') as f:
            js = f.read()
        self.assertIn('id="btn-apply-immediate"', html,
                      'there is no Apply Immediate button')
        self.assertRegex(js, r'hotBtn\.disabled = hot === false',
                         'the button is not gated on the advertisement')
        self.assertRegex(js, r'apply_immediate: true',
                         'the button never sends the immediate trigger')

    def test_the_panel_names_which_triggers_work(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'static', 'app.js'), encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('function reconfigSummary('):]
        body = body[:body.index(chr(10) + '}')]
        self.assertRegex(body, r'cc\.regular_reconfig')
        self.assertRegex(body, r'cc\.hot_reconfig')


class TestWhichThresholdsALaneIsJudgedBy(CMISTestCase):
    """8.32: Page 62h holds the Tx output power thresholds per media lane, in
    0.01 dBm rather than Page 02h's module-wide 0.1 uW. Where a module
    publishes it, that is what applies to a lane. The tool read the page,
    showed it in its own card, and went on colouring every lane against the
    module-wide numbers - so a lane could sit outside its own alarm threshold
    and still be painted as healthy."""

    def _connect(self, backend='mock_1600g_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _lanes(self):
        return self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']

    def _set_lane_threshold(self, lane, hi_dbm, lo_dbm):
        """Write one lane's 62h quad. The two 1600G profiles derive Page 62h
        from the module-wide values, so nothing in the shipped data can tell
        the two rules apart - the discriminating case has to be built."""
        import cmis_registers as c
        base = c.REG_LANE_PWR_THRESHOLDS[1] + (lane - 1) * 8
        for off, dbm in ((0, hi_dbm), (2, lo_dbm)):
            raw = struct.pack('>h', int(round(dbm * 100)))
            poke(0x62, base + off, raw[0])
            poke(0x62, base + off + 1, raw[1])

    def test_the_per_lane_thresholds_reach_the_monitoring_table(self):
        self._connect()
        lane = self._lanes()[0]
        self.assertEqual(lane['tx_threshold_source'], '62h')
        self.assertIsInstance(lane['tx_power_low_alarm_dbm'], float)

    def test_a_module_without_the_page_says_nothing(self):
        """Absent is not the same as zero: a lane with no per-lane thresholds
        has to keep being judged by the module-wide ones."""
        self._connect('mock_dr8')
        self.assertNotIn('tx_threshold_source', self._lanes()[0])

    def test_one_lane_can_be_judged_differently_from_its_neighbour(self):
        """The whole point of a per-lane page."""
        self._connect()
        before = self._lanes()[0]['tx_power_dbm']
        # Lane 1 alone gets a window this module's output sits well above.
        self._set_lane_threshold(1, hi_dbm=before - 1.0, lo_dbm=before - 3.0)
        lanes = self._lanes()
        self.assertAlmostEqual(lanes[0]['tx_power_high_alarm_dbm'],
                               round(before - 1.0, 2), places=1)
        self.assertNotAlmostEqual(lanes[1]['tx_power_high_alarm_dbm'],
                                  lanes[0]['tx_power_high_alarm_dbm'],
                                  places=1)

    def test_the_thresholds_are_re_read_rather_than_cached_at_connect(self):
        """7.5.3 lets a lane's thresholds move with its programmed output
        power, so a value cached once at connect would go stale silently."""
        self._connect()
        first = self._lanes()[0]['tx_power_high_alarm_dbm']
        self._set_lane_threshold(1, hi_dbm=first + 4.0, lo_dbm=-20.0)
        self.assertAlmostEqual(self._lanes()[0]['tx_power_high_alarm_dbm'],
                               round(first + 4.0, 2), places=1)

    def test_the_colouring_follows_the_lane_not_the_module(self):
        js = self._read_js()
        row = js[js.index('const lim = _powerLimits(lane);'):]
        row = row[:row.index('return `<tr>')]
        self.assertRegex(row, r'txDbm < lim\.TX_LOW',
                         'the Tx cell no longer colours against a threshold')
        body = js[js.index('function _powerLimits('):]
        body = body[:body.index(chr(10) + '}')]
        self.assertRegex(body, r"lane\.tx_threshold_source === '62h'",
                         'the per-lane page is never preferred')
        self.assertRegex(body, r'base\.TX_LOW\s*=\s*num\(lane\.tx_power_low_alarm_dbm',
                         'the lane low alarm never replaces the module-wide one')
        self.assertRegex(body, r'base\.TX_HIGH\s*=\s*num\(lane\.tx_power_high_alarm_dbm',
                         'the lane high alarm never replaces the module-wide one')

    def test_rx_is_left_on_the_module_wide_thresholds(self):
        """Page 62h is Tx output power only; silently reusing a Tx threshold
        for Rx would be worse than having none."""
        js = self._read_js()
        body = js[js.index('function _powerLimits('):]
        body = body[:body.index(chr(10) + '}')]
        inner = body[body.index("=== '62h'"):]
        self.assertNotIn('RX_LOW', inner)
        self.assertNotIn('RX_HIGH', inner)

    def test_the_cell_says_which_rule_it_used(self):
        """Two lanes can be coloured by different rules in the same table, so
        the rule has to be legible per cell."""
        js = self._read_js()
        self.assertRegex(js, r'_TX_SRC_NOTE\[lim\.TX_SRC\]',
                         'the Tx cell tooltip does not name the threshold source')
        for key in ("'62h'", "'02h'", "'fallback'"):
            self.assertIn(key, js[js.index('const _TX_SRC_NOTE'):
                                  js.index('const _TX_SRC_NOTE') + 600],
                          'no wording for source ' + key)

    def _read_js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()


class TestSupervisionRelativeToTheProgrammedPower(CMISTestCase):
    """7.5.3: a media lane can be supervised against thresholds relative to
    its own programmed Tx output power instead of the module-wide absolute
    ones on Page 02h. The capability is advertised at 04h:196.6, the offsets
    live in 12h:216-217, and it is enabled per lane at 12h:128-135.1. The
    decoder for the offsets had been in the codebase unexecuted, because no
    profile advertised the bit that reaches it."""

    def _connect(self, backend='mock_coherent_zr'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _laser(self):
        return self.assertOk(self.client.get('/api/module/laser'))['data']

    def _lanes(self):
        return self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']

    def _window(self, lane):
        l = self._lanes()[lane - 1]
        return (l.get('tx_power_low_alarm_dbm'), l.get('tx_power_high_alarm_dbm'))

    def _post_laser(self, body, code=200):
        rv = self.client.post('/api/module/laser', data=json.dumps(body),
                              content_type='application/json')
        self.assertEqual(rv.status_code, code, rv.data)
        return json.loads(rv.data)

    def test_the_offsets_are_decoded(self):
        """Both are U4 counted from half a dB, so the smallest window a module
        can express is nominal +/- 0.5 dB, not 0."""
        self._connect()
        d = self._laser()
        self.assertIs(d['relative_power_thresholds_supported'], True)
        self.assertEqual(d['relative_power_thresholds'], {
            'hi_alarm_offset_db': 2.0, 'hi_warn_offset_db': 1.5,
            'lo_alarm_offset_db': -2.0, 'lo_warn_offset_db': -1.5})

    def test_a_module_without_the_capability_reports_no_offsets(self):
        """04h:196.6 clear means the bytes need not mean anything, so reading
        them anyway would invent a window out of whatever is there."""
        self._connect('mock_coherent')
        d = self._laser()
        self.assertIs(d['relative_power_thresholds_supported'], False)
        self.assertEqual(d['relative_power_thresholds'], {})

    def test_which_lanes_use_it_is_reported(self):
        self._connect()
        self.assertEqual([l['relative_thresholds_enabled']
                          for l in self._laser()['lanes']],
                         [True] * 4 + [False] * 4)

    def test_an_enabled_lane_is_judged_against_its_own_power(self):
        self._connect()
        self.assertEqual(self._window(1), (-2.0, 2.0))

    def test_a_disabled_lane_keeps_the_module_wide_thresholds(self):
        """The two regimes coexist in one module, which is why the thresholds
        had to become per lane before this could be reported at all."""
        self._connect()
        self.assertNotEqual(self._window(5), self._window(1))
        self.assertEqual(self._window(5), (-8.01, 5.0))

    def test_the_window_follows_the_programmed_power(self):
        """The point of relative supervision: retune the lane and its alarm
        limits move with it, with no host arithmetic."""
        self._connect()
        self._post_laser({'lanes': [{'lane': 1, 'target_power_dbm': -3.0}]})
        self.assertEqual(self._window(1), (-5.0, -1.0))
        self.assertEqual(self._window(2), (-2.0, 2.0),
                         'retuning one lane moved another lane thresholds')

    def test_setting_a_grid_does_not_switch_the_lane_to_absolute(self):
        """12h:128-135 is not only the grid: bit 1 decides which supervision
        regime the lane is under. Rebuilding the whole byte from the grid
        moved the lane back to the module-wide thresholds, so a request to
        change a grid quietly changed which alarm limits applied."""
        self._connect()
        before = self._window(1)
        self._post_laser({'lanes': [{'lane': 1, 'grid_code': 5}]})
        self.assertEqual(self._window(1), before)
        self.assertIs(self._laser()['lanes'][0]['relative_thresholds_enabled'],
                      True)

    def test_enabling_fine_tuning_does_not_switch_it_either(self):
        self._connect()
        before = self._window(1)
        self._post_laser({'lanes': [{'lane': 1, 'grid_code': 5,
                                     'fine_tuning_enabled': True}]})
        self.assertEqual(self._window(1), before)
        self.assertIs(self._laser()['lanes'][0]['fine_tuning_enabled'], True)

    def test_the_grid_itself_still_gets_written(self):
        """Preserving the other bits must not cost the write its purpose."""
        self._connect()
        self._post_laser({'lanes': [{'lane': 2, 'grid_code': 4}]})
        self.assertEqual(self._laser()['lanes'][1]['grid_code'], 4)

    def test_the_capability_bit_gates_the_whole_thing(self):
        """04h:196.6 clear means the module does not do relative supervision at
        all, so an enable bit left set in Page 12h must not be acted on - the
        offsets it would use are not required to mean anything."""
        self._connect()
        self.assertEqual(self._window(1), (-2.0, 2.0))
        poke(0x04, 0xC4, 0x00)
        time.sleep(0.1)
        self.assertEqual(self._window(1), self._window(5),
                         'a lane was supervised relatively by a module that '
                         'does not advertise the capability')

    def test_the_field_is_reported_once(self):
        """It is built in a dict literal beside a dozen others; a second key of
        the same name is not an error, it silently wins - and then the value
        everything else computes is the one that never reaches the caller."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        self.assertEqual(src.count("'relative_thresholds_enabled':"), 1)

    def test_the_panel_distinguishes_the_two_regimes(self):
        js = self._read_js()
        body = js[js.index('function supervisionCell('):]
        body = body[:body.index(chr(10) + '}')]
        self.assertRegex(body, r'!l\.relative_thresholds_enabled',
                         'the cell does not branch on the lane enable bit')
        self.assertIn('Absolute', body)
        self.assertIn('Relative', body)
        self.assertRegex(body, r'!offsets \|\| !Object\.keys\(offsets\)\.length',
                         'a module without the capability is still given a regime')
        self.assertIn('n/a', body)

    def test_the_panel_shows_the_window_the_offsets_produce(self):
        """The number an operator needs is the resulting window, not two
        offsets they have to add to a target power themselves."""
        js = self._read_js()
        body = js[js.index('function supervisionCell('):]
        body = body[:body.index(chr(10) + '}')]
        # The names alone are not enough: they also appear in the tooltip
        # text, so a cell that printed them without ever adding them up would
        # still satisfy an assertion that only looked for the identifiers.
        for edge in ('lo', 'hi'):
            self.assertRegex(
                body,
                r'l\.target_power_dbm \+ offsets\.%s_alarm_offset_db' % edge,
                'the %s edge of the window is not computed from the '
                'programmed power' % edge)

    def test_the_offsets_reach_the_panel(self):
        """The cell is fed from a module-level value filled in when the laser
        data loads; without that it can only ever render n/a."""
        js = self._read_js()
        self.assertRegex(
            js, r'_relThresholds = res\.data\.relative_power_thresholds_supported',
            'the offsets are read and then never handed to the panel')

    def _read_js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_module_serves_only_the_pages_it_advertises(self):
        """Page 0Ch's map is built from what the module actually serves, so a
        page served without being advertised in 01h:174 puts the two
        advertisements at odds - which is what Page 0Ch exists to prevent."""
        self._connect()
        caps = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']
        self.assertIs(caps['page_62h_supported'], True)
        self.assertIs(caps['page_60h_supported'], False)
        self.assertIs(caps['page_61h_supported'], False)
        pages = self.assertOk(
            self.client.get('/api/module/ext54'))['data']['supported_pages']
        self.assertIn(0x62, pages)
        self.assertNotIn(0x60, pages)
        self.assertNotIn(0x61, pages)


class TestTheGridCmis54Added(CMISTestCase):
    """CMIS 5.4 added the 300 GHz grid: advertised at 04h:129.5, grid code 9,
    with its channel range continuing the same table at 04h:166-169. The tool
    read the advertisement and listed "300 GHz" among the supported grids -
    then stopped. The channel range table it drives everything else from
    covered codes 0-8 only, so the grid was never offered in the selector, had
    no range hint, and a channel written to it was the one channel the laser
    handler never validated."""

    def _connect(self, backend='mock_coherent_zr'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _laser(self):
        return self.assertOk(self.client.get('/api/module/laser'))['data']

    def _post(self, body, code=200):
        rv = self.client.post('/api/module/laser', data=json.dumps(body),
                              content_type='application/json')
        self.assertEqual(rv.status_code, code, rv.data)
        return json.loads(rv.data)

    def test_the_grid_carries_a_channel_range_like_every_other(self):
        self._connect()
        d = self._laser()
        self.assertEqual(d['grid_channel_ranges'].get('9'), [-13, 13])
        # Reported on its own as well: callers that predate the grid being
        # part of the range table read it from here, and it is the field the
        # negative case asserts is None.
        self.assertEqual(d['grid_300ghz_range'], [-13, 13])

    def test_the_advertised_grids_and_the_selectable_ones_agree(self):
        """The panel listed a grid the selector could not offer, because the
        selector is built from the range table and the capability line is
        not."""
        self._connect()
        d = self._laser()
        self.assertIn('300 GHz', d['grids_supported'])
        names = {int(k): v for k, v in d['grid_channel_ranges'].items()}
        self.assertIn(9, names)

    def test_a_channel_outside_the_range_is_refused(self):
        self._connect()
        body = self._post({'lanes': [{'lane': 1, 'grid_code': 9,
                                      'channel': 99}]}, 400)
        self.assertIn('300 GHz', body['message'])
        self.assertIn('-13 to 13', body['message'])
        self.assertIn('04h:166-169', body['message'])

    def test_a_channel_inside_the_range_is_accepted(self):
        """A validator that refuses everything would pass the test above."""
        self._connect()
        self._post({'lanes': [{'lane': 1, 'grid_code': 9, 'channel': 5}]})
        lane = self._laser()['lanes'][0]
        self.assertEqual(lane['grid_code'], 9)
        self.assertEqual(lane['channel'], 5)

    def test_the_lane_gets_the_range_hint(self):
        self._connect()
        self._post({'lanes': [{'lane': 1, 'grid_code': 9, 'channel': 0}]})
        self.assertEqual(self._laser()['lanes'][0]['channel_range'], [-13, 13])

    def test_the_extra_bytes_are_not_read_without_the_advertisement(self):
        """04h:166-169 is a 5.4 addition and is not required to mean anything
        on a module that does not advertise the grid - reading it anyway would
        turn whatever is there into an advertised channel range."""
        self._connect()
        poke(0x04, 0x81, 0x80)               # fine tuning only, no 300 GHz
        d = self._laser()
        self.assertIs(d['grid_300ghz_supported'], False)
        self.assertNotIn('9', d['grid_channel_ranges'])
        self.assertIsNone(d['grid_300ghz_range'])

    def test_an_unadvertised_grid_is_not_validated_into_existence(self):
        """With the grid unadvertised there is no range, and the handler must
        not invent one - it has nothing to check against."""
        self._connect()
        poke(0x04, 0x81, 0x80)
        self._post({'lanes': [{'lane': 1, 'grid_code': 9, 'channel': 99}]})

    def test_the_manual_does_not_promise_what_no_module_can_show(self):
        """The manual's CMIS 5.4 table marked both E17 (the 300 GHz grid) and
        E9 (relative power thresholds) as supported while neither had a code
        path anything could reach: no shipped profile advertised either bit,
        so the claim could not be checked by using the tool. It is checked
        here instead, against the profile that now demonstrates both."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'CMIS2Customer',
                            'CMIS\u6a21\u5757\u7ba1\u7406\u5de5\u5177\u64cd\u4f5c\u624b\u518c.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        for feature, marker in (('E17', '04h:129[5]'),
                                ('E9', '04h:196[6]')):
            row = html[html.index('<b>%s</b>' % feature):]
            row = row[:row.index('</tr>')]
            self.assertIn(marker, row)
            self.assertIn('\u5df2\u652f\u6301', row,
                          'the manual no longer claims %s; this test should '
                          'be updated with it' % feature)
        self._connect()
        d = self._laser()
        self.assertIs(d['grid_300ghz_supported'], True)
        self.assertIn('9', d['grid_channel_ranges'])
        self.assertIs(d['relative_power_thresholds_supported'], True)
        self.assertTrue(any(l['relative_thresholds_enabled']
                            for l in d['lanes']))

    def test_the_parser_sizes_itself_to_what_it_is_given(self):
        import cmis_registers as c
        # Two shorts per code, so codes 4 and 5 are shorts 8-11.
        data = struct.pack('>20h', *([0] * 8 + [-1, 1, -2, 2] + [0] * 8))
        self.assertEqual(c.parse_grid_channel_ranges(data[:36]), {4: [-1, 1],
                                                                 5: [-2, 2]})
        self.assertEqual(c.parse_grid_channel_ranges(data[:40]), {4: [-1, 1],
                                                                 5: [-2, 2]})
        # Code 9 lives in the last four bytes, which only a 40-byte read has.
        wide = struct.pack('>20h', *([0] * 18 + [-7, 7]))
        self.assertEqual(c.parse_grid_channel_ranges(wide[:36]), {})
        self.assertEqual(c.parse_grid_channel_ranges(wide[:40]), {9: [-7, 7]})


class TestAShippedProfileDemonstratesTheLaneEscape(CMISTestCase):
    """TestVeryWideModules already drives the 01h:142.1-0 = 11b escape, using
    throwaway profiles registered for the duration of the test. What it cannot
    cover is the manual\'s claim about it - that the interface lays itself out
    to the real lane count - because a fixture nobody can select from the
    dropdown demonstrates nothing to whoever is reading that claim.

    Every other CMIS 5.4 row in the manual\'s feature table is demonstrable on
    a shipped profile. This asserts the same of the one the table leads with.
    """

    def _connect(self, backend):
        return self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))['data']

    def _caps(self):
        return self.assertOk(
            self.client.get('/api/module/capabilities'))['data']

    def _shipped(self):
        import i2c_interface
        import i2c_backends            # noqa: F401 - triggers registration
        return sorted(n for n in i2c_interface._BACKENDS if n.startswith('mock'))

    def test_some_shipped_profile_uses_the_escape(self):
        using = []
        for name in self._shipped():
            self._connect(name)
            if self._caps()['extra_lane_banks'] is not None:
                using.append(name)
        self.assertTrue(using,
                        'no profile a user can select exercises the CMIS 5.4 '
                        'lane count escape, so the manual claim about it '
                        'cannot be checked by using the tool')

    def test_that_profile_reports_three_banks_end_to_end(self):
        self._connect('mock_24lane')
        caps = self._caps()
        self.assertEqual(caps['extra_lane_banks'], 2)
        self.assertEqual(caps['banks_supported'], 3)
        self.assertEqual(caps['max_lanes'], 24)
        for path in ('/api/module/monitoring', '/api/module/datapath',
                     '/api/module/flags'):
            lanes = self.assertOk(self.client.get(path))['data']['lanes']
            self.assertEqual([l['lane'] for l in lanes], list(range(1, 25)),
                             path)

    def test_a_datapath_write_reaches_the_third_bank(self):
        """TestVeryWideModules drives banked writes through the squelch
        endpoint; the DataPath endpoint builds its own per-bank masks and can
        stop short on its own. Masks are one bit per lane, so lane 17 only
        moves if all three bytes are written."""
        self._connect('mock_24lane')
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'tx_disable_mask': [0x00, 0x00, 0x05]}),
            content_type='application/json'))
        lanes = self.assertOk(
            self.client.get('/api/module/datapath'))['data']['lanes']
        self.assertEqual([l['lane'] for l in lanes if not l['tx_enable']],
                         [17, 19])

    def test_a_write_to_one_bank_does_not_move_another_banks_readings(self):
        """The mock models the eight lanes of bank 0 dynamically and keeps the
        built values for the rest. Taking the Tx disable mask from whichever
        bank was written last let a write aimed at lane 17 zero the monitor of
        lane 1 - the DataPath table and the monitoring table then disagreed
        about which lanes were on."""
        self._connect('mock_24lane')
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'tx_disable_mask': [0x00, 0x00, 0x05]}),
            content_type='application/json'))
        lanes = self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']
        self.assertGreater(lanes[0]['tx_power_uw'], 0,
                           'disabling a lane in the third bank silenced the '
                           'first lane of the first')
        # And the bank that is modelled still responds, or the fix would just
        # be ignoring the register.
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'tx_disable_mask': [0x05, 0x00, 0x00]}),
            content_type='application/json'))
        lanes = self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']
        self.assertEqual(lanes[0]['tx_power_uw'], 0)
        self.assertEqual(lanes[2]['tx_power_uw'], 0)

    def test_a_profile_the_legacy_field_can_spell_does_not_use_the_escape(self):
        """01h:174 is not required to exist on a module answering 00b/01b/10b,
        so believing it there reads a bank count out of whatever is at that
        address. The fixtures are all wide; this is the negative case."""
        self._connect('mock_1600g_16lane')
        caps = self._caps()
        self.assertIsNone(caps['extra_lane_banks'])
        self.assertEqual(caps['banks_supported'], 2)

    def test_only_the_escape_value_unlocks_the_extra_register(self):
        """Same 01h:174 either way: the legacy code alone decides whether it
        is believed."""
        import cmis_registers as c
        ext = bytes([0x80, 0xE2])
        spelled = c.parse_supported_pages(0x21, ext)
        escaped = c.parse_supported_pages(0x23, ext)
        self.assertIsNone(spelled['extra_lane_banks'])
        self.assertEqual(spelled['banks_supported'], 2)
        self.assertEqual(escaped['extra_lane_banks'], 2)
        self.assertEqual(escaped['banks_supported'], 3)
        self.assertEqual(escaped['max_lanes'], 24)


class TestWhichDiagnosticsAModuleActuallyReports(CMISTestCase):
    """13h:130 (Table 8-113) is RO and Required, and its bits say which
    DiagnosticsSelector values report anything: bit 0 for the bit error ratio
    (selector 01h), bit 1 for bit and error counting (02h-05h), bits 5 and 4
    for media- and host-side input SNR (06h).

    Every shipped profile advertised 0x00 there - no BER, no counts, no SNR -
    while all three panels showed numbers anyway. A module that does not
    support a selector still answers a read of the Page 14h window, so what
    came back looked like a measurement and was not one."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _get(self, path):
        return self.assertOk(self.client.get(path))['data']

    def _set_130(self, value):
        poke(0x13, 0x82, value)

    def test_each_bit_is_decoded(self):
        import cmis_registers as c
        self.assertEqual(c.parse_diag_reporting_caps(0x00), {
            'media_side_fec': False, 'host_side_fec': False,
            'media_side_snr': False, 'host_side_snr': False,
            'bits_and_errors': False, 'bit_error_ratio': False})
        full = c.parse_diag_reporting_caps(0xF3)
        self.assertTrue(all(full.values()))
        # Each bit on its own, so a mask that happens to cover two fields
        # cannot pass for either.
        for bit, field in ((0x80, 'media_side_fec'), (0x40, 'host_side_fec'),
                           (0x20, 'media_side_snr'), (0x10, 'host_side_snr'),
                           (0x02, 'bits_and_errors'), (0x01, 'bit_error_ratio')):
            d = c.parse_diag_reporting_caps(bit)
            self.assertEqual([k for k, v in d.items() if v], [field],
                             'bit 0x%02X' % bit)

    def test_no_shipped_profile_claims_it_reports_nothing(self):
        """0x00 is a legal answer, but it is not the answer any of these
        modules means - and it is what they all used to give."""
        import i2c_interface
        import i2c_backends            # noqa: F401
        for name in sorted(n for n in i2c_interface._BACKENDS
                           if n.startswith('mock')):
            self._connect(name)
            raw = app_module._read_upper(0x13, 0x82, 1)[0]
            self.assertNotEqual(raw, 0x00,
                                '%s advertises that it reports no diagnostics '
                                'at all' % name)

    def test_a_module_without_snr_returns_none_rather_than_the_window(self):
        self._connect('mock_sr8')
        d = self._get('/api/module/snr')
        self.assertEqual(d['supported'], {'host': False, 'media': False})
        self.assertEqual(d['host_snr_db'], [])
        self.assertEqual(d['media_snr_db'], [])

    def test_a_module_with_snr_still_reports_it(self):
        """A gate that refuses everything would pass the test above."""
        self._connect('mock_dr8')
        d = self._get('/api/module/snr')
        self.assertEqual(d['supported'], {'host': True, 'media': True})
        self.assertEqual(len(d['host_snr_db']), 8)
        self.assertEqual(len(d['media_snr_db']), 8)

    def test_the_two_sides_are_gated_separately(self):
        """Bits 5 and 4 are separate, so a module can measure one side only -
        and a blank row for the other reads as zero unless it is labelled."""
        self._connect('mock_dr8')
        self._set_130(0x13)                     # host SNR, BER, counts
        d = self._get('/api/module/snr')
        self.assertEqual(d['supported'], {'host': True, 'media': False})
        self.assertEqual(len(d['host_snr_db']), 8)
        self.assertEqual(d['media_snr_db'], [])
        self._set_130(0x23)                     # media SNR only
        d = self._get('/api/module/snr')
        self.assertEqual(d['supported'], {'host': False, 'media': True})
        self.assertEqual(d['host_snr_db'], [])
        self.assertEqual(len(d['media_snr_db']), 8)

    def test_the_bit_error_ratio_is_gated(self):
        self._connect('mock_dr8')
        self.assertIs(self._get('/api/module/ber')['supported'], True)
        self._set_130(0x32)                     # everything but bit 0
        d = self._get('/api/module/ber')
        self.assertIs(d['supported'], False)
        self.assertEqual(d['lanes'], [])

    def test_bit_and_error_counting_is_gated(self):
        """Separate bits: the spec expects a module that cannot divide 64 bits
        to report counts instead of a ratio, so one can be present without the
        other."""
        self._connect('mock_dr8')
        self.assertIs(self._get('/api/module/counters')['supported'], True)
        self._set_130(0x31)                     # everything but bit 1
        d = self._get('/api/module/counters')
        self.assertIs(d['supported'], False)
        self.assertEqual(d['lanes'], [])
        # ... and the ratio it does advertise still works.
        self.assertIs(self._get('/api/module/ber')['supported'], True)

    def test_an_unsupported_selector_is_never_written(self):
        """The point is not only to hide the number. Writing a selector the
        module does not implement asks it to do something it has said it
        cannot, and leaves the diagnostic window pointing somewhere the host
        did not choose."""
        self._connect('mock_dr8')
        self._set_130(0x00)
        poke(0x14, 0x80, 0x7E)                  # a value no handler writes
        for path in ('/api/module/snr', '/api/module/ber',
                     '/api/module/counters'):
            self._get(path)
            app_module._set_page(0x14)
            self.assertEqual(
                _state['backend'].read_bytes(0x80, 1)[0], 0x7E,
                '%s wrote a diagnostic selector the module does not support'
                % path)

    def test_the_panels_say_so_rather_than_showing_an_empty_row(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('function diagUnsupportedRow('):]
        body = body[:body.index(chr(10) + '}')]
        self.assertIn('13h:130', body,
                      'the row does not name the register that decided it')
        for what, bit in (("'Input SNR measurement', '5-4'", 'snr'),
                          ("'Bit error ratio results', '0'", 'ber'),
                          ("'Bit and error counting', '1'", 'counters')):
            self.assertIn('diagUnsupportedRow(9, ' + what, js,
                          'the %s panel renders no explanation' % bit)
        self.assertRegex(js, r"res\.data\.supported === false",
                         'a panel decides on something other than the flag')


class TestBankBroadcastChangesWhatAWriteMeans(CMISTestCase):
    """Table 8-11: with BankBroadcastEnable (Lower 0x1A.7) set, a write to a
    control register in any bank of a lane-banked page "is executed as a bank
    broadcast - a virtually simultaneous and atomic WRITE of the same value to
    the same register and the same page, in all supported banks".

    The tool writes a different mask byte to each bank in turn. Under
    broadcast every one of those writes lands everywhere, so the last bank's
    value ends up on every lane - a configuration nobody asked for, reported
    as a success. No profile advertised the control, so this could not happen
    and the write path had never been driven in that state."""

    def _connect(self, backend='mock_24lane'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, path, body, code=200):
        rv = self.client.post(path, data=json.dumps(body),
                              content_type='application/json')
        self.assertEqual(rv.status_code, code, rv.data)
        return json.loads(rv.data)

    def _tx_off(self):
        lanes = self.assertOk(
            self.client.get('/api/module/datapath'))['data']['lanes']
        return [l['lane'] for l in lanes if not l['tx_enable']]

    def _enable(self):
        self._post('/api/module/control', {'bank_broadcast': True})

    def _poke_lower_1a(self, value):
        # Lower memory is visible whatever page is selected, so this needs no
        # page select and cannot desynchronise the API's page cache.
        _state['backend'].write_bytes(0x1A, bytes([value]))

    def test_some_profile_advertises_it(self):
        """Only a lane-banked module has anywhere to broadcast to, so the
        control means nothing on the eight-lane profiles - but with none of
        them advertising it, the whole path was unreachable."""
        import i2c_interface
        import i2c_backends            # noqa: F401
        advertising = []
        for name in sorted(n for n in i2c_interface._BACKENDS
                           if n.startswith('mock')):
            self._connect(name)
            caps = self.assertOk(
                self.client.get('/api/module/capabilities'))['data']
            if caps['controls']['bank_broadcast']:
                advertising.append(name)
        self.assertTrue(advertising,
                        'no profile advertises bank broadcast, so nothing '
                        'exercises what it does to a banked write')

    def test_an_unadvertised_module_refuses_the_control(self):
        self._connect('mock_dr8')
        body = self._post('/api/module/control', {'bank_broadcast': True}, 400)
        self.assertIn('01h:156.7', body['message'])

    def test_the_mock_broadcasts_when_it_is_on(self):
        """Written behind the API's back, so what is tested is the module's
        behaviour and not the guard in front of it."""
        self._connect()
        self._enable()
        import cmis_registers as c
        app_module._set_page(0x10, 1)
        _state['backend'].write_bytes(c.REG_TX_OUTPUT_DIS[1], bytes([0x20]))
        app_module._invalidate_page()
        lanes = self.assertOk(
            self.client.get('/api/module/datapath'))['data']['lanes']
        self.assertEqual([l['lane'] for l in lanes if not l['tx_enable']],
                         [6, 14, 22],
                         'a write to one bank did not reach the others')

    def test_it_does_not_broadcast_when_it_is_off(self):
        self._connect()
        import cmis_registers as c
        app_module._set_page(0x10, 1)
        _state['backend'].write_bytes(c.REG_TX_OUTPUT_DIS[1], bytes([0x20]))
        app_module._invalidate_page()
        self.assertEqual(self._tx_off(), [14])

    def test_a_module_that_does_not_advertise_it_ignores_the_bit(self):
        """The bit is RW everywhere; only the advertisement makes it mean
        something. Setting it on a module without the capability must not
        change how writes land."""
        self._connect('mock_1600g_16lane')
        self._poke_lower_1a(0x80)
        import cmis_registers as c
        app_module._set_page(0x10, 1)
        _state['backend'].write_bytes(c.REG_TX_OUTPUT_DIS[1], bytes([0x20]))
        app_module._invalidate_page()
        self.assertEqual(self._tx_off(), [14])

    def test_differing_masks_are_refused_rather_than_flattened(self):
        self._connect()
        self._post('/api/module/datapath', {'tx_disable_mask': [1, 2, 4]})
        self.assertEqual(self._tx_off(), [1, 10, 19])
        self._enable()
        body = self._post('/api/module/datapath',
                          {'tx_disable_mask': [1, 2, 4]}, 400)
        self.assertIn('Lower 0x1A.7', body['message'])
        self.assertIn('tx_disable_mask', body['message'])
        self.assertEqual(self._tx_off(), [1, 10, 19],
                         'the refused write changed the configuration anyway')

    def test_one_value_for_every_bank_is_still_accepted(self):
        """A guard that refused every banked write would pass the test above
        and make the module unusable while broadcast is on."""
        self._connect()
        self._enable()
        self._post('/api/module/datapath', {'tx_disable_mask': [8, 8, 8]})
        self.assertEqual(self._tx_off(), [4, 12, 20])

    def test_the_squelch_masks_are_guarded_too(self):
        """They are written by their own handler, with their own per-bank
        loop, so the DataPath guard says nothing about them."""
        self._connect()
        self._enable()
        body = self._post('/api/module/squelch',
                          {'tx_squelch_force': [1, 2, 3]}, 400)
        self.assertIn('tx_squelch_force', body['message'])

    def test_an_eight_lane_module_is_never_blocked(self):
        """A single-bank module keeps working with broadcast on.

        One bank cannot diverge from itself: every per-bank list has a single
        element, so the divergence check can never fire whether or not the
        bank count is looked at first. The early exit on bank count is
        therefore an expression of intent rather than a behavioural guard, and
        no test can distinguish the two - what this pins is the outcome, that
        such a module is not refused.

        No shipped eight-lane profile advertises the control, since broadcast
        has nowhere to go on one bank, so the advertisement is forced here.
        """
        self._connect('mock_dr8')
        _state['caps']['controls']['bank_broadcast'] = True
        self._poke_lower_1a(0x80)
        self._post('/api/module/datapath', {'tx_disable_mask': 0x05})
        self.assertEqual(self._tx_off(), [1, 3])

    def test_the_guard_needs_the_advertisement_not_just_the_bit(self):
        """The bit reads back set on any module. If the guard believed it
        without the advertisement it would refuse perfectly legal per-bank
        writes on every banked module whose byte happens to have bit 7 set."""
        self._connect('mock_1600g_16lane')
        caps = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']
        self.assertIs(caps['controls']['bank_broadcast'], False)
        self._poke_lower_1a(0x80)
        self._post('/api/module/datapath', {'tx_disable_mask': [1, 2]})
        self.assertEqual(self._tx_off(), [1, 10])


class TestWhoChoosesTheSquelchMethod(CMISTestCase):
    """01h:155.5-4 (Table 8-51) says who decides how a Tx output is squelched:
    00b none, 01b the module reduces OMA, 10b the module reduces Pav, and only
    11b means "Host controls the method". The bit the host would use is Lower
    0x1A.5 (Table 8-11).

    Every profile reported 01b, and the control was accepted regardless - so
    writing it on a module that squelches by OMA left the Module Control panel
    reporting "Pav", the opposite of what the module does, with the write
    reported as a success."""

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _method(self):
        return self.assertOk(
            self.client.get('/api/module/capabilities'))['data'][
                'controls']['squelch_method_tx']

    def _set(self, value, code=200):
        rv = self.client.post('/api/module/control',
                              data=json.dumps({'squelch_method': value}),
                              content_type='application/json')
        self.assertEqual(rv.status_code, code, rv.data)
        return json.loads(rv.data)

    def _bit(self):
        return self.assertOk(
            self.client.get('/api/module/control'))['data'][
                'squelch_method_select']

    def test_a_module_that_fixes_the_method_refuses_the_control(self):
        self._connect('mock_dr8')
        self.assertEqual(self._method(), 1)
        body = self._set(True, 400)
        self.assertIn('01h:155.5-4', body['message'])
        self.assertIn('OMA', body['message'])
        self.assertIs(self._bit(), False, 'the refused write moved the bit')

    def test_the_other_fixed_method_is_named_correctly(self):
        """A message that always said OMA would pass the test above."""
        self._connect('mock_coherent_zr')
        self.assertEqual(self._method(), 2)
        body = self._set(True, 400)
        self.assertIn('Pav', body['message'])

    def test_a_module_that_offers_the_choice_accepts_it(self):
        """A gate that refused everything would pass both tests above and take
        the control away from the modules that do have it."""
        self._connect('mock_1600g_dr8')
        self.assertEqual(self._method(), 3)
        self._set(True)
        self.assertIs(self._bit(), True)
        self._set(False)
        self.assertIs(self._bit(), False)

    def test_some_profile_offers_the_choice_and_some_do_not(self):
        """With every profile reporting the same code, neither branch of the
        gate was ever taken."""
        import i2c_interface
        import i2c_backends            # noqa: F401
        codes = set()
        for name in sorted(n for n in i2c_interface._BACKENDS
                           if n.startswith('mock')):
            self._connect(name)
            codes.add(self._method())
        self.assertIn(3, codes, 'no profile lets the host choose the method')
        self.assertTrue(codes - {3}, 'every profile lets the host choose')

    def test_the_other_controls_in_the_byte_still_work(self):
        """0x1A packs unrelated controls, and the new refusal must not become
        a refusal of the whole register."""
        self._connect('mock_dr8')
        self.assertOk(self.client.post(
            '/api/module/control', data=json.dumps({'allow_lp_hw': False}),
            content_type='application/json'))
        d = self.assertOk(self.client.get('/api/module/control'))['data']
        self.assertIs(d['low_pwr_allow_request_hw'], False)

    def test_the_panel_reports_the_method_in_force_not_the_bit(self):
        """Where the module fixes the method, the bit is not what decides it,
        so rendering the bit reports the opposite of the truth."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('function squelchMethodText('):]
        body = body[:body.index(chr(10) + '}')]
        # Each code bound to what it renders: asserting the branches merely
        # exist says nothing about whether they say different things, and two
        # of them saying the same thing is the whole defect.
        self.assertRegex(
            body, r"code === 3\) return d\.squelch_method_select \? 'Pav' : 'OMA'",
            'where the host chooses, the readout ignores the bit it chose with')
        self.assertRegex(body, r"code === 1\) return 'OMA",
            'a module that fixes OMA is not reported as OMA')
        self.assertRegex(body, r"code === 2\) return 'Pav",
            'a module that fixes Pav is not reported as Pav')
        self.assertIn('fixed by the module', body,
                      'nothing on screen says the bit is not the decision')
        row = js[js.index("<td>Squelch Method</td>"):]
        row = row[:row.index('</tr>')]
        self.assertIn('squelchMethodText(d)', row,
                      'the row still renders the raw bit')


class TestWritingLaserSettingsToANonTunableModule(CMISTestCase):
    """01h:155.6 TransmitterIsTunable (Table 8-51) is what says Pages 04h and
    12h exist at all. The laser handler wrote grid, channel, fine offset and
    target power to Page 12h without consulting it, so on a module with no
    such page the writes went nowhere, the reads that follow came back as
    zeros, and the caller was told the laser had been retuned.

    Against the mock it was louder than that: indexing a page dict that does
    not exist raised KeyError(18) out of the backend, which reached the caller
    as HTTP 500 with the message "18"."""

    def _connect(self, backend):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, body, code=200):
        rv = self.client.post('/api/module/laser', data=json.dumps(body),
                              content_type='application/json')
        self.assertEqual(rv.status_code, code, rv.data)
        return json.loads(rv.data)

    def test_a_non_tunable_module_is_refused(self):
        self._connect('mock_dr8')
        for field, value in (('target_power_dbm', 0.0),
                             ('fine_offset_ghz', 0.0),
                             ('grid_code', 5),
                             ('channel', 0)):
            body = self._post({'lanes': [{'lane': 1, field: value}]}, 400)
            self.assertIn('01h:155.6', body['message'],
                          'writing %s was not refused by advertisement' % field)

    def test_the_refusal_is_not_an_internal_error(self):
        """500 with a page number for a message tells the operator nothing and
        blames the tool for what the module simply does not have."""
        self._connect('mock_dr8')
        rv = self.client.post(
            '/api/module/laser',
            data=json.dumps({'lanes': [{'lane': 1, 'target_power_dbm': 0.0}]}),
            content_type='application/json')
        self.assertEqual(rv.status_code, 400)
        self.assertNotEqual(json.loads(rv.data)['message'], '18')

    def test_a_tunable_module_still_takes_them(self):
        """A gate that refused everything would pass the tests above and take
        laser tuning away from the one profile that has it."""
        self._connect('mock_coherent_zr')
        self._post({'lanes': [{'lane': 1, 'target_power_dbm': -1.5}]})
        lanes = self.assertOk(
            self.client.get('/api/module/laser'))['data']['lanes']
        self.assertEqual(lanes[0]['target_power_dbm'], -1.5)

    def test_the_advertisement_is_what_decides(self):
        """Not the profile name, and not whether Page 12h happens to answer:
        the module states tunability in one place and that is what is read."""
        self._connect('mock_coherent_zr')
        caps = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']
        self.assertIs(caps['controls']['transmitter_tunable'], True)
        self._connect('mock_dr8')
        caps = self.assertOk(
            self.client.get('/api/module/capabilities'))['data']
        self.assertIs(caps['controls']['transmitter_tunable'], False)

    def test_the_backend_does_not_raise_on_a_page_it_does_not_serve(self):
        """Behind the API, so what is tested is the backend rather than the
        guard now standing in front of it. A real module answers a write to an
        unimplemented page without throwing, and the mock has to as well or it
        turns a module difference into a tool crash."""
        self._connect('mock_dr8')
        import cmis_registers as c
        app_module._set_page(0x12)
        _state['backend'].write_bytes(c.REG_TARGET_PWR_TX[1], bytes([0x00, 0x00]))
        app_module._invalidate_page()
        self.assertOk(self.client.get('/api/module/monitoring'))


class TestATransientDataPathIsNotAFault(CMISTestCase):
    """Figure 6-5 splits the seven DataPath states into steady and transient:
    a transition signal is the exit condition of a steady state, while a
    transient exits on its own completion. Steady are DPDeactivated,
    DPInitialized and DPActivated; transient are DPInit, DPDeinit, DPTxTurnOn
    and DPTxTurnOff.

    The monitoring table coloured by name with two branches - Activated and
    Init - and dropped the other five into the style that means "down". So a
    Data Path passing through DPTxTurnOn on its way up looked exactly like a
    dead one, and DPInitialized, a steady state with the Tx simply not turned
    on, looked like one too. The mock produced four of the seven encodings, so
    three of those renderings had never been seen at all."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, body):
        self.assertOk(self.client.post('/api/module/datapath',
                                       data=json.dumps(body),
                                       content_type='application/json'))

    def _lane1(self):
        return self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes'][0]

    def _watch(self, seconds, step=0.02):
        """Sample lane 1 across a transition and return {state: kind}."""
        seen = {}
        deadline = time.time() + seconds
        while time.time() < deadline:
            l = self._lane1()
            seen[l['datapath_state']] = l['datapath_state_kind']
            time.sleep(step)
        return seen

    def test_the_kinds_come_from_the_state_machine(self):
        import cmis_registers as c
        self.assertEqual(
            {n: c.dp_state_kind(n) for n in c.DP_STATE_NAMES.values()},
            {'Reserved': 'unknown', 'Deactivated': 'down', 'Init': 'transient',
             'Deinit': 'transient', 'Activated': 'up', 'TxTurnOn': 'transient',
             'TxTurnOff': 'transient', 'Initialized': 'holding'})

    def test_a_path_coming_up_passes_through_states_that_are_not_down(self):
        self._connect()
        self._post({'app_select': [1] * 8, 'apply': True})
        seen = self._watch(0.55)
        self.assertIn('TxTurnOn', seen, 'the mock never shows DPTxTurnOn')
        self.assertIn('Initialized', seen,
                      'DPInitialized is not produced by anything')
        for state, kind in seen.items():
            if state != 'Deactivated':
                self.assertNotEqual(kind, 'down',
                                    '%s is reported as a down lane' % state)

    def test_a_path_going_down_passes_through_its_transients(self):
        """Figure 6-5 leaves DPActivated through DPTxTurnOff and DPDeinit;
        snapping to the bottom meant neither encoding existed here."""
        self._connect()
        self._post({'app_select': [1] * 8, 'apply': True})
        time.sleep(1.2)
        self._post({'dp_deinit_mask': 0xFF})
        seen = self._watch(0.25)
        self.assertIn('TxTurnOff', seen)
        self.assertIn('Deinit', seen)
        self.assertEqual(seen['TxTurnOff'], 'transient')
        self.assertEqual(seen['Deinit'], 'transient')

    def test_it_still_settles_where_it_should(self):
        """Walking the state machine must not leave a path parked in a
        transient: those exit on their own completion."""
        self._connect()
        self._post({'app_select': [1] * 8, 'apply': True})
        time.sleep(1.2)
        self.assertEqual(self._lane1()['datapath_state_kind'], 'up')
        self._post({'dp_deinit_mask': 0xFF})
        time.sleep(0.6)
        l = self._lane1()
        self.assertEqual(l['datapath_state'], 'Deactivated')
        self.assertEqual(l['datapath_state_kind'], 'down')

    def test_the_table_colours_by_kind_rather_than_by_two_names(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        row = js[js.index("const tbody = document.getElementById('tbl-monitoring')"):]
        row = row[:row.index('}).join')]
        self.assertIn('lane.datapath_state_kind', row,
                      'the state cell still decides on the name alone')
        # Each kind bound to its class: four kinds sharing one class is the
        # defect, so asserting the names merely appear proves nothing.
        for kind, cls in (('up', 'state-activated'),
                          ('transient', 'state-init'),
                          ('holding', 'state-holding'),
                          ('down', 'state-deactivated')):
            self.assertRegex(row, r"%s: '%s'" % (kind, cls),
                             '%s is not given its own style' % kind)

    def test_the_holding_style_exists_and_is_not_the_down_one(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'style.css')
        with open(path, encoding='utf-8') as f:
            css = f.read()
        holding = re.search(r'\.state-holding\s*\{([^}]*)\}', css)
        down = re.search(r'\.state-deactivated\s*\{([^}]*)\}', css)
        self.assertIsNotNone(holding, 'no style for a holding Data Path')
        self.assertNotEqual(holding.group(1).strip(), down.group(1).strip(),
                            'a holding Data Path is painted as a down one')

    def test_every_state_says_what_kind_it_is(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('function dpStateNote('):]
        body = body[:body.index(chr(10) + '}')]
        for kind in ('up', 'transient', 'holding', 'down'):
            self.assertRegex(body, r'\b%s:' % kind,
                             'no wording for a %s Data Path' % kind)
        self.assertIn('not a fault', body,
                      'nothing tells the operator a held path is deliberate')



class TestWhyTheModuleRefusedTheConfiguration(CMISTestCase):
    """Table 8-101 gives a configuration eight named ways to be refused plus a
    reserved block (9h-Bh, "other validation failures") and a custom one
    (Dh-Fh), with 2h-Bh and Dh-Fh together forming one Negative Result Status.
    The monitoring table decided its colour from the name, so a module
    answering 9h or Eh - a rejection - was painted in the grey that means
    "this lane is simply not in use", while the Apply toast fired at the same
    moment read the same register through config_rejected and called it a
    failure.

    Underneath, the mock's validation step only ever answered ConfigSuccess or
    ConfigRejectedInvalidAppSel, so the reasons an operator most needs to tell
    apart had never reached the screen at all."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _stage(self, sel, apply_=True, release=False):
        body = {'app_select': sel, 'apply': apply_}
        if release:
            body['dp_deinit_mask'] = 0x00
        self.assertOk(self.client.post(
            '/api/module/datapath', data=json.dumps(body),
            content_type='application/json'))

    def _status(self):
        lanes = self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']
        return [(l['config_status'], l['config_status_code'],
                 l['config_rejected']) for l in lanes]

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    # ---- what the module said -------------------------------------------

    def test_the_api_reports_the_code_and_not_only_its_name(self):
        """Three of the sixteen encodings have no name in Table 8-101, so a
        reader that only gets the name cannot tell them apart."""
        self._connect()
        self._stage([1] * 8)
        time.sleep(1.0)
        for name, code, rejected in self._status():
            self.assertEqual((name, code, rejected), ('ConfigSuccess', 1, False))

    def test_the_unnamed_rejections_are_named_as_rejections(self):
        import cmis_registers as c
        for code in (0x9, 0xA, 0xB, 0xD, 0xE, 0xF):
            name = c.config_status_name(code)
            self.assertNotIn('Unknown', name,
                             '%Xh is a rejection, not an unintelligible '
                             'answer' % code)
            self.assertIn('Rejected', name)
            self.assertIn(code, c.CONFIG_STATUS_REJECTED)
        for code in (0xD, 0xE, 0xF):
            self.assertIn('custom', c.config_status_name(code),
                          'Dh-Fh is the block Table 8-101 leaves to the '
                          'vendor, and %Xh is in it' % code)
        for code in (0x9, 0xA, 0xB):
            self.assertIn('reserved', c.config_status_name(code))

    # ---- what the mock can now produce ----------------------------------

    def test_an_application_cannot_be_left_on_too_few_lanes(self):
        """6.2.4.3: each used lane must be part of "a valid Application
        completely allocated on lanes supported for that Application". App 2
        on this profile needs four host lanes; two is not that Application."""
        self._connect()
        deactivated(self.client)          # freeing lanes 3-8 needs the path down
        self._stage([2, 2, 0, 0, 0, 0, 0, 0], release=True)
        time.sleep(1.0)
        got = self._status()
        for lane in (0, 1):
            self.assertEqual(got[lane][:2],
                             ('ConfigRejectedInvalidDataPath', 4),
                             'lane %d ran half an Application' % (lane + 1))
        for lane in range(2, 8):
            self.assertEqual(got[lane][1], 1,
                             'lane %d was deprovisioned, which is always '
                             'legal' % (lane + 1))

    def test_an_application_cannot_start_where_it_is_not_allowed(self):
        """HostLaneAssignmentOptions is a bitmap of the lanes an Application
        may start on. App 2 here advertises lanes 1 and 5; lanes 2-5 is the
        right width on the wrong boundary."""
        self._connect()
        apps = self.assertOk(
            self.client.get('/api/module/applications'))['data']['applications']
        self.assertEqual(apps[1]['host_lane_assign_mask'], 0x11,
                         'this test needs an Application with restricted '
                         'starting lanes')
        deactivated(self.client)
        self._stage([0, 2, 2, 2, 2, 0, 0, 0], release=True)
        time.sleep(1.0)
        got = self._status()
        for lane in range(1, 5):
            self.assertEqual(got[lane][:2],
                             ('ConfigRejectedInvalidDataPath', 4),
                             'lane %d started an Application on a lane the '
                             'module does not offer' % (lane + 1))

    def test_two_instances_side_by_side_are_a_valid_allocation(self):
        """The rule is a whole number of instances, not one: eight lanes of a
        four-lane Application is two Data Paths, and refusing it would be a
        stricter module than CMIS describes."""
        self._connect()
        deactivated(self.client)          # App 1 is eight lanes wide; App 2 is four
        self._stage([2] * 8, release=True)
        time.sleep(1.0)
        for lane, (name, code, rejected) in enumerate(self._status()):
            self.assertEqual((name, code, rejected),
                             ('ConfigSuccess', 1, False),
                             'lane %d refused a valid pair of Data Paths'
                             % (lane + 1))

    def test_triggering_half_a_data_path_is_refused(self):
        """8.14.5: configuration procedures operate on entire Data Paths. The
        tool already rounds its trigger up to whole ones - but nothing had
        ever checked that, because the mock accepted any mask it was given."""
        self._connect()
        self._stage([1] * 8)
        time.sleep(1.0)
        poke(0x10, 0x8F, 0x0F)               # ApplyDPInit, lanes 1-4 only
        time.sleep(1.0)
        got = self._status()
        for lane in range(4):
            self.assertEqual(got[lane][:2],
                             ('ConfigRejectedPartialDataPath', 7),
                             'lane %d accepted a trigger covering half of an '
                             'eight-lane Data Path' % (lane + 1))

    def test_hot_reconfiguration_is_the_exception_that_may_trigger_a_subset(self):
        """"with the exception of hot reconfiguration of SI attributes by
        ApplyImmediate ... where triggers on a subset of lanes are allowed".

        This needs a module that honours ApplyImmediate at all: a
        SteppedConfigOnly module ignores the write outright, and the
        validation step the exemption lives in is never reached."""
        self._connect('mock_coherent')
        cc = self.assertOk(
            self.client.get('/api/module/info'))['data']['config_capabilities']
        self.assertTrue(cc['hot_reconfig'],
                        'a module that ignores ApplyImmediate cannot show '
                        'the exemption either way')
        self._stage([1] * 8)
        time.sleep(1.0)
        poke(0x10, 0x90, 0x0F)               # ApplyImmediate, lanes 1-4 only
        time.sleep(1.0)
        got = self._status()
        for lane in range(4):
            self.assertEqual(got[lane][1], 1,
                             'lane %d refused a hot reconfiguration the spec '
                             'allows on a subset' % (lane + 1))

    def test_the_tool_never_triggers_a_partial_data_path_itself(self):
        """The host-side rounding and the module-side rule now meet: changing
        one Data Path of two must still leave both whole."""
        self._connect()
        reconfigure(self.client, [2] * 8)
        time.sleep(1.0)
        reconfigure(self.client, [2, 2, 2, 2, 0, 0, 0, 0])
        time.sleep(1.0)
        for lane, (name, code, _r) in enumerate(self._status()):
            self.assertEqual(code, 1,
                             'lane %d came back %s from an Apply the tool '
                             'itself composed' % (lane + 1, name))

    def test_every_shipped_profile_boots_into_a_set_it_would_accept(self):
        """A module that refuses its own power-up configuration is not a
        module any host would ship against - and until the validation step
        existed, nothing here could tell."""
        names = self.assertOk(self.client.get('/api/backends'))['data']
        mocks = [b['name'] for b in names if b['name'].startswith('mock')]
        self.assertGreaterEqual(len(mocks), 7)
        for backend in mocks:
            with self.subTest(backend=backend):
                self._connect(backend)
                staged = self.assertOk(
                    self.client.get('/api/module/datapath'))['data']
                self._stage(staged['app_select'])
                time.sleep(1.0)
                for lane, (name, _c, rejected) in enumerate(self._status()):
                    self.assertFalse(rejected,
                                     '%s lane %d boots on a configuration it '
                                     'refuses: %s' % (backend, lane + 1, name))

    # ---- what the interface does with it ---------------------------------

    def test_the_cell_colours_by_the_rejection_the_api_found(self):
        js = self._js()
        row = js[js.index("const cfgStatus = lane.config_status"):]
        row = row[:row.index('return `<tr>')]
        self.assertNotIn('startsWith', row,
                         'the cell still decides from how the name is spelled, '
                         'so a reserved or custom rejection reads as idle')
        self.assertRegex(row, r"lane\.config_rejected \? 'flag-active'",
                         'a rejected lane is not painted as a failure')
        self.assertRegex(row, r"config_status_code === 0x1 \? 'state-activated'")
        self.assertRegex(row, r"config_status_code === 0xC \? 'state-init'")

    def test_the_cell_says_what_the_code_means(self):
        js = self._js()
        self.assertRegex(
            js, r'<td class="\$\{cfgClass\}" title="\$\{esc\(configStatusNote')

    def test_the_register_it_names_is_the_right_one_on_a_banked_module(self):
        """Page 11h repeats per bank, so lane 9 is byte 202 of bank 1. The
        flat arithmetic would have sent the operator to byte 206 of bank 0,
        which is lane 7."""
        js = self._js()
        body = js[js.index('function configStatusNote('):]
        body = body[:body.index(chr(10) + '}')]
        self.assertIn('(lane.lane - 1) % 8', body,
                      'the byte is computed from the flat lane number')
        self.assertRegex(body, r"bank \? ' bank ' \+ bank",
                         'the tooltip never says which bank')

    def test_every_named_rejection_has_a_reason_and_a_remedy(self):
        js = self._js()
        table = js[js.index('const _CFG_WHY = {'):]
        table = table[:table.index(chr(10) + '};')]
        for code in (0x2, 0x3, 0x4, 0x5, 0x6, 0x7, 0x8):
            self.assertRegex(table, r'0x%X: \[' % code,
                             'nothing tells the operator what %Xh means' % code)
        # Two entries each: why the module refused, and what to change.
        self.assertEqual(table.count('0x'), 7)

    def test_a_reason_exists_even_for_the_codes_the_spec_does_not_name(self):
        js = self._js()
        body = js[js.index('function configStatusReason('):]
        body = body[:body.index(chr(10) + '}')]
        self.assertRegex(body, r'code >= 0xD',
                         'the custom block is not told apart from the '
                         'reserved one')
        self.assertIn('vendor', body)

    def test_the_toast_groups_by_reason_rather_than_repeating_it_per_lane(self):
        js = self._js()
        body = js[js.index('  if (rejected.length) {'):]
        body = body[:body.index('  } else {')]
        self.assertNotRegex(
            body, r'`L\$\{l\.lane\}: \$\{l\.config_status\}`',
            'the toast still prints the enum name once per lane')
        self.assertIn('configStatusReason(g.sample)', body,
                      'the toast does not say why the module refused')
        self.assertIn('config_status_code', body,
                      'lanes are grouped by something other than the reason')



class TestALaneCarryingNoApplication(CMISTestCase):
    """6.2.3.2: AppSelCode 0000b "indicates that the lane (together with its
    associated resources) is unused and not part of a Data Path", and "the
    module always reports a DPDeactivated state for unused lanes". 6.2.4.3
    makes writing it mandatory rather than optional - "the host must assign
    AppSel = 0000b to each unused host lane", and narrowing a Data Path means
    "any lane that becomes unused must be marked as such".

    The AppSelect dropdown offered only the Applications the module
    advertises, so the interface could not express the one assignment the
    standard requires - a lane could not be freed from this screen at all.
    Read back, a lane the module reported as unused showed the first
    Application in the list instead, and the mismatch warning added to catch
    exactly that could not fire, because `active && active !== staged`
    short-circuits on the value that needs it most. Pressing Apply from that
    screen then sent a configuration the module refuses.

    Underneath, the mock walked every applied lane up to DPActivated whatever
    was staged on it, so a lane carrying no Application reported a running
    Data Path - green, with a tooltip saying the path was up."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _apply(self, sel):
        # Freeing a lane is allowed only from DPDeactivated (6.2.4.3), so this
        # is the two-step procedure, not a single Apply.
        reconfigure(self.client, sel)
        time.sleep(1.3)

    def _lanes(self):
        return self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    # ---- what the module reports -----------------------------------------

    def test_an_unused_lane_reports_deactivated(self):
        self._connect()
        self._apply([2, 2, 2, 2, 0, 0, 0, 0])
        lanes = self._lanes()
        for l in lanes[:4]:
            self.assertEqual(l['datapath_state'], 'Activated',
                             'lane %d carries an Application and should be up'
                             % l['lane'])
        for l in lanes[4:]:
            self.assertEqual(l['datapath_state'], 'Deactivated',
                             'lane %d carries no Application but reports a '
                             'running Data Path' % l['lane'])
            self.assertEqual(l['datapath_state_kind'], 'down')

    def test_freeing_a_lane_is_not_a_rejection(self):
        """Deprovisioning stays legal - the state is what changes, not the
        result status."""
        self._connect()
        self._apply([2, 2, 2, 2, 0, 0, 0, 0])
        for l in self._lanes():
            self.assertFalse(l['config_rejected'],
                             'lane %d refused to be deprovisioned' % l['lane'])

    def test_a_lane_put_back_to_work_comes_back_up(self):
        """A one-way rule would pass the test above and leave the lane dead."""
        self._connect()
        self._apply([2, 2, 2, 2, 0, 0, 0, 0])
        self.assertEqual({l['datapath_state'] for l in self._lanes()[4:]},
                         {'Deactivated'})
        self._apply([2] * 8)
        for l in self._lanes():
            self.assertEqual(l['datapath_state'], 'Activated',
                             'lane %d never came back' % l['lane'])

    def test_the_active_set_decides_not_the_staged_one(self):
        """The staged set is what the host is asking for. A lane still running
        an Application must not be reported down because someone typed 0 into
        the dropdown and has not pressed Apply."""
        self._connect()
        self._apply([2] * 8)
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [2, 2, 2, 2, 0, 0, 0, 0],
                             'apply': False}),
            content_type='application/json'))
        time.sleep(0.3)
        for l in self._lanes():
            self.assertEqual(l['datapath_state'], 'Activated',
                             'lane %d went down on a staged edit nobody '
                             'applied' % l['lane'])

    # ---- what the interface offers ---------------------------------------

    def test_the_dropdown_can_say_unused(self):
        js = self._js()
        body = js[js.index('const opts = _advertisedApps.length'):]
        body = body[:body.index('const appOpts')]
        self.assertRegex(body, r'<option value="0"',
                         'there is no way to mark a lane unused, so a Data '
                         'Path cannot be narrowed from this screen')
        self.assertRegex(body, r"lane\.app_select === 0 \? 'selected'",
                         'a module already running nothing on a lane would '
                         'not have that entry selected')

    def test_a_lane_running_nothing_is_not_called_app_zero(self):
        js = self._js()
        self.assertIn("const appName = n => n ? 'App ' + n : 'no Application'",
                      js, '0000b is the absence of an Application, not App 0')

    def test_the_mismatch_warning_fires_on_a_freed_lane(self):
        js = self._js()
        self.assertNotIn('const stale = active && active !== lane.app_select',
                         js,
                         'the warning still short-circuits on AppSelCode 0')
        self.assertIn('const stale = active !== lane.app_select', js)

    def test_the_dropdown_and_the_module_agree_about_a_freed_lane(self):
        """End to end: what the API reports for a freed lane has to be a value
        the dropdown can actually show as selected."""
        self._connect()
        self._apply([2, 2, 2, 2, 0, 0, 0, 0])
        dp = self.assertOk(self.client.get('/api/module/datapath'))['data']
        self.assertEqual(dp['app_select'], [2, 2, 2, 2, 0, 0, 0, 0])
        self.assertEqual(dp['active_app_select'], [2, 2, 2, 2, 0, 0, 0, 0])
        apps = self.assertOk(
            self.client.get('/api/module/applications'))['data']['applications']
        self.assertNotIn(0, [a['app_sel'] for a in apps],
                         'AppSelCode 0 is not an Application descriptor, so '
                         'the dropdown has to add it itself')



class TestReleasingADeinitHoldCommissionsWhatWasStaged(CMISTestCase):
    """6.2.4.3 allows a Data Path to change width "only while in the
    DPDeactivated state", so reconfiguring one is a two-step procedure: take
    the path down, then set the new Application and release the hold. The
    second step is a single Apply carrying both, and the order the tool wrote
    those registers in decided what the module commissioned.

    DPDeinit went down first. Releasing the hold restarts the Data Path, and
    the module commissions whatever the Staged Control Set holds at that
    moment - which was still the previous Application, because AppSel was
    written afterwards. The path came back up on the old configuration and
    ConfigStatus read ConfigSuccess for it, so the only sequence the standard
    allows for a width change was the one that silently did nothing."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, body):
        return self.assertOk(self.client.post(
            '/api/module/datapath', data=json.dumps(body),
            content_type='application/json'))

    def _active(self):
        return self.assertOk(
            self.client.get('/api/module/datapath'))['data']['active_app_select']

    def _states(self):
        return [l['datapath_state'] for l in self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']]

    def test_the_new_application_is_what_comes_back_up(self):
        self._connect()
        self.assertEqual(self._active(), [1] * 8)
        self._post({'dp_deinit_mask': 0xFF, 'apply': True})
        time.sleep(0.6)
        self.assertEqual(set(self._states()), {'Deactivated'},
                         'the Data Path never came down')
        # One Apply carrying both the new Application and the release.
        self._post({'app_select': [2] * 8, 'dp_deinit_mask': 0x00,
                    'apply': True})
        time.sleep(1.6)
        self.assertEqual(self._active(), [2] * 8,
                         'the Data Path came back up on the Application it '
                         'was running before')
        self.assertEqual(set(self._states()), {'Activated'})

    def test_the_staged_set_is_written_before_the_hold_is_released(self):
        """Pinning the order, because the symptom is silent: the module
        reports ConfigSuccess either way, for whichever configuration it had
        when the path restarted."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        body = src[src.index('        for bank in range(banks):\n'
                             '            _set_page(0x10, bank)'):]
        body = body[:body.index('        applied = []')]
        self.assertLess(body.index('REG_APP_SELECT'), body.index('REG_DP_DEINIT'),
                        'DPDeinit is written before the Staged Control Set, so '
                        'releasing a hold restarts the path on the old '
                        'Application')

    def test_the_polarity_half_of_the_staged_set_goes_first_too(self):
        """Everything the restart commissions has to be in place before the
        release, not just the Application."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        body = src[src.index('        for bank in range(banks):\n'
                             '            _set_page(0x10, bank)'):]
        body = body[:body.index('        applied = []')]
        for reg in ('REG_TX_POL_FLIP', 'REG_RX_POL_FLIP'):
            self.assertLess(body.index(reg), body.index('REG_DP_DEINIT'),
                            '%s is written after the hold is released' % reg)

    def test_a_release_that_changes_nothing_still_brings_the_path_back(self):
        """The reorder must not cost the plain case: hold, then release with
        the same Application staged."""
        self._connect()
        self._post({'dp_deinit_mask': 0xFF, 'apply': True})
        time.sleep(0.6)
        self._post({'dp_deinit_mask': 0x00, 'apply': True})
        time.sleep(1.6)
        self.assertEqual(set(self._states()), {'Activated'},
                         'a released Data Path never came back')
        self.assertEqual(self._active(), [1] * 8)

    def test_taking_the_path_down_still_works_from_the_same_request(self):
        """The other direction: staging first must not stop DPDeinit taking
        effect when it is set rather than released.

        This needs a module carrying two Data Paths. On one whose Application
        spans all eight host lanes, holding lanes 5-8 asks for the whole path
        (Table 8-78), and every lane going down is the right answer."""
        self._connect('mock_fr4x2')
        self._post({'app_select': [1, 1, 1, 1, 2, 2, 2, 2],
                    'dp_deinit_mask': 0xF0, 'apply': True})
        time.sleep(0.6)
        states = self._states()
        self.assertEqual(states[4:], ['Deactivated'] * 4,
                         'the lanes named in DPDeinit stayed up')
        self.assertEqual(states[:4], ['Activated'] * 4,
                         'lanes nobody asked to hold went down')



class TestChangingAPathThatIsStillRunning(CMISTestCase):
    """6.2.4.3 states the precondition twice: a lane in use "can be
    reconfigured to become unused only when the Data Path is in the
    DPDeactivated state", and "the host can change the width of a Data Path
    only while in the DPDeactivated state ... the host must always transition
    an existing Data Path to DPDeactivated before selecting an Application
    with a different lane count". Table 8-101 gives that refusal its own code:
    6h ConfigRejectedLanesInUse, "some lanes not in DPDeactivated".

    The mock never checked, so the tool reported ConfigSuccess for changes a
    real module refuses, and 6h had never been produced. Every shipped profile
    advertises Applications of differing widths, which makes this the rule an
    operator meets first - on these modules any change of Application needs
    the Data Path stopped, and nothing said so."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _post(self, body):
        return self.assertOk(self.client.post(
            '/api/module/datapath', data=json.dumps(body),
            content_type='application/json'))

    def _status(self):
        return [(l['config_status'], l['config_status_code'])
                for l in self.assertOk(
                    self.client.get('/api/module/monitoring'))['data']['lanes']]

    def _active(self):
        return self.assertOk(
            self.client.get('/api/module/datapath'))['data']['active_app_select']

    # ---- the two changes the rule is about -------------------------------

    def test_changing_the_width_of_a_running_path_is_refused(self):
        self._connect()
        self.assertEqual(self._active(), [1] * 8, 'App 1 is the eight-lane one')
        self._post({'app_select': [2] * 8, 'apply': True})
        time.sleep(1.2)
        for lane, (name, code) in enumerate(self._status()):
            self.assertEqual((name, code), ('ConfigRejectedLanesInUse', 6),
                             'lane %d changed width on a running Data Path'
                             % (lane + 1))

    def test_freeing_a_lane_that_is_in_use_is_refused(self):
        """Isolated from the width rule: the module runs two four-lane Data
        Paths, and only the second is asked to give its lanes up. Freeing
        lanes 5-8 of the eight-lane Application instead would leave lanes 1-4
        holding half of it, which is ConfigRejectedInvalidDataPath before the
        state is ever considered."""
        self._connect()
        reconfigure(self.client, [2] * 8)
        time.sleep(1.3)
        self._post({'app_select': [2, 2, 2, 2, 0, 0, 0, 0], 'apply': True})
        time.sleep(1.3)
        got = self._status()
        for lane in range(4, 8):
            self.assertEqual(got[lane], ('ConfigRejectedLanesInUse', 6),
                             'lane %d was freed while its Data Path was up, '
                             'and the module said %s' % (lane + 1, got[lane][0]))
        for lane in range(4):
            self.assertEqual(got[lane][1], 1,
                             'lane %d was refused for a change asked of the '
                             'other Data Path' % (lane + 1))

    def test_a_refused_change_leaves_the_module_running_what_it_had(self):
        """Validation failing means execution is skipped, so the refusal must
        cost nothing."""
        self._connect()
        self._post({'app_select': [2] * 8, 'apply': True})
        time.sleep(1.2)
        self.assertEqual(self._active(), [1] * 8,
                         'a refused change was commissioned anyway')
        self.assertEqual({l['datapath_state'] for l in self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']},
            {'Activated'}, 'a refused change restarted the Data Path')

    # ---- and what is still allowed ---------------------------------------

    def test_the_same_change_is_accepted_once_the_path_is_down(self):
        """A rule that refused everything would pass the tests above."""
        self._connect()
        deactivated(self.client)
        self._post({'app_select': [2] * 8, 'dp_deinit_mask': 0x00,
                    'apply': True})
        time.sleep(1.6)
        for lane, (name, code) in enumerate(self._status()):
            self.assertEqual(code, 1,
                             'lane %d refused a change from DPDeactivated: %s'
                             % (lane + 1, name))
        self.assertEqual(self._active(), [2] * 8)

    def test_re_commissioning_what_is_already_running_is_still_allowed(self):
        """Table 6-3 allows ApplyDPInit on a Data Path in DPActivated - what
        it does not allow is these two particular changes. Refusing a plain
        re-commission would take that away."""
        self._connect()
        self._post({'app_select': [1] * 8, 'apply': True})
        time.sleep(1.2)
        for lane, (name, code) in enumerate(self._status()):
            self.assertEqual(code, 1,
                             'lane %d refused to be re-commissioned on the '
                             'Application it is already running: %s'
                             % (lane + 1, name))

    def test_provisioning_a_lane_that_is_free_needs_no_stop(self):
        """The rule is about lanes in use. A lane carrying no Application is
        already deactivated, so bringing it into a Data Path is not the case
        6.2.4.3 gates."""
        self._connect()
        reconfigure(self.client, [2, 2, 2, 2, 0, 0, 0, 0])
        time.sleep(1.3)
        self.assertEqual(self._active(), [2, 2, 2, 2, 0, 0, 0, 0])
        self._post({'app_select': [2] * 8, 'apply': True})
        time.sleep(1.3)
        for lane, (name, code) in enumerate(self._status()):
            self.assertEqual(code, 1,
                             'lane %d refused to be brought into service from '
                             'free: %s' % (lane + 1, name))
        self.assertEqual(self._active(), [2] * 8)

    def test_the_staged_set_is_judged_before_the_state(self):
        """An Application the module never advertised is wrong whatever the
        Data Path is doing, and naming the state instead would send the
        operator to the wrong control."""
        self._connect()
        self._post({'app_select': [14] * 8, 'apply': True})
        time.sleep(1.2)
        for lane, (name, code) in enumerate(self._status()):
            self.assertEqual((name, code),
                             ('ConfigRejectedInvalidAppSel', 3),
                             'lane %d was told about its state when the '
                             'AppSel code was the problem' % (lane + 1))

    # ---- what the interface says about it --------------------------------

    def test_the_reason_names_the_control_that_fixes_it(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        table = js[js.index('const _CFG_WHY = {'):]
        table = table[:table.index(chr(10) + '};')]
        entry = table[table.index('0x6:'):table.index('0x7:')]
        self.assertIn('DP Deinit', entry,
                      'the remedy does not name the control that performs it')
        self.assertIn('Apply', entry,
                      'nothing says the change takes two Applies')



class TestSignalIntegrityTheModuleCannotCarryOut(CMISTestCase):
    """Table 8-53 publishes a ceiling for each host-controlled signal
    integrity target - TxInputEqMax at 01h:153.3-0, RxOutputEqPostCursorMax
    and RxOutputEqPreCursorMax in the two nibbles of 01h:154 - and the set of
    Rx output amplitude codes that exist in 01h:153.7-4. Asking for more than
    the module advertises is ConfigRejectedInvalidSI (5h), the last named
    rejection in Table 8-101 that nothing here could produce.

    Two reasons it could not. The mock never looked at the signal integrity
    half of the Staged Control Set at all, so an Apply committed targets the
    module had never said it could reach. And every shipped profile advertised
    the largest value that fits in each field - 7, 7, 7 and all four amplitude
    codes - so no module with real restrictions had ever been modelled, and
    the pre- and post-cursor maxima, being equal, could have been read from
    each other's nibble with nothing to notice."""

    def _connect(self, backend='mock_fr4x2'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _apply(self, sel=None):
        body = {'app_select': sel or [1, 1, 1, 1, 2, 2, 2, 2], 'apply': True}
        self.assertOk(self.client.post(
            '/api/module/datapath', data=json.dumps(body),
            content_type='application/json'))
        time.sleep(1.2)

    def _status(self):
        return [l['config_status'] for l in self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']]

    def _write(self, page, addr, data):
        self.assertOk(self.client.post(
            '/api/register/write',
            data=json.dumps({'page': page, 'address': addr, 'data': data}),
            content_type='application/json'))

    def _adv(self):
        return self.assertOk(
            self.client.get('/api/module/datapath'))['data']['si_advertised']

    # ---- a profile whose limits are not simply "the largest that fits" ----

    def test_one_profile_advertises_real_restrictions(self):
        self._connect()
        adv = self._adv()
        self.assertEqual(adv['tx_input_eq_max'], 3)
        self.assertEqual(adv['rx_output_eq_pre_cursor_max'], 5)
        self.assertEqual(adv['rx_output_eq_post_cursor_max'], 2)
        self.assertEqual(adv['rx_output_levels'], [0, 1])

    def test_the_two_cursor_maxima_are_told_apart(self):
        """Equal on every other profile, so this is the only place a swap of
        the two nibbles of 01h:154 would show up."""
        self._connect()
        adv = self._adv()
        self.assertNotEqual(adv['rx_output_eq_pre_cursor_max'],
                            adv['rx_output_eq_post_cursor_max'],
                            'the pre- and post-cursor maxima are equal again, '
                            'so reading either from the wrong nibble is free')

    def test_the_profile_stages_nothing_it_would_refuse(self):
        """A module advertising codes 0-1 while staging code 2 would reject
        its own power-up configuration."""
        self._connect()
        self._apply()
        for lane, name in enumerate(self._status()):
            self.assertEqual(name, 'ConfigSuccess',
                             'lane %d boots on signal integrity settings this '
                             'module refuses: %s' % (lane + 1, name))

    # ---- the refusal itself ----------------------------------------------

    def test_a_target_above_the_advertised_maximum_is_refused(self):
        self._connect()
        # 10h:162 carries the Rx pre-cursor target for lanes 1 and 2; this
        # module advertises 5 as the most it can reach.
        self._write(0x10, 162, [0x06])
        self._apply()
        got = self._status()
        for lane in range(4):
            self.assertEqual(got[lane], 'ConfigRejectedInvalidSI',
                             'lane %d accepted a pre-cursor target above the '
                             'maximum the module advertises' % (lane + 1))

    def test_the_maximum_itself_is_accepted(self):
        """The ceiling is a value the module reaches, not one it refuses."""
        self._connect()
        self._write(0x10, 162, [0x05])
        self._apply()
        self.assertEqual(set(self._status()), {'ConfigSuccess'})

    def test_an_amplitude_code_the_module_does_not_have_is_refused(self):
        self._connect()
        self._write(0x10, 170, [0x22])        # code 2 on lanes 1 and 2
        self._apply()
        got = self._status()
        for lane in range(4):
            self.assertEqual(got[lane], 'ConfigRejectedInvalidSI',
                             'lane %d accepted an amplitude code the module '
                             'does not list' % (lane + 1))

    def test_only_the_data_path_carrying_it_is_refused(self):
        """One value is reported on every lane of a Data Path, and on no
        other - the second port here is untouched."""
        self._connect()
        self._write(0x10, 162, [0x06])        # lanes 1-2, so the first port
        self._apply()
        got = self._status()
        self.assertEqual(set(got[:4]), {'ConfigRejectedInvalidSI'})
        self.assertEqual(set(got[4:]), {'ConfigSuccess'},
                         'the other Data Path was refused for a setting on '
                         'lanes it does not own')

    def test_a_control_the_module_does_not_advertise_is_not_judged(self):
        """A target register a module never announced holds nothing it has to
        honour, so a value in it cannot make the configuration invalid."""
        self._connect('mock_sr8')
        adv = self._adv()
        self.assertFalse(adv['rx_output_amplitude_control'],
                         'this test needs a module without amplitude control')
        self._write(0x10, 170, [0xFF])
        self._apply([1] * 8)
        self.assertEqual(set(self._status()), {'ConfigSuccess'},
                         'a register the module does not implement was held '
                         'against the configuration')

    def test_a_lane_being_freed_is_not_judged_on_settings_it_gives_up(self):
        """6.2.3.2: an unused lane "is unused and not part of a Data Path",
        and what its other registers hold "should be ignored". Holding a
        signal integrity value against a lane on its way out would refuse a
        deprovision for a setting that is about to stop mattering."""
        self._connect()
        # 10h:172 carries the amplitude code for lanes 5 and 6; code 2 is one
        # this module does not have.
        self._write(0x10, 172, [0x22])
        deactivated(self.client)
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [1, 1, 1, 1, 0, 0, 0, 0],
                             'dp_deinit_mask': 0x00, 'apply': True}),
            content_type='application/json'))
        time.sleep(1.4)
        got = self._status()
        for lane in range(4, 8):
            self.assertEqual(got[lane], 'ConfigSuccess',
                             'lane %d was refused for a signal integrity '
                             'setting on a lane it no longer carries: %s'
                             % (lane + 1, got[lane]))

    # ---- and what the table shows ----------------------------------------

    def test_a_value_over_its_limit_is_marked(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('const siLimit = (key) =>'):]
        body = body[:body.index('const lanes =')]
        self.assertRegex(body, r'max !== undefined && v > max',
                         'the cell never compares its value to the limit')
        self.assertIn('flag-active', body,
                      'an out-of-range target is not marked as a fault')
        self.assertIn('rx_output_levels.includes(v)', body,
                      'an amplitude code the module does not have is not '
                      'marked')
        for key in ('tx_input_eq_max', 'rx_output_eq_pre_cursor_max',
                    'rx_output_eq_post_cursor_max'):
            self.assertIn(key, body, '%s is never consulted' % key)

    def test_the_mark_says_what_the_module_would_answer(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('const siLimit = (key) =>'):]
        body = body[:body.index('const lanes =')]
        self.assertEqual(body.count('ConfigRejectedInvalidSI'), 2,
                         'the tooltip does not name the rejection this earns')



class TestTheActiveSetAheadOfTheHardware(CMISTestCase):
    """Table 8-106 makes DPInitPending (11h:235) a Required register, and
    8.14.7 says what it is for: after a Provision triggered by ApplyDPInit has
    copied a Staged Control Set into the Active Control Set, the bit stands
    until the transit through DPInit commits it, because until then "the
    Active Control Set content may deviate from the actual hardware
    configuration".

    This page reads 11h as "what the module is running" - that is the whole
    point of the running-App marker under the dropdown - and the one register
    that says so may be out of date was never read at all.

    It matters most on the modules Table 6-4 describes. Where neither
    intervention-free procedure is advertised, ApplyDPInit is accepted in any
    Data Path state but only provisions: no DPSM transition, so the Active
    Control Set moves and the hardware does not. The mock cycled the Data Path
    whatever Lower 02h said, so that whole class of module had never been
    modelled, and regular_reconfig - parsed and displayed since it was added -
    was consulted by nothing."""

    LANES = 8

    @classmethod
    def setUpClass(cls):
        import copy
        import i2c_interface
        from i2c_backends import mock
        # Two Applications of equal width that may start on the same lane, so
        # that changing between them is a reconfiguration rather than a change
        # of width. No shipped profile has that shape - every one pairs an
        # 8-lane Application with a 4-lane one - so regular reconfiguration
        # could not otherwise be driven at all.
        base = copy.deepcopy(mock._FR4X2_800G)
        apps = list(base['app_descriptors'])
        apps[1] = apps[1][:3] + (0x11,)
        base['app_descriptors'] = apps
        # Lower 02h belongs in the profile rather than being poked in: the
        # API reads the capabilities once, at connect, so a module poked
        # afterwards behaves one way and is described another.
        for name, caps_02, label in (
                ('test_reconfig', 0x00, 'both procedures'),
                ('test_stepped', 0x40, 'neither procedure'),
                # Lower 02h can advertise the hot procedure without the
                # regular one, and there the two halves of the rule pull
                # apart: ApplyDPInit only provisions, ApplyImmediate commits.
                ('test_hotonly', 0x42, 'hot only')):
            profile = copy.deepcopy(base)
            profile['config_caps_02'] = caps_02
            profile['display'] = 'same-width fixture, %s' % label
            i2c_interface._BACKENDS[name] = type(
                'Fixture_' + name, (mock.MockBackend,), {'PROFILE': profile})

    @classmethod
    def tearDownClass(cls):
        import i2c_interface
        for name in ('test_reconfig', 'test_stepped', 'test_hotonly'):
            i2c_interface._BACKENDS.pop(name, None)

    def _connect(self, backend='test_reconfig'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _apply(self, sel):
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': sel, 'apply': True}),
            content_type='application/json'))

    def _dp(self):
        return self.assertOk(
            self.client.get('/api/module/datapath'))['data']

    def _pending(self):
        return [l['dp_init_pending'] for l in self._dp()['lanes']]

    def _states(self):
        return [l['datapath_state'] for l in self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']]

    def _caps(self):
        return self.assertOk(
            self.client.get('/api/module/info'))['data']['config_capabilities']

    # ---- the fixture earns its keep ---------------------------------------

    def test_no_shipped_profile_can_change_application_without_changing_width(self):
        """Which is why this fixture exists. If a shipped profile ever grows a
        same-width pair, this test says so and the fixture can go."""
        from i2c_backends import mock
        for name in dir(mock):
            profile = getattr(mock, name)
            if not (name.startswith('_') and isinstance(profile, dict)
                    and 'app_descriptors' in profile):
                continue
            shapes = [((d[2] >> 4) & 0x0F, d[3])
                      for d in profile['app_descriptors']]
            for i in range(len(shapes)):
                for j in range(i + 1, len(shapes)):
                    self.assertFalse(
                        shapes[i][0] == shapes[j][0]
                        and shapes[i][1] & shapes[j][1],
                        '%s can express a same-width reconfiguration' % name)

    # ---- Table 6-3: copy and cycle ----------------------------------------

    def test_a_reconfiguration_cycles_the_path_and_clears_the_flag(self):
        self._connect()
        self.assertTrue(self._caps()['regular_reconfig'])
        self.assertEqual(self._pending(), [False] * self.LANES)
        self._apply([2] * self.LANES)
        time.sleep(1.6)
        self.assertEqual(self._dp()['active_app_select'], [2] * self.LANES)
        self.assertEqual(set(self._states()), {'Activated'})
        self.assertEqual(self._pending(), [False] * self.LANES,
                         'the Data Path went through DPInit, so nothing is '
                         'still pending')

    def test_the_flag_stands_between_the_provision_and_the_transit(self):
        """Set by the Provision, cleared by DPInit - so there is a window in
        which the Active Control Set is ahead of the hardware even here."""
        self._connect()
        self._apply([2] * self.LANES)
        self.assertTrue(any(self._pending()),
                        'the Provision never raised DPInitPending')

    # ---- Table 6-4: copy only ---------------------------------------------

    def test_a_module_with_neither_procedure_provisions_without_commissioning(self):
        self._connect('test_stepped')
        cc = self._caps()
        self.assertFalse(cc['regular_reconfig'])
        self.assertFalse(cc['hot_reconfig'])
        self._apply([2] * self.LANES)
        time.sleep(1.6)
        self.assertEqual(self._dp()['active_app_select'], [2] * self.LANES,
                         'the Provision did not copy the staged set across')
        self.assertEqual(set(self._states()), {'Activated'},
                         'the Data Path cycled on a module that advertises '
                         'neither intervention-free procedure')
        # Only the first port changed - lanes 5-8 were already on App 2 - and
        # Apply touches only the Data Paths that changed, so only the lanes
        # actually provisioned carry the flag.
        self.assertEqual(self._pending(), [True] * 4 + [False] * 4,
                         'nothing tells the host the hardware is still '
                         'running the previous configuration')

    def test_taking_the_path_down_and_back_commissions_it(self):
        """The pending condition is not a dead end: the stepwise procedure is
        what commits it, and that clears the flag."""
        self._connect('test_stepped')
        self._apply([2] * self.LANES)
        time.sleep(1.2)
        self.assertEqual(self._pending(), [True] * 4 + [False] * 4)
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'dp_deinit_mask': 0xFF, 'apply': True}),
            content_type='application/json'))
        time.sleep(0.7)
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'dp_deinit_mask': 0x00, 'apply': True}),
            content_type='application/json'))
        time.sleep(1.6)
        self.assertEqual(set(self._states()), {'Activated'})
        self.assertEqual(self._pending(), [False] * self.LANES,
                         'the path went through DPInit and the flag still '
                         'says a commissioning is pending')

    def test_hot_reconfiguration_leaves_nothing_pending(self):
        """"DPInitPending bits are not set in response to ApplyImmediate
        triggers" - it commits to hardware itself."""
        self._connect()                       # advertises both procedures
        self.assertTrue(self._caps()['hot_reconfig'])
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [2] * self.LANES,
                             'apply_immediate': True}),
            content_type='application/json'))
        time.sleep(1.0)
        self.assertEqual(self._dp()['active_app_select'], [2] * self.LANES)
        self.assertEqual(self._pending(), [False] * self.LANES,
                         'a hot reconfiguration left a commissioning pending')

    def test_hot_reconfiguration_commits_even_where_the_regular_one_is_not_offered(self):
        """A module can advertise the hot procedure and not the regular one.
        ApplyDPInit there only provisions, but ApplyImmediate still "copies
        and commits" - so the pending condition must not follow the regular
        advertisement alone."""
        self._connect('test_hotonly')
        cc = self._caps()
        self.assertTrue(cc['hot_reconfig'])
        self.assertFalse(cc['regular_reconfig'])
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [2] * self.LANES,
                             'apply_immediate': True}),
            content_type='application/json'))
        time.sleep(1.0)
        self.assertEqual(self._dp()['active_app_select'], [2] * self.LANES)
        self.assertEqual(set(self._states()), {'Activated'},
                         'a hot reconfiguration cycled the Data Path')
        self.assertEqual(self._pending(), [False] * self.LANES,
                         'a hot reconfiguration left a commissioning pending '
                         'on a module that does not offer the regular one')

    # ---- what the page says about it --------------------------------------

    def test_the_row_says_the_hardware_may_not_have_caught_up(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        body = js[js.index('const pendingNote = lane.dp_init_pending'):]
        body = body[:body.index('const appOpts') if 'const appOpts' in body
                    else body.index('return `<tr>')]
        self.assertIn('appsel-pending', body)
        self.assertIn('11h:235', body,
                      'the note never names the register it comes from')
        self.assertIn('DP Deinit', body,
                      'nothing says how to commission what was provisioned')

    def test_the_marker_is_rendered_next_to_the_dropdown(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            js = f.read()
        self.assertIn('${stale}${pendingNote}', js,
                      'the marker is built and never placed in the row')

    def test_a_pending_commissioning_is_not_styled_as_a_fault(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'style.css')
        with open(path, encoding='utf-8') as f:
            css = f.read()
        pending = re.search(r'\.appsel-pending\s*\{([^}]*)\}', css)
        mismatch = re.search(r'\.appsel-mismatch\s*\{([^}]*)\}', css)
        self.assertIsNotNone(pending, 'no style for a pending commissioning')
        self.assertNotEqual(pending.group(1).strip(),
                            mismatch.group(1).strip(),
                            'a provisioned configuration waiting to be '
                            'commissioned is painted as a refused one')



class TestWhichSignalIntegritySettingsAreInForce(CMISTestCase):
    """Tables 8-104 and 8-105 are the Active Control Set's half of the signal
    integrity settings - one Page 11h register for each staged one on Page
    10h - and they say where the values came from:

      "If the ExplicitControl bit for a lane was set ... the contents of the
      registers for that lane ... originate from corresponding registers in
      that Staged Control Set. If the ExplicitControl bit ... was cleared,
      the contents ... were determined by the module according to the
      selected Application."

    pack_appselect writes ExplicitControl clear on every lane - its own
    docstring says "Application-dependent SI settings" - so on every Apply
    this tool makes, the module picks these itself. The Signal Integrity
    table showed the staged numbers and told the reader "Apply on the
    DataPath table commits this set as well", which is true only in the case
    the tool never uses. 11h:214-234 was never read, and the mock never
    filled it in, so nothing here could tell the two apart."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _apply(self):
        dp = self.assertOk(
            self.client.get('/api/module/datapath'))['data']
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': dp['app_select'], 'apply': True}),
            content_type='application/json'))
        time.sleep(1.4)
        return self.assertOk(
            self.client.get('/api/module/datapath'))['data']

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    # ---- the module says what it provisioned ------------------------------

    def test_the_active_half_is_reported(self):
        self._connect()
        d = self._apply()
        self.assertIn('signal_integrity_active', d)
        self.assertEqual(sorted(d['signal_integrity_active']),
                         sorted(d['signal_integrity']),
                         'the two halves describe different controls')
        for key, values in d['signal_integrity_active'].items():
            self.assertEqual(len(values), 8, '%s is not per-lane' % key)

    def test_the_module_provisions_something_of_its_own(self):
        """With ExplicitControl clear the staged values are a request. A mock
        that echoed them back would hide the whole distinction."""
        self._connect()
        d = self._apply()
        differ = [k for k in d['signal_integrity']
                  if d['signal_integrity'][k] != d['signal_integrity_active'][k]]
        self.assertTrue(differ,
                        'the module provisioned exactly what was staged on '
                        'every control, so nothing here exercises the case '
                        'the spec describes')

    def test_setting_explicit_control_takes_the_staged_values(self):
        """The other branch of the same sentence: with the bit set, the
        Active Control Set is copied from the Staged one."""
        self._connect()
        self._apply()
        before = self.assertOk(self.client.get(
            '/api/module/datapath'))['data']['signal_integrity_active']
        self.assertNotEqual(before['rx_output_amplitude'],
                            [2] * 8, 'this test needs them to start apart')
        # DPConfigLane bit 0 is ExplicitControl. It has to be set behind the
        # API and applied the same way: every datapath POST rebuilds these
        # bytes through pack_appselect, which writes the bit clear, so there
        # is no way to reach this branch through the tool at all - which is
        # why the footnote it justified was wrong for every Apply the tool
        # makes, not merely for the default.
        for lane in range(8):
            poke(0x10, 145 + lane, 0x11)
        poke(0x10, 0x8F, 0xFF)                # ApplyDPInit, all lanes
        time.sleep(1.6)
        d = self.assertOk(self.client.get('/api/module/datapath'))['data']
        self.assertEqual(d['signal_integrity_active']['rx_output_amplitude'],
                         d['signal_integrity']['rx_output_amplitude'],
                         'ExplicitControl was set and the module still chose '
                         'its own settings')

    def test_the_two_cursors_come_from_their_own_registers(self):
        """Pre- and post-cursor sit in separate four-byte blocks. Reading
        either from the other's would look identical while the module happens
        to provision them alike, so this pins the pair that differs."""
        self._connect()
        d = self._apply()
        self.assertEqual(d['signal_integrity_active']['rx_eq_pre_cursor'],
                         [1] * 8)
        self.assertEqual(d['signal_integrity_active']['rx_eq_post_cursor'],
                         [0] * 8)

    def test_each_lane_gets_its_own_nibble(self):
        """Every lane staged alike cannot show a swapped nibble pair, and the
        Active Control Set packs two lanes to a byte just as the staged set
        does. Stage a different value on each lane and read it back."""
        self._connect()
        self._apply()
        wanted = [0, 1, 2, 3, 3, 2, 1, 0]
        for lane in range(8):
            poke(0x10, 145 + lane, 0x11)          # AppSel 1, ExplicitControl
        for byte, pair in enumerate(zip(wanted[::2], wanted[1::2])):
            low, high = pair
            poke(0x10, 170 + byte, (high << 4) | low)
        poke(0x10, 0x8F, 0xFF)
        time.sleep(1.6)
        d = self.assertOk(self.client.get('/api/module/datapath'))['data']
        self.assertEqual(d['signal_integrity']['rx_output_amplitude'], wanted,
                         'the staged set itself was not read per lane')
        self.assertEqual(d['signal_integrity_active']['rx_output_amplitude'],
                         wanted,
                         'the Active Control Set pairs lanes to nibbles the '
                         'wrong way round')

    def test_an_unadvertised_control_is_absent_from_both_halves(self):
        """The active half is gated on the same advertisement as the staged
        one, so a module without a control does not grow one here."""
        self._connect('mock_sr8')
        d = self._apply()
        self.assertNotIn('rx_output_amplitude', d['signal_integrity'],
                         'this test needs a module without amplitude control')
        self.assertNotIn('rx_output_amplitude', d['signal_integrity_active'])

    def test_every_shipped_profile_reports_both_halves(self):
        names = [b['name'] for b in self.assertOk(
            self.client.get('/api/backends'))['data']
            if b['name'].startswith('mock')]
        for name in names:
            with self.subTest(backend=name):
                self._connect(name)
                d = self._apply()
                self.assertEqual(sorted(d['signal_integrity_active']),
                                 sorted(d['signal_integrity']),
                                 '%s reports one half and not the other'
                                 % name)

    # ---- and the table stops presenting a request as an answer ------------

    def test_the_table_shows_the_value_in_force_where_it_differs(self):
        js = self._js()
        body = js[js.index('const inForce = (key, i, staged) =>'):]
        body = body[:body.index('const lanes =')]
        self.assertIn('signal_integrity_active', js)
        self.assertRegex(body, r'live === undefined \|\| live === staged',
                         'the marker appears even where nothing differs')
        self.assertIn('11h', body,
                      'the note never says where the value in force comes from')
        self.assertIn('ExplicitControl', body,
                      'nothing explains why the staged value is not the one '
                      'in use')

    def test_the_marker_is_rendered_in_every_cell(self):
        js = self._js()
        self.assertIn('${cell(key, si[key][i])}${inForce(key, i, si[key][i])}',
                      js, 'the marker is built and never placed in the row')

    def test_the_footnote_no_longer_says_apply_commits_them(self):
        js = self._js()
        self.assertNotIn('Apply on the DataPath table commits this ', js,
                         'the footnote still claims Apply commits the staged '
                         'signal integrity set, which holds only with '
                         'ExplicitControl set')
        hint = js[js.index("hint.textContent = 'Read-only"):]
        hint = hint[:hint.index('\n  }')]
        self.assertIn('ExplicitControl', hint)



class TestAModuleWithMoreThanEightApplications(CMISTestCase):
    """AppSelCode is four bits wide, so a module may advertise fifteen
    Applications. Only eight descriptors fit in lower memory; 8.4.17 puts the
    rest on Page 01h - "Bytes 01h:223-250 provide space for seven additional
    Application Descriptors ... in addition to the eight Application
    Descriptors in Bytes 86-177".

    The tool read 32 bytes of lower memory and looped over eight. Anything
    beyond that was invisible three times over: absent from the AppSelect
    dropdown, indistinguishable from a code the module never advertised - so
    the validation answered ConfigRejectedInvalidAppSel to an Application the
    module really has - and unknown to the Data Path grouping, which needs
    each Application's host lane count to decide what an Apply touches."""

    @classmethod
    def setUpClass(cls):
        import copy
        import i2c_interface
        from i2c_backends import mock
        # Ten Applications: the eight that fit in lower memory and two that
        # only exist on Page 01h. No shipped profile has more than two, so
        # this registers a fixture rather than shipping one.
        profile = copy.deepcopy(mock._DR8_800G)
        base = list(profile['app_descriptors'])
        profile['app_descriptors'] = (base * 5)[:10]
        profile['display'] = 'ten-application fixture'
        i2c_interface._BACKENDS['test_tenapps'] = type(
            'FixtureTenApps', (mock.MockBackend,), {'PROFILE': profile})

    @classmethod
    def tearDownClass(cls):
        import i2c_interface
        i2c_interface._BACKENDS.pop('test_tenapps', None)

    def _connect(self, backend='test_tenapps'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _apps(self):
        return self.assertOk(
            self.client.get('/api/module/applications'))['data']['applications']

    def _status(self):
        return [l['config_status'] for l in self.assertOk(
            self.client.get('/api/module/monitoring'))['data']['lanes']]

    # ---- the list runs past lower memory ----------------------------------

    def test_all_ten_are_reported(self):
        self._connect()
        self.assertEqual([a['app_sel'] for a in self._apps()],
                         list(range(1, 11)),
                         'the list stopped at the end of lower memory')

    def test_the_ninth_descriptor_comes_from_page_01h(self):
        """Its fields have to be read from 01h:223 onwards, not from
        somewhere that happens to hold plausible numbers."""
        self._connect()
        apps = self._apps()
        self.assertEqual(apps[8]['app_sel'], 9)
        self.assertEqual(apps[8]['host_lanes'], 8)
        self.assertEqual(apps[8]['host_lane_assign_mask'], 0x01)
        self.assertEqual(apps[9]['app_sel'], 10)
        self.assertEqual(apps[9]['host_lanes'], 4,
                         'the tenth descriptor was read from the wrong offset')
        self.assertEqual(apps[9]['host_lane_assign_mask'], 0x11)

    def test_a_module_with_two_applications_still_reports_two(self):
        """The terminator ends the list wherever it falls. Reading the extra
        block unconditionally would invent Applications out of FFh."""
        self._connect('mock_dr8')
        self.assertEqual([a['app_sel'] for a in self._apps()], [1, 2])

    def test_every_shipped_profile_is_unchanged(self):
        names = [b['name'] for b in self.assertOk(
            self.client.get('/api/backends'))['data']
            if b['name'].startswith('mock')]
        for name in names:
            with self.subTest(backend=name):
                self._connect(name)
                self.assertEqual([a['app_sel'] for a in self._apps()], [1, 2],
                                 '%s grew Applications it does not have'
                                 % name)

    # ---- and the module accepts them --------------------------------------

    def test_an_application_past_the_eighth_is_not_refused(self):
        """The whole cost of not reading them: a code the module advertises
        answered as one it never announced."""
        self._connect()
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'dp_deinit_mask': 0xFF, 'apply': True}),
            content_type='application/json'))
        time.sleep(0.7)
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [9] * 8, 'dp_deinit_mask': 0x00,
                             'apply': True}),
            content_type='application/json'))
        time.sleep(1.6)
        self.assertEqual(set(self._status()), {'ConfigSuccess'},
                         'AppSel 9 was refused on a module that advertises it')
        self.assertEqual(self.assertOk(self.client.get(
            '/api/module/datapath'))['data']['active_app_select'], [9] * 8)

    def test_a_code_past_the_advertised_list_is_still_refused(self):
        """Ten advertised, so eleven is still a code this module never
        announced - the fix must not accept everything."""
        self._connect()
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [11] * 8, 'apply': True}),
            content_type='application/json'))
        time.sleep(1.4)
        self.assertEqual(set(self._status()),
                         {'ConfigRejectedInvalidAppSel'})

    def test_the_apply_path_knows_the_width_too(self):
        """The datapath endpoint builds its own host_lanes_by_app, and uses
        it to round a mask up to whole Data Paths (Table 8-78). Without a
        width for App 10 that rounding has nothing to round to, and asking to
        deinitialise one lane would take one lane rather than its path."""
        self._connect()
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'dp_deinit_mask': 0xFF, 'apply': True}),
            content_type='application/json'))
        time.sleep(0.7)
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'app_select': [2, 2, 2, 2, 10, 10, 10, 10],
                             'dp_deinit_mask': 0x00, 'apply': True}),
            content_type='application/json'))
        time.sleep(1.6)
        dp = self.assertOk(self.client.get('/api/module/datapath'))['data']
        self.assertEqual(dp['active_app_select'],
                         [2, 2, 2, 2, 10, 10, 10, 10],
                         'the ten-lane fixture never reached App 10')
        # Lane 5 alone: its Data Path is lanes 5-8, four lanes wide.
        self.assertOk(self.client.post(
            '/api/module/datapath',
            data=json.dumps({'dp_deinit_mask': 0x10, 'apply': True}),
            content_type='application/json'))
        time.sleep(0.6)
        self.assertEqual(self.assertOk(self.client.get(
            '/api/module/datapath'))['data']['dp_deinit_mask'], 0xF0,
            'App 10 has no width here, so one lane was taken down instead of '
            'the Data Path it belongs to')

    def test_the_grouping_knows_how_wide_the_tenth_application_is(self):
        """host_lanes_by_app feeds _datapath_groups. Without a width for App
        10 it would default to one lane and split a four-lane Data Path into
        four."""
        self._connect()
        apps = self._apps()
        self.assertEqual(apps[9]['host_lanes'], 4)
        self.assertEqual(
            app_module._datapath_groups([10] * 8,
                                        {a['app_sel']: a['host_lanes']
                                         for a in apps}),
            [[0, 1, 2, 3], [4, 5, 6, 7]],
            'an Application past the eighth has no width, so its Data Path '
            'was grouped one lane at a time')



class TestTheFifthByteOfAnApplicationDescriptor(CMISTestCase):
    """6.2.1.6 describes the Application Descriptor as five bytes and says
    where the last one lives: "The fifth byte (MediaLaneAssignmentOptions)
    identifies where the Application instance is supported on the module's
    media interface. Note that the MediaLaneAssignmentOptions registers are
    located on Memory Map Page 01h ... separated from the first four bytes."

    The tool read the four in lower memory and stopped, so every descriptor
    was four fifths told: the Applications table said where an Application may
    start on the host side and nothing about where the instance lands on the
    media. On a breakout Application - four media lanes out of eight - that
    bitmap is the only thing that says which media lanes an instance occupies.

    It is "not required for flat Memory Map modules", which have no Page 01h
    at all, so absent is a shape of module and not a failed read."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _apps(self):
        return self.assertOk(
            self.client.get('/api/module/applications'))['data']['applications']

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def _html(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        with open(path, encoding='utf-8') as f:
            return f.read()

    # ---- the module's answer ---------------------------------------------

    def test_every_application_reports_one(self):
        self._connect()
        for a in self._apps():
            self.assertIsNotNone(a.get('media_lane_assign_mask'),
                                 'App %d has no media lane assignment'
                                 % a['app_sel'])

    def test_each_application_reads_its_own_byte(self):
        """The two Applications here differ, so reading either from the
        other's address shows up. An eight-media-lane Application can only
        start on media lane 1; a four-lane one can start on 1 or 5."""
        self._connect()
        apps = self._apps()
        self.assertEqual(apps[0]['media_lanes'], 8)
        self.assertEqual(apps[0]['media_lane_assign_mask'], 0b00000001)
        self.assertEqual(apps[1]['media_lanes'], 4)
        self.assertEqual(apps[1]['media_lane_assign_mask'], 0b00010001,
                         'the second Application read the first byte')

    def test_it_is_not_the_host_bitmap_under_another_name(self):
        """A profile where the two sides differ, so returning one for the
        other cannot pass."""
        self._connect('mock_coherent')
        for a in self._apps():
            self.assertEqual(a['media_lanes'], 1)
            self.assertEqual(a['media_lane_assign_mask'], 0xFF,
                             'a single media lane Application may start on '
                             'any of them')
            self.assertNotEqual(a['media_lane_assign_mask'],
                                a['host_lane_assign_mask'])

    def test_a_descriptor_without_the_fifth_byte_says_so(self):
        """Flat memory map modules do not carry it, and None is how that
        reads - not a zero, which would claim no lane is supported."""
        import cmis_registers as c
        four = bytes([0x4F, 0x1C, 0x44, 0x11]) + b'\xff' * 28
        apps = c.parse_application_descriptors(four)
        self.assertEqual(len(apps), 1)
        self.assertIsNone(apps[0]['media_lane_assign_mask'])

    def test_the_bytes_line_up_with_the_applications(self):
        """01h:176 is App 1, so a list read one byte out would still look
        plausible on a module whose Applications share a value."""
        import cmis_registers as c
        four = bytes([0x4F, 0x1C, 0x44, 0x11]) * 3 + b'\xff' * 20
        apps = c.parse_application_descriptors(
            four, 0x02, b'', bytes([0x11, 0x22, 0x44]))
        self.assertEqual([a['media_lane_assign_mask'] for a in apps],
                         [0x11, 0x22, 0x44])

    # ---- and the table that shows it --------------------------------------

    def test_the_table_has_a_column_for_it(self):
        html = self._html()
        head = html[html.index('<th>AppSel'):html.index('<tbody id="tbl-apps"')]
        self.assertIn('Media Lane Assign', head,
                      'the descriptor is displayed without its fifth byte')
        self.assertEqual(head.count('<th>'), 7)

    def test_the_empty_state_spans_the_new_width(self):
        html = self._html()
        row = html[html.index('<tbody id="tbl-apps"'):]
        row = row[:row.index('</tbody>')]
        self.assertIn('colspan="7"', row,
                      'the placeholder row is narrower than the table')

    def test_the_cell_says_where_the_value_comes_from(self):
        js = self._js()
        body = js[js.index('function mediaAssignCell('):]
        body = body[:body.index('async function loadApplications')]
        self.assertIn('01h:', body,
                      'the tooltip never names the register')
        self.assertIn('media lane', body)
        self.assertRegex(body, r'm === null \|\| m === undefined',
                         'a module that does not report it would render a '
                         'bitmap of nothing rather than saying so')

    def test_the_cell_is_rendered_in_the_row(self):
        js = self._js()
        self.assertIn('${mediaAssignCell(a)}', js,
                      'the cell is built and never placed in the row')



class TestWhetherTheGeneratorIsActuallySending(CMISTestCase):
    """Table 8-138 "Latched Diagnostics Flags" has five flag bytes on Page
    14h. The tool read two of them - PatternCheckerLOL for host and media -
    and showed the result as the LOL column on the checker tables.

    The three it did not read say things the checker flags cannot. 136 and
    137 are PatternGeneratorLOL: a generator that has not locked is not
    sending the pattern its control registers name, so a lane could be listed
    as generating PRBS31 while the module reported it had lost lock, and the
    errors that followed looked like a link fault rather than a source that
    was never transmitting properly. 132.7 is LossOfReferenceClockFlag, which
    is module-wide: without a reference clock nothing measured on this page
    means anything. 134 and 135 latch when a gated measurement completes.

    The column was tied to the label by one flag - `isChecker = (lolMask !==
    undefined)` - so generators could not have one without being called
    checkers."""

    def _connect(self, backend='mock_dr8'):
        self.assertOk(self.client.post(
            '/api/connect',
            data=json.dumps({'backend': backend, 'bus': 0, 'address': 80}),
            content_type='application/json'))

    def _prbs(self):
        return self.assertOk(self.client.get('/api/module/prbs'))['data']

    def _start_generator(self):
        caps = self._prbs()['pattern_capabilities']['host_gen']
        self.assertTrue(caps, 'this module advertises no host patterns')
        self.assertOk(self.client.post(
            '/api/module/prbs',
            data=json.dumps({'host_gen': {'enable_mask': 0xFF,
                                          'patterns': [caps[0]] * 8}}),
            content_type='application/json'))

    def _js(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'app.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    # ---- what the module reports -----------------------------------------

    def test_the_generator_reports_its_own_lock(self):
        self._connect()
        d = self._prbs()
        for key in ('host_gen_lol_mask', 'media_gen_lol_mask'):
            self.assertIn(key, d)

    def test_a_generator_that_has_not_locked_says_so(self):
        """And clears once it has - a flag stuck on would be as useless as one
        never set."""
        self._connect()
        self._start_generator()
        self.assertEqual(self._prbs()['host_gen_lol_mask'], 0xFF,
                         'a generator just enabled reports itself locked')
        time.sleep(0.7)
        self.assertEqual(self._prbs()['host_gen_lol_mask'], 0x00,
                         'the generator never reached lock')

    def test_the_slip_is_remembered_after_the_flag_clears(self):
        """RO/COR: the read that reports it clears it, so without the history
        a generator that lost lock mid-run reads as though it never did."""
        self._connect()
        self._start_generator()
        self._prbs()                       # the read that latches and clears
        time.sleep(0.7)
        d = self._prbs()
        self.assertEqual(d['host_gen_lol_mask'], 0x00)
        self.assertTrue(all(d['host_gen_lol_seen'][:8]),
                        'nothing records that the generator had lost lock')

    def test_the_generator_and_checker_flags_are_separate_bytes(self):
        """136-137 against 138-139. Starting a generator must not light the
        checker column, which is what reading one for the other would do."""
        self._connect()
        self._start_generator()
        d = self._prbs()
        self.assertEqual(d['host_gen_lol_mask'], 0xFF)
        self.assertEqual(d['host_chk_lol_mask'], 0x00,
                         'the checker flag followed the generator')

    def test_the_reference_clock_flag_is_reported(self):
        self._connect()
        d = self._prbs()
        self.assertIn('reference_clock_lost', d)
        self.assertFalse(d['reference_clock_lost'])
        # 14h:132.7, module-wide rather than per lane.
        self.assertOk(self.client.post(
            '/api/register/write',
            data=json.dumps({'page': 0x14, 'address': 132, 'data': [0x80]}),
            content_type='application/json'))
        self.assertTrue(self._prbs()['reference_clock_lost'],
                        'the module said its reference clock was gone and '
                        'the tool did not notice')

    def test_the_gating_complete_flags_are_reported(self):
        self._connect()
        d = self._prbs()
        for key in ('host_gate_done_mask', 'media_gate_done_mask'):
            self.assertIn(key, d)
        self.assertOk(self.client.post(
            '/api/register/write',
            data=json.dumps({'page': 0x14, 'address': 134, 'data': [0x0F]}),
            content_type='application/json'))
        self.assertEqual(self._prbs()['host_gate_done_mask'], 0x0F)

    # ---- and the table that shows it --------------------------------------

    def test_the_column_no_longer_decides_the_label(self):
        js = self._js()
        self.assertNotIn('const isChecker = (lolMask !== undefined)', js,
                         'the role is still inferred from whether a LOL mask '
                         'was passed, so a generator cannot have the column')
        self.assertIn('const hasLol = (lolMask !== undefined)', js)
        body = js[js.index('function _renderPrbsTable('):]
        body = body[:body.index('\nfunction ')] if '\nfunction ' in body else body
        self.assertIn('if (hasLol) {', body)

    def test_the_generator_tables_are_given_their_flags(self):
        js = self._js()
        for key in ('host_gen_lol_mask', 'media_gen_lol_mask',
                    'host_gen_lol_seen', 'media_gen_lol_seen'):
            self.assertIn('d.' + key, js,
                          '%s never reaches the table' % key)

    def test_both_kinds_keep_their_own_name(self):
        """The tables are still labelled Generator and Checker - the point was
        to separate the column from the label, not to merge the two."""
        js = self._js()
        block = js[js.index("_renderPrbsTable('tbl-prbs-host-gen'"):]
        block = block[:block.index('const refNote')]
        self.assertRegex(block, r"tbl-prbs-host-gen'[\s\S]*?false\)")
        self.assertRegex(block, r"tbl-prbs-host-chk'[\s\S]*?true\)")

    def test_the_module_wide_flag_has_somewhere_to_appear(self):
        js = self._js()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('id="prbs-ref-clock"', html,
                      'the reference clock flag is read and never shown')
        self.assertIn("getElementById('prbs-ref-clock')", js)
        self.assertIn('14h:132.7', js,
                      'the note never names the register it comes from')
        # Pin the condition, not the identifier: leaving the text in place
        # while the render no longer depends on the flag would satisfy every
        # assertion above and show nothing.
        self.assertRegex(js, r'refNote\.innerHTML = d\.reference_clock_lost',
                         'the note is no longer driven by the flag')

    def test_the_generator_tables_have_the_column_header(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        self.assertEqual(html.count('<th>LOL</th>'), 4,
                         'all four pattern tables should carry a LOL column')


if __name__ == '__main__':
    # A failure message quoting the Chinese manual otherwise kills the summary
    # with a UnicodeEncodeError on a GBK console - the failing test's own text
    # is the last thing you want to lose.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
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
