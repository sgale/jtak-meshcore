// ── Zones — polygon/polyline drawing, KML export, QR code ───────────────────
// Supports: closed polygons (regions, perimeters) and open polylines (fire lines)
// KML export URL can be loaded as a live map layer in Meshtastic app

import { attachRowTooltip } from './utils.js';
const API = '/jtak/api';

let _map = null;
const _zones   = {};   // id → zone dict
const _layers  = {};   // id → L.Polygon | L.Polyline
const _layer   = L.layerGroup();

// ── Drawing state ─────────────────────────────────────────────────────────────
let _drawing     = false;
let _drawType    = 'polygon';   // 'polygon' | 'polyline'
let _drawColor   = '#ef4444';
let _drawPoints  = [];          // [{lat,lng}]
let _previewLine = null;        // L.Polyline — drawn so far
let _rubberLine  = null;        // L.Polyline — follows cursor
let _dotMarkers  = [];          // vertex dots

const COLORS = [
  { label: 'Fire Line',   value: '#ef4444' },
  { label: 'Perimeter',   value: '#f97316' },
  { label: 'Caution',     value: '#eab308' },
  { label: 'Region',      value: '#3b82f6' },
  { label: 'Safe Zone',   value: '#22c55e' },
  { label: 'Purple',      value: '#a855f7' },
];

// ── Init ──────────────────────────────────────────────────────────────────────

export function initZones(map) {
  _map = map;
  _layer.addTo(map);
  _buildPanel();
  _loadZones();
}

// ── WebSocket handler ─────────────────────────────────────────────────────────

export function onZoneMessage(msg) {
  if (!msg.id) return;
  if (msg.deleted_at) {
    _removeLayer(msg.id);
    delete _zones[msg.id];
  } else {
    _zones[msg.id] = { ..._zones[msg.id], ...msg };
    _renderLayer(_zones[msg.id]);
  }
  _renderPanel();
}

// ── Load ──────────────────────────────────────────────────────────────────────

async function _loadZones() {
  try {
    const r = await fetch(`${API}/polygons`);
    const list = await r.json();
    list.forEach(z => { _zones[z.id] = z; _renderLayer(z); });
    _renderPanel();
  } catch (e) {
    console.warn('[zones] load failed:', e);
  }
}

// ── Map layer rendering ───────────────────────────────────────────────────────

function _renderLayer(z) {
  if (z.deleted_at) { _removeLayer(z.id); return; }
  const geo   = z.geojson;
  const color = z.color || '#f97316';
  const opts  = { color, weight: 3, opacity: 0.9, fillColor: color, fillOpacity: 0.15 };

  _removeLayer(z.id);

  let lyr;
  if (geo.type === 'Polygon') {
    const latlngs = geo.coordinates[0].map(c => [c[1], c[0]]);
    lyr = L.polygon(latlngs, opts);
  } else if (geo.type === 'LineString') {
    const latlngs = geo.coordinates.map(c => [c[1], c[0]]);
    lyr = L.polyline(latlngs, { ...opts, fill: false });
  } else {
    return;
  }

  lyr.bindPopup(_popupHtml(z), { maxWidth: 240 });
  lyr.addTo(_layer);
  _layers[z.id] = lyr;
}

function _removeLayer(id) {
  if (_layers[id]) { _layer.removeLayer(_layers[id]); delete _layers[id]; }
}

function _popupHtml(z) {
  return `
    <div class="popup-title">${_esc(z.name)}</div>
    ${z.description ? `<div class="popup-row"><span class="pk">Note</span><span class="pv">${_esc(z.description)}</span></div>` : ''}
    <div class="popup-row"><span class="pk">Type</span><span class="pv">${z.type}</span></div>
    <div class="popup-row"><span class="pk">Created</span><span class="pv">${_timeAgo(z.created_at)}</span></div>
    <div style="display:flex;gap:6px;margin-top:6px;">
      <button class="popup-dm-btn" onclick="window._zoneEdit(${z.id})">✏ Edit</button>
      <button class="popup-dm-btn" onclick="window._zoneDelete(${z.id})" style="color:#f87171">✕ Delete</button>
    </div>`;
}

// ── Drawing ───────────────────────────────────────────────────────────────────

export function startDraw(type, color) {
  if (_drawing) _cancelDraw();
  _drawing   = true;
  _drawType  = type;
  _drawColor = color || '#ef4444';
  _drawPoints = [];
  _map.getContainer().style.cursor = 'crosshair';
  _map.on('click',    _onDrawClick);
  _map.on('dblclick', _onDrawDblClick);
  _map.on('mousemove', _onDrawMove);
  _updateDrawStatus('Click to place points — double-click to finish');
}

export function cancelDraw() { _cancelDraw(); }

function _cancelDraw() {
  _drawing = false;
  _map.getContainer().style.cursor = '';
  _map.off('click',    _onDrawClick);
  _map.off('dblclick', _onDrawDblClick);
  _map.off('mousemove', _onDrawMove);
  _clearPreview();
  _updateDrawStatus('');
  _refreshDrawButtons();
}

function _onDrawClick(e) {
  // Leaflet fires click before dblclick — ignore second click of dblclick
  if (e.originalEvent._drawing_dbl) return;
  _drawPoints.push(e.latlng);
  _addDot(e.latlng);
  _updatePreviewLine();
  const n = _drawPoints.length;
  _updateDrawStatus(`${n} point${n > 1 ? 's' : ''} — double-click to finish${n < 2 ? '' : ''}`);
}

function _onDrawDblClick(e) {
  e.originalEvent._drawing_dbl = true;
  if (_drawPoints.length < 2) return;
  _finishDraw();
}

function _onDrawMove(e) {
  if (!_drawPoints.length) return;
  const pts = [..._drawPoints.map(p => [p.lat, p.lng]), [e.latlng.lat, e.latlng.lng]];
  if (_drawType === 'polygon' && _drawPoints.length > 1) pts.push([_drawPoints[0].lat, _drawPoints[0].lng]);
  if (_rubberLine) { _rubberLine.setLatLngs(pts); }
  else { _rubberLine = L.polyline(pts, { color: _drawColor, weight: 2, dashArray: '6,4', opacity: 0.7 }).addTo(_layer); }
}

function _addDot(latlng) {
  const dot = L.circleMarker(latlng, { radius: 5, color: _drawColor, fillColor: _drawColor, fillOpacity: 1, weight: 2 }).addTo(_layer);
  _dotMarkers.push(dot);
}

function _updatePreviewLine() {
  if (_drawPoints.length < 2) return;
  const pts = _drawPoints.map(p => [p.lat, p.lng]);
  if (_previewLine) { _previewLine.setLatLngs(pts); }
  else { _previewLine = L.polyline(pts, { color: _drawColor, weight: 3, opacity: 0.8 }).addTo(_layer); }
}

function _clearPreview() {
  if (_previewLine) { _layer.removeLayer(_previewLine); _previewLine = null; }
  if (_rubberLine)  { _layer.removeLayer(_rubberLine);  _rubberLine  = null; }
  _dotMarkers.forEach(d => _layer.removeLayer(d));
  _dotMarkers = [];
}

async function _finishDraw() {
  const pts = [..._drawPoints];
  _cancelDraw();

  // Prompt for name
  const name = prompt('Zone name:', _drawType === 'polygon' ? 'New Region' : 'New Fire Line');
  if (!name) return;
  const desc = prompt('Description (optional):', '') || null;

  // Build GeoJSON geometry
  let geojson;
  if (_drawType === 'polygon') {
    // Close the ring
    const coords = pts.map(p => [p.lng, p.lat]);
    coords.push(coords[0]);
    geojson = { type: 'Polygon', coordinates: [coords] };
  } else {
    geojson = { type: 'LineString', coordinates: pts.map(p => [p.lng, p.lat]) };
  }

  try {
    const r = await fetch(`${API}/polygons`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description: desc, type: _drawType, color: _drawColor, geojson }),
    });
    if (!r.ok) throw new Error(await r.text());
    const z = await r.json();
    _zones[z.id] = z;
    _renderLayer(z);
    _renderPanel();
  } catch (err) {
    alert('Failed to create zone: ' + err.message);
  }
}

// ── Sidebar panel ─────────────────────────────────────────────────────────────

function _buildPanel() {
  const body = document.getElementById('zones-panel-body');
  if (!body) return;
  body.innerHTML = `
    <div class="zones-draw-bar">
      <div class="zones-color-row">
        ${COLORS.map(c => `<button class="zone-color-btn" data-color="${c.value}" title="${c.label}" style="background:${c.value}"></button>`).join('')}
      </div>
      <div class="zones-btn-row">
        <button id="zones-draw-poly"  class="zone-draw-btn">⬟ Region</button>
        <button id="zones-draw-line"  class="zone-draw-btn">╱ Line</button>
        <button id="zones-draw-cancel" class="zone-draw-btn zone-cancel-btn" style="display:none">✕ Cancel</button>
      </div>
      <div id="zones-draw-status" class="zones-draw-status"></div>
    </div>
    <div class="zones-export-bar">
      <button id="zones-export-btn" class="zone-export-btn" title="Download KML file">⬇ KML</button>
      <button id="zones-qr-btn"     class="zone-export-btn" title="Show QR code for Meshtastic">&#9638; QR</button>
    </div>
    <div id="zones-qr-panel" class="zones-qr-panel" style="display:none;">
      <div class="zones-qr-label">Scan in Meshtastic app → Map → Add Layer</div>
      <img id="zones-qr-img" src="" alt="QR Code" class="zones-qr-img">
      <div id="zones-qr-url" class="zones-qr-url"></div>
      <button id="zones-qr-copy" class="zone-export-btn">Copy URL</button>
    </div>
    <div id="zones-list"></div>`;

  // Color selection
  let _selectedColor = COLORS[0].value;
  body.querySelectorAll('.zone-color-btn').forEach(btn => {
    if (btn.dataset.color === _selectedColor) btn.classList.add('active');
    btn.addEventListener('click', () => {
      _drawColor = btn.dataset.color;
      _selectedColor = _drawColor;
      body.querySelectorAll('.zone-color-btn').forEach(b => b.classList.toggle('active', b === btn));
    });
  });

  // Draw buttons
  document.getElementById('zones-draw-poly').addEventListener('click', () => {
    startDraw('polygon', _drawColor);
    _refreshDrawButtons();
  });
  document.getElementById('zones-draw-line').addEventListener('click', () => {
    startDraw('polyline', _drawColor);
    _refreshDrawButtons();
  });
  document.getElementById('zones-draw-cancel').addEventListener('click', () => {
    _cancelDraw();
  });

  // Export
  document.getElementById('zones-export-btn').addEventListener('click', () => {
    window.open(`${API}/polygons/export.kml`, '_blank');
  });

  // QR
  document.getElementById('zones-qr-btn').addEventListener('click', async () => {
    const qrPanel = document.getElementById('zones-qr-panel');
    if (qrPanel.style.display !== 'none') { qrPanel.style.display = 'none'; return; }
    const img = document.getElementById('zones-qr-img');
    const urlEl = document.getElementById('zones-qr-url');
    const kmlUrl = `${location.protocol}//${location.host}/jtak/api/polygons/export.kml`;
    img.src = `${API}/polygons/qr`;
    urlEl.textContent = kmlUrl;
    qrPanel.style.display = '';
  });

  document.getElementById('zones-qr-copy')?.addEventListener('click', () => {
    const url = `${location.protocol}//${location.host}/jtak/api/polygons/export.kml`;
    navigator.clipboard.writeText(url).then(() => {
      const btn = document.getElementById('zones-qr-copy');
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = 'Copy URL'; }, 2000);
    });
  });

  _renderPanel();
}

function _refreshDrawButtons() {
  const polyBtn   = document.getElementById('zones-draw-poly');
  const lineBtn   = document.getElementById('zones-draw-line');
  const cancelBtn = document.getElementById('zones-draw-cancel');
  if (!polyBtn) return;
  polyBtn.style.display   = _drawing ? 'none' : '';
  lineBtn.style.display   = _drawing ? 'none' : '';
  cancelBtn.style.display = _drawing ? '' : 'none';
}

function _updateDrawStatus(msg) {
  const el = document.getElementById('zones-draw-status');
  if (el) el.textContent = msg;
}

function _renderPanel() {
  const list    = document.getElementById('zones-list');
  const countEl = document.getElementById('zone-count');
  if (!list) return;

  const active = Object.values(_zones).filter(z => !z.deleted_at);
  if (countEl) countEl.textContent = active.length;

  if (!active.length) {
    list.innerHTML = '<div class="wp-empty">No zones yet. Draw one above.</div>';
    return;
  }

  active.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  list.innerHTML = active.map(z => `
    <div class="zone-row" id="zone-row-${z.id}">
      <span class="zone-color-dot" style="background:${z.color || '#f97316'}"></span>
      <div class="zone-row-text" id="zone-text-${z.id}">
        <div class="zone-row-name">${_esc(z.name)}</div>
        <div class="zone-row-meta">${z.type} · ${_timeAgo(z.created_at)}</div>
      </div>
      <div class="wp-row-btns">
        <button id="zone-pan-${z.id}" class="wp-btn-sm" title="Pan to">⌖</button>
        <button id="zone-edit-${z.id}" class="wp-btn-sm" title="Edit">✏</button>
        <button id="zone-del-${z.id}" class="wp-btn-sm wp-btn-del" title="Delete">✕</button>
      </div>
    </div>`).join('');

  active.forEach(z => {
    document.getElementById(`zone-pan-${z.id}`)?.addEventListener('click', () => {
      if (_layers[z.id]) { _map.fitBounds(_layers[z.id].getBounds(), { padding: [30, 30] }); _layers[z.id].openPopup(); }
    });
    document.getElementById(`zone-edit-${z.id}`)?.addEventListener('click', () => _openEditor(z.id));
    document.getElementById(`zone-del-${z.id}`)?.addEventListener('click',  () => _deleteZone(z.id));
    const zoneText = document.getElementById(`zone-text-${z.id}`);
    if (zoneText) {
      zoneText.style.cursor = 'pointer';
      zoneText.addEventListener('click', () => {
        if (_layers[z.id]) { _map.fitBounds(_layers[z.id].getBounds(), { padding: [30, 30] }); _layers[z.id].openPopup(); }
      });
      attachRowTooltip(zoneText, z.description || null);
    }
  });
}

function _openEditor(id) {
  const z = _zones[id];
  if (!z) return;
  const name = prompt('Zone name:', z.name || '');
  if (name === null) return;
  const desc = prompt('Description:', z.description || '') ?? z.description;
  _updateZone(id, { name: name || z.name, description: desc });
}

async function _updateZone(id, patch) {
  try {
    const r = await fetch(`${API}/polygons/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!r.ok) throw new Error(await r.text());
    const z = await r.json();
    _zones[id] = z;
    _renderLayer(z);
    _renderPanel();
  } catch (err) {
    alert('Update failed: ' + err.message);
  }
}

async function _deleteZone(id) {
  if (!confirm('Delete this zone?')) return;
  try {
    const r = await fetch(`${API}/polygons/${id}`, { method: 'DELETE' });
    if (!r.ok) throw new Error(await r.text());
    _removeLayer(id);
    delete _zones[id];
    _renderPanel();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

// Global hooks for popup buttons
window._zoneEdit   = (id) => _openEditor(id);
window._zoneDelete = (id) => _deleteZone(id);

// ── Helpers ────────────────────────────────────────────────────────────────────

function _timeAgo(iso) {
  if (!iso) return '—';
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1)  return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function _esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
