// ── Map — Leaflet + tactical markers ──────────────────────────────────────
import { rssiClass, rssiLabel, fmtDist, fmtBearing, timeAgo } from './utils.js';
import { localFirstTileLayer } from './tilecache.js';

let map;
const markers = {};          // source_id → L.Marker
const rssiHistory = {};      // source_id → number[] (last 10 readings)

const HUB_IDS = new Set(['!677811c9', '!4cc9f0c3', '!34a6049d']);

const RSSI_WEAK_THRESH  = -80;   // dBm — start pulsing
const GHOST_THRESH_MS   = 10 * 60 * 1000;  // 10 minutes

// ── Rolling RSSI average ──────────────────────────────────────────────────
function pushRssi(source_id, rssi) {
  if (rssi == null) return;
  if (!rssiHistory[source_id]) rssiHistory[source_id] = [];
  rssiHistory[source_id].push(rssi);
  if (rssiHistory[source_id].length > 10) rssiHistory[source_id].shift();
}

function avgRssi(source_id) {
  const h = rssiHistory[source_id];
  if (!h || h.length === 0) return null;
  return h.reduce((a, b) => a + b, 0) / h.length;
}

// ── Marker state logic ────────────────────────────────────────────────────
function rssiBorderClass(rssi) {
  if (rssi == null) return '';
  if (rssi >= -65)  return 'rssi-excellent';
  if (rssi >= -75)  return 'rssi-good';
  if (rssi >= -85)  return 'rssi-marginal';
  if (rssi >= -100) return 'rssi-poor';
  return 'rssi-verypoor';
}

function markerState(node) {
  const lastSeen = node.last_position || node.ts;
  const age = lastSeen ? Date.now() - new Date(lastSeen).getTime() : Infinity;

  if (age > GHOST_THRESH_MS) return 'ghost';

  const avg = avgRssi(node.source_id);
  if (avg !== null && avg < RSSI_WEAK_THRESH) return 'weak';

  return 'normal';
}

function makeIcon(node, isSelf = false) {
  if (isSelf) {
    return L.divIcon({
      className: '',
      html: `<div class="marker-self"></div>`,
      iconSize:   [30, 30],
      iconAnchor: [8, 26],
      popupAnchor:[8, -26],
    });
  }
  const isHub  = HUB_IDS.has(node.source_id);
  const state  = markerState(node);
  const base   = isHub ? 'marker-hub' : 'marker-node';
  const extra  = state === 'ghost' ? 'marker-ghost'
               : state === 'weak'  ? 'marker-weak'
               : '';
  const rssiCls = state === 'ghost' ? '' : rssiBorderClass(avgRssi(node.source_id));
  const showArrow = (
    state !== 'ghost' &&
    node.heading_deg != null &&
    node.speed_mph != null && node.speed_mph >= 0.5 &&
    (node.motion_age_s == null || node.motion_age_s <= 180)
  );
  const arrow = showArrow
    ? `<div class="marker-arrow" style="transform:rotate(${node.heading_deg}deg)">
         <svg width="14" height="22" viewBox="0 0 14 22" style="filter:drop-shadow(0 0 3px rgba(0,0,0,0.9))">
           <polygon points="7,0 14,22 7,16 0,22" fill="#7ecfff" stroke="#0a1628" stroke-width="1.5"/>
           <polygon points="7,2 11,18 7,14 3,18"  fill="white"  opacity="0.45"/>
         </svg>
       </div>`
    : '';
  return L.divIcon({
    className: '',
    html: `<div class="marker-wrap"><div class="${base} ${extra} ${rssiCls}"></div>${arrow}</div>`,
    iconSize:   [24, 24],
    iconAnchor: [12, 24],
    popupAnchor:[0, -26],
  });
}

function popupHtml(node) {
  const avg = avgRssi(node.source_id);
  const state = markerState(node);
  const stateTag = state === 'ghost' ? ' ☐ GHOST' : state === 'weak' ? ' ⚠ WEAK' : '';
  const tempF = node.temp_f != null ? `${Math.round(node.temp_f)}°F` : null;
  const tempC = node.temp_c != null ? `${Math.round(node.temp_c)}°C` : null;
  const tempStr = tempF && tempC ? `${tempF} (${tempC})` : '—';
  const rows = [
    ['ID',       node.source_id],
    ['RSSI',     rssiLabel(node.rssi)],
    ['Avg RSSI', avg != null ? `${avg.toFixed(1)} dBm` : '—'],
    ['SNR',      node.snr != null ? `${node.snr} dB` : '—'],
    ['Distance', fmtDist(node.distance_mi)],
    ['Bearing',  fmtBearing(node.bearing_deg)],
    ['Battery',  node.battery_pct != null ? `${Math.round(node.battery_pct)}%` : '—'],
    ...(node.speed_mph != null ? [
      ['Speed',   `${node.speed_mph.toFixed(1)} mph`],
      ['Heading', node.heading_deg != null ? `${Math.round(node.heading_deg)}°` : 'stationary'],
    ] : []),
    ...(node.temp_c != null ? [
      ['Temp',     tempStr],
      ['Humidity', node.humidity_pct != null ? `${Math.round(node.humidity_pct)}%` : '—'],
      ['Pressure', node.pressure_hpa != null ? `${Math.round(node.pressure_hpa)} hPa` : '—'],
    ] : []),
    ['Last seen',timeAgo(node.last_position || node.ts)],
  ];
  const rowsHtml = rows.map(([k,v]) =>
    `<div class="popup-row"><span class="pk">${k}</span><span class="pv">${v}</span></div>`
  ).join('');
  const dmBtn = `<button class="popup-dm-btn" data-id="${node.source_id}" data-name="${node.source_name || node.source_id}">💬 Message</button>`;
  return `<div class="popup-title">${node.source_name || node.source_id}${stateTag}</div>${rowsHtml}${dmBtn}`;
}

// ── Basemap definitions ───────────────────────────────────────────────────
// 'OSM (Offline)' uses the local tile cache — works without internet.
// All other layers require internet and cannot use the local cache.
const BASEMAPS = {
  'Dark':           L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
                      { subdomains: 'abcd', maxZoom: 20, attribution: '© OpenStreetMap © CARTO' }),
  'OSM (Offline)':  localFirstTileLayer(),
  'Satellite':      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                      { maxZoom: 20, attribution: '© Esri © USGS' }),
  'Hybrid':         L.layerGroup([
                      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                        { maxZoom: 20, attribution: '© Esri' }),
                      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
                        { maxZoom: 20, opacity: 0.8 }),
                    ]),
  'Topo':           L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
                      { subdomains: 'abc', maxZoom: 17, attribution: '© OpenTopoMap © OpenStreetMap' }),
  'USGS Topo':      L.tileLayer('https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',
                      { maxZoom: 16, attribution: '© USGS National Map' }),
  'Street':         L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
                      { subdomains: 'abcd', maxZoom: 20, attribution: '© OpenStreetMap © CARTO' }),
};

// ── Map init ──────────────────────────────────────────────────────────────
export function initMap(center, zoom) {
  map = L.map('map', { center, zoom, zoomControl: false, attributionControl: false });
  L.control.zoom({ position: 'bottomright' }).addTo(map);

  const saved = sessionStorage.getItem('jtak_basemap') || 'Satellite';
  const active = BASEMAPS[saved] || BASEMAPS['Satellite'];
  active.addTo(map);

  L.control.layers(BASEMAPS, {}, { position: 'bottomright', collapsed: true })
    .addTo(map);

  map.on('baselayerchange', e => sessionStorage.setItem('jtak_basemap', e.name));

  // Popup DM button — event delegation on the map pane
  document.getElementById('map').addEventListener('click', e => {
    const btn = e.target.closest('.popup-dm-btn');
    if (!btn) return;
    document.dispatchEvent(new CustomEvent('mesh:compose', {
      detail: { nodeId: btn.dataset.id, nodeName: btn.dataset.name }
    }));
  });

  return map;
}

// ── Offline basemap switching ──────────────────────────────────────────────
function _activeBasemapName() {
  return Object.keys(BASEMAPS).find(name => {
    const layer = BASEMAPS[name];
    return map.hasLayer(layer) ||
      (layer.getLayers && layer.getLayers().length && map.hasLayer(layer.getLayers()[0]));
  }) || 'Dark';
}

function _setBasemap(name) {
  const next = BASEMAPS[name];
  if (!next) return;
  Object.values(BASEMAPS).forEach(l => {
    if (map.hasLayer(l)) map.removeLayer(l);
    if (l.getLayers) l.getLayers().forEach(sub => { if (map.hasLayer(sub)) map.removeLayer(sub); });
  });
  next.addTo(map);
  sessionStorage.setItem('jtak_basemap', name);
}

export function switchToOffline() {
  const current = _activeBasemapName();
  if (current !== 'OSM (Offline)') {
    sessionStorage.setItem('jtak_basemap_before_offline', current);
    _setBasemap('OSM (Offline)');
  }
}

export function restoreBasemap() {
  const prev = sessionStorage.getItem('jtak_basemap_before_offline');
  if (prev && prev !== 'OSM (Offline)') {
    _setBasemap(prev);
    sessionStorage.removeItem('jtak_basemap_before_offline');
  }
}

// ── Update node marker ────────────────────────────────────────────────────
export function updateNode(node) {
  const { source_id, source_name, latitude: lat, longitude: lon } = node;
  if (!lat || !lon) return;

  // Track RSSI history
  if (node.rssi != null) pushRssi(source_id, node.rssi);

  const icon   = makeIcon(node);
  const latlng = [lat, lon];

  if (markers[source_id]) {
    markers[source_id].setLatLng(latlng);
    markers[source_id].setIcon(icon);
    markers[source_id].setPopupContent(popupHtml(node));
  } else {
    const m = L.marker(latlng, { icon })
      .bindPopup(popupHtml(node), { maxWidth: 240 })
      .addTo(map);
    m.bindTooltip(source_name || source_id, {
      permanent: false, direction: 'top',
      offset: [0, -18],
    });
    markers[source_id] = m;
  }
}

// ── Periodic marker refresh (re-evaluate ghost/weak state) ───────────────
export function startMarkerRefresh(nodeCache) {
  setInterval(() => {
    Object.values(nodeCache).forEach(node => {
      if (markers[node.source_id]) {
        markers[node.source_id].setIcon(makeIcon(node));
        markers[node.source_id].setPopupContent(popupHtml(node));
      }
    });
  }, 30000);  // re-evaluate every 30s
}

// ── Self "you are here" marker ────────────────────────────────────────────
let selfMarker = null;
let selfStatus = null;   // latest status payload for popup

export function updateSelfMarker(pos, status) {
  if (!pos || !pos.latitude || !pos.longitude) return;
  if (status) selfStatus = status;

  const s = selfStatus || {};
  const latlng = [pos.latitude, pos.longitude];

  const popup = `
    <div class="popup-title">${pos.source_name || 'This Hub'}</div>
    <div class="popup-row"><span class="pk">ID</span><span class="pv">${pos.source_id || '—'}</span></div>
    <div class="popup-row"><span class="pk">CPU</span><span class="pv">${s.cpu_pct != null ? Math.round(s.cpu_pct) + '%' : '—'} / ${s.cpu_temp_c != null ? Math.round(s.cpu_temp_c) + '°C' : '—'}</span></div>
    <div class="popup-row"><span class="pk">Mem</span><span class="pv">${s.mem_pct != null ? s.mem_pct.toFixed(0) + '%' : '—'}</span></div>
    <div class="popup-row"><span class="pk">Disk free</span><span class="pv">${s.disk_free_gb != null ? s.disk_free_gb + ' GB' : '—'}</span></div>
    ${s.bme_temp_f ? `<div class="popup-row"><span class="pk">Ext temp</span><span class="pv">${s.bme_temp_f}°F</span></div>` : ''}
  `;

  if (selfMarker) {
    selfMarker.setLatLng(latlng);
    selfMarker.setPopupContent(popup);
  } else {
    selfMarker = L.marker(latlng, { icon: makeIcon(null, true), zIndexOffset: 1000 })
      .bindPopup(popup, { maxWidth: 220 })
      .addTo(map);
    selfMarker.bindTooltip(pos.source_name || 'This Hub', {
      permanent: false,
      direction: 'top',
      offset: [0, -22],
      className: 'tooltip-self',
    });
  }
}

// ── Center on this hub at max zoom ───────────────────────────────────────
export function centerOnHub(pos) {
  if (!pos || !pos.latitude || !pos.longitude) return;
  map.setView([pos.latitude, pos.longitude], 19, { animate: true });
}

export function fitAllNodes() {
  const pts = Object.values(markers).map(m => m.getLatLng());
  if (pts.length > 1) map.fitBounds(L.latLngBounds(pts), { padding: [40, 40] });
  else if (pts.length === 1) map.setView(pts[0], 15);
}

export function panTo(source_id) {
  if (markers[source_id]) {
    map.panTo(markers[source_id].getLatLng());
    markers[source_id].openPopup();
  }
}
