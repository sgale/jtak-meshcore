// ── RF Signal Heatmap ─────────────────────────────────────────────────────────
// One circleMarker per contact (no bucketing) so each marker maps 1:1 to the
// contact list.  Circles start fully visible; updateHeatmapTime() dims future
// contacts as the timeline is scrubbed.

import { renderTerrain } from './terrain.js';

let _map      = null;
let _layer    = null;
let _markers  = [];    // [{circle, contact, idx}]
let _onSelect = null;  // (contactIdx) → void

// ── Color helpers ─────────────────────────────────────────────────────────────

function _rssiColor(rssi) {
  if (rssi == null)  return '#9e9e9e';
  if (rssi >= -65)   return '#00e5ff';
  if (rssi >= -75)   return '#00e676';
  if (rssi >= -85)   return '#ffea00';
  if (rssi >= -100)  return '#ff6d00';
  return '#f44336';
}

function _rssiRadius(rssi) {
  if (rssi == null)  return 6;
  if (rssi >= -65)   return 10;
  if (rssi >= -75)   return 8;
  if (rssi >= -85)   return 7;
  return 6;
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Render all contacts as circles.
 * @param {L.Map} map
 * @param {Array} contacts  — S.contacts from app.js
 * @param {Object} opts
 *   onSelect(idx)  — called when a circle is clicked (idx into contacts array)
 */
export function renderHeatmap(map, contacts, { onSelect } = {}) {
  clearHeatmap(map);
  _map      = map;
  _onSelect = onSelect || null;
  _layer    = L.layerGroup().addTo(map);
  _markers  = [];

  contacts.forEach((c, idx) => {
    if (c.node_lat == null || c.node_lon == null) return;

    const color  = _rssiColor(c.rssi);
    const radius = _rssiRadius(c.rssi);
    const _n = v => { const n = +v; return (v == null || v === '' || isNaN(n)) ? null : n; };
    const rssiStr  = _n(c.rssi)           != null ? `${_n(c.rssi)} dBm`                      : '—';
    const distStr  = _n(c.distance_mi)    != null ? `${_n(c.distance_mi).toFixed(2)} mi`     : '—';
    const timeStr  = c.ts ? c.ts.slice(11, 19) : '';
    const snrStr   = _n(c.snr)            != null ? `${_n(c.snr)} dB`                        : '—';
    const bearStr  = _n(c.bearing_deg)    != null ? `${Math.round(_n(c.bearing_deg))}°`      : '—';
    const plStr    = _n(c.path_loss_db)   != null ? `${_n(c.path_loss_db).toFixed(1)} dB`    : '—';
    const fsplStr  = _n(c.predicted_fspl) != null ? `${_n(c.predicted_fspl).toFixed(1)} dB`  : '—';
    const exStr    = _n(c.excess_loss_db) != null ? `${_n(c.excess_loss_db).toFixed(1)} dB`  : '—';

    const circle = L.circleMarker([c.node_lat, c.node_lon], {
      radius,
      fillColor: color, fillOpacity: 0.75,
      color: color, weight: 1.5, opacity: 0.9,
    });

    circle.bindPopup(`
      <div class="popup-title">${c.source_name || c.source_id}</div>
      <div class="popup-time">${timeStr}</div>
      <div class="popup-grid">
        <span class="pk">RSSI</span>       <span class="pv" style="color:${color};font-weight:700">${rssiStr}</span>
        <span class="pk">SNR</span>        <span class="pv">${snrStr}</span>
        <span class="pk">Distance</span>   <span class="pv">${distStr}</span>
        <span class="pk">Bearing</span>    <span class="pv">${bearStr}</span>
        <span class="pk">Path loss</span>  <span class="pv">${plStr}</span>
        <span class="pk">FSPL</span>       <span class="pv">${fsplStr}</span>
        <span class="pk">Excess loss</span><span class="pv">${exStr}</span>
        <span class="pk">Elev delta</span> <span class="pv popup-elev-delta">${c.elev_delta_m != null ? Math.round(c.elev_delta_m) + ' m' : '…'}</span>
        <span class="pk">Elev angle</span> <span class="pv popup-elev-angle">${c.elev_angle_deg != null ? c.elev_angle_deg.toFixed(1) + '°' : '…'}</span>
        <span class="pk">Hub</span>        <span class="pv">${c.hub_name || c.hub_id || '—'}</span>
      </div>
      <div class="popup-terrain"></div>
    `, { className: 'contact-popup', maxWidth: 320, minWidth: 280 });

    let _ctrl = null;
    circle.on('popupopen', () => {
      const popupEl = circle.getPopup()?.getElement();
      if (!popupEl) return;
      const container = popupEl.querySelector('.popup-terrain');
      if (!container || container.dataset.loaded) return;
      container.dataset.loaded = '1';
      _ctrl = new AbortController();
      renderTerrain(c, container, _ctrl.signal).then(data => {
        if (!data) return;
        const el2 = circle.getPopup()?.getElement();
        if (!el2) return;
        if (c.elev_delta_m == null && data.node_alt_m != null && data.hub_alt_m != null) {
          const delta = data.node_alt_m - data.hub_alt_m;
          const distM = (c.distance_mi || data.dist_mi || 0) * 1609.34;
          const angle = distM > 0 ? (Math.atan2(delta, distM) * 180 / Math.PI).toFixed(1) : '0.0';
          const dEl = el2.querySelector('.popup-elev-delta');
          const aEl = el2.querySelector('.popup-elev-angle');
          if (dEl) dEl.textContent = `${Math.round(delta)} m`;
          if (aEl) aEl.textContent = `${angle}°`;
        }
      });
    });
    circle.on('popupclose', () => { if (_ctrl) { _ctrl.abort(); _ctrl = null; } });

    circle.bindTooltip(`${c.source_name || c.source_id} · ${rssiStr}`, { sticky: true });

    circle.on('click', () => {
      if (_onSelect) _onSelect(idx);
    });

    _markers.push({ circle, contact: c, idx });
    _layer.addLayer(circle);
  });

}

/**
 * Dim/show circles based on current virtual playback time.
 * Contacts at or before virtualMs are fully visible; future ones are dim.
 * Pass Infinity to show all (default/initial state).
 */
export function updateHeatmapTime(virtualMs) {
  for (const { circle, contact } of _markers) {
    const past = contact._ms <= virtualMs;
    circle.setStyle({
      fillOpacity: past ? 0.78 : 0.08,
      opacity:     past ? 0.95 : 0.15,
    });
  }
}

/**
 * Highlight a specific contact's circle (brief pulse + bring to front).
 * Used when user clicks a contact in the list.
 */
export function highlightHeatmapContact(idx) {
  const entry = _markers.find(m => m.idx === idx);
  if (!entry) return;
  const { circle, contact } = entry;

  // Make sure it's visible
  circle.setStyle({ fillOpacity: 0.95, opacity: 1 });

  // Pulse ring animation
  if (!_map || !contact.node_lat || !contact.node_lon) return;
  const color    = circle.options.fillColor || '#fff';
  const baseR    = circle.options.radius || 8;
  const maxR     = baseR + 36;
  const duration = 2400;   // ms
  const start    = performance.now();

  const ring = L.circleMarker([contact.node_lat, contact.node_lon], {
    radius: baseR, color, weight: 3, fillOpacity: 0.15, opacity: 1.0,
  }).addTo(_map);
  // Second inner ring for punch
  const ring2 = L.circleMarker([contact.node_lat, contact.node_lon], {
    radius: baseR * 0.6, color, weight: 2, fillOpacity: 0, opacity: 0.7,
  }).addTo(_map);

  const step = (now) => {
    const frac = Math.min((now - start) / duration, 1);
    const ease = 1 - Math.pow(1 - frac, 2);
    if (frac >= 1) { _map.removeLayer(ring); _map.removeLayer(ring2); return; }
    ring.setRadius(baseR + ease * (maxR - baseR));
    ring.setStyle({ opacity: (1 - frac), fillOpacity: (1 - frac) * 0.1, weight: 3 - frac * 2 });
    ring2.setRadius(baseR * 0.6 + ease * 14);
    ring2.setStyle({ opacity: (1 - frac) * 0.55 });
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// Open the popup for a contact at a given index, rebinding with that contact's exact data
export function openHeatmapPopup(map, idx, contact = null) {
  const entry = _markers.find(m => m.idx === idx);
  if (!entry) return false;
  if (map) map.closePopup();
  if (contact) {
    // Rebind popup so it shows THIS contact's data (not whatever was bound at render time)
    const c = contact;
    const _n = v => { const n = +v; return (v == null || v === '' || isNaN(n)) ? null : n; };
    const color    = _rssiColor(c.rssi);
    const rssiStr  = _n(c.rssi)           != null ? `${_n(c.rssi)} dBm`                      : '—';
    const snrStr   = _n(c.snr)            != null ? `${_n(c.snr)} dB`                        : '—';
    const distStr  = _n(c.distance_mi)    != null ? `${_n(c.distance_mi).toFixed(2)} mi`     : '—';
    const bearStr  = _n(c.bearing_deg)    != null ? `${Math.round(_n(c.bearing_deg))}°`      : '—';
    const plStr    = _n(c.path_loss_db)   != null ? `${_n(c.path_loss_db).toFixed(1)} dB`    : '—';
    const fsplStr  = _n(c.predicted_fspl) != null ? `${_n(c.predicted_fspl).toFixed(1)} dB`  : '—';
    const exStr    = _n(c.excess_loss_db) != null ? `${_n(c.excess_loss_db).toFixed(1)} dB`  : '—';
    const timeStr  = c.ts ? c.ts.slice(11, 19) : '';
    entry.circle.bindPopup(`
      <div class="popup-title">${c.source_name || c.source_id}</div>
      <div class="popup-time">${timeStr}</div>
      <div class="popup-grid">
        <span class="pk">RSSI</span>       <span class="pv" style="color:${color};font-weight:700">${rssiStr}</span>
        <span class="pk">SNR</span>        <span class="pv">${snrStr}</span>
        <span class="pk">Distance</span>   <span class="pv">${distStr}</span>
        <span class="pk">Bearing</span>    <span class="pv">${bearStr}</span>
        <span class="pk">Path loss</span>  <span class="pv">${plStr}</span>
        <span class="pk">FSPL</span>       <span class="pv">${fsplStr}</span>
        <span class="pk">Excess loss</span><span class="pv">${exStr}</span>
        <span class="pk">Elev delta</span> <span class="pv popup-elev-delta">${c.elev_delta_m != null ? Math.round(c.elev_delta_m) + ' m' : '…'}</span>
        <span class="pk">Elev angle</span> <span class="pv popup-elev-angle">${c.elev_angle_deg != null ? c.elev_angle_deg.toFixed(1) + '°' : '…'}</span>
        <span class="pk">Hub</span>        <span class="pv">${c.hub_name || c.hub_id || '—'}</span>
      </div>
      <div class="popup-terrain"></div>
    `, { className: 'contact-popup', maxWidth: 320, minWidth: 280 });
  }
  entry.circle.openPopup();
  return true;
}

export function clearHeatmap(map) {
  if (_layer && map) map.removeLayer(_layer);
  _layer   = null;
  _markers = [];
  _map     = null;
}
