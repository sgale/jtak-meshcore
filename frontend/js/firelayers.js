// ── Fire Layers — WFIGS perimeters + NASA FIRMS hotspots ─────────────────────
// Activated when onlineMode turns on. Both layers cache on the frontend too
// so they persist when internet is toggled back off mid-mission.

const API      = '/jtak/api';
const WFIGS_MS = 30 * 60 * 1000;   // re-fetch perimeters every 30 min
const FIRMS_MS = 15 * 60 * 1000;   // re-fetch hotspots every 15 min

// Confidence → marker colour
const CONF_COLOR = { low: '#ff9900', nominal: '#ff4400', high: '#cc0000' };

let _map              = null;
let _wfigsLayer       = null;   // L.geoJSON
let _firmsLayer       = null;   // L.layerGroup of circle markers
let _wfigsTimer       = null;
let _firmsTimer       = null;
let _hubPos           = null;

// Cached GeoJSON for offline persistence
let _wfigsCache       = null;
let _firmsCache       = null;

// Hub state (ISO 3166-2, e.g. "US-UT") resolved once via Nominatim
let _hubState         = null;   // e.g. "US-UT"
let _hubStateName     = null;   // e.g. "Utah"
let _hubStateResolving = false;

export function initFireLayers(map, hubPos) {
  _map    = map;
  _hubPos = hubPos;
}

export function setFireHubPos(pos) {
  _hubPos = pos;
  // Kick off state resolution once we have a position and haven't done it yet
  if (pos && !_hubState && !_hubStateResolving) _resolveHubState(pos);
}

// ── Resolve hub's US state via Nominatim (one-time, cached in module) ─────────
async function _resolveHubState(pos) {
  _hubStateResolving = true;
  try {
    const url = `https://nominatim.openstreetmap.org/reverse?lat=${pos.latitude}&lon=${pos.longitude}&format=json&zoom=5`;
    const r = await fetch(url, { headers: { 'Accept-Language': 'en' } });
    if (!r.ok) return;
    const data = await r.json();
    // ISO3166-2-lvl4 gives "US-UT" style code; fallback to address.state_code
    _hubState     = data.address?.['ISO3166-2-lvl4'] || null;
    _hubStateName = data.address?.state || null;
    // Re-render HUD now that we know the state
    if (_wfigsCache || _firmsCache) _updateFireHud(_wfigsCache, _firmsCache);
  } catch (_) {
    // No internet or failed — fitToFires falls back to 50-mile radius
  } finally {
    _hubStateResolving = false;
  }
}

// ── Start (called when onlineMode ON) ─────────────────────────────────────────
export function startFireLayers() {
  _fetchWfigs();
  _fetchFirms();
  _wfigsTimer = setInterval(_fetchWfigs, WFIGS_MS);
  _firmsTimer = setInterval(_fetchFirms, FIRMS_MS);
}

// ── Stop (called when onlineMode OFF) — keep cached layers visible ────────────
export function stopFireLayers() {
  clearInterval(_wfigsTimer);
  clearInterval(_firmsTimer);
  _wfigsTimer = null;
  _firmsTimer = null;
  // Layers stay on map from cache — user can still see last known fire data
}

// ── Clear everything (called if user explicitly hides fire data) ───────────────
// ── Toast notification ─────────────────────────────────────────────────────────
function _fireToast(msg) {
  let t = document.getElementById('fire-toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'fire-toast';
    t.className = 'fire-toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('visible');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('visible'), 3500);
}

export function clearFireLayers() {
  stopFireLayers();
  if (_wfigsLayer) { _map.removeLayer(_wfigsLayer); _wfigsLayer = null; }
  if (_firmsLayer) { _map.removeLayer(_firmsLayer); _firmsLayer = null; }
}

// ── WFIGS perimeters ──────────────────────────────────────────────────────────
async function _fetchWfigs() {
  try {
    const r = await fetch(`${API}/fire/perimeters`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const geojson = await r.json();
    _wfigsCache = geojson;
    _renderWfigs(geojson);
    _updateFireHud(geojson, _firmsCache);
  } catch (e) {
    console.warn('[FireLayers] WFIGS fetch failed:', e.message);
    // If we have a cache, keep showing it
    if (_wfigsCache) _renderWfigs(_wfigsCache);
  }
}

function _renderWfigs(geojson) {
  if (_wfigsLayer) _map.removeLayer(_wfigsLayer);

  _wfigsLayer = L.geoJSON(geojson, {
    style: feat => ({
      color:       '#ff4400',
      fillColor:   '#ff2200',
      fillOpacity: 0.18,
      weight:      2,
      opacity:     0.85,
      dashArray:   '4 3',
    }),
    onEachFeature: (feat, layer) => {
      const p = feat.properties;
      const acres      = p.acres ? p.acres.toLocaleString() + ' ac' : '—';
      const contained  = p.contained_pct != null ? `${p.contained_pct}% contained` : 'containment unknown';
      const discovered = p.discovered ? new Date(p.discovered).toLocaleDateString() : '—';
      const inciwebUrl = `https://inciweb.nwcg.gov/?search=${encodeURIComponent(p.name)}`;
      layer.bindPopup(`
        <div class="popup-title">🔥 ${p.name}</div>
        <div class="popup-row"><span class="pk">Size</span><span class="pv">${acres}</span></div>
        <div class="popup-row"><span class="pk">Status</span><span class="pv">${contained}</span></div>
        <div class="popup-row"><span class="pk">State</span><span class="pv">${p.state || '—'}</span></div>
        <div class="popup-row"><span class="pk">Discovered</span><span class="pv">${discovered}</span></div>
        <div class="popup-row"><span class="pk">Source</span>
          <span class="pv"><span class="online-globe">&#127760;</span> NIFC WFIGS${geojson.cached ? ` (${geojson.cache_age_min}m ago)` : ''}</span>
        </div>
        <div class="popup-row" style="margin-top:6px">
          <a href="${inciwebUrl}" target="_blank" rel="noopener" class="popup-wfigs-link">&#128279; InciWeb — incident details</a>
        </div>
      `);
      layer.bindTooltip(p.name, { sticky: true });
    },
  }).addTo(_map);
}

// ── FIRMS hotspots ────────────────────────────────────────────────────────────
async function _fetchFirms() {
  const pos = _hubPos;
  if (!pos) return;
  try {
    const r = await fetch(`${API}/fire/hotspots?lat=${pos.latitude}&lon=${pos.longitude}`);
    if (!r.ok) {
      if (r.status === 503) return;   // key not configured — silently skip
      throw new Error(`HTTP ${r.status}`);
    }
    const data = await r.json();
    _firmsCache = data;
    _renderFirms(data);
    _updateFireHud(_wfigsCache, data);
  } catch (e) {
    console.warn('[FireLayers] FIRMS fetch failed:', e.message);
    if (_firmsCache) _renderFirms(_firmsCache);
  }
}

function _renderFirms(data) {
  if (_firmsLayer) _map.removeLayer(_firmsLayer);
  _firmsLayer = L.layerGroup();

  data.features.forEach(feat => {
    const [lon, lat] = feat.geometry.coordinates;
    const p          = feat.properties;
    const color      = CONF_COLOR[p.confidence] || CONF_COLOR.nominal;
    const radius     = p.frp ? Math.min(6 + p.frp * 0.04, 18) : 7;  // scale by Fire Radiative Power

    const circle = L.circleMarker([lat, lon], {
      radius,
      color:       color,
      fillColor:   color,
      fillOpacity: 0.75,
      weight:      1,
      opacity:     0.9,
    });

    const frpStr  = p.frp  != null ? `${p.frp} MW` : '—';
    const timeStr = p.acq_date && p.acq_time
      ? `${p.acq_date} ${p.acq_time.padStart(4,'0').replace(/(\d{2})(\d{2})/, '$1:$2')} UTC`
      : '—';

    circle.bindPopup(`
      <div class="popup-title">🌡 Thermal Hotspot</div>
      <div class="popup-row"><span class="pk">Confidence</span><span class="pv firms-conf-${p.confidence}">${p.confidence}</span></div>
      <div class="popup-row"><span class="pk">Fire Power</span><span class="pv">${frpStr}</span></div>
      <div class="popup-row"><span class="pk">Brightness</span><span class="pv">${p.brightness ? p.brightness + ' K' : '—'}</span></div>
      <div class="popup-row"><span class="pk">Detected</span><span class="pv">${timeStr}</span></div>
      <div class="popup-row"><span class="pk">Satellite</span><span class="pv">${p.satellite || '—'}</span></div>
      <div class="popup-row"><span class="pk">Source</span>
        <span class="pv"><span class="online-globe">&#127760;</span> NASA FIRMS${data.cached ? ` (${data.cache_age_min}m ago)` : ''}</span>
      </div>
    `);
    circle.bindTooltip(`${p.confidence} confidence hotspot`, { sticky: true });
    _firmsLayer.addLayer(circle);
  });

  _firmsLayer.addTo(_map);
}

// ── Fire HUD chip ─────────────────────────────────────────────────────────────
function _updateFireHud(wfigs, firms) {
  let el = document.getElementById('fire-data-hud');
  if (!el) {
    el = document.createElement('div');
    el.id = 'fire-data-hud';
    el.className = 'hud-chip';
    const chips = document.getElementById('hud-chips');
    const anchor = document.getElementById('center-hub-btn');
    if (chips && anchor) chips.insertBefore(el, anchor);
    else if (chips) chips.appendChild(el);
  }

  // Count fires filtered to hub's state; fall back to national total with label
  let perimCount = '—';
  let stateLabel = '';
  if (wfigs?.features) {
    if (_hubState) {
      const stateFeatures = wfigs.features.filter(f => f.properties?.state === _hubState);
      perimCount = stateFeatures.length;
      stateLabel = ` <span style="color:var(--muted);font-size:10px">${_hubStateName || _hubState}</span>`;
    } else {
      perimCount = wfigs.features.length;
      stateLabel = ` <span style="color:var(--muted);font-size:10px">US</span>`;
    }
  }
  const hotspotCount = firms?.features?.length ?? '—';
  el.innerHTML =
    `<span class="online-globe">&#127760;</span> ` +
    `<span style="color:var(--orange-l)">&#x25A0;</span> ${perimCount} fires${stateLabel}` +
    `&nbsp;<span style="color:#ff4400">&#9679;</span> ${hotspotCount} hotspots` +
    `&nbsp;<button id="fit-fires-btn" class="hud-fit-btn" title="Zoom to fires in state">&#x1F525; Fit</button>`;

  document.getElementById('fit-fires-btn').addEventListener('click', fitToFires);
}

// ── Haversine distance (miles) ─────────────────────────────────────────────────
function _haversineMi(lat1, lon1, lat2, lon2) {
  const R = 3958.8;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dLat/2)**2 +
            Math.cos(lat1*Math.PI/180) * Math.cos(lat2*Math.PI/180) * Math.sin(dLon/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

// ── Centroid of a GeoJSON polygon feature (bbox midpoint) ─────────────────────
function _featureCentroid(feat) {
  const coords = feat.geometry?.coordinates;
  if (!coords) return null;
  // Flatten all rings to get all [lon,lat] pairs
  const pts = feat.geometry.type === 'MultiPolygon'
    ? coords.flat(2)
    : coords.flat(1);
  if (!pts.length) return null;
  const lons = pts.map(p => p[0]);
  const lats = pts.map(p => p[1]);
  return {
    lat: (Math.min(...lats) + Math.max(...lats)) / 2,
    lon: (Math.min(...lons) + Math.max(...lons)) / 2,
  };
}

// ── Fit map to fires in hub's state (fallback: 50-mile radius) ───────────────
export function fitToFires() {
  if (!_wfigsCache?.features?.length) {
    _fireToast('No fire data loaded — enable internet first');
    return;
  }

  // ── Try state filter first ─────────────────────────────────────────────────
  if (_hubState) {
    const inState = _wfigsCache.features.filter(
      feat => feat.properties?.state === _hubState
    );
    if (inState.length) {
      const bounds = L.geoJSON({ type: 'FeatureCollection', features: inState }).getBounds();
      _map.fitBounds(bounds, { padding: [40, 40], maxZoom: 10 });
      _fireToast(`${inState.length} active fire${inState.length > 1 ? 's' : ''} in ${_hubStateName || _hubState}`);
      return;
    }
    _fireToast(`No active fires in ${_hubStateName || _hubState}`);
    return;
  }

  // ── Fallback: 50-mile radius ───────────────────────────────────────────────
  const hub = _hubPos;
  if (hub) {
    const nearby = _wfigsCache.features.filter(feat => {
      const c = _featureCentroid(feat);
      return c && _haversineMi(hub.latitude, hub.longitude, c.lat, c.lon) <= 50;
    });
    if (nearby.length) {
      const bounds = L.geoJSON({ type: 'FeatureCollection', features: nearby }).getBounds();
      _map.fitBounds(bounds, { padding: [40, 40], maxZoom: 10 });
      return;
    }
    _fireToast('No active fires within 50 mi');
    return;
  }

  // ── No position at all — fit to all national fires ─────────────────────────
  const bounds = L.geoJSON(_wfigsCache).getBounds();
  _map.fitBounds(bounds, { padding: [40, 40], maxZoom: 6 });
}
