"""Independent regressions for the October 2026 CMIS/Mock standards audit."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import app as a
from i2c_interface import create_backend


class TestAuditManagementSafety(unittest.TestCase):
    def setUp(self):
        self.client = a.app.test_client()
        self.client.post('/api/disconnect')

    def tearDown(self):
        self.client.post('/api/disconnect')

    def connect(self, name='mock_dr8'):
        r = self.client.post('/api/connect', json={'backend': name})
        self.assertEqual(r.status_code, 200, r.json)
        return a._state['backend']

    def test_empty_and_partial_chunks_fail_without_looping(self):
        for returned in (b'', b'\x01'):
            with self.subTest(returned=returned):
                self.connect()
                a._state['max_read'] = 8
                with patch.object(a, '_bus_read', return_value=returned) as read:
                    with self.assertRaisesRegex(IOError, 'asked 8.*got'):
                        a._read_chunked(128, 16)
                    self.assertEqual(read.call_count, 1)

    def test_page_verification_is_per_bank(self):
        b = self.connect('mock_1600g_16lane')
        a._set_page(0x13, 0)
        write = b.write_bytes
        def missing_bank(addr, data):
            write(addr, data)
            if addr == 126 and data == bytes([1, 0x13]):
                b._current_page = b._registers[None][127] = 0
        with patch.object(b, 'write_bytes', side_effect=missing_bank):
            with self.assertRaisesRegex(IOError, 'does not have Page 13h'):
                a._set_page(0x13, 1)
        self.assertIn((0, 0x13), a._state['pages_ok'])
        self.assertNotIn((1, 0x13), a._state['pages_ok'])

    def test_unknown_capabilities_do_not_authorize_controls(self):
        b = create_backend('mock_dr8')
        read = b.read_bytes
        def broken(addr, length):
            if b._current_page == 1 and addr == 251:
                raise IOError('mandatory advertisement unreadable')
            return read(addr, length)
        with patch.object(b, 'read_bytes', side_effect=broken), patch.object(a, 'create_backend', return_value=b):
            r = self.client.post('/api/connect', json={'backend': 'mock_dr8'})
        self.assertEqual(r.status_code, 502, r.json)
        self.assertFalse(a._state['connected'])
        self.assertFalse(a._monitor_present('tx_optical_power'))
        self.assertEqual(self.client.post('/api/module/control', json={'low_pwr': False}).status_code, 503)

    def test_future_major_is_refused_before_upper_memory_write(self):
        b = create_backend('mock_dr8')
        read = b.read_bytes
        def future(addr, length):
            raw = bytearray(read(addr, length))
            if addr <= 1 < addr + length:
                raw[1 - addr] = 0x60
            return bytes(raw)
        with patch.object(b, 'read_bytes', side_effect=future), patch.object(b, 'write_bytes', wraps=b.write_bytes) as write, patch.object(a, 'create_backend', return_value=b):
            r = self.client.post('/api/connect', json={'backend': 'mock_dr8'})
            self.assertEqual(r.status_code, 502)
            self.assertIn('cannot be managed', r.json['message'])
            write.assert_not_called()

    def test_unconfirmed_config_holds_the_whole_data_path(self):
        from test_api import deactivated
        for code in (0x0, 0xC, 0xE):
            with self.subTest(code=code):
                b = self.connect()
                deactivated(self.client)
                read, write = b.read_bytes, b.write_bytes
                triggered = [False]
                def pending(addr, length):
                    if triggered[0] and b._current_page == 0x11 and addr == 202:
                        return bytes([0x10 | code, 0x11, 0x11, 0x11])
                    return read(addr, length)
                def apply(addr, data):
                    write(addr, data)
                    if b._current_page == 0x10 and addr == 143:
                        triggered[0] = True
                with patch.object(b, 'read_bytes', side_effect=pending), patch.object(b, 'write_bytes', side_effect=apply), patch.object(a, '_advertised_seconds', return_value=0):
                    r = self.client.post('/api/module/datapath', json={'dp_deinit_mask': 0, 'apply': True})
                self.assertEqual(r.status_code, 200, r.json)
                self.assertEqual(sorted(r.json['data']['kept_deinit']), list(range(1, 9)))
                self.assertFalse(r.json['data']['configuration_complete'])
                self.assertEqual(b._registers[0x10][128], 255)

    def test_cor_survives_a_later_chunk_failure(self):
        b = self.connect()
        self.client.get('/api/module/flags')
        b._registers[0x11][134] = 1
        a._state['max_read'] = 8
        read = b.read_bytes
        def broken(addr, length):
            if b._current_page == 0x11 and addr == 142:
                raise IOError('failed after first COR chunk')
            return read(addr, length)
        with patch.object(b, 'read_bytes', side_effect=broken):
            self.assertEqual(self.client.get('/api/module/flags').status_code, 500)
        self.assertEqual(b._registers[0x11][134], 0)
        recovered = self.client.get('/api/module/flags').json['data']
        self.assertIn('dp_state_changed', recovered['lanes'][0]['seen'])
        self.assertTrue(any(e['page'] == 17 and e['address'] == 134 and e['bits_seen'] & 1 for e in recovered['cor_events']))

    def test_diagnostic_auxiliary_failure_reports_error_and_retains_cor(self):
        b = self.connect()
        b._registers[0x14][138] = 1
        read = b.read_bytes
        def broken(addr, length):
            if b._current_page == 0x13 and addr == 206:
                raise IOError('diagnostic masks unreadable')
            return read(addr, length)
        with patch.object(b, 'read_bytes', side_effect=broken):
            r = self.client.get('/api/module/prbs')
        self.assertEqual(r.status_code, 500, r.json)
        self.assertIn('diagnostic masks unreadable', r.json['message'])
        self.assertIn('host_prbs_lol', a._state['flag_history'][1])
        self.assertEqual(b._registers[0x14][138], 0)

    def test_module_diagnostic_and_tuning_cor_are_retained_at_read(self):
        b = self.connect('mock_coherent_zr')
        for page, addr, value, owner, name in [
                (0, 9, 1, 'module', 'temp_high_alarm'),
                (0x14, 138, 1, 1, 'host_prbs_lol'),
                (0x12, 231, 4, 'tuning_1', 'invalid_channel_number')]:
            with self.subTest(page=page):
                a._set_page(page)
                b._registers[None if addr < 128 else page][addr] = value
                a._bus_read(addr, 1)
                self.assertIn(name, a._state['flag_history'].get(owner, ()))

    def test_raw_and_normal_reset_share_cache_and_holdoff(self):
        import time
        for path, body in [('/api/register/write', {'page': 0, 'address': 26, 'data': [8]}), ('/api/module/control', {'action': 'reset'})]:
            with self.subTest(path=path):
                b = self.connect()
                b.MGMT_INIT_S = 0.15
                a._state['pages_ok'].add((1, 32))
                r = self.client.post(path, json=body)
                self.assertEqual(r.status_code, 200, r.json)
                self.assertEqual(a._state['pages_ok'], set())
                self.assertTrue(a._state['capabilities_stale'])
                self.assertGreater(a._state['holdoff_until'] - time.perf_counter(), 1.5)
                r = self.client.get('/api/module/status')
                self.assertEqual(r.status_code, 200, r.json)
                self.assertFalse(a._state['capabilities_stale'])

    def test_autonomous_restart_invalidates_discovery(self):
        b = self.connect('mock_coherent_zr')
        a._restart_watch()
        b._registers[0x13].update({addr: 0 for addr in range(184, 192)})
        self.assertTrue(a._restart_watch()['restarted'])
        self.assertEqual(a._state['pages_ok'], set())
        self.assertTrue(a._state['capabilities_stale'])

    def test_cdb_method_zero_refuses_wrong_trigger_format(self):
        b = self.connect()
        b._registers[1][165] &= 0x7F
        a._state['caps']['cdb']['trigger_on_stop'] = False
        r = self.client.post('/api/register/write', json={'page': 0x9F, 'address': 128, 'data': [0, 0, 0, 0]})
        self.assertEqual(r.status_code, 400, r.json)
        self.assertIn('ending at 9Fh:129', r.json['message'])
        r = self.client.post('/api/register/write', json={'page': 0x9F, 'address': 128, 'data': [0, 0]})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn('cdb', r.json['data'])

    def test_reset_freeze_is_a_bank_difference(self):
        b = self.connect('mock_1600g_16lane')
        b._registers[(0x13, 1)][177] |= 0x20
        m = a._measurement_window()
        self.assertEqual(m['banks_that_differ'], [1])
        self.assertTrue(m['controls_banks'][1]['reset_error_information'])

    def test_failed_test_entry_returns_nonzero(self):
        script = "import runpy,unittest; C=type('Sentinel',(unittest.TestCase,),{'test_fail':lambda s:s.fail('sentinel')}); unittest.TestLoader.loadTestsFromModule=lambda *a,**k:unittest.defaultTestLoader.loadTestsFromTestCase(C); runpy.run_path('test_api.py',run_name='__main__')"
        r = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).parent, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b'FAILED (failures=1)', r.stderr)


class TestAuditMockMeasurement(unittest.TestCase):
    def gate_case(self, auto, times):
        with patch('i2c_backends.mock.time.time', return_value=1000.0):
            b = create_backend('mock_1600g_dr8')
            b.connect(0, 80)
            p13 = b._registers[0x13]
            p13[160] = p13[168] = 1
            p13[177] = 0x12 if auto else 0x02
            b._restart_counting(0, b._gate_of(0))
        for t in times:
            with patch('i2c_backends.mock.time.time', return_value=1000.0 + t):
                b.read_bytes(0, 1)
        return b

    def test_single_gate_includes_final_interval_without_polling(self):
        values = []
        for times in ([6], [2, 6], [1, 2, 3, 4, 5]):
            b = self.gate_case(False, times)
            count = b._gate_of(0)['counts'][('host', 0)]
            self.assertEqual(count[1], 5 * 212500000000)
            values.append(count[0])
        self.assertAlmostEqual(values[0], values[1], delta=0.01)
        self.assertAlmostEqual(values[0], values[2], delta=0.01)

    def test_auto_gate_preserves_absolute_boundaries(self):
        for elapsed, start in [(6, 5), (16, 15)]:
            b = self.gate_case(True, [elapsed])
            g = b._gate_of(0)
            self.assertEqual(g['counts'][('host', 0)][1], 5 * 212500000000)
            self.assertEqual(b._counts[('host', 0)][1], 212500000000)
            self.assertEqual(g['start'], 1000 + start)

    def test_fec_positions_have_independent_fixtures_and_rates(self):
        with patch('i2c_backends.mock.time.time', return_value=1000):
            b = create_backend('mock_coherent')
            b.connect(0, 80)
            p = b._registers[0x13]
            pre = b._counter_stream('media', 0)
            before = b._ber_now(0)[0][1]
            p[171] = 1
            post = b._counter_stream('media', 0)
            after = b._ber_now(0)[0][1]
        self.assertAlmostEqual(pre['bits_per_s'], 989090909088)
        self.assertEqual(post['bits_per_s'], 850000000000)
        self.assertGreater(before, 100 * after)
        self.assertTrue(b.diagnostic_model()['synthetic'])

    def test_application_changes_host_lane_rate(self):
        b = create_backend('mock_coherent')
        b.connect(0, 80)
        self.assertEqual(b._counter_stream('host', 0)['bits_per_s'], 106250000000)
        b._registers[0x11][206] = 0x20
        self.assertEqual(b._counter_stream('host', 0)['bits_per_s'], 212500000000)

    def test_zr_descriptors_are_rate_and_lane_consistent(self):
        for name in ('mock_coherent_zr', 'mock_zr16'):
            b = create_backend(name)
            self.assertEqual(b.PROFILE['app_descriptors'], [(0x51, 0x6C, 0x81, 1), (0x51, 0x6D, 0x81, 1)])
            b.connect(0, 80)
            self.assertEqual(b._registers[0x12][128] >> 4, 8)
            self.assertEqual(int.from_bytes(bytes(b._registers[0x12][168+i] for i in range(4)), 'big'), 193175000)

    def test_fixed_wavelength_ranges_are_inside_the_transmit_specs(self):
        for name, low, high in [('mock_coherent', 1310.8833, 1311.1126), ('mock_1600g_dr8', 1304.5, 1317.5), ('mock_1600g_16lane', 1303.5, 1316.5), ('mock_24lane', 1303.5, 1316.5), ('mock_sr8', 844, 863)]:
            b = create_backend(name)
            b.connect(0, 80)
            p = b._registers[1]
            center = ((p[138] << 8) | p[139]) * .05
            tol = ((p[140] << 8) | p[141]) * .005
            self.assertGreater(center, 0)
            self.assertGreaterEqual(center - tol, low)
            self.assertLessEqual(center + tol, high)

    def test_tunable_wavelength_follows_actual_frequency(self):
        b = create_backend('mock_coherent_zr')
        b.connect(0, 80)
        p12, p01 = b._registers[0x12], b._registers[1]
        p12[128], p12[136], p12[137] = 0x50, 0, 7
        b.read_bytes(0, 1)
        center = ((p01[138] << 8) | p01[139]) * .05
        self.assertAlmostEqual(center, 299792.458 / 193.8, delta=.0251)

    def test_sixteen_lane_name_describes_two_applications(self):
        p = create_backend('mock_1600g_16lane').PROFILE
        self.assertIn('2×800G', p['display'])
        self.assertNotIn('1.6TAUI-16', p['display'])
