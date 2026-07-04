// ── Aircraft — dual-source ADS-B (SDR 📡 + OpenSky 🌐) ─────────────────────
// Two independent layers.  OpenSky deduplicates against SDR by ICAO24 so
// the same aircraft never appears twice on the map.
//
// SDR polling starts at boot (no internet needed).
// OpenSky layer appears/starts only when internet is active.

const API = '/jtak/api';

let _map          = null;
let _sdrLayer     = null;
let _openskyLayer = null;
let _sdrTimer     = null;
let _openskyTimer = null;
let _sdrCache     = null;   // last SDR response
let _openskyCache = null;   // last OpenSky response
let _hudEl        = null;
let _radiusMi     = 50;
let _refreshSec   = 30;
let _sdrOn        = true;
let _openskyOn    = true;
let _internetOn   = false;

function _acSave() {
  try { localStorage.setItem('jtak_ac', JSON.stringify({ sdrOn: _sdrOn, openskyOn: _openskyOn, radiusMi: _radiusMi, refreshSec: _refreshSec })); } catch {}
}
function _acLoad() {
  try {
    const s = JSON.parse(localStorage.getItem('jtak_ac') || 'null');
    if (!s) return;
    _sdrOn      = s.sdrOn      ?? true;
    _openskyOn  = s.openskyOn  ?? true;
    _radiusMi   = s.radiusMi   ?? 50;
    _refreshSec = s.refreshSec ?? 30;
  } catch {}
}

// ── Public API ───────────────────────────────────────────────────────────────

export function initAircraft(map) {
  _acLoad();
  _map = map;
  _buildHud();
  if (_sdrOn) _startSdr();
}

/** Called by app.js when internet becomes available. */
export function onInternetOn() {
  _internetOn = true;
  const btn = document.getElementById('ac-sky-btn');
  if (btn) btn.style.display = '';
  if (_openskyOn) _startOpensky();
}

/** Called by app.js when internet goes away. */
export function onInternetOff() {
  _internetOn = false;
  const btn = document.getElementById('ac-sky-btn');
  if (btn) btn.style.display = 'none';
  _stopOpensky();
  if (_openskyLayer) { _map.removeLayer(_openskyLayer); _openskyLayer = null; }
  _updateCounts();
}

// ── SDR polling ──────────────────────────────────────────────────────────────

function _startSdr() {
  _fetchSdr();
  _scheduleSdr();
}

function _stopSdr() {
  clearTimeout(_sdrTimer);
  _sdrTimer = null;
}

function _scheduleSdr() {
  clearTimeout(_sdrTimer);
  _sdrTimer = setTimeout(() => { _fetchSdr(); _scheduleSdr(); }, _refreshSec * 1000);
}

async function _fetchSdr() {
  try {
    const r = await fetch(`${API}/aircraft?source=sdr&radius_mi=${_radiusMi}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    _sdrCache = data;
    if (_sdrOn) _renderSdr(data);
    else _updateCounts();
  } catch(e) {
    console.warn('[Aircraft SDR] fetch failed:', e.message);
    if (_sdrCache && _sdrOn) _renderSdr(_sdrCache);
  }
}

function _renderSdr(data) {
  if (_sdrLayer) _map.removeLayer(_sdrLayer);
  _sdrLayer = L.layerGroup();
  if (data.available) {
    data.features.forEach(feat => _addMarker(_sdrLayer, feat, 'sdr'));
  }
  _sdrLayer.addTo(_map);
  // Re-render OpenSky so dedup reflects current SDR set
  if (_openskyOn && _openskyCache) _renderOpensky(_openskyCache);
  _updateCounts();
}

// ── OpenSky polling ──────────────────────────────────────────────────────────

function _startOpensky() {
  clearTimeout(_openskyTimer);
  _fetchOpensky();
  _scheduleOpensky();
}

function _stopOpensky() {
  clearTimeout(_openskyTimer);
  _openskyTimer = null;
}

function _scheduleOpensky() {
  clearTimeout(_openskyTimer);
  _openskyTimer = setTimeout(() => { _fetchOpensky(); _scheduleOpensky(); }, _refreshSec * 1000);
}

async function _fetchOpensky() {
  try {
    const r = await fetch(`${API}/aircraft?source=opensky&radius_mi=${_radiusMi}`);
    if (r.status === 429) { _updateCounts(); return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    _openskyCache = data;
    if (_openskyOn) _renderOpensky(data);
    else _updateCounts();
  } catch(e) {
    console.warn('[Aircraft OpenSky] fetch failed:', e.message);
    if (_openskyCache && _openskyOn) _renderOpensky(_openskyCache);
  }
}

function _renderOpensky(data) {
  if (_openskyLayer) _map.removeLayer(_openskyLayer);
  _openskyLayer = L.layerGroup();

  // Dedup: build set of ICAOs already shown by SDR
  const sdrIcaos = new Set(
    (_sdrOn && _sdrCache?.available ? _sdrCache.features : [])
      .map(f => f.properties.icao24.toLowerCase())
  );

  const unique = data.features.filter(
    f => !sdrIcaos.has(f.properties.icao24.toLowerCase())
  );
  unique.forEach(feat => _addMarker(_openskyLayer, feat, 'opensky'));
  _openskyLayer.addTo(_map);
  _updateCounts();
}

// ── Marker builder ────────────────────────────────────────────────────────────

function _project(lat, lon, bearing_deg, dist_mi) {
  const br = bearing_deg * Math.PI / 180;
  return [
    lat + (dist_mi / 69.0) * Math.cos(br),
    lon + (dist_mi / (69.0 * Math.cos(lat * Math.PI / 180))) * Math.sin(br),
  ];
}

const TYPE_COLOR = {
  jet: '#00cfff', turboprop: '#ffc400', helicopter: '#00e676',
  piston: '#9e9e9e', glider: '#ce93d8', unknown: '#00cfff',
};

function _addMarker(layer, feat, src) {
  const [lon, lat] = feat.geometry.coordinates;
  const p = feat.properties;
  const heading = p.heading ?? 0;
  const type    = p.ac_type || 'unknown';

  const symbol     = type === 'helicopter' ? '🚁' : '✈';
  const baseOffset = type === 'helicopter' ? 270 : 80;
  const icon = L.divIcon({
    className: '',
    html: `<div class="ac-marker ac-${type}" style="transform:rotate(${heading - baseOffset}deg);width:24px;height:24px;">${symbol}</div>`,
    iconSize:   [24, 24],
    iconAnchor: [12, 12],
  });

  const marker = L.marker([lat, lon], { icon, zIndexOffset: 500 });

  const altStr   = p.alt_ft    != null ? `${p.alt_ft.toLocaleString()} ft` : '—';
  const spdStr   = p.vel_kts   != null ? `${p.vel_kts} kts`                : '—';
  const hdgStr   = p.heading   != null ? `${Math.round(p.heading)}°`        : '—';
  const vrateStr = p.vrate_fpm != null
    ? `${p.vrate_fpm > 0 ? '▲' : p.vrate_fpm < 0 ? '▼' : '→'} ${Math.abs(p.vrate_fpm).toLocaleString()} fpm`
    : '—';
  const typeLabel = { helicopter:'Helicopter', jet:'Jet', turboprop:'Turboprop',
                      piston:'Piston', glider:'Glider', unknown:'Unknown' }[type] || type;
  const typeBadge = `<span class="ac-type-badge ac-badge-${type}">${typeLabel}</span>`;

  const title = p.registration
    ? `${p.callsign} · ${p.registration}`
    : p.callsign;

  const srcLabel = src === 'sdr'
    ? '📡 Local SDR'
    : `<span class="online-globe">&#127760;</span> OpenSky`;

  marker.bindPopup(`
    <div class="popup-title">${symbol} ${title} ${typeBadge}</div>
    ${p.model    ? `<div class="popup-row"><span class="pk">Aircraft</span><span class="pv">${p.model}</span></div>` : ''}
    ${p.operator ? `<div class="popup-row"><span class="pk">Operator</span><span class="pv">${p.operator}</span></div>` : ''}
    <div class="popup-row"><span class="pk">ICAO</span><span class="pv">${p.icao24}</span></div>
    <div class="popup-row"><span class="pk">Altitude</span><span class="pv">${altStr}</span></div>
    <div class="popup-row"><span class="pk">Speed</span><span class="pv">${spdStr}</span></div>
    <div class="popup-row"><span class="pk">Heading</span><span class="pv">${hdgStr}</span></div>
    <div class="popup-row"><span class="pk">Vert rate</span><span class="pv">${vrateStr}</span></div>
    ${p.squawk ? `<div class="popup-row"><span class="pk">Squawk</span><span class="pv">${p.squawk}</span></div>` : ''}
    ${p.rssi != null ? `<div class="popup-row"><span class="pk">RSSI</span><span class="pv">${p.rssi} dBFS</span></div>` : ''}
    <div class="popup-row"><span class="pk">Source</span><span class="pv">${srcLabel}</span></div>
  `);
  marker.bindTooltip(p.model ? `${p.callsign} · ${p.model}` : p.callsign, { sticky: true });
  layer.addLayer(marker);

  // Heading vector
  if (p.vel_kts && p.vel_kts > 0 && p.heading != null) {
    const dist_mi = (p.vel_kts * 1.15078) * (_refreshSec / 3600);
    const tip_ll  = _project(lat, lon, heading, dist_mi);
    const color   = TYPE_COLOR[type] || '#00cfff';
    const vector  = L.polyline([[lat, lon], tip_ll], {
      color, weight: 1.5, opacity: 0.55, dashArray: '4 4',
    });
    vector.bindTooltip(`${p.callsign} · ${Math.round(dist_mi * 10) / 10} mi in ${_refreshSec}s`, { sticky: true });
    layer.addLayer(vector);
  }
}

// ── HUD ───────────────────────────────────────────────────────────────────────

function _buildHud() {
  if (_hudEl) return;
  _hudEl = document.createElement('div');
  _hudEl.id = 'aircraft-hud';
  _hudEl.className = 'hud-chip ac-hud';
  const chips = document.getElementById('hud-chips');
  const anchor = document.getElementById('center-hub-btn');
  if (chips && anchor) chips.insertBefore(_hudEl, anchor);
  else if (chips) chips.appendChild(_hudEl);

  _hudEl.innerHTML = `
    <span class="ac-hud-icon" title="Air Traffic">✈</span>
    <button id="ac-sdr-btn" class="ac-src-btn sdr ${_sdrOn ? 'active' : ''}" title="Toggle SDR aircraft (local dump1090)">📡 <span id="ac-sdr-count">—</span></button>
    <button id="ac-sky-btn" class="ac-src-btn sky ${_openskyOn ? 'active' : ''}" title="Toggle OpenSky aircraft (internet)" style="display:none">🌐 <span id="ac-sky-count">—</span></button>
    <span class="ac-divider">|</span>
    <label class="ac-ctrl-label">R</label>
    <input id="ac-radius" class="ac-input" type="number" min="25" max="500" step="25" value="${_radiusMi}" title="Radius (mi)">
    <span class="ac-unit">mi</span>
    <span class="ac-divider">|</span>
    <span id="ac-refresh-label" class="ac-ctrl-label">${_refreshSec}s</span>
    <input id="ac-refresh" class="ac-slider" type="range" min="5" max="60" step="5" value="${_refreshSec}" title="Refresh every N seconds">
  `;

  // SDR toggle
  document.getElementById('ac-sdr-btn').addEventListener('click', () => {
    _sdrOn = !_sdrOn;
    document.getElementById('ac-sdr-btn').classList.toggle('active', _sdrOn);
    if (_sdrOn) {
      if (_sdrCache) _renderSdr(_sdrCache);
      else _fetchSdr();
    } else {
      if (_sdrLayer) { _map.removeLayer(_sdrLayer); _sdrLayer = null; }
      if (_openskyOn && _openskyCache) _renderOpensky(_openskyCache);
      _updateCounts();
    }
    _acSave();
  });

  // OpenSky toggle
  document.getElementById('ac-sky-btn').addEventListener('click', () => {
    _openskyOn = !_openskyOn;
    document.getElementById('ac-sky-btn').classList.toggle('active', _openskyOn);
    if (_openskyOn) {
      if (_openskyCache) _renderOpensky(_openskyCache);
      else _startOpensky();
    } else {
      if (_openskyLayer) { _map.removeLayer(_openskyLayer); _openskyLayer = null; }
      _updateCounts();
    }
    _acSave();
  });

  // Radius
  document.getElementById('ac-radius').addEventListener('change', e => {
    const v = parseInt(e.target.value);
    if (!isNaN(v) && v >= 25 && v <= 500) {
      _radiusMi = v;
      if (_sdrOn) _fetchSdr();
      if (_openskyOn && _internetOn) _fetchOpensky();
      _acSave();
    }
  });

  // Refresh rate
  document.getElementById('ac-refresh').addEventListener('input', e => {
    _refreshSec = parseInt(e.target.value);
    document.getElementById('ac-refresh-label').textContent = `${_refreshSec}s`;
    if (_sdrTimer)     { _stopSdr();     _startSdr();     }
    if (_openskyTimer) { _stopOpensky(); _startOpensky(); }
    _acSave();
  });
}

function _updateCounts() {
  const sdrEl = document.getElementById('ac-sdr-count');
  const skyEl = document.getElementById('ac-sky-count');
  if (!sdrEl || !skyEl) return;

  if (_sdrOn && _sdrCache) {
    sdrEl.textContent = _sdrCache.available ? _sdrCache.count : '✕';
  } else {
    sdrEl.textContent = '—';
  }

  if (_openskyOn && _openskyCache) {
    // Show deduped count
    const sdrIcaos = new Set(
      (_sdrOn && _sdrCache?.available ? _sdrCache.features : [])
        .map(f => f.properties.icao24.toLowerCase())
    );
    const uniq = _openskyCache.features.filter(
      f => !sdrIcaos.has(f.properties.icao24.toLowerCase())
    ).length;
    skyEl.textContent = uniq;
  } else {
    skyEl.textContent = '—';
  }
}
