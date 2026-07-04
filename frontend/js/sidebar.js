// ── Sidebar — node list, RF stats, hub health, event log ──────────────────
import { rssiClass, rssiLabel, timeAgo, isStale, fmtBearing } from './utils.js';
import { panTo } from './map.js';

const nodeList   = document.getElementById('node-list');
const nodeCount  = document.getElementById('node-count');
const nodeSearch = document.getElementById('node-search');
const nodeSortBtn= document.getElementById('node-sort-btn');
const rfPanel    = document.getElementById('rf-stats');
const healthGrid = document.getElementById('health-grid');
const sensorList = document.getElementById('sensor-list');
const logList    = document.getElementById('log-list');

const sensorCache = {};   // source_id → sensor row

let logCount = 0;
const MAX_LOG = 120;

// ── Node sort/search state ────────────────────────────────────────────────────
// Modes: 'alpha-asc' | 'heard-asc' | 'heard-desc'
let _sortMode  = 'alpha-asc';
let _searchStr = '';
let _lastNodes = [];

const _SORT_LABELS = { 'alpha-asc': 'A→Z', 'heard-desc': '🕐↓', 'heard-asc': '🕐↑' };
const _SORT_CYCLE  = { 'alpha-asc': 'heard-desc', 'heard-desc': 'heard-asc', 'heard-asc': 'alpha-asc' };

function _applyFilter(nodes) {
  const q = _searchStr.toLowerCase().trim();
  let sorted = [...nodes];

  if (q) {
    // Matches float to top, non-matches follow — both groups alpha-sorted
    const hit  = sorted.filter(n => (n.source_name || n.source_id || '').toLowerCase().includes(q));
    const miss = sorted.filter(n => !(n.source_name || n.source_id || '').toLowerCase().includes(q));
    hit.sort( (a,b) => (a.source_name||'').localeCompare(b.source_name||''));
    miss.sort((a,b) => (a.source_name||'').localeCompare(b.source_name||''));
    return [...hit, ...miss];
  }

  const _t = n => new Date(n.last_position || n.ts || 0).getTime();
  if (_sortMode === 'alpha-asc') {
    sorted.sort((a,b) => (a.source_name||'').localeCompare(b.source_name||''));
  } else if (_sortMode === 'heard-desc') {
    sorted.sort((a,b) => _t(b) - _t(a));
  } else {
    sorted.sort((a,b) => _t(a) - _t(b));
  }
  return sorted;
}

if (nodeSearch) {
  nodeSearch.addEventListener('input', () => {
    _searchStr = nodeSearch.value;
    _renderNodeItems(_applyFilter(_lastNodes));
  });
}
if (nodeSortBtn) {
  nodeSortBtn.addEventListener('click', () => {
    _sortMode = _SORT_CYCLE[_sortMode];
    nodeSortBtn.textContent = _SORT_LABELS[_sortMode];
    _searchStr = '';
    if (nodeSearch) nodeSearch.value = '';
    _renderNodeItems(_applyFilter(_lastNodes));
  });
}

// ── Battery icon ─────────────────────────────────────────────────────────────
function _batteryIcon(pct) {
  const fill  = pct > 60 ? '#2ecc71' : pct > 20 ? '#e6a817' : '#e74c3c';
  const w     = Math.round(Math.max(1, (pct / 100) * 11));
  return `<svg width="16" height="9" viewBox="0 0 16 9" style="vertical-align:middle">
    <rect x="0.5" y="0.5" width="13" height="8" rx="1.5" fill="none" stroke="${fill}" stroke-width="1"/>
    <rect x="13.5" y="2.5" width="2" height="4" rx="0.5" fill="${fill}"/>
    <rect x="1.5" y="1.5" width="${w}" height="6" rx="1" fill="${fill}"/>
  </svg>`;
}

// ── Node list ────────────────────────────────────────────────────────────────
function _renderNodeItems(nodes) {
  nodeList.innerHTML = '';
  nodes.forEach(n => {
    const stale = isStale(n.last_position || n.ts);
    const div = document.createElement('div');
    div.className = 'node-item';
    div.dataset.id = n.source_id;
    div.innerHTML = `
      <div class="node-dot ${stale ? 'stale' : ''}"></div>
      <div style="flex:1;min-width:0">
        <div class="node-name">${n.source_name || n.source_id}</div>
        <div class="node-meta">
          ${timeAgo(n.last_position || n.ts)}${n.distance_mi != null ? ` · ${parseFloat(n.distance_mi).toFixed(2)} mi` : ''}${n.bearing_deg != null ? ` <span class="node-bearing">${fmtBearing(n.bearing_deg)}</span>` : ''}
        </div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
        <div class="node-rssi ${rssiClass(n.rssi)}">${rssiLabel(n.rssi)}</div>
        ${n.battery_pct != null ? `<div class="node-battery">${_batteryIcon(n.battery_pct)}<span class="node-batt-pct">${Math.round(n.battery_pct)}%</span></div>` : ''}
        ${n.speed_mph != null && n.speed_mph >= 0.5 ? `
          <div class="node-speed">
            ${n.speed_mph.toFixed(1)} mph
            ${n.heading_deg != null ? `
              <svg width="9" height="14" viewBox="0 0 14 22" style="vertical-align:middle;transform:rotate(${n.heading_deg}deg);transform-origin:center;filter:drop-shadow(0 0 2px rgba(0,0,0,0.8))">
                <polygon points="7,0 14,22 7,16 0,22" fill="#7ecfff" stroke="#0a1628" stroke-width="1.5"/>
                <polygon points="7,2 11,18 7,14 3,18"  fill="white"  opacity="0.45"/>
              </svg>` : ''}
          </div>` : ''}
      </div>
    `;
    div.addEventListener('click', () => {
      document.querySelectorAll('.node-item').forEach(el => el.classList.remove('selected'));
      div.classList.add('selected');
      panTo(n.source_id);
    });
    nodeList.appendChild(div);
  });
}

export function renderNodes(nodes) {
  _lastNodes = nodes;
  nodeCount.textContent = nodes.length;
  _renderNodeItems(_applyFilter(nodes));
}

// ── RF stats (latest packet) ─────────────────────────────────────────────────
export function updateRFStats(msg) {
  if (!rfPanel) return;
  const rows = [
    ['Node',    msg.source_name || msg.source_id || '—'],
    ['RSSI',    msg.rssi   != null ? `${msg.rssi} dBm`   : '—'],
    ['SNR',     msg.snr    != null ? `${msg.snr} dB`     : '—'],
    ['Type',    msg.packet_type || '—'],
    ['Dist',    msg.distance_mi != null ? `${parseFloat(msg.distance_mi).toFixed(2)} mi` : '—'],
  ];
  rfPanel.innerHTML = rows.map(([l,v]) =>
    `<div class="rf-row"><span class="rf-label">${l}</span><span class="rf-val">${v}</span></div>`
  ).join('');
}

// ── Hub health ───────────────────────────────────────────────────────────────
export function updateHealth(s) {
  if (!healthGrid) return;

  // Pi4 throttles at 80°C — warn earlier, blink at threshold
  const t = s.cpu_temp_c;
  const throttling  = t != null && t >= 80;   // Pi4 soft throttle
  const tempWarn    = t != null && t >= 70;
  const cpuCrit     = s.cpu_pct != null && s.cpu_pct > 85;
  const cpuWarn     = s.cpu_pct != null && s.cpu_pct > 70;
  const sysAlert    = (throttling || cpuCrit) ? 'alert-crit' : (tempWarn || cpuWarn) ? 'alert-warn' : '';
  const tempAlert   = throttling ? 'alert-crit' : tempWarn ? 'alert-warn' : '';
  const tempLabel   = throttling ? 'CPU TEMP ⚠ THROTTLE' : 'CPU TEMP';

  // BME cells
  const hasBme = s.bme_temp_c != null;
  const bmeF   = s.bme_temp_f != null ? s.bme_temp_f + '°F' : '—';
  const bmeC   = s.bme_temp_c != null ? s.bme_temp_c + '°C' : '—';
  const bmeHum  = s.bme_humidity_pct  != null ? s.bme_humidity_pct.toFixed(0) + '%' : '—';
  const bmePres = s.bme_pressure_hpa  != null ? Math.round(s.bme_pressure_hpa) + ' hPa' : '—';
  const bmeIaq  = s.bme_iaq_pct       != null ? s.bme_iaq_pct.toFixed(0) + '%' : '—';
  const smokeClass = s.bme_smoke_alert ? 'smoke-alert' : '';
  const iaq = s.bme_iaq_pct;
  const iaqClass = iaq == null ? '' : iaq < 25 ? 'iaq-hazard' : iaq < 50 ? 'iaq-poor' : iaq < 75 ? 'iaq-mod' : '';

  healthGrid.innerHTML = `
    <div class="health-sys ${sysAlert}">
      <div class="sys-item">
        <span class="sys-val">${s.cpu_pct != null ? s.cpu_pct.toFixed(0) + '%' : '—'}</span>
        <span class="sys-label">CPU</span>
      </div>
      <div class="sys-item">
        <span class="sys-val">${s.mem_pct != null ? s.mem_pct.toFixed(0) + '%' : '—'}</span>
        <span class="sys-label">MEM</span>
      </div>
      <div class="sys-item">
        <span class="sys-val">${s.disk_free_gb != null ? Math.round(s.disk_free_gb) + ' GB' : '—'}</span>
        <span class="sys-label">DISK FREE</span>
      </div>
    </div>
    <div class="health-sys ${tempAlert}" style="justify-content:center;gap:0;">
      <div class="sys-item" style="align-items:center;">
        <span class="sys-val" style="font-size:20px;">${t != null ? Math.round(t) + '°C / ' + Math.round(t * 9/5 + 32) + '°F' : '—'}</span>
        <span class="sys-label">${tempLabel}</span>
      </div>
    </div>
    ${hasBme ? `
    <div class="health-bme">
      <div class="bme-cell ${smokeClass}">
        <div class="b-val">${bmeF}</div>
        <div class="b-label">EXT TEMP</div>
      </div>
      <div class="bme-cell">
        <div class="b-val">${bmeC}</div>
        <div class="b-label">EXT °C</div>
      </div>
      <div class="bme-cell">
        <div class="b-val">${bmeHum}</div>
        <div class="b-label">HUMIDITY</div>
      </div>
      <div class="bme-cell">
        <div class="b-val">${bmePres}</div>
        <div class="b-label">BARO</div>
      </div>
      <div class="bme-cell ${smokeClass} ${iaqClass}">
        <div class="b-val">${bmeIaq}</div>
        <div class="b-label">AIR QUAL</div>
      </div>
    </div>` : ''}
  `;
}

// ── Sensor panel ─────────────────────────────────────────────────────────────
export function updateSensors(msg) {
  if (msg.temp_c == null || !sensorList) return;
  sensorCache[msg.source_id] = msg;

  sensorList.innerHTML = Object.values(sensorCache).map(s => {
    const tempF = s.temp_f        != null ? Math.round(s.temp_f)       + '°F'   : '—';
    const tempC = s.temp_c        != null ? Math.round(s.temp_c)       + '°C'   : '—';
    const hum   = s.humidity_pct  != null ? Math.round(s.humidity_pct) + '%'    : '—';
    const pres  = s.pressure_hpa  != null ? Math.round(s.pressure_hpa) + ' hPa' : '—';
    return `
      <div style="padding:6px 12px;border-bottom:1px solid var(--border);">
        <div style="font-size:12px;font-weight:700;color:var(--orange-l);margin-bottom:3px;">${s.source_name || s.source_id}</div>
        <div style="display:flex;gap:10px;font-size:11px;flex-wrap:nowrap;align-items:center;">
          <span><span style="color:var(--muted)">TEMP </span><b>${tempF} / ${tempC}</b></span>
          <span><span style="color:var(--muted)">RH </span><b>${hum}</b></span>
          <span><span style="color:var(--muted)">BARO </span><b>${pres}</b></span>
        </div>
      </div>`;
  }).join('');
}

// ── Event log ────────────────────────────────────────────────────────────────
export function logEvent(text, highlight = false) {
  const now = new Date().toLocaleTimeString([], {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
  const entry = document.createElement('div');
  entry.className = 'log-entry';
  entry.innerHTML = `<span class="log-time">${now}</span><span class="log-text ${highlight ? 'hi' : ''}">${text}</span>`;
  logList.prepend(entry);
  logCount++;
  if (logCount > MAX_LOG) logList.lastChild?.remove();
}
