// ── Tile Cache Manager ────────────────────────────────────────────────────────
// Sidebar panel: list caches, create new via pin-drop, progress, delete, prune.

const API = '/jtak/api';

let _map            = null;
let _pinMarker      = null;
let _coverageCircle = null;  // live radius preview while hovering
let _pinMode        = false;
let _pendingPin     = null;  // { lat, lon } set when user drops pin
let _thumbMaps      = {};    // cache_id → Leaflet mini-map instance

// ── Init ──────────────────────────────────────────────────────────────────────

export function initTileCache(map) {
  _map = map;
  _buildPanel();
  _loadCaches();
}

// ── Panel HTML ────────────────────────────────────────────────────────────────

function _buildPanel() {
  const panel = document.getElementById('tilecache-panel');
  if (!panel) return;

  panel.innerHTML = `
    <div class="tc-toolbar">
      <button id="tc-new-btn" class="tc-btn primary">＋ NEW CACHE</button>
    </div>

    <div id="tc-new-form" class="tc-form" style="display:none;">
      <div class="tc-form-row">
        <span class="tc-form-label">Name</span>
        <input id="tc-name" class="tc-input" type="text" placeholder="e.g. MOAB OPS" maxlength="40">
      </div>
      <div class="tc-form-row">
        <span class="tc-form-label">Center</span>
        <span id="tc-pin-coords" class="tc-coords-val">Move mouse over map…</span>
        <button id="tc-repin-btn" class="tc-btn-sm">📍 repin</button>
      </div>
      <div class="tc-form-row">
        <span class="tc-form-label">Radius</span>
        <select id="tc-radius" class="tc-select">
          <option value="5">5 mi</option>
          <option value="10">10 mi</option>
          <option value="25">25 mi</option>
          <option value="30" selected>30 mi</option>
          <option value="50">50 mi</option>
          <option value="75">75 mi</option>
        </select>
      </div>
      <div class="tc-form-row">
        <span class="tc-form-label">Coverage</span>
        <select id="tc-zoom" class="tc-select">
          <option value="overview">Overview (zoom 1-13)</option>
          <option value="street">Street (zoom 1-15)</option>
          <option value="tactical" selected>Tactical (zoom 1-17)</option>
          <option value="detail">Detail (zoom 1-18)</option>
        </select>
      </div>
      <div id="tc-estimate" class="tc-estimate">Select a center point to estimate size</div>
      <div class="tc-form-actions">
        <button id="tc-cancel-btn" class="tc-btn">CANCEL</button>
        <button id="tc-download-btn" class="tc-btn primary" disabled>DOWNLOAD</button>
      </div>
    </div>

    <div id="tc-list" class="tc-list"></div>

    <div id="tc-disk-row" class="tc-disk-row" style="display:none;">
      <span id="tc-disk-used">—</span>
      <button id="tc-prune-btn" class="tc-btn-sm tc-btn-danger">🗑 PRUNE</button>
    </div>
  `;

  document.getElementById('tc-new-btn').addEventListener('click',  _startPinMode);
  document.getElementById('tc-cancel-btn').addEventListener('click', _cancelForm);
  document.getElementById('tc-repin-btn').addEventListener('click', _startPinMode);
  document.getElementById('tc-download-btn').addEventListener('click', _submitDownload);
  document.getElementById('tc-prune-btn').addEventListener('click', _pruneOrphans);

  document.getElementById('tc-radius').addEventListener('change', () => {
    _updateEstimate();
    _updateCoverageRadius();
  });
  document.getElementById('tc-zoom').addEventListener('change', _updateEstimate);
}

// ── Pin-drop mode ─────────────────────────────────────────────────────────────

function _startPinMode() {
  _pinMode = true;
  _pendingPin = null;
  document.getElementById('tc-new-form').style.display = '';
  document.getElementById('tc-pin-coords').textContent = 'Move mouse over map…';
  document.getElementById('tc-download-btn').disabled = true;

  if (_map) {
    _map.getContainer().style.cursor = 'crosshair';
    _map.on('mousemove', _onMapHover);
    _map.once('click', _onMapClick);
  }
}

function _onMapHover(e) {
  const radiusM = _currentRadiusM();
  if (_coverageCircle) {
    _coverageCircle.setLatLng(e.latlng).setRadius(radiusM);
  } else {
    _coverageCircle = L.circle(e.latlng, {
      radius:      radiusM,
      color:       '#f97316',
      weight:      2,
      fillOpacity: 0.10,
      dashArray:   '6 4',
      interactive: false,
    }).addTo(_map);
  }
}

function _onMapClick(e) {
  _pinMode = false;
  _map.getContainer().style.cursor = '';
  _map.off('mousemove', _onMapHover);
  _pendingPin = { lat: e.latlng.lat, lon: e.latlng.lng };

  // Solidify circle at dropped pin
  if (_coverageCircle) {
    _coverageCircle.setLatLng(e.latlng).setStyle({ fillOpacity: 0.15, dashArray: null });
  }

  // Show/move pin marker
  if (_pinMarker) {
    _pinMarker.setLatLng(e.latlng);
  } else {
    _pinMarker = L.marker(e.latlng, {
      icon: L.divIcon({
        className: '',
        html: '<div class="tc-pin-marker">📍</div>',
        iconAnchor: [12, 32],
      }),
      zIndexOffset: 2000,
    }).addTo(_map);
  }

  document.getElementById('tc-pin-coords').textContent =
    `${e.latlng.lat.toFixed(4)}, ${e.latlng.lng.toFixed(4)}`;
  document.getElementById('tc-download-btn').disabled = false;
  _updateEstimate();
}

function _cancelForm() {
  document.getElementById('tc-new-form').style.display = 'none';
  _pinMode = false;
  _pendingPin = null;
  if (_map) {
    _map.getContainer().style.cursor = '';
    _map.off('mousemove', _onMapHover);
    _map.off('click', _onMapClick);
  }
  if (_pinMarker)      { _map.removeLayer(_pinMarker);      _pinMarker = null; }
  if (_coverageCircle) { _map.removeLayer(_coverageCircle); _coverageCircle = null; }
}

function _currentRadiusM() {
  const mi = parseFloat(document.getElementById('tc-radius')?.value || '30');
  return mi * 1609.34;
}

function _updateCoverageRadius() {
  if (_coverageCircle) _coverageCircle.setRadius(_currentRadiusM());
}

// ── Estimate ──────────────────────────────────────────────────────────────────

async function _updateEstimate() {
  if (!_pendingPin) return;
  const radius = parseFloat(document.getElementById('tc-radius').value);
  const zoom   = document.getElementById('tc-zoom').value;
  const el     = document.getElementById('tc-estimate');
  el.textContent = 'Estimating…';
  try {
    const r = await fetch(
      `${API}/tilecache/estimate?center_lat=${_pendingPin.lat}&center_lon=${_pendingPin.lon}&radius_mi=${radius}&zoom_preset=${zoom}`
    );
    const d = await r.json();
    el.textContent = `~${_fmtNum(d.tile_count)} tiles · ~${_fmtBytes(d.size_bytes)}`;
  } catch {
    el.textContent = 'Estimate unavailable';
  }
}

// ── Submit download ───────────────────────────────────────────────────────────

async function _submitDownload() {
  const name = document.getElementById('tc-name').value.trim();
  if (!name)        { alert('Enter a cache name'); return; }
  if (!_pendingPin) { alert('Drop a pin on the map first'); return; }

  document.getElementById('tc-download-btn').disabled = true;

  const body = {
    name,
    center_lat:  _pendingPin.lat,
    center_lon:  _pendingPin.lon,
    radius_mi:   parseFloat(document.getElementById('tc-radius').value),
    zoom_preset: document.getElementById('tc-zoom').value,
    cache_type:  'mission',
  };

  try {
    const r = await fetch(`${API}/tilecache/caches`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const e = await r.json();
      alert(e.detail || 'Download failed');
      document.getElementById('tc-download-btn').disabled = false;
      return;
    }
    const manifest = await r.json();
    _cancelForm();
    _loadCaches();
    _watchProgress(manifest.id);
  } catch (e) {
    alert('Network error: ' + e.message);
    document.getElementById('tc-download-btn').disabled = false;
  }
}

// ── Load / render cache list ──────────────────────────────────────────────────

async function _loadCaches() {
  try {
    const r = await fetch(`${API}/tilecache/caches`);
    const d = await r.json();
    _renderList(d.caches, d.disk);
  } catch {
    document.getElementById('tc-list').innerHTML =
      '<div class="tc-empty">Could not load caches</div>';
  }
}

function _renderList(caches, disk) {
  const list = document.getElementById('tc-list');

  // Destroy existing thumbnail maps
  Object.values(_thumbMaps).forEach(m => { try { m.remove(); } catch {} });
  _thumbMaps = {};

  if (!caches.length) {
    list.innerHTML = '<div class="tc-empty">No caches yet. Create one above.</div>';
  } else {
    list.innerHTML = caches.map(c => _cacheCardHtml(c)).join('');

    // Wire delete/stop/refresh buttons
    caches.forEach(c => {
      document.getElementById(`tc-del-${c.id}`)
        ?.addEventListener('click', () => _deleteCache(c.id, c.name));

      document.getElementById(`tc-pause-${c.id}`)
        ?.addEventListener('click', () => _pauseCache(c.id));

      document.getElementById(`tc-resume-${c.id}`)
        ?.addEventListener('click', () => _refreshCache(c.id));

      document.getElementById(`tc-refresh-${c.id}`)
        ?.addEventListener('click', () => _refreshCache(c.id));

      // Highlight cache area on map on click
      document.getElementById(`tc-show-${c.id}`)
        ?.addEventListener('click', () => _showOnMap(c));

      // Build thumbnail mini-map (lazy, after DOM insert)
      setTimeout(() => _buildThumb(c), 50);

      // Re-attach SSE for in-progress caches (survives browser refresh)
      if (c.status === 'downloading') _watchProgress(c.id);
    });
  }

  // Disk row
  const diskRow = document.getElementById('tc-disk-row');
  if (disk && caches.length) {
    diskRow.style.display = '';
    document.getElementById('tc-disk-used').textContent =
      `Cache: ${_fmtBytes(disk.cache_bytes)} · Free: ${_fmtBytes(disk.disk_free_bytes)}`;
  } else {
    diskRow.style.display = 'none';
  }
}

function _cacheCardHtml(c) {
  const pct     = c.total_tiles > 0
    ? Math.round((c.downloaded_tiles || 0) / c.total_tiles * 100) : 0;
  const active  = c.status === 'downloading';
  const done    = c.status === 'complete' || c.status === 'complete_with_errors';
  const paused  = c.status === 'paused' || c.status === 'stopped';
  const typeIcon = c.type === 'home' ? '🏠' : '📍';
  const statusBadge = active ? '<span class="tc-badge tc-badge-dl">⬇ DOWNLOADING</span>'
                    : paused ? '<span class="tc-badge tc-badge-stop">⏸ PAUSED</span>'
                    : done   ? ''
                    :          '<span class="tc-badge">PENDING</span>';

  return `
    <div class="tc-card" id="tc-card-${c.id}">
      <div class="tc-card-header">
        <span class="tc-card-icon">${typeIcon}</span>
        <span class="tc-card-name">${c.name}</span>
        ${statusBadge}
        <div class="tc-card-actions">
          <button id="tc-show-${c.id}" class="tc-btn-icon" title="Show on map">🗺</button>
          ${done ? `<button id="tc-refresh-${c.id}" class="tc-btn-icon" title="Re-download">↺</button>` : ''}
          ${paused ? `<button id="tc-resume-${c.id}" class="tc-btn-icon tc-btn-resume" title="Resume download">▶</button>` : ''}
          ${active ? `<button id="tc-pause-${c.id}" class="tc-btn-icon tc-btn-warn" title="Pause">⏸</button>` : ''}
          <button id="tc-del-${c.id}" class="tc-btn-icon tc-btn-danger" title="Delete">✕</button>
        </div>
      </div>

      <div class="tc-card-body">
        <div id="tc-thumb-${c.id}" class="tc-thumb"></div>
        <div class="tc-card-meta">
          <div class="tc-meta-row">
            <span class="tc-meta-label">Area</span>
            <span>${c.radius_mi} mi · ${c.zoom_preset || 'tactical'}</span>
          </div>
          <div class="tc-meta-row">
            <span class="tc-meta-label">Tiles</span>
            <span id="tc-tile-count-${c.id}">${_fmtNum(c.downloaded_tiles || 0)} / ${_fmtNum(c.total_tiles || 0)}</span>
          </div>
          <div class="tc-meta-row">
            <span class="tc-meta-label">Size</span>
            <span id="tc-size-${c.id}">${_fmtBytes(c.size_bytes || 0)}</span>
          </div>
          <div class="tc-meta-row">
            <span class="tc-meta-label">Created</span>
            <span>${c.created ? c.created.slice(0,10) : '—'}</span>
          </div>
        </div>
      </div>

      <div id="tc-progress-${c.id}" class="tc-progress-wrap" style="${(active || paused) && pct > 0 ? '' : 'display:none'}">
        <div class="tc-progress-bar">
          <div class="tc-progress-fill ${paused ? 'tc-progress-paused' : ''}" id="tc-fill-${c.id}" style="width:${pct}%"></div>
        </div>
        <span class="tc-progress-label" id="tc-pct-${c.id}">${pct}%</span>
      </div>
    </div>
  `;
}

// ── Thumbnail mini-map ────────────────────────────────────────────────────────

function _buildThumb(c) {
  const el = document.getElementById(`tc-thumb-${c.id}`);
  if (!el || el.dataset.built) return;
  el.dataset.built = '1';

  const thumbMap = L.map(el, {
    center: c.center,
    zoom: 10,
    zoomControl: false,
    attributionControl: false,
    dragging: false,
    scrollWheelZoom: false,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
    touchZoom: false,
  });

  // Use local-first tile layer
  localFirstTileLayer().addTo(thumbMap);

  // Draw bbox rectangle
  const [lat1, lon1, lat2, lon2] = c.bbox;
  L.rectangle([[lat1, lon1], [lat2, lon2]], {
    color: '#f97316', weight: 2, fillOpacity: 0.12,
  }).addTo(thumbMap);

  thumbMap.fitBounds([[lat1, lon1], [lat2, lon2]], { padding: [6, 6] });
  _thumbMaps[c.id] = thumbMap;
}

// ── Local-first tile layer (exported for map.js too) ─────────────────────────
// Tries local cache → OSM mirror → OSM fallback.

const _LocalFirstLayer = L.TileLayer.extend({
  initialize(remoteUrl, options) {
    this._remoteUrl  = remoteUrl  || 'https://tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png';
    this._fallbackUrl = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    L.TileLayer.prototype.initialize.call(this, this._remoteUrl, options);
  },
  createTile(coords, done) {
    const img = document.createElement('img');
    img.alt = '';
    const local   = `/jtak/api/tiles/${coords.z}/${coords.x}/${coords.y}.png`;
    const rem     = L.Util.template(this._remoteUrl,   { z: coords.z, x: coords.x, y: coords.y, s: 'a', r: '' });
    const fb      = L.Util.template(this._fallbackUrl, { z: coords.z, x: coords.x, y: coords.y, s: 'a', r: '' });
    img.src     = local;
    img.onload  = () => done(null, img);
    img.onerror = () => {
      img.onload  = () => done(null, img);
      img.onerror = () => {
        img.onload  = () => done(null, img);
        img.onerror = () => done(new Error('tile unavailable'), img);
        img.src = fb;
      };
      img.src = rem;
    };
    return img;
  },
});

export function localFirstTileLayer(remoteUrl) {
  return new _LocalFirstLayer(
    remoteUrl || 'https://tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png',
    { maxZoom: 19, attribution: '© OpenStreetMap contributors' }
  );
}

// ── Show cache area on main map ───────────────────────────────────────────────

let _bboxLayer = null;
function _showOnMap(c) {
  if (_bboxLayer) { _map.removeLayer(_bboxLayer); _bboxLayer = null; }
  const [lat1, lon1, lat2, lon2] = c.bbox;
  _bboxLayer = L.rectangle([[lat1, lon1], [lat2, lon2]], {
    color: '#f97316', weight: 2, fillOpacity: 0.08, dashArray: '6 4',
  }).addTo(_map);
  _map.fitBounds([[lat1, lon1], [lat2, lon2]], { padding: [40, 40] });
  setTimeout(() => { if (_bboxLayer) { _map.removeLayer(_bboxLayer); _bboxLayer = null; } }, 8000);
}

// ── SSE progress watcher ──────────────────────────────────────────────────────

function _watchProgress(cacheId) {
  const es = new EventSource(`${API}/tilecache/caches/${cacheId}/progress`);
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.ping) return;

    const fill  = document.getElementById(`tc-fill-${cacheId}`);
    const pctEl = document.getElementById(`tc-pct-${cacheId}`);
    const wrap  = document.getElementById(`tc-progress-${cacheId}`);
    const tiles = document.getElementById(`tc-tile-count-${cacheId}`);

    if (fill)  fill.style.width  = `${d.pct || 0}%`;
    if (pctEl) pctEl.textContent = `${d.pct || 0}%`;
    if (wrap)  wrap.style.display = '';
    if (tiles) tiles.textContent = `${_fmtNum(d.done)} / ${_fmtNum(d.total)}`;

    if (d.status && d.status !== 'downloading') {
      es.close();
      setTimeout(_loadCaches, 500);
    }
  };
  es.onerror = () => es.close();
}

// ── Delete / stop / refresh ───────────────────────────────────────────────────

async function _deleteCache(id, name) {
  if (!confirm(`Delete cache "${name}"?\n\nTiles remain on disk until you Prune.`)) return;
  await fetch(`${API}/tilecache/caches/${id}`, { method: 'DELETE' });
  _loadCaches();
}

async function _pauseCache(id) {
  await fetch(`${API}/tilecache/caches/${id}/pause`, { method: 'POST' });
  _loadCaches();
}

async function _refreshCache(id) {
  await fetch(`${API}/tilecache/caches/${id}/refresh`, { method: 'POST' });
  _loadCaches();
  _watchProgress(id);
}

async function _pruneOrphans() {
  if (!confirm('Remove tiles not covered by any saved cache?\nThis cannot be undone.')) return;
  const btn = document.getElementById('tc-prune-btn');
  btn.textContent = '⏳ Pruning…';
  btn.disabled    = true;
  try {
    const r = await fetch(`${API}/tilecache/prune`, { method: 'POST' });
    const d = await r.json();
    alert(`Pruned ${_fmtNum(d.deleted_tiles)} tiles`);
    _loadCaches();
  } catch {
    alert('Prune failed');
  } finally {
    btn.textContent = '🗑 PRUNE';
    btn.disabled    = false;
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _fmtBytes(b) {
  if (!b) return '0 B';
  if (b < 1024)       return `${b} B`;
  if (b < 1048576)    return `${Math.round(b/1024)} KB`;
  if (b < 1073741824) return `${Math.round(b/1048576)} MB`;
  return `${Math.round(b/1073741824)} GB`;
}

function _fmtNum(n) {
  return n != null ? n.toLocaleString() : '—';
}
