/* CMIS Module Manager — Frontend Logic */
'use strict';

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------
const AppState = {
  connected: false,
  currentTab: 'info',
  monitoringInterval: null,
  monitoringIntervalMs: 2000,
  monitoringManual: false,
  backendsCache: [],   // cached result of /api/backends
};

// Alarm thresholds (dBm)
const ALARM = { TX_LOW: -10, TX_HIGH: 3, RX_LOW: -10, RX_HIGH: 3 };

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function apiFetch(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  try {
    const resp = await fetch(path, opts);
    return await resp.json();
  } catch (e) {
    return { status: 'error', message: e.message };
  }
}

const apiGet  = (path)       => apiFetch('GET',  path);
const apiPost = (path, body) => apiFetch('POST', path, body);

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------
function toast(message, type = 'info', durationMs = 3000) {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), durationMs);
}

// ---------------------------------------------------------------------------
// Connection panel
// ---------------------------------------------------------------------------
async function loadBackends() {
  const sel = document.getElementById('sel-backend');
  if (!sel) return;

  // Remember the currently selected value so we can restore it after refresh
  const previousValue = sel.value;

  const res = await apiGet('/api/backends');
  if (res.status !== 'ok') { toast('Failed to load backends', 'error'); return; }

  // Cache backends list for later use (e.g. backend info area)
  AppState.backendsCache = res.data || [];

  sel.innerHTML = '';
  for (const b of AppState.backendsCache) {
    const opt = document.createElement('option');
    opt.value = b.name;
    opt.textContent = `${b.name}${b.available ? '' : ' (unavailable)'}`;
    opt.disabled = !b.available;
    sel.appendChild(opt);
  }

  // Restore previous selection if it still exists in the new list
  if (previousValue && sel.querySelector(`option[value="${CSS.escape(previousValue)}"]`)) {
    sel.value = previousValue;
  }
}

async function connectModule() {
  const backend = document.getElementById('sel-backend').value;
  const bus     = parseInt(document.getElementById('inp-bus').value, 10) || 0;
  const addrStr = document.getElementById('inp-address').value.trim() || '0x50';
  const address = addrStr.startsWith('0x') || addrStr.startsWith('0X')
    ? parseInt(addrStr, 16)
    : parseInt(addrStr, 10);
  if (Number.isNaN(address)) {
    toast(`Invalid I2C address: ${addrStr}`, 'error');
    return;
  }

  setConnectBusy(true);
  const res = await apiPost('/api/connect', { backend, bus, address });
  setConnectBusy(false);

  if (res.status !== 'ok') {
    toast(`Connect failed: ${res.message}`, 'error', 5000);
    return;
  }
  AppState.connected = true;
  updateConnectionUI(true, `${backend}  bus:${bus}  addr:0x${address.toString(16).toUpperCase()}`);
  updateBackendInfoArea(backend);
  toast('Connected', 'success');
  // Auto-load info tab
  switchTab('info');
  loadInfo();
}

async function disconnectModule() {
  await apiGet('/api/disconnect');
  AppState.connected = false;
  stopMonitoring();
  updateConnectionUI(false, '');
  clearBackendInfoArea();
  toast('Disconnected', 'info');
  clearTabContent();
  // Switch back to the info tab so the active tab matches the enabled state
  switchTab('info');
}

function setConnectBusy(busy) {
  document.getElementById('btn-connect').disabled = busy;
  document.getElementById('btn-connect').innerHTML = busy
    ? '<span class="spinner"></span> Connecting…'
    : 'Connect';
}

function updateConnectionUI(connected, label) {
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  const btnConn  = document.getElementById('btn-connect');
  const btnDisc  = document.getElementById('btn-disconnect');

  dot.className  = `status-dot ${connected ? 'connected' : 'disconnected'}`;
  text.textContent = connected ? `Connected — ${label}` : 'Disconnected';
  btnConn.disabled = connected;
  btnDisc.disabled = !connected;

  // Enable/disable tabs
  document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
    btn.disabled = !connected;
  });
}

function updateBackendInfoArea(backendName) {
  const area = document.getElementById('backend-info-area');
  if (!area) return;

  const info = AppState.backendsCache.find(b => b.name === backendName);
  if (!info) {
    area.innerHTML = `<span style="color:var(--text)">Connected to <strong>${backendName}</strong></span>`;
    return;
  }

  const availBadge = info.available
    ? '<span class="badge badge-ok">available</span>'
    : '<span class="badge badge-off">unavailable</span>';

  const descHtml = info.description
    ? `<div style="margin-top:6px;color:var(--text)">${info.description}</div>`
    : '';

  area.innerHTML =
    `<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">` +
    `  <strong style="color:var(--text)">${info.name}</strong>${availBadge}` +
    `</div>` +
    descHtml;
}

function clearBackendInfoArea() {
  const area = document.getElementById('backend-info-area');
  if (area) {
    area.innerHTML = 'Select a backend and connect to view info.';
  }
}

function clearTabContent() {
  const tableIds = [
    'tbl-info', 'tbl-monitoring', 'tbl-datapath', 'tbl-flags', 'tbl-thresholds',
    'tbl-ber', 'tbl-snr', 'tbl-counters', 'tbl-laser', 'tbl-apps', 'tbl-control',
    'tbl-prbs-host-gen', 'tbl-prbs-media-gen', 'tbl-prbs-host-chk', 'tbl-prbs-media-chk',
  ];
  tableIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '';
  });
  const dump = document.getElementById('hex-dump');
  if (dump) dump.textContent = '';
  const laserCaps = document.getElementById('laser-caps');
  if (laserCaps) laserCaps.innerHTML = '';
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
function switchTab(name) {
  if (!AppState.connected && name !== 'info') return;

  // Stop monitoring if leaving that tab
  if (AppState.currentTab === 'monitoring' && name !== 'monitoring') {
    stopMonitoring();
  }

  AppState.currentTab = name;

  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

  const btn   = document.querySelector(`.tab-btn[data-tab="${name}"]`);
  const panel = document.getElementById(`tab-${name}`);
  if (btn)   btn.classList.add('active');
  if (panel) panel.classList.add('active');

  // Auto-load data for tab
  if (name === 'info')        loadInfo();
  if (name === 'monitoring')  startMonitoring();
  if (name === 'datapath')  { loadModuleControl(); loadApplications(); loadDatapath(); loadSquelch(); }
  if (name === 'diagnostics') { loadLoopback(); loadPrbs(); }
}

// ---------------------------------------------------------------------------
// Module Info tab
// ---------------------------------------------------------------------------
async function loadInfo() {
  if (!AppState.connected) return;

  const [infoRes, statusRes] = await Promise.all([
    apiGet('/api/module/info'),
    apiGet('/api/module/status'),
  ]);

  const tbody = document.getElementById('tbl-info');
  if (!tbody) return;

  if (infoRes.status !== 'ok') { toast(`Info error: ${infoRes.message}`, 'error'); return; }
  if (statusRes.status !== 'ok') { toast(`Status error: ${statusRes.message}`, 'error'); return; }

  const d = infoRes.data;
  const s = statusRes.data;

  // [field, value, page, address, cmis_definition]
  const rows = [
    ['Module Type',     d.module_type,                                                            'Lower', '0x00',        'SFF-8024 Identifier (Table 8-5)'],
    ['Module ID',       `0x${(d.module_id||0).toString(16).toUpperCase().padStart(2,'0')}`,      'Lower', '0x00',        'Identifier byte (raw hex)'],
    ['CMIS Revision',   d.cmis_revision,                                                          'Lower', '0x01',        'Upper nibble=major, lower=minor (0x53=5.3)'],
    ['Memory Model',    d.memory_model,                                                           'Lower', '0x02[7]',     '0=Paged, 1=Flat'],
    ['Media Type',      d.media_type,                                                             'Lower', '0x55',        'Media type code (Table 8-21)'],
    ['Module State',    s.module_state,                                                           'Lower', '0x03[3:1]',   'Current state machine (Table 8-7)'],
    ['Vendor Name',     d.vendor_name,                                                            '00h',   '0x81–0x90',   'Vendor Name, 16-byte ASCII'],
    ['Vendor OUI',      d.vendor_oui,                                                             '00h',   '0x91–0x93',   'IEEE OUI (3 bytes hex)'],
    ['Vendor P/N',      d.vendor_pn,                                                              '00h',   '0x94–0xA3',   'Vendor Part Number, 16-byte ASCII'],
    ['Vendor Rev',      d.vendor_rev,                                                             '00h',   '0xA4–0xA5',   'Vendor Revision, 2-byte ASCII'],
    ['Vendor S/N',      d.vendor_sn,                                                              '00h',   '0xA6–0xB5',   'Vendor Serial Number, 16-byte ASCII'],
    ['Date Code',       d.date_code,                                                              '00h',   '0xB6–0xBD',   'Date Code, 8-byte ASCII (YYMMDDLL)'],
    ['CLEI Code',       d.clei_code || '(none)',                                                  '00h',   '0xBE–0xC7',   'CLEI Code, 10-byte ASCII'],
    ['Power Class',     `Class ${d.power_class}`,                                                 '00h',   '0xC8[7:5]',   'Module Power Class (1–8)'],
    ['Max Power',       `${d.max_power_w} W`,                                                     '00h',   '0xC9',        'Maximum Power Consumption (×0.25 W)'],
    ['Cable Length',    d.cable_length_m === 0 ? '— (transceiver)' : `${d.cable_length_m} m`,    '00h',   '0xCA',        '[7:6]=mult, [5:0]=base (m)'],
    ['Connector',       `${d.connector_type} (0x${(d.connector_code||0).toString(16).toUpperCase().padStart(2,'0')})`, '00h', '0xCB', 'SFF-8024 Connector Type (Table 4-3)'],
    ['Media Interface', `${d.media_if_tech} (0x${(d.media_if_tech_code||0).toString(16).toUpperCase().padStart(2,'0')})`, '00h', '0xD4', 'Media Interface Technology (Table 8-40)'],
    ['Host Lanes',      d.lanes_detail ? `${d.host_lanes} <span style="color:var(--text-muted);font-size:11px">(${d.lanes_detail})</span>` : `${d.host_lanes}`,  'Lower', '0x56+', 'Max concurrent host lanes across all AppDescriptors'],
    ['Media Lanes',     `${d.media_lanes}`, 'Lower', '0x56+', 'Max concurrent media lanes'],
    ['FW Revision',     d.fw_revision,                                                            '01h',   '0x84–0x85',   'Active Firmware Major.Minor'],
    ['HW Revision',     d.hw_revision,                                                            '01h',   '0x82–0x83',   'Hardware Revision Major.Minor'],
    ['Temperature',     `${s.temperature_c?.toFixed(2)} °C`,                                     'Lower', '0x0E–0x0F',   'Module Temperature (s16/256)'],
    ['Supply Voltage',  `${s.voltage_v?.toFixed(4)} V`,                                          'Lower', '0x10–0x11',   'Supply Voltage (u16 × 100 µV)'],
    ['Alarms',          s.alarm_active ? '<span class="text-danger">Active</span>' : '<span class="text-success">None</span>', 'Lower', '0x08–0x0D', 'Module-Level Flags'],
  ];

  tbody.innerHTML = rows.map(([k, v, pg, addr, def]) =>
    `<tr>
      <td style="color:var(--text-muted);width:140px">${k}</td>
      <td>${v}</td>
      <td class="td-page">${pg}</td>
      <td class="td-addr">${addr}</td>
      <td class="td-def">${def}</td>
    </tr>`
  ).join('');
}

// ---------------------------------------------------------------------------
// Monitoring tab
// ---------------------------------------------------------------------------
function startMonitoring() {
  stopMonitoring();
  loadThresholds();
  loadMonitoring();
  if (AppState.monitoringManual) return;
  AppState.monitoringInterval = setInterval(loadMonitoring, AppState.monitoringIntervalMs);
}

function stopMonitoring() {
  if (AppState.monitoringInterval) {
    clearInterval(AppState.monitoringInterval);
    AppState.monitoringInterval = null;
  }
}

async function loadMonitoring() {
  if (!AppState.connected) return;

  const [monRes, statusRes, flagsRes] = await Promise.all([
    apiGet('/api/module/monitoring'),
    apiGet('/api/module/status'),
    apiGet('/api/module/flags'),
  ]);

  if (monRes.status !== 'ok') {
    toast(`Monitoring error: ${monRes.message}`, 'error');
    stopMonitoring();  // halt auto-refresh so a dead server doesn't spam toasts
    return;
  }
  if (statusRes.status !== 'ok') return;
  if (flagsRes.status === 'ok') renderFlags(flagsRes.data.lanes);

  const s = statusRes.data;
  const summaryEl = document.getElementById('monitor-summary');
  if (summaryEl) {
    const tempClass = s.temperature_c > 70 ? 'text-danger' : s.temperature_c > 60 ? 'text-warning' : 'text-success';
    summaryEl.innerHTML =
      `<span class="${tempClass}">Temp: ${s.temperature_c?.toFixed(2)} °C</span>` +
      `&ensp;|&ensp;<span>Voltage: ${s.voltage_v?.toFixed(4)} V</span>` +
      (s.alarm_active ? `&ensp;|&ensp;<span class="text-danger">⚠ Alarm Active</span>` : '');
  }

  const tbody = document.getElementById('tbl-monitoring');
  if (!tbody) return;

  const lanes = monRes.data.lanes;
  const allActivated = lanes.length > 0 && lanes.every(l => l.datapath_state === 'Activated');
  const dotEl = document.getElementById('monitor-all-activated-dot');
  if (dotEl) dotEl.style.display = allActivated ? 'inline-block' : 'none';

  tbody.innerHTML = lanes.map(lane => {
    const txDbm = lane.tx_power_dbm;
    const rxDbm = lane.rx_power_dbm;
    const txCls = txDbm < ALARM.TX_LOW ? 'alarm-low' : txDbm > ALARM.TX_HIGH ? 'alarm-high' : '';
    const rxCls = rxDbm < ALARM.RX_LOW ? 'alarm-low' : rxDbm > ALARM.RX_HIGH ? 'alarm-high' : '';
    const stateClass = lane.datapath_state === 'Activated'
      ? 'state-activated' : lane.datapath_state === 'Init'
      ? 'state-init' : 'state-deactivated';

    const cfgStatus = lane.config_status || '—';
    const cfgClass = cfgStatus === 'ConfigSuccess' ? 'state-activated'
                   : cfgStatus === 'ConfigInProgress' ? 'state-init'
                   : cfgStatus.startsWith('Config') && cfgStatus !== 'ConfigUndefined' ? 'state-deactivated'
                   : '';
    return `<tr>
      <td>${lane.lane}</td>
      <td class="${txCls}">${lane.tx_power_uw.toFixed(1)} µW<br><small>${txDbm.toFixed(2)} dBm</small></td>
      <td>${lane.tx_bias_ma.toFixed(3)} mA</td>
      <td class="${rxCls}">${lane.rx_power_uw.toFixed(1)} µW<br><small>${rxDbm.toFixed(2)} dBm</small></td>
      <td class="${stateClass}">${lane.datapath_state}</td>
      <td class="${cfgClass}">${cfgStatus}</td>
    </tr>`;
  }).join('');
}

function setRefreshInterval(ms) {
  AppState.monitoringIntervalMs = ms;
  // Restart monitoring if we are currently on the monitoring tab
  // (covers both the case where interval was running and the case
  //  where the user switched back from "Manual" mode)
  if (AppState.connected && AppState.currentTab === 'monitoring') {
    startMonitoring();
  }
}

// ---------------------------------------------------------------------------
// DataPath config tab
// ---------------------------------------------------------------------------
async function loadDatapath() {
  if (!AppState.connected) return;

  const res = await apiGet('/api/module/datapath');
  if (res.status !== 'ok') { toast(`DataPath error: ${res.message}`, 'error'); return; }

  const tbody = document.getElementById('tbl-datapath');
  if (!tbody) return;

  tbody.innerHTML = res.data.lanes.map(lane => {
    const appOpts = Array.from({length: 16}, (_, i) =>
      `<option value="${i}" ${lane.app_select === i ? 'selected' : ''}>App ${i}</option>`
    ).join('');

    return `<tr class="datapath-lane-row">
      <td>Lane ${lane.lane}</td>
      <td><select id="app-sel-${lane.lane}" class="app-select-input">${appOpts}</select></td>
      <td><input type="checkbox" id="tx-en-${lane.lane}" ${lane.tx_enable ? 'checked' : ''}></td>
      <td><input type="checkbox" id="tx-pol-${lane.lane}" ${lane.tx_polarity_flip ? 'checked' : ''}></td>
      <td><input type="checkbox" id="rx-pol-${lane.lane}" ${lane.rx_polarity_flip ? 'checked' : ''}></td>
      <td>${lane.dp_deinit ? '<span class="text-warning">Deinit</span>' : '<span class="text-success">Active</span>'}</td>
    </tr>`;
  }).join('');
}

async function applyDatapath() {
  const app_select = [];
  let tx_disable_mask = 0, tx_pol_mask = 0, rx_pol_mask = 0;

  for (let i = 1; i <= 8; i++) {
    const appSel = document.getElementById(`app-sel-${i}`);
    const txEn   = document.getElementById(`tx-en-${i}`);
    if (!appSel || !txEn) continue;
    app_select.push(parseInt(appSel.value, 10));
    if (!txEn.checked) tx_disable_mask |= (1 << (i - 1));
    if (document.getElementById(`tx-pol-${i}`)?.checked) tx_pol_mask |= (1 << (i - 1));
    if (document.getElementById(`rx-pol-${i}`)?.checked) rx_pol_mask |= (1 << (i - 1));
  }

  const res = await apiPost('/api/module/datapath', {
    tx_disable_mask,
    app_select,
    tx_polarity_flip_mask: tx_pol_mask,
    rx_polarity_flip_mask: rx_pol_mask,
    apply: true,
  });

  if (res.status === 'ok') {
    toast('DataPath configuration applied', 'success');
  } else {
    toast(`Apply failed: ${res.message}`, 'error');
  }
}

// ---------------------------------------------------------------------------
// Module Control panel (DataPath tab)
// ---------------------------------------------------------------------------
async function loadModuleControl() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/control');
  if (res.status !== 'ok') { toast(`Control error: ${res.message}`, 'error'); return; }
  const d = res.data;
  const tbody = document.getElementById('tbl-control');
  if (!tbody) return;

  const yes = '<span class="text-success">●</span>';
  const no  = '<span style="color:var(--text-muted)">○</span>';

  tbody.innerHTML = `
    <tr>
      <td>Software Reset</td>
      <td>${d.software_reset ? yes : no}</td>
      <td class="td-addr">0x1A[3]</td>
      <td><button class="btn-danger btn-sm" id="btn-mod-reset">Reset Module</button></td>
    </tr>
    <tr>
      <td>Low Power Request (SW)</td>
      <td>${d.low_pwr_request_sw ? yes : no}</td>
      <td class="td-addr">0x1A[4]</td>
      <td>
        <button class="btn-secondary btn-sm" id="btn-mod-lp">Enter LowPwr</button>
        <button class="btn-secondary btn-sm" id="btn-mod-hp">Exit LowPwr</button>
      </td>
    </tr>
    <tr>
      <td>Allow LowPwrRequestHW</td>
      <td>${d.low_pwr_allow_request_hw ? yes : no}</td>
      <td class="td-addr">0x1A[6]</td>
      <td>—</td>
    </tr>
    <tr>
      <td>Squelch Method</td>
      <td>${d.squelch_method_select ? 'Pav' : 'OMA'}</td>
      <td class="td-addr">0x1A[5]</td>
      <td>—</td>
    </tr>`;

  document.getElementById('btn-mod-reset')?.addEventListener('click', async () => {
    if (!confirm('Software-reset the module? All DataPaths will reinitialize.')) return;
    const r = await apiPost('/api/module/control', { action: 'reset' });
    toast(r.status === 'ok' ? 'Module reset issued' : `Reset failed: ${r.message}`,
          r.status === 'ok' ? 'success' : 'error');
  });
  document.getElementById('btn-mod-lp')?.addEventListener('click', async () => {
    const r = await apiPost('/api/module/control', { action: 'low_power' });
    toast(r.status === 'ok' ? 'LowPwr requested' : `Failed: ${r.message}`,
          r.status === 'ok' ? 'success' : 'error');
    setTimeout(loadModuleControl, 200);
  });
  document.getElementById('btn-mod-hp')?.addEventListener('click', async () => {
    const r = await apiPost('/api/module/control', { action: 'high_power' });
    toast(r.status === 'ok' ? 'High power requested' : `Failed: ${r.message}`,
          r.status === 'ok' ? 'success' : 'error');
    setTimeout(loadModuleControl, 200);
  });
}

// ---------------------------------------------------------------------------
// Application Descriptors (DataPath tab)
// ---------------------------------------------------------------------------
async function loadApplications() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/applications');
  if (res.status !== 'ok') { toast(`Apps error: ${res.message}`, 'error'); return; }
  const tbody = document.getElementById('tbl-apps');
  if (!tbody) return;

  const apps = res.data.applications;
  if (!apps || apps.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="placeholder-text">No applications advertised.</td></tr>';
    return;
  }
  tbody.innerHTML = apps.map(a => {
    const hostHex = `0x${a.host_if_id.toString(16).toUpperCase().padStart(2,'0')}`;
    const mediaHex = `0x${a.media_if_id.toString(16).toUpperCase().padStart(2,'0')}`;
    const assignBin = a.host_lane_assign_mask.toString(2).padStart(8, '0');
    return `<tr>
      <td>${a.app_sel}</td>
      <td>${hostHex}</td>
      <td>${mediaHex}</td>
      <td>${a.host_lanes || '—'}</td>
      <td>${a.media_lanes || '—'}</td>
      <td><code>${assignBin}</code></td>
    </tr>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// SNR (Diagnostics tab)
// ---------------------------------------------------------------------------
async function loadSnr() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/snr');
  const tbody = document.getElementById('tbl-snr');
  if (!tbody) return;
  if (res.status !== 'ok') {
    tbody.innerHTML = `<tr><td colspan="9" class="placeholder-text">SNR not available: ${res.message}</td></tr>`;
    return;
  }
  const fmt = (v) => (v == null ? '—' : v.toFixed(2));
  const hostCells = res.data.host_snr_db.map(v => `<td>${fmt(v)}</td>`).join('');
  const mediaCells = res.data.media_snr_db.map(v => `<td>${fmt(v)}</td>`).join('');
  tbody.innerHTML =
    `<tr><td style="color:var(--text-muted)">Host<span class="reg-badge">14h/0xC0+16 sel=0x06</span></td>${hostCells}</tr>` +
    `<tr><td style="color:var(--text-muted)">Media<span class="reg-badge">14h/0xC0+48 sel=0x06</span></td>${mediaCells}</tr>`;
}

// ---------------------------------------------------------------------------
// Raw Register tab
// ---------------------------------------------------------------------------

function parseHexOrDec(s) {
  s = (s || '').trim();
  if (s.startsWith('0x') || s.startsWith('0X')) return parseInt(s, 16);
  return parseInt(s, 10);
}

async function rawRead() {
  const page    = parseHexOrDec(document.getElementById('raw-page').value);
  const address = parseHexOrDec(document.getElementById('raw-address').value);
  const length  = parseInt(document.getElementById('raw-length').value, 10) || 1;

  const res = await apiPost('/api/register/read', { page, address, length });
  const dumpEl = document.getElementById('hex-dump');

  if (res.status !== 'ok') {
    dumpEl.textContent = `Error: ${res.message}`;
    toast(`Read error: ${res.message}`, 'error');
    return;
  }

  dumpEl.textContent = formatHexDump(res.data.data, address);
}

async function rawWrite() {
  const page    = parseHexOrDec(document.getElementById('raw-page').value);
  const address = parseHexOrDec(document.getElementById('raw-address').value);
  const dataStr = document.getElementById('raw-data').value.trim();

  if (!dataStr) { toast('Enter data bytes (space-separated hex)', 'error'); return; }

  const data = dataStr.split(/\s+/).map(h => parseInt(h, 16)).filter(v => !isNaN(v));
  if (!data.length) { toast('Invalid hex data', 'error'); return; }

  const res = await apiPost('/api/register/write', { page, address, data });
  const dumpEl = document.getElementById('hex-dump');

  if (res.status === 'ok') {
    dumpEl.textContent = `Written ${res.data.bytes_written} byte(s) to page 0x${page.toString(16).toUpperCase()} addr 0x${address.toString(16).toUpperCase().padStart(2,'0')}`;
    toast(`Written ${res.data.bytes_written} byte(s)`, 'success');
  } else {
    dumpEl.textContent = `Error: ${res.message}`;
    toast(`Write error: ${res.message}`, 'error');
  }
}

function formatHexDump(byteArray, baseAddr) {
  const ROW = 8;
  let out = '';
  for (let i = 0; i < byteArray.length; i += ROW) {
    const slice = byteArray.slice(i, i + ROW);
    const addrStr = `0x${(baseAddr + i).toString(16).toUpperCase().padStart(2, '0')}`;
    const hex = slice.map(b => b.toString(16).toUpperCase().padStart(2, '0')).join(' ').padEnd(ROW * 3 - 1, ' ');
    const asc = slice.map(b => (b >= 32 && b < 127) ? String.fromCharCode(b) : '.').join('');
    out += `${addrStr}: ${hex}  ${asc}\n`;
  }
  return out || '(no data)';
}

// ---------------------------------------------------------------------------
// PRBS pattern names
// ---------------------------------------------------------------------------
const PRBS_PATTERNS = [
  'PRBS31Q','PRBS31','PRBS23Q','PRBS23','PRBS15Q','PRBS15',
  'PRBS13Q','PRBS13','PRBS9Q','PRBS9','PRBS7Q','PRBS7','SSPRQ',
];

function formatBer(ber) {
  if (ber == null) return '—';
  if (ber === 0) return '&lt; 1e-15';
  if (!isFinite(ber)) return '—';
  return ber.toExponential(2);
}

// ---------------------------------------------------------------------------
// Flags (Monitoring tab)
// ---------------------------------------------------------------------------
function renderFlags(lanes) {
  const tbody = document.getElementById('tbl-flags');
  if (!tbody || !lanes) return;

  tbody.innerHTML = lanes.map(lane => {
    function flagCell(val, isAlarm) {
      if (val) {
        return isAlarm
          ? '<span class="flag-active">&#9632; ALARM</span>'
          : '<span class="flag-warn">&#9650; WARN</span>';
      }
      return '<span class="flag-ok">&#9679;</span>';
    }
    const txFault   = flagCell(lane.tx_fault, true);
    const txLos     = flagCell(lane.tx_los, true);
    const txCdrLol  = flagCell(lane.tx_cdr_lol, true);
    const rxLos     = flagCell(lane.rx_los, true);
    const rxCdrLol  = flagCell(lane.rx_cdr_lol, true);

    const anyAlarm = lane.tx_power_high_alarm || lane.tx_power_low_alarm ||
                     lane.tx_bias_high_alarm  || lane.tx_bias_low_alarm  ||
                     lane.rx_power_high_alarm || lane.rx_power_low_alarm;
    const anyWarn  = lane.tx_power_high_warn  || lane.tx_power_low_warn  ||
                     lane.tx_bias_high_warn   || lane.tx_bias_low_warn   ||
                     lane.rx_power_high_warn  || lane.rx_power_low_warn;
    const summary = anyAlarm
      ? '<span class="flag-active">&#9632; Alarm</span>'
      : anyWarn
      ? '<span class="flag-warn">&#9650; Warn</span>'
      : '<span class="flag-ok">&#9679;</span>';

    return `<tr>
      <td>${lane.lane}</td>
      <td>${txFault}</td>
      <td>${txLos}</td>
      <td>${txCdrLol}</td>
      <td>${rxLos}</td>
      <td>${rxCdrLol}</td>
      <td>${summary}</td>
    </tr>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// Thresholds (Monitoring tab)
// ---------------------------------------------------------------------------
async function loadThresholds() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/thresholds');
  if (res.status !== 'ok') { toast(`Thresholds error: ${res.message}`, 'error'); return; }
  const d = res.data;
  const tbody = document.getElementById('tbl-thresholds');
  if (!tbody) return;

  // [label, page/addr, ha, la, hw, lw]
  const rows = [
    ['Temperature (°C)', '02h / 0x80–0x87', d.temp_high_alarm, d.temp_low_alarm, d.temp_high_warn, d.temp_low_warn],
    ['Vcc (V)',          '02h / 0x88–0x8F', d.vcc_high_alarm,  d.vcc_low_alarm,  d.vcc_high_warn,  d.vcc_low_warn],
    ['Tx Power (dBm)',   '02h / 0xB0–0xB7', d.tx_power_high_alarm_dbm, d.tx_power_low_alarm_dbm, d.tx_power_high_warn_dbm, d.tx_power_low_warn_dbm],
    ['Tx Bias (mA)',     '02h / 0xB8–0xBF', d.tx_bias_high_alarm_ma,   d.tx_bias_low_alarm_ma,   d.tx_bias_high_warn_ma,   d.tx_bias_low_warn_ma],
    ['Rx Power (dBm)',   '02h / 0xC0–0xC7', d.rx_power_high_alarm_dbm, d.rx_power_low_alarm_dbm, d.rx_power_high_warn_dbm, d.rx_power_low_warn_dbm],
  ];

  tbody.innerHTML = rows.map(([label, reg, ha, la, hw, lw]) =>
    `<tr>
      <td style="color:var(--text-muted)">${label}</td>
      <td class="td-addr">${reg}</td>
      <td class="ha">${ha}</td>
      <td class="la">${la}</td>
      <td class="hw">${hw}</td>
      <td class="lw">${lw}</td>
    </tr>`
  ).join('');
}

// ---------------------------------------------------------------------------
// Squelch / Output Controls (DataPath tab)
// ---------------------------------------------------------------------------
function _mkCheckbox(id) {
  return `<input type="checkbox" id="${id}">`;
}

function _populateBitmaskRow(prefix, mask) {
  for (let i = 0; i < 8; i++) {
    const td = document.getElementById(`${prefix}-td-${i}`);
    if (!td) continue;
    td.innerHTML = _mkCheckbox(`${prefix}-cb-${i}`);
    document.getElementById(`${prefix}-cb-${i}`).checked = !!((mask >> i) & 1);
  }
}

function _readBitmaskRow(prefix) {
  let mask = 0;
  for (let i = 0; i < 8; i++) {
    const cb = document.getElementById(`${prefix}-cb-${i}`);
    if (cb && cb.checked) mask |= (1 << i);
  }
  return mask;
}

async function loadSquelch() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/squelch');
  if (res.status !== 'ok') { toast(`Squelch error: ${res.message}`, 'error'); return; }
  _populateBitmaskRow('sq', res.data.tx_squelch_disable);
  _populateBitmaskRow('sf', res.data.tx_squelch_force);
  _populateBitmaskRow('od', res.data.rx_output_disable);
  _populateBitmaskRow('rd', res.data.rx_squelch_disable);
}

async function applySquelch() {
  const res = await apiPost('/api/module/squelch', {
    tx_squelch_disable: _readBitmaskRow('sq'),
    tx_squelch_force:   _readBitmaskRow('sf'),
    rx_output_disable:  _readBitmaskRow('od'),
    rx_squelch_disable: _readBitmaskRow('rd'),
  });
  if (res.status === 'ok') toast('Output controls applied', 'success');
  else toast(`Apply failed: ${res.message}`, 'error');
}

// ---------------------------------------------------------------------------
// Loopback (Diagnostics tab)
// ---------------------------------------------------------------------------
function _populateLoopbackRow(prefix, mask) {
  for (let i = 0; i < 8; i++) {
    const td = document.getElementById(`lb-${prefix}-${i}`);
    if (!td) continue;
    td.innerHTML = _mkCheckbox(`lb-cb-${prefix}-${i}`);
    document.getElementById(`lb-cb-${prefix}-${i}`).checked = !!((mask >> i) & 1);
  }
}

function _readLoopbackRow(prefix) {
  let mask = 0;
  for (let i = 0; i < 8; i++) {
    const cb = document.getElementById(`lb-cb-${prefix}-${i}`);
    if (cb && cb.checked) mask |= (1 << i);
  }
  return mask;
}

async function loadLoopback() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/loopback');
  if (res.status !== 'ok') { toast(`Loopback error: ${res.message}`, 'error'); return; }
  _populateLoopbackRow('mso', res.data.media_side_output);
  _populateLoopbackRow('msi', res.data.media_side_input);
  _populateLoopbackRow('hso', res.data.host_side_output);
  _populateLoopbackRow('hsi', res.data.host_side_input);
}

async function applyLoopback() {
  const res = await apiPost('/api/module/loopback', {
    media_side_output: _readLoopbackRow('mso'),
    media_side_input:  _readLoopbackRow('msi'),
    host_side_output:  _readLoopbackRow('hso'),
    host_side_input:   _readLoopbackRow('hsi'),
  });
  if (res.status === 'ok') toast('Loopback configuration applied', 'success');
  else toast(`Apply failed: ${res.message}`, 'error');
}

// ---------------------------------------------------------------------------
// PRBS (Diagnostics tab)
// ---------------------------------------------------------------------------
function _renderPrbsTable(tbodyId, block, lolMask) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  const isChecker = (lolMask !== undefined);
  tbody.innerHTML = Array.from({length: 8}, (_, i) => {
    const en  = !!((block.enable_mask    >> i) & 1);
    const inv = !!((block.invert_mask    >> i) & 1);
    const sw  = !!((block.byte_swap_mask >> i) & 1);
    const fec = !!((block.fec_mask       >> i) & 1);
    const pattern = block.patterns[i] || 0;
    const patOpts = PRBS_PATTERNS.map((name, idx) =>
      `<option value="${idx}" ${pattern === idx ? 'selected' : ''}>${name}</option>`
    ).join('');
    let lolCell = '';
    if (isChecker) {
      const lol = !!((lolMask >> i) & 1);
      lolCell = `<td>${lol ? '<span class="flag-active">LOL</span>' : '<span class="flag-ok">●</span>'}</td>`;
    }
    return `<tr>
      <td>L${i+1}</td>
      <td><input type="checkbox" id="${tbodyId}-en-${i}"  ${en  ? 'checked' : ''}></td>
      <td><input type="checkbox" id="${tbodyId}-inv-${i}" ${inv ? 'checked' : ''}></td>
      <td><input type="checkbox" id="${tbodyId}-sw-${i}"  ${sw  ? 'checked' : ''}></td>
      <td><input type="checkbox" id="${tbodyId}-fec-${i}" ${fec ? 'checked' : ''}></td>
      <td><select class="app-select-input" id="${tbodyId}-pat-${i}">${patOpts}</select></td>
      ${lolCell}
    </tr>`;
  }).join('');
}

function _readPrbsSection(tbodyId) {
  let en = 0, inv = 0, sw = 0, fec = 0;
  const patterns = [];
  for (let i = 0; i < 8; i++) {
    if (document.getElementById(`${tbodyId}-en-${i}`)?.checked)  en  |= (1 << i);
    if (document.getElementById(`${tbodyId}-inv-${i}`)?.checked) inv |= (1 << i);
    if (document.getElementById(`${tbodyId}-sw-${i}`)?.checked)  sw  |= (1 << i);
    if (document.getElementById(`${tbodyId}-fec-${i}`)?.checked) fec |= (1 << i);
    const sel = document.getElementById(`${tbodyId}-pat-${i}`);
    patterns.push(sel ? parseInt(sel.value, 10) : 0);
  }
  return { enable_mask: en, invert_mask: inv, byte_swap_mask: sw, fec_mask: fec, patterns };
}

async function loadPrbs() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/prbs');
  if (res.status !== 'ok') { toast(`PRBS error: ${res.message}`, 'error'); return; }
  const d = res.data;
  _renderPrbsTable('tbl-prbs-host-gen',  d.host_gen);
  _renderPrbsTable('tbl-prbs-media-gen', d.media_gen);
  _renderPrbsTable('tbl-prbs-host-chk',  d.host_chk,  d.host_chk_lol_mask);
  _renderPrbsTable('tbl-prbs-media-chk', d.media_chk, d.media_chk_lol_mask);
}

async function applyPrbs() {
  const res = await apiPost('/api/module/prbs', {
    host_gen:  _readPrbsSection('tbl-prbs-host-gen'),
    media_gen: _readPrbsSection('tbl-prbs-media-gen'),
    host_chk:  _readPrbsSection('tbl-prbs-host-chk'),
    media_chk: _readPrbsSection('tbl-prbs-media-chk'),
  });
  if (res.status === 'ok') toast('PRBS configuration applied', 'success');
  else toast(`Apply failed: ${res.message}`, 'error');
}

// ---------------------------------------------------------------------------
// BER (Diagnostics tab)
// ---------------------------------------------------------------------------
async function loadBer() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/ber');
  const tbody = document.getElementById('tbl-ber');
  if (!tbody) return;
  if (res.status !== 'ok') { toast(`BER error: ${res.message}`, 'error'); return; }
  const hostCells = res.data.lanes.map(l => `<td>${formatBer(l.host_ber)}</td>`).join('');
  const mediaCells = res.data.lanes.map(l => `<td>${formatBer(l.media_ber)}</td>`).join('');
  tbody.innerHTML =
    `<tr><td style="color:var(--text-muted)">Host<span class="reg-badge">14h/0xC0</span></td>${hostCells}</tr>` +
    `<tr><td style="color:var(--text-muted)">Media<span class="reg-badge">14h/0xD0</span></td>${mediaCells}</tr>`;
}

// ---------------------------------------------------------------------------
// Error / Bit Counters (Diagnostics tab)
// ---------------------------------------------------------------------------
async function loadCounters() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/counters');
  const tbody = document.getElementById('tbl-counters');
  if (!tbody) return;
  if (res.status !== 'ok') { toast(`Counters error: ${res.message}`, 'error'); return; }
  const lanes = res.data.lanes;
  const fmtCount = (v) => v != null ? v.toLocaleString() : '—';
  const fmtBer = (v) => v != null && v > 0 ? v.toExponential(2) : '—';

  const hostErrCells = lanes.map(l => `<td>${fmtCount(l.host_error_count)}</td>`).join('');
  const hostBitCells = lanes.map(l => `<td>${fmtCount(l.host_total_bits)}</td>`).join('');
  const hostBerCells = lanes.map(l => `<td>${fmtBer(l.host_ber)}</td>`).join('');
  const mediaErrCells = lanes.map(l => `<td>${fmtCount(l.media_error_count)}</td>`).join('');
  const mediaBitCells = lanes.map(l => `<td>${fmtCount(l.media_total_bits)}</td>`).join('');
  const mediaBerCells = lanes.map(l => `<td>${fmtBer(l.media_ber)}</td>`).join('');

  tbody.innerHTML = `
    <tr><td style="color:var(--text-muted)">Host Errors</td>${hostErrCells}</tr>
    <tr><td style="color:var(--text-muted)">Host Total Bits</td>${hostBitCells}</tr>
    <tr><td style="color:var(--text-muted)">Host BER (calc)</td>${hostBerCells}</tr>
    <tr><td style="color:var(--text-muted)">Media Errors</td>${mediaErrCells}</tr>
    <tr><td style="color:var(--text-muted)">Media Total Bits</td>${mediaBitCells}</tr>
    <tr><td style="color:var(--text-muted)">Media BER (calc)</td>${mediaBerCells}</tr>`;
}

// ---------------------------------------------------------------------------
// Laser Tuning (Diagnostics tab)
// ---------------------------------------------------------------------------
let _laserData = null;

async function loadLaser() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/laser');
  const tbody = document.getElementById('tbl-laser');
  const capsEl = document.getElementById('laser-caps');
  if (!tbody) return;
  if (res.status !== 'ok') {
    tbody.innerHTML = `<tr><td colspan="7" class="placeholder-text">Laser tuning not available: ${res.message}</td></tr>`;
    return;
  }
  _laserData = res.data;
  const d = res.data;

  // Non-tunable module: Page 04h reads all zeros → no grids advertised
  if (!d.grids_supported || d.grids_supported.length === 0) {
    if (capsEl) {
      capsEl.innerHTML = '<span style="color:var(--dim)">Not a tunable laser module (Media Interface Technology is not C-band/L-band); Page 04h tuning capabilities not advertised.</span>';
    }
    tbody.innerHTML = '<tr><td colspan="7" class="placeholder-text">Non-tunable module — no Page 04h/12h data.</td></tr>';
    return;
  }

  if (capsEl) {
    capsEl.innerHTML =
      `Grids: <b>${d.grids_supported.join(', ')}</b> | ` +
      `Fine Tuning: <b>${d.fine_tuning_supported ? 'Yes' : 'No'}</b>` +
      (d.fine_tuning_supported ? ` (res ${d.fine_resolution_ghz} GHz, range ${d.fine_range_ghz[0]}..${d.fine_range_ghz[1]} GHz)` : '') +
      ` | Power: <b>${d.power_range_dbm[0]}..${d.power_range_dbm[1]} dBm</b>`;
  }

  const gridOpts = (cur) => [
    [5, '100 GHz'], [4, '50 GHz'], [3, '25 GHz'], [7, '75 GHz'],
    [6, '33 GHz'], [2, '12.5 GHz'], [1, '6.25 GHz'], [0, '3.125 GHz'], [8, '150 GHz'],
  ].map(([code, name]) =>
    `<option value="${code}" ${cur === code ? 'selected' : ''}>${name}</option>`
  ).join('');

  tbody.innerHTML = d.lanes.map(l => {
    const lockIcon = l.wavelength_locked
      ? '<span class="flag-ok">Locked</span>'
      : l.tuning_in_progress
      ? '<span class="flag-warn">Tuning...</span>'
      : '<span class="flag-active">Unlocked</span>';
    return `<tr>
      <td>${l.lane}</td>
      <td><select class="app-select-input" id="laser-grid-${l.lane}">${gridOpts(l.grid_code)}</select></td>
      <td><input type="number" id="laser-ch-${l.lane}" value="${l.channel}" style="width:70px" class="raw-data-input"></td>
      <td><input type="number" id="laser-ft-${l.lane}" value="${l.fine_offset_ghz}" step="0.001" style="width:80px" class="raw-data-input"></td>
      <td style="font-family:var(--font-mono)">${l.frequency_thz.toFixed(6)}</td>
      <td><input type="number" id="laser-pwr-${l.lane}" value="${l.target_power_dbm}" step="0.01" style="width:70px" class="raw-data-input"></td>
      <td>${lockIcon}</td>
    </tr>`;
  }).join('');
}

async function applyLaser() {
  const lanes = [];
  for (let i = 1; i <= 8; i++) {
    const gridSel = document.getElementById(`laser-grid-${i}`);
    const chInput = document.getElementById(`laser-ch-${i}`);
    const ftInput = document.getElementById(`laser-ft-${i}`);
    const pwrInput = document.getElementById(`laser-pwr-${i}`);
    if (!gridSel) continue;
    const fineOffset = parseFloat(ftInput.value);
    lanes.push({
      lane: i,
      grid_code: parseInt(gridSel.value, 10),
      channel: parseInt(chInput.value, 10) || 0,
      fine_tuning_enabled: !Number.isNaN(fineOffset) && fineOffset !== 0,
      fine_offset_ghz: Number.isNaN(fineOffset) ? 0 : fineOffset,
      target_power_dbm: parseFloat(pwrInput.value) || 0,
    });
  }
  const res = await apiPost('/api/module/laser', { lanes });
  if (res.status === 'ok') {
    toast('Laser tuning applied', 'success');
    setTimeout(loadLaser, 300);
  } else {
    toast(`Apply failed: ${res.message}`, 'error');
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  // Load backends
  loadBackends();

  // Connect / Disconnect buttons
  document.getElementById('btn-connect').addEventListener('click', connectModule);
  document.getElementById('btn-disconnect').addEventListener('click', disconnectModule);
  document.getElementById('btn-refresh-backends').addEventListener('click', loadBackends);

  // Tab buttons
  document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!btn.disabled) switchTab(btn.dataset.tab);
    });
  });

  // Info refresh
  document.getElementById('btn-refresh-info')?.addEventListener('click', loadInfo);

  // Monitoring interval selector
  document.getElementById('sel-refresh-interval')?.addEventListener('change', e => {
    const v = e.target.value;
    if (v === 'manual') {
      AppState.monitoringManual = true;
      stopMonitoring();
    } else {
      AppState.monitoringManual = false;
      setRefreshInterval(parseInt(v, 10));
    }
  });
  document.getElementById('btn-refresh-monitor')?.addEventListener('click', loadMonitoring);

  // DataPath apply
  document.getElementById('btn-apply-datapath')?.addEventListener('click', applyDatapath);
  document.getElementById('btn-refresh-datapath')?.addEventListener('click', loadDatapath);

  // Thresholds
  document.getElementById('btn-refresh-thresholds')?.addEventListener('click', loadThresholds);

  // Squelch / output controls
  document.getElementById('btn-refresh-squelch')?.addEventListener('click', loadSquelch);
  document.getElementById('btn-apply-squelch')?.addEventListener('click', applySquelch);

  // Loopback
  document.getElementById('btn-refresh-loopback')?.addEventListener('click', loadLoopback);
  document.getElementById('btn-apply-loopback')?.addEventListener('click', applyLoopback);

  // PRBS
  document.getElementById('btn-refresh-prbs')?.addEventListener('click', loadPrbs);
  document.getElementById('btn-apply-prbs')?.addEventListener('click', applyPrbs);

  // BER, SNR, Counters, Laser
  document.getElementById('btn-refresh-ber')?.addEventListener('click', loadBer);
  document.getElementById('btn-refresh-snr')?.addEventListener('click', loadSnr);
  document.getElementById('btn-refresh-counters')?.addEventListener('click', loadCounters);
  document.getElementById('btn-refresh-laser')?.addEventListener('click', loadLaser);
  document.getElementById('btn-apply-laser')?.addEventListener('click', applyLaser);

  // Module Control & Applications
  document.getElementById('btn-refresh-control')?.addEventListener('click', loadModuleControl);
  document.getElementById('btn-refresh-apps')?.addEventListener('click', loadApplications);

  // Raw register read/write
  document.getElementById('btn-raw-read')?.addEventListener('click', rawRead);
  document.getElementById('btn-raw-write')?.addEventListener('click', rawWrite);

  // Set initial UI state
  updateConnectionUI(false, '');
});
