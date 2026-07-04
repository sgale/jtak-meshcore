// ── Playback engine — animates RF contacts along timeline ────────────────────
//
// virtualTime: the "current" session timestamp (ISO string → ms epoch).
// At each RAF tick, virtualTime advances by (real_elapsed_ms × speed).
// Contacts whose ts ≤ virtualTime are considered "heard."
// Each contact gets a brief "ping" ring, then a persistent node marker.
// Nodes not heard in the last WINDOW_MS of virtual time are grayed-out.

import { renderTerrain } from './terrain.js';

const WINDOW_MS  = 10 * 60 * 1000;   // 10 min virtual window keeps node "active"
const PING_MS    = 2800;              // ping animation real-time duration (ms)
const PING_MAX_R = 38;               // max ping ring radius (px)

let _mapMode     = 'dark';  // 'dark' | 'light' — controls pin outline style
let _map         = null;
let _contacts    = [];   // sorted by ts (ms)
let _startMs     = 0;
let _endMs       = 0;
let _speed       = 30;   // default 30× — changed at runtime by speed buttons
let _playing     = false;
let _virtualMs   = 0;    // current virtual time in ms
let _lastRealMs  = null; // real time of last RAF frame
let _rafId       = null;
let _cursor      = 0;    // index into _contacts of next unseen contact

// Leaflet layers
let _hubMarkers  = {};   // hub_id → L.marker
let _hubTrails   = {};   // hub_id → { polyline, points: [[lat,lon]] }
let _nodeMarkers = {};   // source_id → { marker, lastMs }
let _pingLayer   = null; // layer for ping animations
let _pings       = [];   // [{circle, startRealMs}]

// Callbacks provided by app.js
let _onTick      = null; // (virtualMs) → void
let _onContact   = null; // (contact) → void
let _heatmapMode = false; // when true: skip node/hub markers (heatmap manages the map)

// ─────────────────────────────────────────────────────────────────────────────

export function initPlayback(map, contacts, { onTick, onContact, heatmapMode = false } = {}) {
  _map          = map;

  // Ensure every contact has a valid _ms; recompute from ts if missing/NaN
  const fixed = contacts.slice().map(c => {
    if (c._ms != null && !isNaN(c._ms)) return c;
    const ts = (c.ts || '').replace(' ', 'T');
    const ms = new Date(ts).getTime();
    return { ...c, _ms: ms };
  }).filter(c => !isNaN(c._ms));

  if (fixed.length !== contacts.length) {
    console.warn('[PB] initPlayback: dropped', contacts.length - fixed.length, 'contacts with invalid _ms');
  }


  _contacts     = fixed.sort((a, b) => a._ms - b._ms);
  _onTick       = onTick    || (() => {});
  _onContact    = onContact || (() => {});
  _heatmapMode  = heatmapMode;

  _startMs   = _contacts.length ? _contacts[0]._ms  : Date.now();
  _endMs     = _contacts.length ? _contacts[_contacts.length - 1]._ms : _startMs + 3600000;
  _virtualMs = _startMs;
  _cursor    = 0;

  // Init ping layer
  clearPlayback();
  _pingLayer = L.layerGroup().addTo(_map);

  return { startMs: _startMs, endMs: _endMs };
}

export function startPlayback() {
  if (_playing) return;
  _playing    = true;
  _lastRealMs = null;
  _rafId      = requestAnimationFrame(_tick);
}

export function pausePlayback() {
  _playing = false;
  if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
}

export function stopPlayback() {
  pausePlayback();
  seekPlayback(_startMs);
}

export function seekPlayback(ms) {
  const wasPlaying = _playing;
  pausePlayback();

  _virtualMs = Math.max(_startMs, Math.min(_endMs, ms));
  _cursor    = 0;

  // Re-emit all contacts up to new position (without animation)
  _clearNodeLayer();
  for (let i = 0; i < _contacts.length; i++) {
    const c = _contacts[i];
    if (c._ms <= _virtualMs) {
      _cursor = i + 1;
      if (!_heatmapMode) {
        _upsertHubMarker(c);
        _upsertNodeMarker(c, false);
      }
    } else {
      break;
    }
  }
  _pruneInactive();
  _onTick(_virtualMs);

  if (wasPlaying) startPlayback();
}

export function setSpeedPlayback(s) {
  _speed = s;
}

// Open the popup for a given source_id, optionally rebinding with a specific contact's data
export function openNodePopup(sourceId, contact = null) {
  const nm = _nodeMarkers[sourceId];
  if (!nm) return false;
  if (_map) _map.closePopup();
  if (contact) _bindNodePopup(nm.marker, contact);
  nm.marker.openPopup();
  return true;
}

// Remove markers for specific source_ids without clearing the whole layer
export function pruneExcluded(excludedIds) {
  const set = new Set(excludedIds);
  for (const [id, nm] of Object.entries(_nodeMarkers)) {
    if (set.has(id)) {
      if (_map) _map.removeLayer(nm.marker);
      delete _nodeMarkers[id];
    }
  }
}

export function setMapMode(mode) {
  // mode: 'dark' | 'light'
  _mapMode = mode;
  // Refresh all existing node markers with new pin style
  for (const id of Object.keys(_nodeMarkers)) {
    const nm = _nodeMarkers[id];
    const color  = _rssiColor(nm.contact?.rssi);
    const label  = nm.contact?.source_name || id;
    const iconFn = _isHubNode(nm.contact || {}) ? _hubNodeIcon : _nodeIcon;
    nm.marker.setIcon(iconFn(label, color));
  }
}

export function clearPlayback() {
  pausePlayback();
  _clearNodeLayer();
  _clearHubLayer();
  if (_pingLayer && _map) { _map.removeLayer(_pingLayer); }
  _pingLayer  = null;
  _pings      = [];
}

// ── RAF loop ──────────────────────────────────────────────────────────────────

function _tick(realMs) {
  if (!_playing) return;

  // Always reschedule first
  if (_playing) _rafId = requestAnimationFrame(_tick);

  // ── Advance virtual time ──────────────────────────────────────────────────
  if (_lastRealMs !== null) {
    const elapsed = realMs - _lastRealMs;
    _virtualMs   += elapsed * _speed;
    if (_virtualMs >= _endMs) {
      _virtualMs = _endMs;
      _playing   = false;
      cancelAnimationFrame(_rafId);
      _rafId = null;
    }
  }
  _lastRealMs = realMs;

  // ── Emit contacts that have become visible ────────────────────────────────
  while (_cursor < _contacts.length && _contacts[_cursor]._ms <= _virtualMs) {
    const c = _contacts[_cursor++];
    if (!_heatmapMode) {
      try { _upsertHubMarker(c); }  catch (e) { console.warn('[Playback] hub marker error:', e); }
      try { _upsertNodeMarker(c, true); } catch (e) { console.warn('[Playback] node marker error:', c.source_id, e); }
    }
    try { _onContact(c); } catch (e) { /* ignore */ }
  }

  // ── Animate pings + prune + tick callback ─────────────────────────────────
  try { _tickPings(realMs); }    catch (e) { /* ignore */ }
  try { _pruneInactive(); }      catch (e) { /* ignore */ }
  try { _onTick(_virtualMs); }   catch (e) { console.warn('[Playback] onTick error:', e); }
}

// ── Hub markers ───────────────────────────────────────────────────────────────

function _upsertHubMarker(c) {
  if (!c.hub_lat || !c.hub_lon) return;
  const id  = c.hub_id || 'hub';
  const ll  = [c.hub_lat, c.hub_lon];

  // Trail
  if (!_hubTrails[id]) {
    const polyline = L.polyline([], {
      color: '#ffd740', weight: 2, opacity: 0.55, dashArray: '4 6',
    }).addTo(_map);
    _hubTrails[id] = { polyline, points: [] };
  }
  const trail = _hubTrails[id];
  const last  = trail.points[trail.points.length - 1];
  // Only add point if hub moved >10m from last point (avoid clutter)
  if (!last || Math.abs(last[0] - ll[0]) > 0.0001 || Math.abs(last[1] - ll[1]) > 0.0001) {
    trail.points.push(ll);
    trail.polyline.setLatLngs(trail.points);
  }

  // Marker
  if (_hubMarkers[id]) {
    _hubMarkers[id].setLatLng(ll);
  } else {
    const icon = L.divIcon({
      className: '',
      html: `<div class="marker-hub" title="${c.hub_name || id}"></div>`,
      iconSize: [28, 28], iconAnchor: [6, 22],
    });
    const m = L.marker(ll, { icon, zIndexOffset: 1000 });
    m.bindTooltip(c.hub_name || id, { permanent: false });
    m.addTo(_map);
    _hubMarkers[id] = m;
  }
}

// ── Node markers ──────────────────────────────────────────────────────────────

function _rssiColor(rssi) {
  if (rssi == null)  return '#9e9e9e';
  if (rssi >= -65)   return '#00e5ff';
  if (rssi >= -75)   return '#00e676';
  if (rssi >= -85)   return '#ffea00';
  if (rssi >= -100)  return '#ff6d00';
  return '#f44336';
}

function _isHubNode(c) {
  const name = (c.source_name || c.source_id || '').toUpperCase();
  return name.includes('HUB') || c.source_id === c.hub_id;
}

function _upsertNodeMarker(c, withPing) {
  if (!c.node_lat || !c.node_lon) return;
  const id    = c.source_id;
  const color = _rssiColor(c.rssi);
  const label = c.source_name || id;
  const iconFn = _isHubNode(c) ? _hubNodeIcon : _nodeIcon;

  if (_nodeMarkers[id]) {
    const nm = _nodeMarkers[id];
    nm.marker.setLatLng([c.node_lat, c.node_lon]);
    nm.marker.setIcon(iconFn(label, color));
    nm.lastMs  = c._ms;
    nm.contact = c;
    // Update popup content in-place — avoids re-adding event listeners on every contact
    nm.marker.getPopup()?.setContent(_nodePopupHtml(c));
    nm.marker.getTooltip()?.setContent(label);
  } else {
    const icon = iconFn(label, color);
    const m = L.marker([c.node_lat, c.node_lon], { icon, zIndexOffset: 500 });
    _bindNodePopup(m, c);
    m.bindTooltip(label, { direction: 'top', offset: [0, -6], className: 'node-hover-tip' });
    m.addTo(_map);
    _nodeMarkers[id] = { marker: m, lastMs: c._ms, contact: c };
  }

  if (withPing && _pingLayer) {
    const strokeColor  = _mapMode === 'dark' ? color : '#000';
    const strokeWeight = _mapMode === 'dark' ? 2.5   : 3.5;
    const ring1 = L.circleMarker([c.node_lat, c.node_lon], {
      radius: 8, color: strokeColor, weight: strokeWeight, fillOpacity: 0, opacity: 1.0,
      interactive: false,
    }).addTo(_pingLayer);
    const ring2 = L.circleMarker([c.node_lat, c.node_lon], {
      radius: 4, color, weight: 1.5, fillColor: color, fillOpacity: 0.3, opacity: 0.9,
      interactive: false,
    }).addTo(_pingLayer);

    // Floating short-name label that fades with the ping
    const shortName = c.source_name || c.source_id || '';
    const label = L.marker([c.node_lat, c.node_lon], {
      icon: L.divIcon({
        className: '',
        html: `<div class="ping-label" style="color:${color}">${shortName}</div>`,
        iconSize:   null,
        iconAnchor: [-6, 28],   // offset: right of and above the node
      }),
      interactive: false,
      zIndexOffset: 2000,
    }).addTo(_pingLayer);

    _pings.push({ ring: ring1, ring2, label, startRealMs: performance.now() });
  }
}

function _nodeIcon(label, color) {
  const outline = _mapMode === 'dark'
    ? `drop-shadow(0 0 3px rgba(255,255,255,0.5))`
    : `drop-shadow(1px 0 0 #000) drop-shadow(-1px 0 0 #000) drop-shadow(0 1px 0 #000) drop-shadow(0 -1px 0 #000) drop-shadow(0 0 3px #000)`;
  const bg = _mapMode === 'dark' ? 'rgba(0,0,0,0.6)' : 'rgba(0,0,0,0.82)';
  return L.divIcon({
    className: '',
    html: `<div class="hist-node-pin" style="border-color:${color};color:${color};background:${bg};filter:${outline}" title="${label}">▲</div>`,
    iconSize: [20, 20], iconAnchor: [10, 10],
  });
}

function _hubNodeIcon(label, _color) {
  return L.divIcon({
    className: '',
    html: `<div class="marker-hub" title="${label}"></div>`,
    iconSize: [28, 28], iconAnchor: [6, 22],
  });
}

function _nodePopupHtml(c) {
  const _n = v => { const n = +v; return (v == null || v === '' || isNaN(n)) ? null : n; };

  // Self-reported hub GPS position — no RF link involved
  if (c.direct_or_relay === 'SELF') {
    const alt = _n(c.node_alt_m);
    return `
      <div class="popup-title">${c.source_name || c.source_id}</div>
      <div class="popup-time">${_fmtTime(c.ts)}</div>
      <div class="popup-self-badge">📍 Hub GPS Position</div>
      <div class="popup-grid">
        ${alt != null ? `<span class="pk">Altitude</span><span class="pv">${Math.round(alt)} m</span>` : ''}
        <span class="pk">Sats</span>      <span class="pv">${_n(c.hub_sats) != null ? _n(c.hub_sats) : '—'}</span>
        <span class="pk">HDOP</span>      <span class="pv">${_n(c.hub_hdop) != null ? _n(c.hub_hdop).toFixed(1) : '—'}</span>
      </div>
      <div class="popup-self-note">Self-reported gpsd position — no RF link data available</div>
    `;
  }

  const rssiCol = _rssiColor(c.rssi);
  return `
    <div class="popup-title">${c.source_name || c.source_id}</div>
    <div class="popup-time">${_fmtTime(c.ts)}</div>
    <div class="popup-grid">
      <span class="pk">RSSI</span>       <span class="pv">${_n(c.rssi) != null ? `${_n(c.rssi)} dBm <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${rssiCol};vertical-align:middle;margin-left:3px;"></span>` : '—'}</span>
      <span class="pk">SNR</span>        <span class="pv">${_n(c.snr) != null ? `${_n(c.snr)} dB` : '—'}</span>
      <span class="pk">Distance</span>   <span class="pv">${_n(c.distance_mi) != null ? `${_n(c.distance_mi).toFixed(2)} mi` : '—'}</span>
      <span class="pk">Bearing</span>    <span class="pv">${_n(c.bearing_deg) != null ? `${Math.round(_n(c.bearing_deg))}°` : '—'}</span>
      <span class="pk">Path loss</span>  <span class="pv">${_n(c.path_loss_db) != null ? `${_n(c.path_loss_db).toFixed(1)} dB` : '—'}</span>
      <span class="pk">FSPL</span>       <span class="pv">${_n(c.predicted_fspl) != null ? `${_n(c.predicted_fspl).toFixed(1)} dB` : '—'}</span>
      <span class="pk">Excess loss</span><span class="pv">${_n(c.excess_loss_db) != null ? `${_n(c.excess_loss_db).toFixed(1)} dB` : '—'}</span>
      <span class="pk">Elev delta</span> <span class="pv popup-elev-delta">${c.elev_delta_m != null ? Math.round(c.elev_delta_m) + ' m' : '…'}</span>
      <span class="pk">Elev angle</span> <span class="pv popup-elev-angle">${c.elev_angle_deg != null ? c.elev_angle_deg.toFixed(1) + '°' : '…'}</span>
      <span class="pk">Hub</span>        <span class="pv">${c.hub_name || c.hub_id || '—'}</span>
    </div>
    <div class="popup-terrain"></div>
  `;
}

function _bindNodePopup(marker, c) {
  marker.bindPopup(_nodePopupHtml(c), { className: 'contact-popup', maxWidth: 320, minWidth: 280 });

  let _ctrl = null;
  // .off().on() guards so repeated calls never stack duplicate listeners
  marker.off('popupopen').on('popupopen', () => {
    if (c.direct_or_relay === 'SELF') return;  // no terrain/LoS for self-position
    const popupEl   = marker.getPopup().getElement();
    if (!popupEl) return;
    const container = popupEl.querySelector('.popup-terrain');
    if (!container || container.dataset.loaded) return;
    container.dataset.loaded = '1';
    _ctrl = new AbortController();
    renderTerrain(c, container, _ctrl.signal).then(data => {
      // Back-fill elev delta/angle from terrain response if not in contact
      if (!data) return;
      const popupEl2 = marker.getPopup()?.getElement();
      if (!popupEl2) return;
      if (c.elev_delta_m == null && data.node_alt_m != null && data.hub_alt_m != null) {
        const delta = data.node_alt_m - data.hub_alt_m;
        const distM = (c.distance_mi || data.dist_mi || 0) * 1609.34;
        const angle = distM > 0 ? (Math.atan2(delta, distM) * 180 / Math.PI).toFixed(1) : '0.0';
        const dEl = popupEl2.querySelector('.popup-elev-delta');
        const aEl = popupEl2.querySelector('.popup-elev-angle');
        if (dEl) dEl.textContent = `${Math.round(delta)} m`;
        if (aEl) aEl.textContent = `${angle}°`;
      }
    });
  });
  marker.off('popupclose').on('popupclose', () => { if (_ctrl) { _ctrl.abort(); _ctrl = null; } });
}

// ── Ping animation ────────────────────────────────────────────────────────────

function _tickPings(realMs) {
  const surviving = [];
  for (const p of _pings) {
    const age  = realMs - p.startRealMs;
    const frac = Math.min(age / PING_MS, 1);
    if (frac >= 1) {
      if (_pingLayer) {
        _pingLayer.removeLayer(p.ring);
        if (p.ring2)  _pingLayer.removeLayer(p.ring2);
        if (p.label)  _pingLayer.removeLayer(p.label);
      }
      continue;
    }
    // Outer ring: expands and fades
    const ease = 1 - Math.pow(1 - frac, 2);   // ease-out
    p.ring.setRadius(8 + ease * (PING_MAX_R - 8));
    p.ring.setStyle({ opacity: (1 - frac) * 1.0, weight: 2.5 - frac * 1.5 });
    // Inner ring: slower pulse, stays smaller
    if (p.ring2) {
      p.ring2.setRadius(4 + ease * 12);
      p.ring2.setStyle({ opacity: (1 - frac) * 0.6, fillOpacity: (1 - frac) * 0.2 });
    }
    // Label: fade out over second half
    if (p.label) {
      const el = p.label.getElement();
      if (el) el.style.opacity = Math.max(0, 1 - frac * 2);
    }
    surviving.push(p);
  }
  _pings = surviving;
}

// ── Prune stale nodes ─────────────────────────────────────────────────────────

function _pruneInactive() {
  for (const [id, nm] of Object.entries(_nodeMarkers)) {
    const age = _virtualMs - nm.lastMs;
    const alpha = age > WINDOW_MS ? 0.25 : 1.0;
    // Grey-out icon opacity via CSS — re-apply only when crossing threshold
    const el = nm.marker.getElement();
    if (el) el.style.opacity = alpha;
  }
}

// ── Cleanup ───────────────────────────────────────────────────────────────────

function _clearNodeLayer() {
  for (const { marker } of Object.values(_nodeMarkers)) {
    if (_map) _map.removeLayer(marker);
  }
  _nodeMarkers = {};
  _pings.forEach(p => { if (_pingLayer) _pingLayer.removeLayer(p.ring); });
  _pings = [];
}

function _clearHubLayer() {
  for (const m of Object.values(_hubMarkers)) {
    if (_map) _map.removeLayer(m);
  }
  _hubMarkers = {};
  for (const t of Object.values(_hubTrails)) {
    if (_map) _map.removeLayer(t.polyline);
  }
  _hubTrails = {};
}

function _fmtTime(ts) {
  if (!ts) return '—';
  return ts.length > 10 ? ts.slice(11, 19) : ts;
}
