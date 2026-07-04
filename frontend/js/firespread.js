// ── Fire Spread Cone — wildfire spread prediction overlay ────────────────────
// Click 🔥 FIRE SPREAD in the map HUD to activate.
// Click any point on the map → fetches slope from /api/firespread,
// combines with NOAA wind → draws 15/30/60-min spread cone.

import { getWeather, getWeatherAge } from './weather.js';

const API = '/jtak/api';

const HALF_ANGLE  = 35;    // degrees either side of spread direction
const ARC_STEPS   = 48;    // arc polygon resolution
const MIN_SLOPE   = 5.0;   // ignore slope contribution below this

const RINGS = [
  { inner: 'r15', outer: 'r30', fill: '#ff7700', border: '#e85000', opacity: 0.35, label: '30 min' },
  { inner: 'r30', outer: 'r60', fill: '#dd1100', border: '#aa0000', opacity: 0.35, label: '60 min' },
  { inner: 0,     outer: 'r15', fill: '#ffcc00', border: '#e8a000', opacity: 0.38, label: '15 min' },
];

let _map          = null;
let _active       = false;
let _originMarker = null;
let _layers       = [];

export function initFireSpread(map) {
  _map = map;
  document.getElementById('fire-btn').addEventListener('click', _toggleTool);
}

export function isFireSpreadActive() { return _active; }

export function stopFireSpread() {
  if (!_active) return;
  _active = false;
  document.getElementById('fire-btn').classList.remove('active');
  _clearAll();
  _map.getContainer().style.cursor = '';
  _map.off('click', _onMapClick);
  _setStatus(null);
}

function _toggleTool() {
  _active = !_active;
  document.getElementById('fire-btn').classList.toggle('active', _active);

  if (_active) {
    _map.getContainer().style.cursor = 'crosshair';
    _map.on('click', _onMapClick);
    _setStatus('Click map to place fire origin');
  } else {
    _clearAll();
    _map.getContainer().style.cursor = '';
    _map.off('click', _onMapClick);
    _setStatus(null);
  }
}

function _setStatus(msg) {
  let el = document.getElementById('fire-status-hud');
  if (!msg) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement('div');
    el.id = 'fire-status-hud';
    el.className = 'hud-chip';
    document.getElementById('map-hud').appendChild(el);
  }
  el.textContent = msg;
}

function _clearAll() {
  _layers.forEach(l => _map.removeLayer(l));
  _layers = [];
  if (_originMarker) { _map.removeLayer(_originMarker); _originMarker = null; }
}

async function _onMapClick(e) {
  const { lat, lng: lon } = e.latlng;
  _clearAll();
  _setStatus('Fetching terrain…');

  // Loading marker
  _originMarker = L.marker([lat, lon], {
    icon: L.divIcon({ className: '', html: '<div class="fire-origin-marker">⏳</div>',
      iconSize: [28,28], iconAnchor: [14,14] }),
    interactive: false, zIndexOffset: 2000,
  }).addTo(_map);

  try {
    const r = await fetch(`${API}/firespread?lat=${lat}&lon=${lon}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const slope = await r.json();

    const weather       = getWeather();
    const wind_from_deg = weather?.wind_dir_deg ?? null;
    const wind_spd_mph  = weather?.wind_spd_mph ?? 5;
    const wind_source   = weather
      ? (getWeatherAge() > 0 ? `NOAA (${getWeatherAge()}m ago)` : 'NOAA live')
      : 'no NOAA data — 5 mph assumed';

    const spread_dir  = _calcSpreadDir(wind_from_deg, slope);
    const slope_mult  = 1 + slope.slope_deg * 0.04;
    const rate_mph    = Math.max(0.1, wind_spd_mph * 0.3) * slope_mult;

    const r15 = rate_mph * (15 / 60);
    const r30 = rate_mph * (30 / 60);
    const r60 = rate_mph * (60 / 60);
    const radii = { r15, r30, r60 };

    // Draw rings outermost first (so inner rings paint on top)
    RINGS.forEach(rd => {
      const pts = _annularSector(
        lat, lon, spread_dir, HALF_ANGLE,
        typeof rd.inner === 'number' ? rd.inner : radii[rd.inner],
        radii[rd.outer],
      );
      const poly = L.polygon(pts, {
        color: rd.border, fillColor: rd.fill,
        fillOpacity: rd.opacity, weight: 1.5, opacity: 0.8,
      }).addTo(_map);
      poly.bindTooltip(rd.label, { sticky: true, className: 'fire-tooltip' });
      _layers.push(poly);
    });

    // Centerline dashed arrow
    const arrowTip = _offset(lat, lon, spread_dir, r60 * 1.08);
    const arrow = L.polyline([[lat, lon], arrowTip], {
      color: '#ffffff', weight: 2, opacity: 0.7, dashArray: '6 4',
    }).addTo(_map);
    _layers.push(arrow);

    // Flame marker
    _map.removeLayer(_originMarker);
    const windDesc = wind_from_deg != null
      ? `FROM ${Math.round(wind_from_deg)}° @ ${wind_spd_mph} mph`
      : `${wind_spd_mph} mph assumed`;

    const wind_toward   = wind_from_deg != null ? (wind_from_deg + 180) % 360 : null;
    const use_slope     = slope.slope_deg >= MIN_SLOPE;
    const slope_note    = use_slope
      ? `slope ${slope.slope_deg}° toward ${Math.round(slope.upslope_deg)}° pulling ${Math.round(Math.abs(spread_dir - (wind_toward ?? spread_dir)))}° off wind`
      : `flat terrain — wind only`;

    _originMarker = L.marker([lat, lon], {
      icon: L.divIcon({ className: '', html: '<div class="fire-origin-marker">🔥</div>',
        iconSize: [30,30], iconAnchor: [15,28] }),
      zIndexOffset: 2000,
    }).bindPopup(`
      <div class="popup-title">🔥 Fire Origin</div>
      <div class="popup-row"><span class="pk">Elevation</span><span class="pv">${slope.elevation_m} m / ${Math.round(slope.elevation_m * 3.281)} ft</span></div>
      <div class="popup-row"><span class="pk">Slope</span><span class="pv">${slope.slope_deg}°</span></div>
      <div class="popup-row"><span class="pk">Wind</span><span class="pv">${windDesc}</span></div>
      <div class="popup-row"><span class="pk">Wind src</span><span class="pv">${wind_source}</span></div>
      ${wind_toward != null ? `<div class="popup-row"><span class="pk">Wind →</span><span class="pv">${Math.round(wind_toward)}° (downwind)</span></div>` : ''}
      <div class="popup-row"><span class="pk">Spread dir</span><span class="pv">${Math.round(spread_dir)}° &nbsp;<em style="color:var(--muted);font-size:10px">${slope_note}</em></span></div>
      <div class="popup-row"><span class="pk">Spread rate</span><span class="pv">~${rate_mph.toFixed(1)} mph <em style="color:var(--muted);font-size:10px">(${wind_spd_mph} mph × 0.3 Rothermel)</em></span></div>
      <div class="popup-row"><span class="pk">15 min</span><span class="pv">${r15.toFixed(2)} mi</span></div>
      <div class="popup-row"><span class="pk">30 min</span><span class="pv">${r30.toFixed(2)} mi</span></div>
      <div class="popup-row"><span class="pk">60 min</span><span class="pv">${r60.toFixed(2)} mi</span></div>
    `).addTo(_map);

    _setStatus(`Spread ${Math.round(spread_dir)}° · ~${rate_mph.toFixed(1)} mph est. (wind ${wind_spd_mph} mph)`);

  } catch (err) {
    _setStatus(`Error: ${err.message}`);
    console.error('[FireSpread]', err);
  }
}

// ── Spread direction: circular weighted mean of wind-toward + upslope ────────
function _calcSpreadDir(wind_from_deg, slope) {
  const wind_toward = wind_from_deg != null ? (wind_from_deg + 180) % 360 : null;
  const use_slope   = slope.slope_deg >= MIN_SLOPE;

  if (!wind_toward && !use_slope) return 0;
  if (!wind_toward) return slope.upslope_deg;
  if (!use_slope)   return wind_toward;

  // Weighted circular mean
  const toRad = d => d * Math.PI / 180;
  const wx = 0.7 * Math.sin(toRad(wind_toward)) + 0.3 * Math.sin(toRad(slope.upslope_deg));
  const wy = 0.7 * Math.cos(toRad(wind_toward)) + 0.3 * Math.cos(toRad(slope.upslope_deg));
  return (Math.atan2(wx, wy) * 180 / Math.PI + 360) % 360;
}

// ── Geometry ──────────────────────────────────────────────────────────────────
function _offset(lat, lon, bearing_deg, dist_mi) {
  const br = bearing_deg * Math.PI / 180;
  return [
    lat + (dist_mi / 69.0) * Math.cos(br),
    lon + (dist_mi / (69.0 * Math.cos(lat * Math.PI / 180))) * Math.sin(br),
  ];
}

function _annularSector(lat, lon, bearing, half_ang, r_inner, r_outer) {
  const s = bearing - half_ang;
  const e = bearing + half_ang;

  const outer = [];
  for (let i = 0; i <= ARC_STEPS; i++)
    outer.push(_offset(lat, lon, s + (e - s) * i / ARC_STEPS, r_outer));

  const inner = [];
  for (let i = 0; i <= ARC_STEPS; i++) {
    const b = e - (e - s) * i / ARC_STEPS;
    inner.push(r_inner > 0 ? _offset(lat, lon, b, r_inner) : [lat, lon]);
  }

  const pts = [...outer, ...inner];
  pts.push(pts[0]);
  return pts;
}
