// ── Lightning — real-time strike map overlay ──────────────────────────────────
// Polls /api/lightning every 30 seconds when internet is online.
// Renders age-fading bolt markers on the map; shows nearest strike in HUD chip.

const API     = '/jtak/api';
const POLL_MS = 30 * 1000;
const TTL_S   = 30 * 60;   // matches server TTL

let _pollTimer = null;
let _markers   = new Map();   // ts_key → Leaflet marker
let _map       = null;

export function initLightning(map) {
  _map = map;
}

export function startLightning() {
  _pollNow();
  _pollTimer = setInterval(_pollNow, POLL_MS);
}

export function stopLightning() {
  clearInterval(_pollTimer);
  _pollTimer = null;
  _clearMarkers();
  _renderChip(null);
}

// ── Polling ───────────────────────────────────────────────────────────────────

async function _pollNow() {
  try {
    const r = await fetch(`${API}/lightning`);
    if (!r.ok) return;
    const d = await r.json();
    _renderStrikes(d);
    _renderChip(d);
  } catch(e) { /* silent */ }
}

// ── Map rendering ─────────────────────────────────────────────────────────────

// Strike age → color + opacity
function _ageStyle(age_s) {
  const t = age_s / TTL_S;   // 0=fresh, 1=expired
  if (t < 0.167)  return { color: '#ff3b3b', opacity: 0.95 };   // <5 min — red
  if (t < 0.5)    return { color: '#e6a817', opacity: 0.75 };   // 5-15 min — amber
  return              { color: '#cccc44', opacity: 0.45 };       // 15-30 min — faded yellow
}

function _boltSvg(color, opacity) {
  const s = `opacity:${opacity}`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 24" width="14" height="21" style="${s}">
    <polygon points="10,0 4,13 8,13 6,24 14,9 9,9" fill="${color}" stroke="#000" stroke-width="0.8"/>
  </svg>`;
}

function _makeIcon(color, opacity) {
  return L.divIcon({
    className: '',
    html: _boltSvg(color, opacity),
    iconSize:   [14, 21],
    iconAnchor: [7, 21],
  });
}

function _renderStrikes(d) {
  if (!_map) return;
  const now = Date.now() / 1000;

  // Build set of current strike keys from server
  const serverKeys = new Set();
  for (const s of (d.strikes || [])) {
    const key = `${s.lat.toFixed(3)}_${s.lon.toFixed(3)}_${Math.round(s.ts / 60)}`;
    serverKeys.add(key);

    if (!_markers.has(key)) {
      const { color, opacity } = _ageStyle(s.age_s);
      const marker = L.marker([s.lat, s.lon], { icon: _makeIcon(color, opacity) })
        .bindTooltip(`⚡ ${s.dist_mi} mi · ${Math.round(s.age_s / 60)}m ago`, {
          permanent: false, className: 'lightning-tip',
        });
      marker.addTo(_map);
      _markers.set(key, { marker, age_s: s.age_s });
    } else {
      // Update icon opacity as strike ages
      const { color, opacity } = _ageStyle(s.age_s);
      _markers.get(key).marker.setIcon(_makeIcon(color, opacity));
    }
  }

  // Remove markers no longer in server response
  for (const [key, entry] of _markers) {
    if (!serverKeys.has(key)) {
      entry.marker.remove();
      _markers.delete(key);
    }
  }
}

function _clearMarkers() {
  for (const { marker } of _markers.values()) marker.remove();
  _markers.clear();
}

// ── HUD chip ──────────────────────────────────────────────────────────────────

function _renderChip(d) {
  const chip = document.getElementById('lightning-hud-chip');
  if (!chip) return;

  if (!d || d.strike_count === 0) {
    chip.style.display = 'none';
    return;
  }

  chip.style.display = 'flex';

  const alert = d.alert;
  const color = alert ? '#ff3b3b' : '#e6a817';

  let nearestStr = '';
  if (d.nearest) {
    const mi  = d.nearest.dist_mi;
    const min = Math.round(d.nearest.age_s / 60);
    nearestStr = `<span class="ltng-nearest">${mi} mi · ${min}m ago</span>`;
  }

  chip.innerHTML = `
    <span class="ltng-bolt" style="color:${color}">⚡</span>
    <span class="ltng-count" style="color:${color}">${d.strike_count}</span>
    <span class="ltng-label">STRIKES</span>
    ${nearestStr}
    ${!d.connected ? '<span class="ltng-offline">OFFLINE</span>' : ''}
  `;

  // Pulse chip when alert is active
  chip.classList.toggle('lightning-alert', alert);
}
