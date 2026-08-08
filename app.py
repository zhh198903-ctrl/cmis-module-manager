"""Flask REST API for CMIS optical module management."""
# Single source of truth for the version shown in the UI, /api/version, the
# console banner and the operation manual footer. Bump this, not the copies.
__version__ = '2.0.8'

import sys
import os
import shutil
import struct
import threading
import time
import webbrowser

from flask import Flask, jsonify, render_template, request

import cmis_registers as cmis
import updater
from i2c_interface import list_backends, create_backend

_BASE = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            template_folder=os.path.join(_BASE, 'templates'),
            static_folder=os.path.join(_BASE, 'static'))
# No CORS: the UI is same-origin. Allowing cross-origin requests would let any
# website the user visits drive I2C register writes on the attached module.

# ---------------------------------------------------------------------------
# Module-level state (single-user desktop app)
# ---------------------------------------------------------------------------
_state = {
    'backend': None,
    'connected': False,
    'bus': None,
    'address': None,
    # Page currently selected in the module's PageMapping register, or None
    # when unknown. Never assume a page without having selected it.
    'page': None,
}


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
# Page-switch helpers
# ---------------------------------------------------------------------------

def _invalidate_page():
    """Forget which page is selected; the next access will re-select it.

    Call after anything that can move the PageMapping register out from under
    us: connecting, a module reset, or a raw write touching byte 0x7F.
    """
    _state['page'] = None


def _set_page(page: int):
    """Select an upper-memory page, waiting out the module's access hold-off.

    CMIS 5.3 gives tBPC, the maximum Bank/Page Change time, as 10 ms; reading
    sooner can return the previous page's contents. Re-selecting a page that is
    already current costs another hold-off for nothing, and the panels read the
    same page repeatedly - a thresholds refresh alone re-selected page 02h
    twenty times - so skip the write when the page is already known.
    """
    if _state['page'] == page:
        return
    _state['page'] = None  # unknown while the write is in flight
    _state['backend'].write_bytes(0x7F, bytes([page]))
    time.sleep(0.010)
    _state['page'] = page


def _read_lower(addr: int, length: int) -> bytes:
    return _state['backend'].read_bytes(addr, length)


def _read_upper(page: int, addr: int, length: int) -> bytes:
    _set_page(page)
    return _state['backend'].read_bytes(addr, length)


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
    body = request.get_json(force=True, silent=True) or {}
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
    return _ok({'backend': backend_name, 'bus': bus, 'address': address})


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

        # Page 00h vendor information block (CORRECT addresses per CMIS 5.3 Table 8-26)
        vendor_name_raw = _read_upper(0x00, 0x81, 16)
        vendor_oui_raw  = _read_upper(0x00, 0x91, 3)
        vendor_pn_raw   = _read_upper(0x00, 0x94, 16)
        vendor_rev_raw  = _read_upper(0x00, 0xA4, 2)
        vendor_sn_raw   = _read_upper(0x00, 0xA6, 16)
        date_code_raw   = _read_upper(0x00, 0xB6, 8)
        clei_raw        = _read_upper(0x00, 0xBE, 10)

        # Capabilities
        pwr_class_raw    = _read_upper(0x00, 0xC8, 1)
        max_pwr_raw      = _read_upper(0x00, 0xC9, 1)
        cable_len_raw    = _read_upper(0x00, 0xCA, 1)
        connector_raw    = _read_upper(0x00, 0xCB, 1)
        media_lane_raw   = _read_upper(0x00, 0xD2, 1)
        media_if_tech_raw= _read_upper(0x00, 0xD4, 1)

        # Parse ALL Application Descriptors (lower mem bytes 86-117)
        # and compute module aggregate capacity across non-overlapping apps.
        appdesc_raw = _read_lower(0x56, 32)
        apps = cmis.parse_application_descriptors(appdesc_raw)
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
            hw_rev_raw = _read_upper(0x01, 0x82, 2)
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


@app.route('/api/module/monitoring', methods=['GET'])
def api_module_monitoring():
    err = _require_connected()
    if err:
        return err
    try:
        # 4 bytes — DataPath state is 4 bits/lane (Page 11h:128-131)
        dp_state_raw  = _read_upper(0x11, 0x80, 4)
        tx_power_raw  = _read_upper(0x11, 0x9A, 16)
        tx_bias_raw   = _read_upper(0x11, 0xAA, 16)
        rx_power_raw  = _read_upper(0x11, 0xBA, 16)
        cfg_status_raw= _read_upper(0x11, 0xCA, 4)

        dp_states = cmis.parse_dp_states(dp_state_raw)
        cfg_statuses = cmis.parse_config_status(cfg_status_raw)

        lanes = []
        for i in range(8):
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
        dp_deinit_raw = _read_upper(*cmis.REG_DP_DEINIT)
        tx_dis_raw    = _read_upper(*cmis.REG_TX_OUTPUT_DIS)
        app_sel_raw   = _read_upper(*cmis.REG_APP_SELECT)
        try:
            tx_pol_mask = _read_upper(*cmis.REG_TX_POL_FLIP)[0]
            rx_pol_mask = _read_upper(*cmis.REG_RX_POL_FLIP)[0]
        except Exception:
            tx_pol_mask = 0
            rx_pol_mask = 0

        tx_disable_mask = tx_dis_raw[0]
        dp_deinit_mask = dp_deinit_raw[0]
        app_select = cmis.unpack_appselect(app_sel_raw)

        lanes = []
        for i in range(8):
            lanes.append({
                'lane': i + 1,
                'tx_enable': not bool((tx_disable_mask >> i) & 1),
                'dp_deinit': bool((dp_deinit_mask >> i) & 1),
                'app_select': app_select[i],
                'tx_polarity_flip': bool((tx_pol_mask >> i) & 1),
                'rx_polarity_flip': bool((rx_pol_mask >> i) & 1),
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
        apps = cmis.parse_application_descriptors(data)
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
        body = request.get_json(force=True, silent=True) or {}
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

        _state['backend'].write_bytes(0x1A, bytes([val]))
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
        body = request.get_json(force=True, silent=True) or {}
        tx_disable_mask = int(body.get('tx_disable_mask', 0)) & 0xFF
        app_select = body.get('app_select', [1] * 8)
        tx_pol_mask = int(body.get('tx_polarity_flip_mask', 0)) & 0xFF
        rx_pol_mask = int(body.get('rx_polarity_flip_mask', 0)) & 0xFF
        apply = bool(body.get('apply', False))

        _set_page(0x10)
        # 129-130 are contiguous: InputPolarityFlipTx then OutputDisableTx
        _state['backend'].write_bytes(cmis.REG_TX_POL_FLIP[1],
                                      bytes([tx_pol_mask, tx_disable_mask]))
        _state['backend'].write_bytes(cmis.REG_RX_POL_FLIP[1], bytes([rx_pol_mask]))
        _state['backend'].write_bytes(cmis.REG_APP_SELECT[1],
                                      cmis.pack_appselect(app_select))

        # ApplyDPInit latches the Staged Control Set into the active configuration
        if apply:
            _state['backend'].write_bytes(cmis.REG_APPLY_DATAPATH[1], bytes([0xFF]))
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
        block = _read_upper(0x11, 0x87, 18)

        def flags(addr): return cmis.parse_lane_flags(block[addr - 0x87])

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
        for i in range(8):
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
        # 131-132 contiguous, 138-139 contiguous
        tx_sq, tx_sf = _read_upper(0x10, cmis.REG_TX_SQUELCH_DIS[1], 2)
        rx_od, rx_sq = _read_upper(0x10, cmis.REG_RX_OUTPUT_DIS[1], 2)
        return _ok({
            'tx_squelch_disable': tx_sq,
            'tx_squelch_force':   tx_sf,
            'rx_output_disable':  rx_od,
            'rx_squelch_disable': rx_sq,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/squelch', methods=['POST'])
def api_squelch_set():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(force=True, silent=True) or {}
        tx_sq = int(body.get('tx_squelch_disable', 0)) & 0xFF
        tx_sf = int(body.get('tx_squelch_force',   0)) & 0xFF
        rx_od = int(body.get('rx_output_disable',  0)) & 0xFF
        rx_sq = int(body.get('rx_squelch_disable', 0)) & 0xFF
        _set_page(0x10)
        _state['backend'].write_bytes(cmis.REG_TX_SQUELCH_DIS[1], bytes([tx_sq, tx_sf]))
        _state['backend'].write_bytes(cmis.REG_RX_OUTPUT_DIS[1], bytes([rx_od, rx_sq]))
        return _ok({'message': 'Squelch/output controls written'})
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/loopback', methods=['GET'])
def api_loopback_get():
    err = _require_connected()
    if err:
        return err
    try:
        media_out = _read_upper(0x13, 0xB4, 1)[0]
        media_in  = _read_upper(0x13, 0xB5, 1)[0]
        host_out  = _read_upper(0x13, 0xB6, 1)[0]
        host_in   = _read_upper(0x13, 0xB7, 1)[0]
        return _ok({
            'media_side_output': media_out,
            'media_side_input':  media_in,
            'host_side_output':  host_out,
            'host_side_input':   host_in,
        })
    except Exception as e:
        return _err(str(e), 500)


@app.route('/api/module/loopback', methods=['POST'])
def api_loopback_set():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(force=True, silent=True) or {}
        media_out = int(body.get('media_side_output', 0)) & 0xFF
        media_in  = int(body.get('media_side_input',  0)) & 0xFF
        host_out  = int(body.get('host_side_output',  0)) & 0xFF
        host_in   = int(body.get('host_side_input',   0)) & 0xFF
        _set_page(0x13)
        _state['backend'].write_bytes(0xB4, bytes([media_out, media_in, host_out, host_in]))
        return _ok({'message': 'Loopback configuration written'})
    except Exception as e:
        return _err(str(e), 500)


def _read_prbs_block(base_addr: int) -> dict:
    """Read 8-byte PRBS block: enable, invert, swap, fec, pattern×4."""
    data = _read_upper(0x13, base_addr, 8)
    return {
        'enable_mask':       data[0],
        'invert_mask':       data[1],
        'byte_swap_mask':    data[2],
        'fec_mask':          data[3],   # PreFEC for gen, PostFEC for chk
        'patterns':          cmis.unpack_prbs_patterns(data[4:8]),
    }


@app.route('/api/module/prbs', methods=['GET'])
def api_prbs_get():
    err = _require_connected()
    if err:
        return err
    try:
        # Try to also read pattern checker LOL flags from Page 14h
        try:
            host_lol = _read_upper(0x14, 0x8A, 1)[0]
            media_lol = _read_upper(0x14, 0x8B, 1)[0]
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
        body = request.get_json(force=True, silent=True) or {}
        _set_page(0x13)
        for key, base_addr in [
            ('host_gen',  0x90),
            ('media_gen', 0x98),
            ('host_chk',  0xA0),
            ('media_chk', 0xA8),
        ]:
            section = body.get(key, {})
            if not section:
                continue
            en_mask  = int(section.get('enable_mask', 0))    & 0xFF
            inv_mask = int(section.get('invert_mask', 0))    & 0xFF
            sw_mask  = int(section.get('byte_swap_mask', 0)) & 0xFF
            fec_mask = int(section.get('fec_mask', 0))       & 0xFF
            patterns = section.get('patterns', [0] * 8)
            block = bytes([en_mask, inv_mask, sw_mask, fec_mask]) + cmis.pack_prbs_patterns(patterns)
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
        _state['backend'].write_bytes(0x80, bytes([0x06]))
        time.sleep(0.005)
        # Selector 0x06: bytes 192-207 reserved, 208-223 host SNR, 240-255 media SNR
        data = _read_upper(0x14, 0xC0, 64)
        host_snr = []
        media_snr = []
        # Host SNR at offset 16 (bytes 208-223 = data[16:32])
        # Media SNR at offset 48 (bytes 240-255 = data[48:64])
        for i in range(8):
            host_snr.append(round(cmis.parse_snr_db(data[16 + i*2:18 + i*2]), 3))
            media_snr.append(round(cmis.parse_snr_db(data[48 + i*2:50 + i*2]), 3))
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
        _state['backend'].write_bytes(0x80, bytes([0x01]))
        time.sleep(0.005)
        # Host BER at 0xC0–0xCF, Media BER at 0xD0–0xDF (8 lanes × 2B each)
        ber_raw = _read_upper(0x14, 0xC0, 32)
        lanes = []
        for i in range(8):
            host_ber = cmis.parse_f16_ber(ber_raw[i*2:(i+1)*2])
            media_ber = cmis.parse_f16_ber(ber_raw[16 + i*2:16 + (i+1)*2])
            lanes.append({
                'lane': i + 1,
                'host_ber': host_ber,
                'media_ber': media_ber,
            })
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
        grid_sup = _read_upper(0x04, 0x80, 2)
        fine_res = _read_upper(0x04, 0xBE, 2)
        fine_low = _read_upper(0x04, 0xC0, 2)
        fine_high = _read_upper(0x04, 0xC2, 2)
        pwr_min = _read_upper(0x04, 0xC6, 2)
        pwr_max = _read_upper(0x04, 0xC8, 2)

        grids_supported = []
        grid_names = ['3.125 GHz','6.25 GHz','12.5 GHz','25 GHz',
                      '50 GHz','100 GHz','33 GHz','75 GHz']
        for i, name in enumerate(grid_names):
            if (grid_sup[0] >> i) & 1:
                grids_supported.append(name)
        if (grid_sup[1] >> 6) & 1:
            grids_supported.append('150 GHz')
        fine_tuning_supported = bool((grid_sup[1] >> 7) & 1)

        # Current state (Page 12h) — 8 lanes
        grid_spacing = _read_upper(0x12, 0x80, 8)
        channel_num  = _read_upper(0x12, 0x88, 16)
        fine_offset  = _read_upper(0x12, 0x98, 16)
        current_freq = _read_upper(0x12, 0xA8, 32)
        target_pwr   = _read_upper(0x12, 0xC8, 16)
        tuning_status= _read_upper(0x12, 0xDE, 8)

        grid_codes = {0:'3.125 GHz',1:'6.25 GHz',2:'12.5 GHz',3:'25 GHz',
                      4:'50 GHz',5:'100 GHz',6:'33 GHz',7:'75 GHz',8:'150 GHz'}

        lanes = []
        for i in range(8):
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
            })

        return _ok({
            'grids_supported': grids_supported,
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
        body = request.get_json(force=True, silent=True) or {}
        _set_page(0x12)
        lanes = body.get('lanes', [])
        for ldata in lanes:
            lane = int(ldata.get('lane', 1)) - 1
            if not (0 <= lane < 8):
                continue
            if 'grid_code' in ldata:
                gc = int(ldata['grid_code']) & 0x0F
                fine_en = 1 if ldata.get('fine_tuning_enabled', False) else 0
                _state['backend'].write_bytes(0x80 + lane, bytes([(gc << 4) | fine_en]))
            if 'channel' in ldata:
                ch = int(ldata['channel'])
                ch_bytes = struct.pack(">h", ch)
                _state['backend'].write_bytes(0x88 + lane * 2, ch_bytes)
            if 'fine_offset_ghz' in ldata:
                ft = int(round(float(ldata['fine_offset_ghz']) / 0.001))
                _state['backend'].write_bytes(0x98 + lane * 2, struct.pack(">h", ft))
            if 'target_power_dbm' in ldata:
                pwr = int(round(float(ldata['target_power_dbm']) / 0.01))
                _state['backend'].write_bytes(0xC8 + lane * 2, struct.pack(">h", pwr))
        return _ok({'message': 'Laser tuning parameters written'})
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
        for sel, lane_start, side in [
            (0x02, 0, 'host'), (0x03, 4, 'host'),
            (0x04, 0, 'media'), (0x05, 4, 'media'),
        ]:
            _set_page(0x14)
            _state['backend'].write_bytes(0x80, bytes([sel]))
            time.sleep(0.005)
            data = _read_upper(0x14, 0xC0, 64)
            for li in range(4):
                off = li * 16
                error_count = struct.unpack("<Q", data[off:off+8])[0]
                total_bits_raw = struct.unpack("<Q", data[off+8:off+16])[0]
                psl = total_bits_raw & 1  # pattern sync loss indicator
                total_bits = total_bits_raw & ~1
                lane_idx = lane_start + li
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


@app.route('/api/register/read', methods=['POST'])
def api_register_read():
    err = _require_connected()
    if err:
        return err
    try:
        body = request.get_json(force=True, silent=True) or {}
        page_raw = body.get('page', 0)
        if isinstance(page_raw, str):
            page = int(page_raw, 0)
        else:
            page = int(page_raw)

        addr_raw = body.get('address', 0)
        if isinstance(addr_raw, str):
            address = int(addr_raw, 0)
        else:
            address = int(addr_raw)

        length = int(body.get('length', 1))
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
        body = request.get_json(force=True, silent=True) or {}
        page_raw = body.get('page', 0)
        if isinstance(page_raw, str):
            page = int(page_raw, 0)
        else:
            page = int(page_raw)

        addr_raw = body.get('address', 0)
        if isinstance(addr_raw, str):
            address = int(addr_raw, 0)
        else:
            address = int(addr_raw)

        data_list = body.get('data', [])
        if not data_list:
            return _err("No data provided")
        if isinstance(data_list, str):
            data_list = [int(h, 16) for h in data_list.split()]
        data = bytes(int(b) & 0xFF for b in data_list)

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
        'cmis_revision_supported': '5.3',
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


@app.route('/api/update/apply', methods=['POST'])
def api_update_apply():
    """Download the newer release and hand the swap to a detached helper."""
    if not updater.is_frozen():
        return _err('Running from source — upgrade with git pull instead of '
                    'replacing an executable', 400)
    rel = updater.fetch_latest_release()
    if rel is None:
        return _err('Could not reach GitHub to download the update', 502)
    if not updater.is_newer(rel['version'], __version__):
        return _err(f'Already on the newest version ({__version__})', 400)

    staged = updater.staging_dir()
    try:
        if os.path.isdir(staged):
            shutil.rmtree(staged, ignore_errors=True)
        os.makedirs(staged, exist_ok=True)
        archive = os.path.join(staged, rel['asset_name'])
        updater.download_asset(rel['asset_url'], archive,
                               total_hint=rel['asset_size'])
        if not updater.verify_sha256(archive, rel['sha256']):
            shutil.rmtree(staged, ignore_errors=True)
            return _err('Downloaded file failed its checksum; update aborted', 500)
        updater.extract_payload(archive, staged)
        os.remove(archive)
    except Exception as e:
        shutil.rmtree(staged, ignore_errors=True)
        return _err(f'Update download failed: {e}', 500)

    updater.stage_and_swap(staged)
    # The helper is now waiting for this process to release the exe. Give the
    # response time to reach the browser, then quit so the swap can proceed.
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return _ok({
        'message': f'Updating to {rel["version"]}. The window will close and '
                   'reopen automatically.',
        'version': rel['version'],
    })


@app.route('/')
def index():
    return render_template('index.html', version=__version__)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    url = 'http://127.0.0.1:5000'
    print(f"CMIS Module Manager v{__version__} starting on {url}")
    # Set CMIS_NO_BROWSER=1 to start the server without opening a tab. Repeated
    # automated launches otherwise leave a pile of tabs behind, and after a
    # self-update the relaunched instance would open yet another one on top of
    # the page the user is already looking at.
    if os.environ.get('CMIS_NO_BROWSER', '').strip() not in ('1', 'true', 'True'):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=False)
