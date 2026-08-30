/* CMIS Module Manager — Frontend Logic */
'use strict';

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------
// Marks a field CMIS 5.4 introduced. Which fields those are comes from the
// server (/api/module/capabilities -> new_in_5_4), so the list is never
// retyped here; this is only how it looks.
const NEW54 = '<span class="badge-new54" title="CMIS 5.4 新增字段">5.4 新增</span>';

const AppState = {
  connected: false,
  currentTab: 'info',
  monitoringInterval: null,
  monitoringIntervalMs: 2000,
  monitoringManual: false,
  backendsCache: [],   // cached result of /api/backends
  // Lanes the connected module actually has. Eight until it says otherwise;
  // CMIS 5.4 allows up to 256, and every lane loop below sizes from this.
  lanes: 8,
  caps: {},
};

// Fallback optical power limits (dBm), used only until the module's own
// Page 02h thresholds have been read. Colouring by these when the module
// advertises different limits would contradict both the Thresholds card and
// the module's own alarm flags.
const ALARM_FALLBACK = { TX_LOW: -10, TX_HIGH: 3, RX_LOW: -10, RX_HIGH: 3 };
let _moduleThresholds = null;
let _advertisedApps = [];

// ---------------------------------------------------------------------------
// Register hover tooltips
//
// Every setting on screen maps to a byte in the module's memory. These build a
// consistent hover string naming the CMIS field, its Page and byte address, and
// what that byte reads right now, so any displayed value can be traced back to
// the register it came from without cross-referencing the spec.
// ---------------------------------------------------------------------------
/** Escape for interpolation into HTML text or a quoted attribute. */
const esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const hex8  = v => '0x' + (v & 0xFF).toString(16).toUpperCase().padStart(2, '0');
const bin8  = v => '0b' + (v & 0xFF).toString(2).padStart(8, '0');
const pageName = p => (p === null || p === undefined)
  ? 'Lower Memory'
  : `Page ${p.toString(16).toUpperCase().padStart(2, '0')}h`;

/**
 * @param {object} o
 * @param {string} o.field  CMIS field name, e.g. "AutoSquelchDisableTx"
 * @param {?number} o.page  page number, null/undefined for Lower Memory
 * @param {number} o.addr   byte address
 * @param {number} [o.value] current raw byte
 * @param {number} [o.bit]   bit index within the byte, when the control is one bit
 * @param {string} [o.note]  extra line, e.g. decoded meaning or units
 */
function regTip(o) {
  const lines = [o.field,
    `${pageName(o.page)} · byte ${hex8(o.addr)} hex = ${o.addr} dec`];
  if (o.value !== undefined && o.value !== null && !Number.isNaN(o.value)) {
    const v = o.value & 0xFF;
    lines.push(`Current value: ${hex8(v)} hex = ${bin8(v)} bin = ${v} dec`);
    if (o.bit !== undefined && o.bit !== null) {
      lines.push(`Bit ${o.bit} = ${(o.value >> o.bit) & 1}`);
    }
  }
  if (o.note) lines.push(o.note);
  return lines.join('\n');
}

/**
 * Write, then re-read the panel from the module.
 *
 * A module may reject or clamp a control value (CMIS lets it report
 * ConfigRejected), so the panel must show what the device actually holds
 * afterwards, not what we asked for. Re-reading also refreshes the register
 * tooltips, which quote the current byte.
 */
async function applyAndReload(label, path, body, reload) {
  const res = await apiPost(path, body);
  if (res.status !== 'ok') {
    toast(`Apply failed: ${res.message}`, 'error');
    return res;
  }
  await reload();
  toast(`${label} applied`, 'success');
  return res;
}

/** Multi-byte field (e.g. a 16-byte ASCII string or a 2-byte word). */
function regTipRange(field, page, addr, len, note) {
  const end = addr + len - 1;
  const lines = [field,
    `${pageName(page)} · bytes ${hex8(addr)}-${hex8(end)} hex = ${addr}-${end} dec`];
  if (note) lines.push(note);
  return lines.join('\n');
}

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
// Display preferences
// ---------------------------------------------------------------------------
// Everything lands on <html>: the post-update screen replaces document.body
// wholesale, and anything parked there would go with it. The same key is read
// by the inline bootstrap in index.html, which applies all of this before the
// first paint; this module only handles later changes.
const PREFS_KEY = 'cmis.ui';
const PREFS_DEFAULT = Object.freeze({
  v: 1, theme: 'midnight', scale: 1, scaleAuto: true, fontStep: 0, fontSans: 'system',
});

// innerWidth is CSS pixels, so Windows DPI scaling and browser zoom are already
// folded in - it measures usable workspace, not pixel density. Multiplying by
// devicePixelRatio on top would double-count and blow up a 125%-scaled laptop.
// Only ever scales up: a small viewport means a small screen, and shrinking the
// text there is backwards.
function autoScale(width) {
  if (width >= 3000) return 1.5;
  if (width >= 2400) return 1.35;
  if (width >= 1900) return 1.15;
  return 1;
}

function loadPrefs() {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || 'null');
    if (raw && typeof raw === 'object') return { ...PREFS_DEFAULT, ...raw };
  } catch (e) { /* corrupt preferences must not cost the user the whole UI */ }
  return { ...PREFS_DEFAULT };
}

function savePrefs(prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch (e) {
    toast('Could not save display settings in this browser', 'error');
  }
}

function applyPrefs(prefs) {
  const root = document.documentElement;
  root.setAttribute('data-theme', prefs.theme);
  root.setAttribute('data-font-sans', prefs.fontSans);
  // The unit is not optional: calc(10px + 2) is invalid at computed-value time,
  // which would silently collapse every font-size to its inherited value.
  root.style.setProperty('--fs-step', prefs.fontStep + 'px');
  root.style.setProperty(
    '--ui-zoom', prefs.scaleAuto ? autoScale(window.innerWidth) : prefs.scale);
}

function initSettings() {
  const dialog = document.getElementById('settings-dialog');
  const theme = document.getElementById('set-theme');
  const scale = document.getElementById('set-scale');
  const step = document.getElementById('set-fontstep');
  const font = document.getElementById('set-font');
  if (!dialog) return;

  let prefs = loadPrefs();

  const toForm = () => {
    theme.value = prefs.theme;
    scale.value = prefs.scaleAuto ? 'auto' : String(prefs.scale);
    step.value = String(prefs.fontStep);
    font.value = prefs.fontSans;
  };
  const commit = () => { applyPrefs(prefs); savePrefs(prefs); };

  applyPrefs(prefs);
  toForm();

  // Applied live, saved immediately: a single-user local tool has no reason to
  // make someone confirm what they can already see.
  theme.addEventListener('change', () => { prefs.theme = theme.value; commit(); });
  font.addEventListener('change', () => { prefs.fontSans = font.value; commit(); });
  step.addEventListener('change', () => { prefs.fontStep = parseInt(step.value, 10); commit(); });
  scale.addEventListener('change', () => {
    prefs.scaleAuto = scale.value === 'auto';
    if (!prefs.scaleAuto) prefs.scale = parseFloat(scale.value);
    commit();
  });

  document.getElementById('btn-settings-reset')?.addEventListener('click', () => {
    prefs = { ...PREFS_DEFAULT };
    commit();
    toForm();
  });

  document.getElementById('btn-settings')?.addEventListener('click', () => {
    toForm();
    dialog.showModal();
  });

  // Clicking the backdrop lands on the dialog element itself, not the form.
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
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
  AppState.lanes = res.data?.lanes || 8;
  const caps = await apiGet('/api/module/capabilities');
  AppState.caps = caps.status === 'ok' ? caps.data : {};
  rebuildLaneColumns();
  updateConnectionUI(true, `${backend}  bus:${bus}  addr:0x${address.toString(16).toUpperCase()}`
    + (AppState.lanes > 8 ? `  ${AppState.lanes} lanes` : ''));
  updateBackendInfoArea(backend);
  toast('Connected', 'success');
  // Auto-load info tab
  switchTab('info');
  loadInfo();
}

async function disconnectModule() {
  await apiGet('/api/disconnect');
  AppState.connected = false;
  AppState.lanes = 8;
  AppState.caps = {};
  // The next module advertises its own limits and its own Applications.
  _moduleThresholds = null;
  _advertisedApps = [];
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

  // The Output Controls and Loopback checkboxes live in cells the table sweep
  // above does not touch, and the summary line keeps its last reading. Without
  // this, reconnecting to a different module briefly shows the previous one's
  // squelch settings, temperature and voltage as if they were the new module's.
  ['sq', 'sf', 'od', 'rd'].forEach(p => {
    for (let i = 0; i < 8; i++) {
      const td = document.getElementById(`${p}-td-${i}`);
      if (td) td.innerHTML = '';
    }
  });
  ['mso', 'msi', 'hso', 'hsi'].forEach(p => {
    for (let i = 0; i < 8; i++) {
      const td = document.getElementById(`lb-${p}-${i}`);
      if (td) td.innerHTML = '';
    }
  });
  const summary = document.getElementById('monitor-summary');
  if (summary) summary.innerHTML = '';
  clearMonitoringStale();
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
  // The AppSelect dropdown is built from the advertised Applications, so they
  // must be in hand before the DataPath table renders.
  if (name === 'datapath') {
    loadModuleControl();
    loadSquelch();
    loadApplications().then(loadDatapath);
  }
  // Load the whole Diagnostics tab, not half of it: BER, SNR, counters and
  // laser tuning used to sit empty until each card's own refresh was clicked,
  // so half the page showed live data next to placeholders or values from
  // before a reset.
  if (name === 'diagnostics') {
    loadLoopback(); loadPrbs(); loadBer(); loadSnr(); loadCounters(); loadLaser();
  }
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

  // Strings here come straight out of module EEPROM, so escape them rather
  // than trusting a device to hand back clean ASCII.
  const d = Object.fromEntries(Object.entries(infoRes.data)
    .map(([k, v]) => [k, typeof v === 'string' ? esc(v) : v]));
  const s = Object.fromEntries(Object.entries(statusRes.data)
    .map(([k, v]) => [k, typeof v === 'string' ? esc(v) : v]));
  const c = AppState.caps || {};

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
    ['Heatsink Type',   c.heatsink_type ? `Type ${c.heatsink_type} (SFF-8024)` : '— (not specified)', 'Lower', '0x3D[7:4]', 'SFF8024HeatsinkType (Table 8-18)', true],
    ['Module Lanes',    `${c.max_lanes || 8}  (${c.banks_supported || 1} bank${(c.banks_supported||1) > 1 ? 's' : ''})`, '01h', '0x8E[1:0]', 'BanksSupported; 11b escapes to 01h:174 for up to 256 lanes', (c.max_lanes || 8) > 32],
    ['Default Polarity', polaritySummary(c.default_polarity), '01h', '0xAB–0xAC', 'DefaultInputPolarityTx / DefaultOutputPolarityRx (Table 8-57)', true],
    ['Media Lane Switching', c.media_lane_switching_supported ? 'Supported' : 'Not supported', '01h', '0xFC[5]', 'MediaLaneSwitchingSupported (Table 8-62)', true],
    ['Extra Pages',     extraPagesSummary(c), '01h', '0xAD–0xAE', 'Pages 0Ch/0Dh/60h/61h/62h advertisement (Table 8-58)', true],
    ['Host Lanes',      d.lanes_detail ? `${d.host_lanes} <span style="color:var(--text-muted);font-size:var(--fs-xs)">(${d.lanes_detail})</span>` : `${d.host_lanes}`,  'Lower', '0x56+', 'Max concurrent host lanes across all AppDescriptors'],
    ['Media Lanes',     `${d.media_lanes}`, 'Lower', '0x56+', 'Max concurrent media lanes'],
    ['FW Revision',     d.fw_revision,                                                            'Lower', '0x27–0x28',   'Module Active Firmware Major.Minor'],
    ['HW Revision',     d.hw_revision,                                                            '01h',   '0x82–0x83',   'Hardware Revision Major.Minor'],
    ['Temperature',     `${s.temperature_c?.toFixed(2)} °C`,                                     'Lower', '0x0E–0x0F',   'Module Temperature (s16/256)'],
    ['Supply Voltage',  `${s.voltage_v?.toFixed(4)} V`,                                          'Lower', '0x10–0x11',   'Supply Voltage (u16 × 100 µV)'],
    ['Alarms',          s.alarm_active ? '<span class="text-danger">Active</span>' : '<span class="text-success">None</span>', 'Lower', '0x08–0x0D', 'Module-Level Flags'],
  ];

  tbody.innerHTML = rows.map(([k, v, pg, addr, def, since]) => {
    const plain = String(v).replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
    const tip = esc([
      k,
      `${pg === 'Lower' ? 'Lower Memory' : 'Page ' + pg} · byte ${addr}`,
      `Current: ${plain}`,
      def,
    ].join('\n'));
    return `<tr title="${tip}">
      <td style="color:var(--text-muted);width:140px">${k}${since ? NEW54 : ''}</td>
      <td>${v}</td>
      <td class="td-page">${pg}</td>
      <td class="td-addr">${addr}</td>
      <td class="td-def">${def}</td>
    </tr>`;
  }).join('');
}

function rebuildLaneColumns() {
  // Five tables lay lanes out as columns, with L1..L8 written into the HTML.
  // A 16-lane module fills sixteen data cells under eight headings otherwise:
  // every reading after the eighth lines up under the wrong column.
  document.querySelectorAll('tr[data-lane-cols="1"]').forEach(tr => {
    const first = tr.cells[0];
    tr.innerHTML = '';
    tr.appendChild(first);
    for (let i = 1; i <= AppState.lanes; i++) {
      const th = document.createElement('th');
      th.textContent = `L${i}`;
      tr.appendChild(th);
    }
  });
}

function polaritySummary(list) {
  if (!Array.isArray(list) || !list.length) return '—';
  const tx = list.filter(l => l.input_tx_inverted).map(l => l.lane);
  const rx = list.filter(l => l.output_rx_inverted).map(l => l.lane);
  if (!tx.length && !rx.length) return 'All regular';
  const part = [];
  if (tx.length) part.push(`Tx inverted: ${tx.join(', ')}`);
  if (rx.length) part.push(`Rx inverted: ${rx.join(', ')}`);
  return part.join(' · ');
}

function extraPagesSummary(c) {
  const on = [['0Ch', c.page_0ch_supported], ['0Dh', c.page_0dh_supported],
              ['60h', c.page_60h_supported], ['61h', c.page_61h_supported],
              ['62h', c.page_62h_supported]].filter(x => x[1]).map(x => x[0]);
  return on.length ? on.join(', ') : '— (none advertised)';
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

// ---------------------------------------------------------------------------
// Update check
//
// Only ever triggered by the button: this tool runs on isolated lab networks
// and must not reach out to GitHub on its own.
// ---------------------------------------------------------------------------
async function checkForUpdate() {
  const btn = document.getElementById('btn-check-update');
  if (!btn) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Checking…';

  const res = await apiGet('/api/update/check');
  btn.disabled = false;

  if (res.status !== 'ok') {
    btn.textContent = label;
    toast(res.message, 'error', 9000);
    return;
  }

  const d = res.data;
  if (!d.update_available) {
    btn.textContent = label;
    btn.classList.remove('update-available');
    toast(`Already on the newest version (v${d.current_version})`, 'success');
    return;
  }

  btn.textContent = `↑ v${d.latest_version}`;
  btn.classList.add('update-available');

  const mb = (d.asset_size / 1048576).toFixed(1);
  if (!d.can_self_update) {
    // Running from source: replacing an executable is meaningless here.
    toast(`v${d.latest_version} is available. Running from source — `
          + 'use git pull to upgrade.', 'success', 9000);
    window.open(d.release_url, '_blank', 'noopener');
    return;
  }

  const ok = confirm(
    `A newer version is available.\n\n`
    + `Installed: v${d.current_version}\nAvailable: v${d.latest_version}  (${mb} MB)\n\n`
    + `Download and install it now?\n\n`
    + `The tool closes once the files are replaced, then you start `
    + `CMIS_Module_Manager.exe again — one double-click.\n\n`
    + `Disconnect from the module first if a measurement is running.`);
  if (!ok) return;

  btn.disabled = true;
  btn.textContent = 'Updating…';
  toast(`Downloading v${d.latest_version} (${mb} MB)…`, 'success', 20000);

  const applied = await apiPost('/api/update/apply', {});
  if (applied.status !== 'ok') {
    btn.disabled = false;
    btn.textContent = `↑ v${d.latest_version}`;
    toast(applied.message, 'error', 12000);
    return;
  }

  const outcome = await _followUpdateProgress(btn);
  if (outcome.state === 'ready') {
    await _waitForNewVersion(applied.data.version);
  } else {
    btn.disabled = false;
    btn.textContent = `↑ v${d.latest_version}`;
    toast(outcome.message || 'The update did not complete', 'error', 12000);
  }
}

/**
 * Report the background download until it finishes, one way or the other.
 *
 * Sixteen megabytes takes seconds on a good link and the better part of an
 * hour on a slow one, and without a number on screen those two look identical
 * - the second one looks like a hang, and the natural response is to kill the
 * tool. So the button carries the percentage, and the source that won the
 * speed probe is named once it is known.
 */
async function _followUpdateProgress(btn) {
  let announced = '';
  for (;;) {
    await new Promise(r => setTimeout(r, 1000));
    let p;
    try {
      const r = await fetch('/api/update/progress', { cache: 'no-store' });
      p = (await r.json()).data;
    } catch (e) {
      // The old build exits as soon as the swap is handed over, so the poll
      // failing is the expected end of a successful update, not a fault.
      return { state: 'ready' };
    }
    if (p.state === 'ready' || p.state === 'error') return p;

    if (p.state === 'downloading' && p.total) {
      const pct = Math.floor(p.done / p.total * 100);
      btn.textContent = `Updating… ${pct}%`;
      if (p.source && p.source !== announced) {
        announced = p.source;
        toast(`Downloading from ${p.source}`, 'success', 6000);
      }
    } else {
      btn.textContent = p.state === 'probing' ? 'Picking a source…'
                      : p.state === 'verifying' ? 'Verifying…'
                      : p.state === 'installing' ? 'Installing…'
                      : 'Updating…';
    }
  }
}

/**
 * Hold the page while the install is swapped, then reload into the new build.
 *
 * The updated instance is started with the browser suppressed, so this tab is
 * the one the user keeps looking at - it has to come back on its own rather
 * than ask them to relaunch anything.
 */
async function _waitForNewVersion(expected) {
  // Say what to do straight away. Spinning for a minute first, on the chance
  // the process relaunches itself, just looks like the tool has hung — the
  // files are already updated by this point and one double-click finishes it.
  document.body.innerHTML =
    `<div style="display:flex;align-items:center;justify-content:center;`
    + `height:100%;font-family:var(--font-sans);text-align:center;padding:24px">`
    + `<div style="max-width:520px">`
    + `<div style="font-size:3em;line-height:1">✓</div>`
    + `<h2 style="color:var(--success-fg);margin:12px 0 4px">Updated to v${esc(expected)}</h2>`
    + `<p style="color:var(--accent-fg);font-size:var(--fs-h1);margin:18px 0 6px">`
    + `Start <b>CMIS_Module_Manager.exe</b> again to continue.</p>`
    + `<p style="color:var(--text-muted);line-height:1.7;font-size:var(--fs-md)">`
    + `The new version is already installed — the old console window has closed. `
    + `This page reconnects on its own if the tool comes back up.<br>`
    + `A record of the update is in <code>update.log</code> next to the exe.</p>`
    + `</div></div>`;

  // Keep watching quietly: if the relaunch did work, the user should not have
  // to do anything after all.
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const r = await fetch('/api/version', { cache: 'no-store' });
      const j = await r.json();
      if (j.status === 'ok' && j.data.version === expected) {
        location.reload();
        return;
      }
    } catch (e) { /* expected while the old instance is gone */ }
  }
}

/**
 * Optical power limits to colour by: the module's own advertised alarm
 * thresholds when they have been read, otherwise the generic fallback.
 *
 * A module can advertise anything - mock_dr8 alarms Tx low at -8.0 dBm, not
 * -10 - so colouring by a fixed pair made the monitoring table, the Thresholds
 * card and the module's Lane Flags give three different answers about the same
 * lane.
 */
function _powerLimits() {
  const t = _moduleThresholds;
  if (!t) return ALARM_FALLBACK;
  const num = (v, fb) => (typeof v === 'number' && Number.isFinite(v)) ? v : fb;
  return {
    TX_LOW:  num(t.tx_power_low_alarm_dbm,  ALARM_FALLBACK.TX_LOW),
    TX_HIGH: num(t.tx_power_high_alarm_dbm, ALARM_FALLBACK.TX_HIGH),
    RX_LOW:  num(t.rx_power_low_alarm_dbm,  ALARM_FALLBACK.RX_LOW),
    RX_HIGH: num(t.rx_power_high_alarm_dbm, ALARM_FALLBACK.RX_HIGH),
  };
}

/** Say plainly that what is on screen is no longer live. */
function markMonitoringStale(reason) {
  const el = document.getElementById('monitor-stale');
  if (el) {
    el.innerHTML = `⚠ Readings below are STALE — last refresh failed: `
      + `${esc(reason || 'unknown error')}`;
    el.style.display = '';
  }
  document.getElementById('tbl-monitoring')?.closest('table')
    ?.classList.add('stale-data');
  document.getElementById('tbl-flags')?.closest('table')
    ?.classList.add('stale-data');
}

function clearMonitoringStale() {
  const el = document.getElementById('monitor-stale');
  if (el) el.style.display = 'none';
  document.querySelectorAll('.stale-data')
    .forEach(node => node.classList.remove('stale-data'));
}

async function loadMonitoring() {
  if (!AppState.connected) return;

  const [monRes, statusRes, flagsRes] = await Promise.all([
    apiGet('/api/module/monitoring'),
    apiGet('/api/module/status'),
    apiGet('/api/module/flags'),
  ]);

  // Any failure here would otherwise leave the previous reading on screen while
  // the page still looks live - the operator would read minutes-old power
  // levels, or worse, all-green lane flags, as the current state.
  if (monRes.status !== 'ok') {
    toast(`Monitoring error: ${monRes.message}`, 'error');
    markMonitoringStale(monRes.message);
    stopMonitoring();  // halt auto-refresh so a dead server doesn't spam toasts
    return;
  }
  if (statusRes.status !== 'ok') {
    toast(`Status error: ${statusRes.message}`, 'error');
    markMonitoringStale(statusRes.message);
    return;
  }
  if (flagsRes.status === 'ok') {
    renderFlags(flagsRes.data.lanes);
    clearMonitoringStale();
  } else {
    toast(`Lane flags error: ${flagsRes.message}`, 'error');
    markMonitoringStale(flagsRes.message);
  }

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
    const lim = _powerLimits();
    const txCls = txDbm < lim.TX_LOW ? 'alarm-low' : txDbm > lim.TX_HIGH ? 'alarm-high' : '';
    const rxCls = rxDbm < lim.RX_LOW ? 'alarm-low' : rxDbm > lim.RX_HIGH ? 'alarm-high' : '';
    const stateClass = lane.datapath_state === 'Activated'
      ? 'state-activated' : lane.datapath_state === 'Init'
      ? 'state-init' : 'state-deactivated';

    const cfgStatus = lane.config_status || '—';
    // A rejected configuration is a failure, so it must not share the muted
    // grey used for "this lane is simply not in use".
    const cfgClass = cfgStatus === 'ConfigSuccess' ? 'state-activated'
                   : cfgStatus === 'ConfigInProgress' ? 'state-init'
                   : cfgStatus.startsWith('ConfigRejected') ? 'flag-active'
                   : 'state-deactivated';
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

  const d = res.data;
  tbody.innerHTML = d.lanes.map(lane => {
    const i = lane.lane - 1;
    // Offer only the Applications this module advertises. Listing 0-15 let
    // the user pick a code the module never announced, which it then rejects
    // in ConfigStatus - and AppSelCode 0 means "no application", not App 0.
    const opts = _advertisedApps.length
      ? _advertisedApps.map(a =>
          `<option value="${a.app_sel}" ${lane.app_select === a.app_sel ? 'selected' : ''}>`
          + `App ${a.app_sel} — ${hex8(a.host_if_id)}/${hex8(a.media_if_id)} `
          + `${a.host_lanes}H/${a.media_lanes}M</option>`)
      : Array.from({length: 15}, (_, i) =>
          `<option value="${i + 1}" ${lane.app_select === i + 1 ? 'selected' : ''}>App ${i + 1}</option>`);
    // A lane can sit on a code the module no longer advertises; keep it
    // visible rather than silently snapping the dropdown to another value.
    if (lane.app_select && !_advertisedApps.some(a => a.app_sel === lane.app_select)) {
      opts.unshift(`<option value="${lane.app_select}" selected>`
                   + `App ${lane.app_select} — not advertised</option>`);
    }
    const appOpts = opts.join('');

    // DPConfigLane is one byte per lane; the rest are one bit per lane.
    const tipApp = regTip({
      field: `DPConfigLane${lane.lane}`, page: 0x10, addr: 0x91 + i,
      note: `AppSelCode = ${lane.app_select} (bits 7-4); DataPathID bits 3-1, ExplicitControl bit 0`,
    });
    const tipTx = regTip({
      field: `OutputDisableTx${lane.lane}`, page: 0x10, addr: 0x82,
      value: d.tx_disable_mask, bit: i,
      note: lane.tx_enable ? 'Checked = Tx output enabled (disable bit clear)'
                           : 'Unchecked = Tx output disabled (disable bit set)',
    });
    const tipTxPol = regTip({
      field: `InputPolarityFlipTx${lane.lane}`, page: 0x10, addr: 0x81,
      value: d.tx_polarity_flip_mask, bit: i,
      note: lane.tx_polarity_flip ? 'Host-side input polarity flipped' : 'No input polarity flip',
    });
    const tipRxPol = regTip({
      field: `OutputPolarityFlipRx${lane.lane}`, page: 0x10, addr: 0x89,
      value: d.rx_polarity_flip_mask, bit: i,
      note: lane.rx_polarity_flip ? 'Host-side output polarity flipped' : 'No output polarity flip',
    });
    const tipDeinit = regTip({
      field: `DPDeinitLane${lane.lane}`, page: 0x10, addr: 0x80,
      value: d.dp_deinit_mask, bit: i,
      note: lane.dp_deinit ? 'Data Path held de-initialised' : 'Data Path released for operation',
    });

    return `<tr class="datapath-lane-row">
      <td>Lane ${lane.lane}</td>
      <td title="${esc(tipApp)}"><select id="app-sel-${lane.lane}" class="app-select-input" title="${esc(tipApp)}">${appOpts}</select></td>
      <td title="${esc(tipTx)}"><input type="checkbox" id="tx-en-${lane.lane}" title="${esc(tipTx)}" ${lane.tx_enable ? 'checked' : ''}></td>
      <td title="${esc(tipTxPol)}"><input type="checkbox" id="tx-pol-${lane.lane}" title="${esc(tipTxPol)}" ${lane.tx_polarity_flip ? 'checked' : ''}></td>
      <td title="${esc(tipRxPol)}"><input type="checkbox" id="rx-pol-${lane.lane}" title="${esc(tipRxPol)}" ${lane.rx_polarity_flip ? 'checked' : ''}></td>
      <td title="${esc(tipDeinit)}">${lane.dp_deinit ? '<span class="text-warning">Deinit</span>' : '<span class="text-success">Active</span>'}</td>
    </tr>`;
  }).join('');
}

async function applyDatapath() {
  const app_select = [];
  // One mask byte per bank of eight lanes: a 16-lane module needs two, and
  // sending a single byte would silently configure only the first half.
  const banks = Math.ceil(AppState.lanes / 8);
  const tx_disable_mask = new Array(banks).fill(0);
  const tx_pol_mask = new Array(banks).fill(0);
  const rx_pol_mask = new Array(banks).fill(0);

  for (let i = 1; i <= AppState.lanes; i++) {
    const appSel = document.getElementById(`app-sel-${i}`);
    const txEn   = document.getElementById(`tx-en-${i}`);
    if (!appSel || !txEn) continue;
    app_select.push(parseInt(appSel.value, 10));
    const b = Math.floor((i - 1) / 8), bit = (i - 1) % 8;
    if (!txEn.checked) tx_disable_mask[b] |= (1 << bit);
    if (document.getElementById(`tx-pol-${i}`)?.checked) tx_pol_mask[b] |= (1 << bit);
    if (document.getElementById(`rx-pol-${i}`)?.checked) rx_pol_mask[b] |= (1 << bit);
  }

  const res = await apiPost('/api/module/datapath', {
    tx_disable_mask,
    app_select,
    tx_polarity_flip_mask: tx_pol_mask,
    rx_polarity_flip_mask: rx_pol_mask,
    apply: true,
  });

  if (res.status !== 'ok') {
    toast(`Apply failed: ${res.message}`, 'error');
    return;
  }
  // ApplyDPInit restarts the Data Path state machines, so give the module a
  // moment before reading back what it settled on.
  await new Promise(r => setTimeout(r, 300));
  await loadDatapath();
  await loadSquelch();

  // The write itself succeeding says nothing about whether the module accepted
  // the configuration - it reports that per lane in ConfigStatus.
  const mon = await apiGet('/api/module/monitoring');
  const rejected = mon.status === 'ok'
    ? mon.data.lanes.filter(l => (l.config_status || '').startsWith('ConfigRejected'))
    : [];
  if (rejected.length) {
    const detail = rejected.map(l => `L${l.lane}: ${l.config_status}`).join(', ');
    toast(`Module rejected the configuration — ${detail}`, 'error', 8000);
  } else {
    toast('DataPath configuration applied', 'success');
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

  const ctrlTip = (field, bit, note) =>
    esc(regTip({ field, page: null, addr: 0x1A, value: d.raw, bit, note }));

  tbody.innerHTML = `
    <tr title="${ctrlTip('SoftwareReset', 3, d.software_reset ? 'Reset in progress' : 'Idle; writing 1 restarts the module')}">
      <td>Software Reset</td>
      <td>${d.software_reset ? yes : no}</td>
      <td class="td-addr">0x1A[3]</td>
      <td><button class="btn-danger btn-sm" id="btn-mod-reset">Reset Module</button></td>
    </tr>
    <tr title="${ctrlTip('LowPwrRequestSW', 4, d.low_pwr_request_sw ? 'Module held in low power' : 'Module allowed to reach high power')}">
      <td>Low Power Request (SW)</td>
      <td>${d.low_pwr_request_sw ? yes : no}</td>
      <td class="td-addr">0x1A[4]</td>
      <td>
        <button class="btn-secondary btn-sm" id="btn-mod-lp">Enter LowPwr</button>
        <button class="btn-secondary btn-sm" id="btn-mod-hp">Exit LowPwr</button>
      </td>
    </tr>
    <tr title="${ctrlTip('LowPwrAllowRequestHW', 6, d.low_pwr_allow_request_hw ? 'Hardware LPMode pin honoured' : 'Hardware LPMode pin ignored')}">
      <td>Allow LowPwrRequestHW</td>
      <td>${d.low_pwr_allow_request_hw ? yes : no}</td>
      <td class="td-addr">0x1A[6]</td>
      <td>—</td>
    </tr>
    <tr title="${ctrlTip('SquelchMethodSelect', 5, d.squelch_method_select ? 'Squelch on average power (Pav)' : 'Squelch on modulation amplitude (OMA)')}">
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
    if (r.status !== 'ok') return;
    // A reset reinitialises every Data Path, so refresh the whole tab rather
    // than leaving stale values and tooltips behind.
    setTimeout(() => {
      loadModuleControl(); loadDatapath(); loadSquelch(); loadApplications();
    }, 400);
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
  // The DataPath AppSelect dropdown offers exactly these.
  _advertisedApps = Array.isArray(apps) ? apps : [];
  if (!apps || apps.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="placeholder-text">No applications advertised.</td></tr>';
    return;
  }
  tbody.innerHTML = apps.map(a => {
    const hostHex = `0x${a.host_if_id.toString(16).toUpperCase().padStart(2,'0')}`;
    const mediaHex = `0x${a.media_if_id.toString(16).toUpperCase().padStart(2,'0')}`;
    // Prefix the bitmap: a bare "00000001" reads as decimal one to anyone
    // scanning the table.
    const assignBin = '0b' + a.host_lane_assign_mask.toString(2).padStart(8, '0');
    const lanesTip = `Host Lane Assignment: ${assignBin} bin = `
      + `${hex8(a.host_lane_assign_mask)} hex = ${a.host_lane_assign_mask} dec\n`
      + 'Bit n set = this Application may start on host lane n+1';
    return `<tr>
      <td title="AppSelCode ${a.app_sel} (dec), 1-15">${a.app_sel}</td>
      <td title="Host Interface ID ${hostHex} hex = ${a.host_if_id} dec (SFF-8024)">${hostHex}</td>
      <td title="Media Interface ID ${mediaHex} hex = ${a.media_if_id} dec">${mediaHex}</td>
      <td title="Host lane count (dec)">${a.host_lanes || '—'}</td>
      <td title="Media lane count (dec)">${a.media_lanes || '—'}</td>
      <td title="${esc(lanesTip)}"><code>${assignBin}</code></td>
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
    // The write returning ok only means the I2C transfer completed. Read the
    // bytes back so the user sees what the module actually holds - writes to
    // read-only or protected registers are silently discarded.
    const back = await apiPost('/api/register/read', { page, address, length: data.length });
    if (back.status === 'ok') {
      const got = back.data.data;
      const same = got.length === data.length && got.every((b, i) => b === data[i]);
      dumpEl.textContent =
        `Wrote ${data.length} byte(s) to page 0x${page.toString(16).toUpperCase()} `
        + `addr 0x${address.toString(16).toUpperCase().padStart(2,'0')}\n`
        + `read back:\n${formatHexDump(got, address)}`;
      toast(same ? `Written and verified ${data.length} byte(s)`
                 : 'Write completed but the module reports different values — '
                   + 'the register may be read-only or the value was clamped',
            same ? 'success' : 'error', same ? 3000 : 8000);
    } else {
      dumpEl.textContent = `Wrote ${data.length} byte(s); read-back failed: ${back.message}`;
      toast(`Written, but read-back failed: ${back.message}`, 'error', 6000);
    }
  } else {
    dumpEl.textContent = `Error: ${res.message}`;
    toast(`Write error: ${res.message}`, 'error');
  }
}

function formatHexDump(byteArray, baseAddr) {
  const ROW = 8;
  // Header states the radix: the body is bare 2-digit groups with no 0x, so
  // without it a row of "00 01 10" is ambiguous.
  let out = 'addr (hex) : bytes (hex)                ascii\n';
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

/**
 * Render a module-reported BER.
 *
 * A zero F16 word means the module counted no errors, not that it measured a
 * bound. This used to render as a fixed "less than" figure with no basis - the
 * format bottoms out around 1e-24 - which is the sort of number that ends up
 * quoted in a test report as if it had been measured.
 */
function formatBer(ber) {
  if (ber == null || !isFinite(ber)) return '—';
  if (ber === 0) {
    return '<span title="Module reports no errors. This is a zero count, not a '
         + 'measured limit — the achievable floor depends on how many bits the '
         + 'gate accumulated.">0</span>';
  }
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
  _moduleThresholds = d;   // drives the monitoring table's alarm colouring
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

function _populateBitmaskRow(prefix, mask, meta) {
  for (let i = 0; i < 8; i++) {
    const td = document.getElementById(`${prefix}-td-${i}`);
    if (!td) continue;
    td.innerHTML = _mkCheckbox(`${prefix}-cb-${i}`);
    const cb = document.getElementById(`${prefix}-cb-${i}`);
    cb.checked = !!((mask >> i) & 1);
    if (meta) {
      const tip = regTip({
        field: `${meta.field}${i + 1}`,
        page: meta.page, addr: meta.addr, value: mask, bit: i,
        note: ((mask >> i) & 1) ? meta.onNote : meta.offNote,
      });
      cb.title = tip;
      td.title = tip;
    }
  }
}

function _readBitmaskRow(prefix) {
  // One mask byte per bank of eight lanes; the checkbox index runs over every
  // lane the module has, so the bit position restarts at each bank.
  const banks = Math.ceil(AppState.lanes / 8);
  const masks = new Array(banks).fill(0);
  for (let i = 0; i < AppState.lanes; i++) {
    const cb = document.getElementById(`${prefix}-cb-${i}`);
    if (cb && cb.checked) masks[Math.floor(i / 8)] |= (1 << (i % 8));
  }
  return banks === 1 ? masks[0] : masks;
}

async function loadSquelch() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/squelch');
  if (res.status !== 'ok') { toast(`Squelch error: ${res.message}`, 'error'); return; }
  _populateBitmaskRow('sq', res.data.tx_squelch_disable, {
    field: 'AutoSquelchDisableTx', page: 0x10, addr: 0x83,
    onNote: 'Auto-squelch controller disabled for this lane',
    offNote: 'Auto-squelch controller enabled for this lane' });
  _populateBitmaskRow('sf', res.data.tx_squelch_force, {
    field: 'OutputSquelchForceTx', page: 0x10, addr: 0x84,
    onNote: 'Tx output squelch forced on', offNote: 'Tx output not force-squelched' });
  _populateBitmaskRow('od', res.data.rx_output_disable, {
    field: 'OutputDisableRx', page: 0x10, addr: 0x8A,
    onNote: 'Rx output disabled', offNote: 'Rx output enabled' });
  _populateBitmaskRow('rd', res.data.rx_squelch_disable, {
    field: 'AutoSquelchDisableRx', page: 0x10, addr: 0x8B,
    onNote: 'Auto-squelch controller disabled for this lane',
    offNote: 'Auto-squelch controller enabled for this lane' });
}

async function applySquelch() {
  return applyAndReload('Output controls', '/api/module/squelch', {
    tx_squelch_disable: _readBitmaskRow('sq'),
    tx_squelch_force:   _readBitmaskRow('sf'),
    rx_output_disable:  _readBitmaskRow('od'),
    rx_squelch_disable: _readBitmaskRow('rd'),
  }, loadSquelch);
}

// ---------------------------------------------------------------------------
// Loopback (Diagnostics tab)
// ---------------------------------------------------------------------------
function _populateLoopbackRow(prefix, mask, meta) {
  for (let i = 0; i < 8; i++) {
    const td = document.getElementById(`lb-${prefix}-${i}`);
    if (!td) continue;
    td.innerHTML = _mkCheckbox(`lb-cb-${prefix}-${i}`);
    const cb = document.getElementById(`lb-cb-${prefix}-${i}`);
    cb.checked = !!((mask >> i) & 1);
    if (meta) {
      const tip = regTip({
        field: `${meta.field}Lane${i + 1}`,
        page: 0x13, addr: meta.addr, value: mask, bit: i,
        note: ((mask >> i) & 1) ? 'Loopback engaged on this lane' : 'Normal non-loopback operation',
      });
      cb.title = tip;
      td.title = tip;
    }
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
  _populateLoopbackRow('mso', res.data.media_side_output,
    { field: 'MediaSideOutputLoopbackEnable', addr: 0xB4 });
  _populateLoopbackRow('msi', res.data.media_side_input,
    { field: 'MediaSideInputLoopbackEnable', addr: 0xB5 });
  _populateLoopbackRow('hso', res.data.host_side_output,
    { field: 'HostSideOutputLoopbackEnable', addr: 0xB6 });
  _populateLoopbackRow('hsi', res.data.host_side_input,
    { field: 'HostSideInputLoopbackEnable', addr: 0xB7 });
}

async function applyLoopback() {
  return applyAndReload('Loopback configuration', '/api/module/loopback', {
    media_side_output: _readLoopbackRow('mso'),
    media_side_input:  _readLoopbackRow('msi'),
    host_side_output:  _readLoopbackRow('hso'),
    host_side_input:   _readLoopbackRow('hsi'),
  }, loadLoopback);
}

// ---------------------------------------------------------------------------
// PRBS (Diagnostics tab)
// ---------------------------------------------------------------------------
function _renderPrbsTable(tbodyId, block, lolMask, base, side) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  const isChecker = (lolMask !== undefined);
  // Field names follow CMIS 5.3 Tables 8-109/8-111/8-113/8-115: each block is
  // 8 bytes from `base` — Enable, DataInvert, SwapSymbolBits, Pre/PostFECEnable,
  // then 4 PatternSelect bytes holding two 4-bit lane selectors each.
  const role = isChecker ? 'Checker' : 'Generator';
  const fecName = isChecker ? 'PostFECEnable' : 'PreFECEnable';
  const tip = (suffix, off, mask, i, note) => esc(regTip({
    field: `${side}Side${role}${suffix}Lane${i + 1}`, page: 0x13, addr: base + off,
    value: mask, bit: i, note,
  }));
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
    // Lane i's 4-bit pattern selector sits in the low or high nibble of
    // pattern byte base+4+(i>>1).
    // Lane i's 4-bit PatternSelect sits in byte base+4+(i>>1); odd lanes
    // (Lane 1, 3, 5, 7) occupy bits 3-0, even lanes bits 7-4.
    const patAddr = base + 4 + (i >> 1);
    const patTip = esc(regTip({
      field: `${side}Side${role}PatternSelectLane${i + 1}`, page: 0x13, addr: patAddr,
      note: `Bits ${(i % 2) ? '7-4' : '3-0'} = ${pattern} (${PRBS_PATTERNS[pattern] || '?'})`,
    }));
    const tEn  = tip('Enable', 0, block.enable_mask, i,
                     en ? `${role} running on this lane` : `${role} stopped on this lane`);
    const tInv = tip('DataInvert', 1, block.invert_mask, i,
                     inv ? 'Pattern data inverted' : 'Pattern data not inverted');
    const tSw  = tip('SwapSymbolBits', 2, block.byte_swap_mask, i,
                     sw ? 'Symbol bit order swapped' : 'Normal symbol bit order');
    const tFec = tip(fecName, 3, block.fec_mask, i,
                     fec ? 'Applied at the FEC-coded side' : 'Applied at the raw side');

    return `<tr>
      <td>L${i+1}</td>
      <td title="${tEn}"><input type="checkbox" id="${tbodyId}-en-${i}" title="${tEn}" ${en  ? 'checked' : ''}></td>
      <td title="${tInv}"><input type="checkbox" id="${tbodyId}-inv-${i}" title="${tInv}" ${inv ? 'checked' : ''}></td>
      <td title="${tSw}"><input type="checkbox" id="${tbodyId}-sw-${i}" title="${tSw}" ${sw  ? 'checked' : ''}></td>
      <td title="${tFec}"><input type="checkbox" id="${tbodyId}-fec-${i}" title="${tFec}" ${fec ? 'checked' : ''}></td>
      <td title="${patTip}"><select class="app-select-input" id="${tbodyId}-pat-${i}" title="${patTip}">${patOpts}</select></td>
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
  _renderPrbsTable('tbl-prbs-host-gen',  d.host_gen,  undefined, 0x90, 'Host');
  _renderPrbsTable('tbl-prbs-media-gen', d.media_gen, undefined, 0x98, 'Media');
  _renderPrbsTable('tbl-prbs-host-chk',  d.host_chk,  d.host_chk_lol_mask,  0xA0, 'Host');
  _renderPrbsTable('tbl-prbs-media-chk', d.media_chk, d.media_chk_lol_mask, 0xA8, 'Media');
}

async function applyPrbs() {
  return applyAndReload('PRBS configuration', '/api/module/prbs', {
    host_gen:  _readPrbsSection('tbl-prbs-host-gen'),
    media_gen: _readPrbsSection('tbl-prbs-media-gen'),
    host_chk:  _readPrbsSection('tbl-prbs-host-chk'),
    media_chk: _readPrbsSection('tbl-prbs-media-chk'),
  }, loadPrbs);
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
  // While the checker has lost pattern sync the counters keep accumulating but
  // mean nothing, so say so rather than rendering a number that reads like a
  // measurement.
  const fmtBer = (v, psl) => psl ? '<span class="flag-active">no sync</span>'
                           : (v != null && v > 0 ? v.toExponential(2) : '—');
  const cell = (l, psl, html) =>
    `<td${psl ? ' class="text-muted" title="Pattern sync lost on this lane — counts are not a valid measurement"' : ''}>${html}</td>`;

  const row = (side, field, fmt) => lanes.map(l =>
    cell(l, l[`${side}_psl`], fmt(l[`${side}_${field}`]))).join('');

  const hostErrCells  = row('host', 'error_count', fmtCount);
  const hostBitCells  = row('host', 'total_bits', fmtCount);
  const hostBerCells  = lanes.map(l => cell(l, l.host_psl, fmtBer(l.host_ber, l.host_psl))).join('');
  const mediaErrCells = row('media', 'error_count', fmtCount);
  const mediaBitCells = row('media', 'total_bits', fmtCount);
  const mediaBerCells = lanes.map(l => cell(l, l.media_psl, fmtBer(l.media_ber, l.media_psl))).join('');

  const anyPsl = lanes.some(l => l.host_psl || l.media_psl);
  const note = anyPsl
    ? `<tr><td colspan="9" class="flag-active" style="font-size:var(--fs-xs)">`
      + `⚠ Pattern sync lost on one or more lanes (14h counter bit 0). Their error `
      + `and bit counts are not a valid BER measurement.</td></tr>`
    : '';

  tbody.innerHTML = `
    <tr><td style="color:var(--text-muted)">Host Errors</td>${hostErrCells}</tr>
    <tr><td style="color:var(--text-muted)">Host Total Bits</td>${hostBitCells}</tr>
    <tr><td style="color:var(--text-muted)">Host BER (calc)</td>${hostBerCells}</tr>
    <tr><td style="color:var(--text-muted)">Media Errors</td>${mediaErrCells}</tr>
    <tr><td style="color:var(--text-muted)">Media Total Bits</td>${mediaBitCells}</tr>
    <tr><td style="color:var(--text-muted)">Media BER (calc)</td>${mediaBerCells}</tr>
    ${note}`;
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
      capsEl.innerHTML = '<span style="color:var(--muted)">Not a tunable laser module (Media Interface Technology is not C-band/L-band); Page 04h tuning capabilities not advertised.</span>';
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
    const i = l.lane - 1;
    // Page 12h: 1 byte/lane grid, then S16 per lane for channel, fine offset
    // and target power; frequency and status are read-only feedback.
    const tipGrid = esc(regTip({
      field: `GridSpacingTx${l.lane}`, page: 0x12, addr: 0x80 + i,
      note: `Bits 7-4 = ${l.grid_code} (${l.grid}); bit 0 FineTuningEnableTx = `
          + `${l.fine_tuning_enabled ? 1 : 0} (bits 3-1 reserved)`,
    }));
    const tipCh = esc(regTipRange(`ChannelNumberTx${l.lane}`, 0x12, 0x88 + i * 2, 2,
      `S16 channel number, current ${l.channel}`));
    const tipFt = esc(regTipRange(`FineTuningOffsetTx${l.lane}`, 0x12, 0x98 + i * 2, 2,
      `S16 in units of 0.001 GHz, current ${l.fine_offset_ghz} GHz`));
    const tipFreq = esc(regTipRange(`CurrentLaserFrequencyTx${l.lane}`, 0x12, 0xA8 + i * 4, 4,
      `U32 in units of 0.001 GHz, current ${l.frequency_thz.toFixed(6)} THz (read-only)`));
    const tipPwr = esc(regTipRange(`TargetOutputPowerTx${l.lane}`, 0x12, 0xC8 + i * 2, 2,
      `S16 in units of 0.01 dBm, current ${l.target_power_dbm} dBm`));
    const tipStat = esc(regTip({
      field: `TuningInProgressTx${l.lane} / WavelengthUnlockedTx${l.lane}`,
      page: 0x12, addr: 0xDE + i,
      note: `Bit 1 TuningInProgressTx = ${l.tuning_in_progress ? 1 : 0}; `
          + `bit 0 WavelengthUnlockedTx = ${l.wavelength_locked ? 0 : 1} (bits 7-2 reserved)`,
    }));

    return `<tr>
      <td>${l.lane}</td>
      <td title="${tipGrid}"><select class="app-select-input" id="laser-grid-${l.lane}" title="${tipGrid}">${gridOpts(l.grid_code)}</select></td>
      <td title="${tipCh}"><input type="number" id="laser-ch-${l.lane}" title="${tipCh}" value="${l.channel}" style="width:70px" class="raw-data-input"></td>
      <td title="${tipFt}"><input type="number" id="laser-ft-${l.lane}" title="${tipFt}" value="${l.fine_offset_ghz}" step="0.001" style="width:80px" class="raw-data-input"></td>
      <td style="font-family:var(--font-mono)" title="${tipFreq}">${l.frequency_thz.toFixed(6)}</td>
      <td title="${tipPwr}"><input type="number" id="laser-pwr-${l.lane}" title="${tipPwr}" value="${l.target_power_dbm}" step="0.01" style="width:70px" class="raw-data-input"></td>
      <td title="${tipStat}">${lockIcon}</td>
    </tr>`;
  }).join('');
}

async function applyLaser() {
  const lanes = [];
  for (let i = 1; i <= AppState.lanes; i++) {
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
  // Display settings — wired first so the controls work even before a module
  // is connected, which is exactly when someone needs to fix an unreadable UI.
  initSettings();

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
  document.getElementById('btn-check-update')?.addEventListener('click', checkForUpdate);

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
