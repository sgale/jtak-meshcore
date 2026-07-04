// ── Weather — NOAA online data, activated by internet toggle ─────────────────
const API = '/jtak/api';
const POLL_MS = 5 * 60 * 1000;   // 5 min — NOAA updates hourly

let _pollTimer = null;
let _windMarker = null;           // Leaflet wind arrow marker on map
let _map = null;
let _hubPos = null;               // {latitude, longitude} for the API call
let _lastWeather = null;
let _lastWeatherTs = null;        // epoch ms when _lastWeather was fetched
let _localSensors = { temp_f: null, rh: null };  // BME sensor data from status API

// ── Ember particle system — Albini (1983) spotting distance ──────────────────
let _leafCanvas = null;
let _leafCtx    = null;
let _leafAnimId = null;
let _leafParticles = [];
let _leafWind   = { dir: 0, spd: 0 };
let _leafLastT  = null;
let _emberActive  = false;   // toggled by HUD EMBER SPOT button
let _emberOrigin  = null;    // {latitude, longitude} — user-clicked map point
let _spotLayers   = [];      // Leaflet polygon layers for spotting cones
let _spotMarker   = null;    // burning-tree marker at origin

// Spotting distance in meters — two-regime model:
//   Surface fire  (I < 2000 kW/m): Albini (1979) point-source lofting
//   Crown/intense (I ≥ 2000 kW/m): convective column model
//     H_plume = 1.5 * sqrt(I)  [empirical, matches large-fire observations]
// Wind-aloft correction: log wind profile lifts effective speed 15-50% above surface
// Terminal velocity: 0.4 m/s (fine bark flakes) vs the prior 1.0 m/s (bark chunks)
function _spotDistM(windMph, intensityKwm) {
  const U_surf = windMph * 0.44704;   // surface wind m/s (NOAA at ~6m)

  let Hb;
  if (intensityKwm < 2000) {
    // Low-intensity surface fire: Albini (1979) fireline lofting height
    Hb = 0.39 * Math.pow(intensityKwm, 1 / 3);
  } else {
    // Crown / intense fire: convective column height drives ember loft
    Hb = 1.5 * Math.sqrt(intensityKwm);
  }

  // Log wind profile — embers ride at roughly Hb/2 effective height.
  // U_eff = U_surf * ln(Hb/2 / z0) / ln(6 / z0), z0 = 0.1m (open terrain)
  const z0   = 0.1;
  const zRef = 6.0;
  const zEmb = Math.max(Hb / 2, z0 + 0.1);
  const U_eff = U_surf * (Math.log(zEmb / z0) / Math.log(zRef / z0));

  const Vt = 0.4;   // fine bark flake terminal velocity (m/s)
  return U_eff * Hb / Vt;
}

// Map 1-hr FM → fireline intensity class
function _fdfmToIntensity(fdfm) {
  if (fdfm == null || fdfm > 16) return { kw:    500, label: 'LOW',     color: 'rgba(255,210,0,0.22)'  };
  if (fdfm > 12)                  return { kw:  2_000, label: 'MOD',     color: 'rgba(255,140,0,0.22)'  };
  if (fdfm > 8)                   return { kw:  5_000, label: 'HIGH',    color: 'rgba(255,60,0,0.25)'   };
  return                                 { kw: 10_000, label: 'EXTREME', color: 'rgba(200,0,0,0.28)'    };
}

// Pixels per meter at current zoom (uses hub lat/lon reference)
function _pxPerMeter() {
  if (!_map || !_hubPos || _hubPos.latitude == null) return 0;
  const lat = _hubPos.latitude, lon = _hubPos.longitude;
  const p0 = _map.latLngToContainerPoint([lat, lon]);
  const p1 = _map.latLngToContainerPoint([lat + 0.009, lon]); // ~1 km north
  return Math.abs(p1.y - p0.y) / 1000;
}

function _startLeaves() {
  if (_leafCanvas || !_map) return;
  const container = _map.getContainer();
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:450;';
  container.appendChild(canvas);
  _leafCanvas = canvas;
  _leafCtx = canvas.getContext('2d');
  const resize = () => {
    canvas.width  = container.offsetWidth;
    canvas.height = container.offsetHeight;
  };
  resize();
  _map.on('resize', resize);
  _leafLastT = null;
  _leafAnimate();
}

function _stopLeaves() {
  if (_leafAnimId) { cancelAnimationFrame(_leafAnimId); _leafAnimId = null; }
  if (_leafCanvas) { _leafCanvas.remove(); _leafCanvas = null; }
  _leafParticles = [];
}

function _leafAnimate() {
  const now = performance.now();
  const dt  = _leafLastT ? Math.min((now - _leafLastT) / 1000, 0.05) : 0;
  _leafLastT = now;

  const W = _leafCanvas.width, H = _leafCanvas.height;

  _leafCtx.clearRect(0, 0, W, H);

  // Spawn from clicked ember origin, fall back to hub position
  let arcX = W / 2, arcY = H / 2;
  const _spawnPos = _emberOrigin || _hubPos;
  if (_spawnPos && _spawnPos.latitude != null && _map) {
    const pt = _map.latLngToContainerPoint([_spawnPos.latitude, _spawnPos.longitude]);
    arcX = pt.x; arcY = pt.y;
  }

  const spd = _leafWind.spd;
  if (spd >= 0.5) {
    const moveDeg  = (_leafWind.dir + 180) % 360;
    const moveRad  = (moveDeg - 90) * Math.PI / 180;
    const pxPerSec = spd * 5;
    const vxBase   = Math.cos(moveRad) * pxPerSec;
    const vyBase   = Math.sin(moveRad) * pxPerSec;

    // ── Embers — spawn from hub/tree position ────────────────────────────
    const target = Math.min(40, Math.max(8, Math.round(spd * 1.6)));
    while (_leafParticles.length < target) {
      _leafParticles.push(_spawnLeaf(arcX, arcY, vxBase, vyBase, spd));
    }

    _leafParticles = _leafParticles.filter(p => {
      p.life += dt;
      if (p.life >= p.maxLife) return false;
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      // Perpendicular thermal flutter
      const flutter = Math.sin(p.life * p.flutterFreq + p.flutterPhase) * spd * 0.15;
      p.x += -Math.sin(moveRad + Math.PI / 2) * flutter * dt;
      p.y +=  Math.cos(moveRad + Math.PI / 2) * flutter * dt;
      const fadeIn  = Math.min(1, p.life / 0.3);
      const fadeOut = Math.min(1, (p.maxLife - p.life) / 0.8);
      p.opacity = p.maxOpacity * Math.min(fadeIn, fadeOut);
      _drawEmber(_leafCtx, p, now);
      return true;
    });
  }

  _leafAnimId = requestAnimationFrame(_leafAnimate);
}

function _spawnLeaf(hubX, hubY, vxBase, vyBase, spd) {
  const spread  = 20 + spd * 1.5;
  const vSpread = spd * 0.2;
  return {
    x: hubX + (Math.random() - 0.5) * spread,
    y: hubY + (Math.random() - 0.5) * spread,
    vx: vxBase + (Math.random() - 0.5) * vSpread,
    vy: vyBase + (Math.random() - 0.5) * vSpread,
    size: 2.5 + Math.random() * 3.5,
    opacity: 0,
    maxOpacity: 0.7 + Math.random() * 0.3,
    life: Math.random() * 2.5,
    maxLife: 4 + Math.random() * 5,
    flutterFreq:  2.5 + Math.random() * 3,
    flutterPhase: Math.random() * Math.PI * 2,
    flickerRate:  8 + Math.random() * 12,   // Hz — fast flicker like a flame
    flickerPhase: Math.random() * Math.PI * 2,
  };
}

function _drawEmber(ctx, p, now) {
  // Cooling: 0=fresh white-yellow, 1=dead dark red
  const t = Math.max(0, Math.min(1, p.life / p.maxLife));

  // Fast brightness flicker — simulates flame lick
  const flicker = 0.7 + 0.3 * Math.sin(now * 0.001 * p.flickerRate + p.flickerPhase);
  const r = p.size * flicker;

  // Color cools from white→yellow→orange→red→ember-red
  let core, glow, glowR;
  if (t < 0.15) {
    core = `rgba(255,255,200,${p.opacity})`; glow = '#ffe066'; glowR = r * 5;
  } else if (t < 0.35) {
    core = `rgba(255,210,80,${p.opacity})`;  glow = '#ff9900'; glowR = r * 4;
  } else if (t < 0.6) {
    core = `rgba(255,120,20,${p.opacity})`;  glow = '#ff4400'; glowR = r * 3;
  } else if (t < 0.8) {
    core = `rgba(200,50,10,${p.opacity})`;   glow = '#cc2200'; glowR = r * 2;
  } else {
    core = `rgba(120,20,5,${p.opacity})`;    glow = '#660000'; glowR = r * 1.5;
  }

  ctx.save();
  ctx.shadowColor = glow;
  ctx.shadowBlur  = glowR;
  ctx.beginPath();
  ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
  ctx.fillStyle = core;
  ctx.fill();
  // Hot white core on fresh embers
  if (t < 0.4) {
    ctx.shadowBlur = 0;
    ctx.beginPath();
    ctx.arc(p.x, p.y, r * 0.38, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(255,255,255,${p.opacity * (1 - t / 0.4) * 0.9})`;
    ctx.fill();
  }
  ctx.restore();
}

// ── Ember spotting cones (Leaflet polygons, toggled by wind sock click) ───────

const SPOT_HALF_ANG = 35;
const SPOT_STEPS    = 48;

function _geoOffset(lat, lon, bearing_deg, dist_mi) {
  const br = bearing_deg * Math.PI / 180;
  return [
    lat + (dist_mi / 69.0) * Math.cos(br),
    lon + (dist_mi / (69.0 * Math.cos(lat * Math.PI / 180))) * Math.sin(br),
  ];
}

function _spotSector(lat, lon, bearing, r_inner, r_outer) {
  const s = bearing - SPOT_HALF_ANG;
  const e = bearing + SPOT_HALF_ANG;
  const outer = [];
  for (let i = 0; i <= SPOT_STEPS; i++)
    outer.push(_geoOffset(lat, lon, s + (e - s) * i / SPOT_STEPS, r_outer));
  const inner = [];
  for (let i = 0; i <= SPOT_STEPS; i++) {
    const b = e - (e - s) * i / SPOT_STEPS;
    inner.push(r_inner > 0.0005 ? _geoOffset(lat, lon, b, r_inner) : [lat, lon]);
  }
  return [...outer, ...inner, outer[0]];
}

function _fmtMi(mi) {
  return mi >= 0.10 ? `${mi.toFixed(2)} mi` : `${Math.round(mi * 5280)} ft`;
}

function _drawSpotCones() {
  _clearSpotCones();
  if (!_map) return;
  const origin = _emberOrigin || _effectivePos();
  if (!origin || !origin.latitude) return;

  const lat = origin.latitude;
  const lon = origin.longitude;

  // Use live wind; fall back to last fetched weather if _leafWind hasn't been set yet
  let spd     = _leafWind.spd;
  let fromDir = _leafWind.dir;
  if (spd === 0 && _lastWeather && _lastWeather.wind_spd_mph) {
    spd     = _lastWeather.wind_spd_mph;
    fromDir = _lastWeather.wind_dir_deg || 0;
  }

  if (spd < 0.5) {
    // No usable wind data — draw a placeholder marker so user knows it placed
    const marker = L.circleMarker([lat, lon], {
      radius: 8, color: '#f5a623', fillColor: '#f5a623',
      fillOpacity: 0.5, weight: 2,
    }).bindTooltip('⚠ No wind data yet — cones will appear when weather loads', {
      className: 'fire-tooltip', permanent: false,
    }).addTo(_map);
    _spotLayers.push(marker);
    return;
  }

  const downwind = (fromDir + 180) % 360;
  const modMi  = _spotDistM(spd, 2_000)  / 1609.34;
  const highMi = _spotDistM(spd, 5_000)  / 1609.34;
  const extrMi = _spotDistM(spd, 10_000) / 1609.34;

  const tiers = [
    { inner: highMi, outer: extrMi, fill: '#555555', border: '#333333', opacity: 0.50, label: `EXTREME MAX SPOT · ${_fmtMi(extrMi)}` },
    { inner: modMi,  outer: highMi, fill: '#888888', border: '#555555', opacity: 0.50, label: `HIGH MAX SPOT · ${_fmtMi(highMi)}` },
    { inner: 0,      outer: modMi,  fill: '#bbbbbb', border: '#888888', opacity: 0.50, label: `MOD MAX SPOT · ${_fmtMi(modMi)}` },
  ];

  tiers.forEach(tier => {
    if (tier.outer < 0.0005) return;
    const pts  = _spotSector(lat, lon, downwind, tier.inner, tier.outer);
    const poly = L.polygon(pts, {
      color: tier.border, fillColor: tier.fill,
      fillOpacity: tier.opacity, weight: 1.5, opacity: 0.9,
    }).addTo(_map);
    poly.bindTooltip(`🌲 ${tier.label}`, { className: 'fire-tooltip', sticky: true });
    _spotLayers.push(poly);
  });

  // Dashed centerline — only draw if there's a meaningful distance
  if (extrMi >= 0.001) {
    const tip  = _geoOffset(lat, lon, downwind, extrMi * 1.05);
    const line = L.polyline([[lat, lon], tip], {
      color: '#fff', weight: 2, opacity: 0.55, dashArray: '6 4',
    }).addTo(_map);
    _spotLayers.push(line);
  }
}

function _clearSpotCones() {
  _spotLayers.forEach(l => { try { _map.removeLayer(l); } catch {} });
  _spotLayers = [];
  if (_spotMarker && _map) { try { _map.removeLayer(_spotMarker); } catch {} _spotMarker = null; }
}

function _burningTreeSvg() {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 50" width="36" height="46">
    <!-- Trunk -->
    <rect x="17" y="36" width="6" height="12" fill="#6d4c41" rx="1"/>
    <!-- Pine canopy — 3 tiered layers -->
    <polygon points="20,32 4,44 36,44" fill="#1b5e20"/>
    <polygon points="20,22 6,36 34,36" fill="#2e7d32"/>
    <polygon points="20,12 8,28 32,28" fill="#388e3c"/>
    <!-- Top spire -->
    <polygon points="20,2 14,18 26,18" fill="#43a047"/>
  </svg>`;
}

// ── EMBER SPOT HUD toggle — called from app.js ────────────────────────────────
// Returns new active state so the caller can update the button UI.
export function toggleEmberSpot(lat, lon) {
  _emberOrigin = (lat != null && lon != null) ? { latitude: lat, longitude: lon } : null;
  _emberActive = !_emberActive;
  if (_emberActive) {
    _startLeaves();
    _drawSpotCones();
  } else {
    _stopLeaves();
    _clearSpotCones();
    _emberOrigin = null;
  }
  return _emberActive;
}

export function isEmberActive() { return _emberActive; }

export function deactivateEmber() {
  if (!_emberActive) return;
  _emberActive = false;
  _stopLeaves();
  _clearSpotCones();
  _emberOrigin = null;
}

export function setEmberOrigin(lat, lon) {
  _emberOrigin = { latitude: lat, longitude: lon };
  if (_emberActive) _drawSpotCones();
}

// ── end spotting cones ────────────────────────────────────────────────────────

const weatherPanel  = document.getElementById('weather-panel');
const weatherBody   = document.getElementById('weather-body');
const windHud       = document.getElementById('wind-hud');

// Map center fallback — used when hub GPS isn't locked
const MAP_CENTER = { latitude: 40.5729, longitude: -111.9941 };

// ── Called from app.js once map + hub position are known ─────────────────────
export function initWeather(map, hubPosition) {
  _map = map;
  _hubPos = hubPosition;
}

export function setHubPosition(pos) {
  _hubPos = pos;
}

// Called from app.js whenever status is fetched — keeps FDFM using local sensor
export function updateLocalSensors(temp_f, rh) {
  _localSensors = { temp_f, rh };
  // Re-render HUD immediately if weather is already loaded
  if (_lastWeather) _renderHud(_lastWeather);
}

// ── Fine Dead Fuel Moisture — NFDRS Simard (1968) equations ──────────────────
// Inputs: temp in °F, RH in %. Returns EMC% (≈ 1-hr FDFM), or null.
function _calcFDFM(temp_f, rh) {
  if (temp_f == null || rh == null) return null;
  let emc;
  if (rh < 10) {
    emc = 0.03229 + 0.281073 * rh - 0.000578 * temp_f * rh;
  } else if (rh < 50) {
    emc = 2.22749 + 0.160107 * rh - 0.014784 * temp_f;
  } else {
    emc = 21.0606 + 0.005565 * rh * rh - 0.00035 * temp_f * rh - 0.483199 * rh;
  }
  return Math.max(1, Math.round(emc * 10) / 10);
}

function _fdfmDanger(pct) {
  if (pct == null)  return { label: '—',        cls: '' };
  if (pct < 8)      return { label: 'EXTREME',   cls: 'fdfm-extreme' };
  if (pct < 12)     return { label: 'HIGH',      cls: 'fdfm-high' };
  if (pct < 16)     return { label: 'MODERATE',  cls: 'fdfm-mod' };
  if (pct < 25)     return { label: 'LOW',       cls: 'fdfm-low' };
  return             { label: 'VERY LOW',  cls: 'fdfm-low' };
}

function _effectivePos() {
  if (_hubPos && _hubPos.latitude) return _hubPos;
  return MAP_CENTER;   // fall back to map center when GPS not locked
}

// ── Start / stop polling based on onlineMode toggle ───────────────────────────
export function startWeather() {
  if (_pollTimer) return;
  _fetchAndRender();
  _pollTimer = setInterval(_fetchAndRender, POLL_MS);
  if (weatherPanel) weatherPanel.style.display = '';
}

export function stopWeather() {
  clearInterval(_pollTimer);
  _pollTimer = null;
  _emberActive = false;
  // Keep _lastWeather — firespread uses it for up to 1 hour even when offline
  if (weatherBody) weatherBody.innerHTML = '<div class="rf-row"><span class="rf-label">Offline</span></div>';
  if (weatherPanel) weatherPanel.style.display = 'none';
  if (windHud) windHud.style.display = 'none';
  if (_windMarker && _map) { _map.removeLayer(_windMarker); _windMarker = null; }
  _stopLeaves();
  _clearSpotCones();
}

async function _fetchAndRender() {
  const pos = _effectivePos();
  try {
    const r = await fetch(`${API}/weather?lat=${pos.latitude}&lon=${pos.longitude}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const w = await r.json();
    _lastWeather   = w;
    _lastWeatherTs = Date.now();
    _renderSidebar(w);
    _renderHud(w);
    _renderWindArrow(w);
  } catch(e) {
    if (weatherBody) weatherBody.innerHTML =
      `<div class="rf-row"><span class="rf-label">NOAA unavailable: ${e.message}</span></div>`;
  }
}

function _renderSidebar(w) {
  if (!weatherBody) return;
  const rows = [
    { label: 'CONDITIONS', val: w.description || '—' },
    { label: 'WIND',       val: _windStr(w), online: true },
    { label: 'TEMP',       val: w.temp_f != null ? `${w.temp_f}°F / ${w.temp_c}°C` : '—', online: true },
    { label: 'HUMIDITY',   val: w.humidity_pct != null ? `${w.humidity_pct}%` : '—', online: true },
    { label: 'BARO',       val: w.baro_hpa != null ? `${w.baro_hpa} hPa` : '—', online: true },
    { label: 'STATION',    val: w.station || '—' },
  ];
  weatherBody.innerHTML = rows.map(r => `
    <div class="rf-row">
      <span class="rf-label">${r.label}</span>
      <span class="rf-val">
        ${r.online ? '<span class="online-globe" title="NOAA — online data">&#127760;</span>' : ''}
        ${r.val}
      </span>
    </div>`).join('');
}

function _renderHud(w) {
  if (!windHud) return;
  const dir  = w.wind_dir_deg != null ? w.wind_dir_deg : null;
  const card = w.wind_cardinal || '—';
  const spd  = w.wind_spd_mph  != null ? `${w.wind_spd_mph} mph` : '—';
  const gust = w.wind_gust_mph != null ? `gust ${w.wind_gust_mph} mph` : '';

  // 1-hr FM: prefer NOAA temp+RH (station), fall back to local BME sensor
  const tf  = w.temp_f       ?? _localSensors.temp_f;
  const rh  = w.humidity_pct ?? _localSensors.rh;
  const src = (w.temp_f != null) ? 'NWS' : 'STA';
  const fdfm   = _calcFDFM(tf, rh);
  const danger = _fdfmDanger(fdfm);

  // Ignition Component (IC) — probability 0-100 that a firebrand ignites fine fuels
  // NFDRS: IC driven by FM and wind speed
  const windMph = w.wind_spd_mph ?? 0;
  const ic = fdfm != null
    ? Math.round(Math.max(0, Math.min(100, (33 - fdfm) / 33 * 100 * Math.pow(Math.max(windMph, 1), 0.46) / Math.pow(30, 0.46))))
    : null;
  const icLabel = ic == null ? null
    : ic >= 80 ? 'CRITICAL' : ic >= 60 ? 'HIGH' : ic >= 35 ? 'MOD' : 'LOW';
  const icCls = ic == null ? '' : ic >= 80 ? 'fdfm-extreme' : ic >= 60 ? 'fdfm-high' : ic >= 35 ? 'fdfm-mod' : 'fdfm-low';

  windHud.style.display = '';
  windHud.innerHTML = `
    ${dir != null ? `<span class="wind-hud-sock">${_windsockSvg(dir, 36)}</span>` : ''}
    <div class="wind-hud-text">
      <span class="wind-hud-dir">WIND FROM ${card}</span>
      <span class="wind-hud-spd">${spd}</span>
      ${gust ? `<span class="wind-hud-gust">${gust}</span>` : ''}
      ${fdfm != null ? `<span class="wind-hud-fdfm" title="1-Hour Fine Dead Fuel Moisture — estimated moisture % of thin dead vegetation (grass, needles, twigs). Lower = drier = more fire danger.">
        <span class="fdfm-val">1-hr FM ${fdfm}%</span>
        <span class="fdfm-label ${danger.cls}">${danger.label}</span>
        <span class="fdfm-src">${src}</span>
      </span>` : ''}
      ${ic != null ? `<span class="wind-hud-fdfm" title="Ignition Component (NFDRS) — 0–100 probability that a spark or ember ignites fine fuels given current moisture and wind. 80+ = extreme ignition risk.">
        <span class="fdfm-val">IC ${ic}%</span>
        <span class="fdfm-label ${icCls}">${icLabel}</span>
      </span>` : ''}
    </div>
  `;
}

function _renderWindArrow(w) {
  if (!_map || w.wind_dir_deg == null) return;

  const pos  = _effectivePos();
  const dir  = w.wind_dir_deg;
  const spd  = w.wind_spd_mph != null ? `${w.wind_spd_mph} mph` : '';
  const card = w.wind_cardinal || '';

  // Update leaf particle wind state; redraw cones if ember spot is active
  const prevSpd = _leafWind.spd;
  _leafWind.dir = dir;
  _leafWind.spd = w.wind_spd_mph || 0;
  if (_emberActive) _drawSpotCones();  // refresh cones on wind update (handles no-wind → wind transition)


}

// SVG windsock: opening faces wind source (FROM direction), tail points downwind.
// size = diameter of the containing circle in px.
function _windsockSvg(windFromDeg, size) {
  // SVG opening faces LEFT (West = 270° compass) in default orientation.
  // To make it face compass bearing D, rotate by (D + 90) % 360.
  const rot = (windFromDeg + 90) % 360;
  const s   = size;
  const hw  = s / 2;
  return `<svg width="${s}" height="${s}" viewBox="0 0 100 100"
               style="transform:rotate(${rot}deg);display:block;overflow:visible;">
    <!-- Mounting post center -->
    <circle cx="50" cy="50" r="5" fill="#c8cdd4" opacity="0.9"/>
    <!-- Arm pointing left (toward wind source) -->
    <line x1="50" y1="50" x2="10" y2="50"
          stroke="#c8cdd4" stroke-width="3" stroke-linecap="round"/>
    <!-- Sock: opens at left (x=10), tapers to right (x=82) -->
    <!-- Top edge -->
    <path d="M10,38 Q46,36 82,48" fill="none" stroke="#f97316" stroke-width="2.5" stroke-linecap="round"/>
    <!-- Bottom edge -->
    <path d="M10,62 Q46,64 82,52" fill="none" stroke="#f97316" stroke-width="2.5" stroke-linecap="round"/>
    <!-- Fill -->
    <path d="M10,38 Q46,36 82,48 Q82,52 82,52 Q46,64 10,62 Z"
          fill="#f97316" fill-opacity="0.75"/>
    <!-- Opening ring -->
    <ellipse cx="10" cy="50" rx="3" ry="12" fill="none" stroke="#ff8c42" stroke-width="2.5"/>
    <!-- Stripes -->
    <line x1="28" y1="39.5" x2="28" y2="60.5" stroke="white" stroke-width="2" opacity="0.55"/>
    <line x1="48" y1="38.5" x2="48" y2="61.5" stroke="white" stroke-width="2" opacity="0.55"/>
    <line x1="65" y1="41"   x2="65" y2="59"   stroke="white" stroke-width="2" opacity="0.55"/>
  </svg>`;
}

export function getWeather() {
  if (!_lastWeather || !_lastWeatherTs) return null;
  if (Date.now() - _lastWeatherTs > 60 * 60 * 1000) return null;  // expired after 1 hour
  return _lastWeather;
}

// Returns age of cached weather in whole minutes, or -1 if no data
export function getWeatherAge() {
  if (!_lastWeatherTs) return -1;
  return Math.floor((Date.now() - _lastWeatherTs) / 60000);
}

function _windStr(w) {
  if (w.wind_dir_deg == null && w.wind_spd_mph == null) return '—';
  const dir = w.wind_cardinal ? `FROM ${w.wind_cardinal}` : '';
  const spd = w.wind_spd_mph  != null ? `${w.wind_spd_mph} mph` : '';
  const gst = w.wind_gust_mph != null ? ` (gust ${w.wind_gust_mph})` : '';
  return [dir, spd + gst].filter(Boolean).join(' · ');
}
