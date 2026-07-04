// ── Waypoints — Meshtastic mesh waypoints + manual HUD pin drops ────────────
// Ingest:  WebSocket type:"waypoint" | REST GET /api/waypoints/all (on load)
// Map:     Leaflet L.LayerGroup, separate from node markers
// Sidebar: editor panel — expiry management, soft-delete, restore
// TRACKS:  backend provides GET /api/history/waypoints?date=&start_ts=&end_ts=

import { attachRowTooltip } from './utils.js';
const API = '/jtak/api';

let _map = null;
const _wps     = {};   // id → waypoint dict
const _markers = {};   // id → L.Marker
const _layer   = L.layerGroup();

let _dropping    = false;
let _dropHandler = null;

// ── Init ──────────────────────────────────────────────────────────────────────

export function initWaypoints(map) {
  _map = map;
  _layer.addTo(map);
  _buildPanel();
  _loadWaypoints();
  _wireDropButton();
  // Re-evaluate expiry every 60s so markers fade and panel updates without a reload
  setInterval(_tickExpiry, 60_000);
}

function _tickExpiry() {
  let changed = false;
  Object.values(_wps).forEach(wp => {
    if (_isExpired(wp) && _markers[wp.id]) {
      _renderMarker(wp);   // updates icon opacity/color to faded grey
      changed = true;
    }
  });
  if (changed) _renderPanel();
}

// ── WebSocket message handler — called from app.js ────────────────────────────

export function onWaypointMessage(msg) {
  if (!msg.id) return;
  const prev = _wps[msg.id];
  _wps[msg.id] = prev ? { ...prev, ...msg } : msg;
  _renderMarker(_wps[msg.id]);
  _renderPanel();
}

// ── Load existing waypoints on startup ────────────────────────────────────────

async function _loadWaypoints() {
  try {
    const r = await fetch(`${API}/waypoints/all`);
    const list = await r.json();
    list.forEach(wp => {
      _wps[wp.id] = wp;
      _renderMarker(wp);
    });
    _renderPanel();
  } catch (e) {
    console.warn('[waypoints] load failed:', e);
  }
}

// ── Marker rendering ──────────────────────────────────────────────────────────

function _isExpired(wp) {
  return !!wp.expires_at && new Date(wp.expires_at) < new Date();
}

function _isActive(wp) {
  return !wp.deleted_at && !_isExpired(wp);
}

function _pinColor(wp) {
  if (wp.deleted_at)            return '#555';
  if (_isExpired(wp))           return '#888';
  if (wp.source_type === 'manual') return '#4fc3f7';
  return '#f97316';   // orange — mesh-sourced
}

function _makeIcon(wp) {
  const color   = _pinColor(wp);
  const opacity = _isActive(wp) ? 1 : 0.35;
  const emoji   = wp.icon || '📍';
  const label   = _esc(wp.name || 'Waypoint');
  return L.divIcon({
    className: '',
    html: `<div class="wp-pin" style="opacity:${opacity}">
             <div class="wp-pin-dot" style="background:${color};border-color:${color}">
               <span class="wp-pin-emoji">${emoji}</span>
             </div>
             <div class="wp-pin-label">${label}</div>
           </div>`,
    iconSize:   [48, 46],
    iconAnchor: [24, 38],
    popupAnchor:[0,  -40],
  });
}

function _popupHtml(wp) {
  const expLabel = wp.expires_at
    ? (_isExpired(wp)
        ? `<span style="color:#f87171">Expired ${_timeAgo(wp.expires_at)}</span>`
        : `Expires ${_timeAgo(wp.expires_at)}`)
    : 'No expiry';
  return `
    <div class="popup-title">${_esc(wp.name || 'Waypoint')}</div>
    ${wp.description ? `<div class="popup-row"><span class="pk">Note</span><span class="pv">${_esc(wp.description)}</span></div>` : ''}
    <div class="popup-row"><span class="pk">From</span><span class="pv">${_esc(wp.source_name || wp.source_type || '—')}</span></div>
    <div class="popup-row"><span class="pk">Placed</span><span class="pv">${_timeAgo(wp.created_at)}</span></div>
    <div class="popup-row"><span class="pk">Expiry</span><span class="pv">${expLabel}</span></div>
    ${wp.source_id && wp.source_type !== 'manual'
      ? `<button class="popup-dm-btn" data-id="${_esc(wp.source_id)}" data-name="${_esc(wp.source_name || wp.source_id)}">💬 Respond</button>`
      : ''}
    <button class="popup-dm-btn popup-edit-btn" onclick="window._wpEdit(${wp.id})">✏ Edit</button>`;
}

function _renderMarker(wp) {
  if (!wp.lat || !wp.lon) return;
  if (wp.deleted_at) {
    if (_markers[wp.id]) { _layer.removeLayer(_markers[wp.id]); delete _markers[wp.id]; }
    return;
  }
  const icon   = _makeIcon(wp);
  const latlng = [wp.lat, wp.lon];
  if (_markers[wp.id]) {
    _markers[wp.id].setLatLng(latlng);
    _markers[wp.id].setIcon(icon);
    _markers[wp.id].setPopupContent(_popupHtml(wp));
  } else {
    _markers[wp.id] = L.marker(latlng, { icon })
      .bindPopup(_popupHtml(wp), { maxWidth: 260 })
      .addTo(_layer);
  }
}

// ── Drop-pin HUD button ───────────────────────────────────────────────────────

function _wireDropButton() {
  const btn = document.getElementById('wp-drop-btn');
  if (!btn) return;
  btn.addEventListener('click', () => { _dropping ? _stopDrop() : _startDrop(); });
  window._wpStopDrop = _stopDrop;   // exposed for app.js mutual exclusion
}

function _startDrop() {
  _dropping = true;
  const btn = document.getElementById('wp-drop-btn');
  if (btn) btn.classList.add('active');
  _map.getContainer().style.cursor = 'crosshair';
  _dropHandler = (e) => _onDropClick(e);
  _map.once('click', _dropHandler);
}

export function stopDrop() { _stopDrop(); }

function _stopDrop() {
  _dropping = false;
  const btn = document.getElementById('wp-drop-btn');
  if (btn) btn.classList.remove('active');
  _map.getContainer().style.cursor = '';
  if (_dropHandler) { _map.off('click', _dropHandler); _dropHandler = null; }
}

async function _onDropClick(e) {
  _dropping = false;
  const btn = document.getElementById('wp-drop-btn');
  if (btn) btn.classList.remove('active');
  _map.getContainer().style.cursor = '';

  const name = prompt('Waypoint name:', 'New Waypoint');
  if (!name) return;
  const desc = prompt('Description (optional):', '') || null;

  try {
    const r = await fetch(`${API}/waypoints`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description: desc, lat: e.latlng.lat, lon: e.latlng.lng }),
    });
    if (!r.ok) throw new Error(await r.text());
    const wp = await r.json();
    _wps[wp.id] = wp;
    _renderMarker(wp);
    _renderPanel();
  } catch (err) {
    alert('Failed to create waypoint: ' + err.message);
  }
}

// ── Sidebar panel ─────────────────────────────────────────────────────────────

let _showAll     = false;
let _openEditorId = null;

function _buildPanel() {
  const body = document.getElementById('wp-panel-body');
  if (!body) return;
  body.innerHTML = `
    <div class="wp-toolbar">
      <button class="wp-tab active" id="wp-tab-active">Active</button>
      <button class="wp-tab" id="wp-tab-all">All</button>
    </div>
    <div id="wp-list"></div>`;

  document.getElementById('wp-tab-active').addEventListener('click', () => {
    _showAll = false;
    document.getElementById('wp-tab-active').classList.add('active');
    document.getElementById('wp-tab-all').classList.remove('active');
    _renderPanel();
  });
  document.getElementById('wp-tab-all').addEventListener('click', () => {
    _showAll = true;
    document.getElementById('wp-tab-all').classList.add('active');
    document.getElementById('wp-tab-active').classList.remove('active');
    _renderPanel();
  });
}

function _renderPanel() {
  const list = document.getElementById('wp-list');
  if (!list) return;

  const countEl  = document.getElementById('wp-count');
  const activeN  = Object.values(_wps).filter(_isActive).length;
  if (countEl) countEl.textContent = activeN;

  let wps = Object.values(_wps);
  if (!_showAll) wps = wps.filter(_isActive);
  wps.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));

  if (!wps.length) {
    list.innerHTML = `<div class="wp-empty">${_showAll ? 'No waypoints yet.' : 'No active waypoints.'}</div>`;
    return;
  }

  list.innerHTML = wps.map(_wpRowHtml).join('');

  wps.forEach(wp => {
    document.getElementById(`wp-edit-${wp.id}`)?.addEventListener('click', () => _openEditor(wp.id));
    document.getElementById(`wp-del-${wp.id}`)?.addEventListener('click',  () => _deleteWp(wp.id));
    document.getElementById(`wp-res-${wp.id}`)?.addEventListener('click',  () => _restoreWp(wp.id));
    document.getElementById(`wp-pan-${wp.id}`)?.addEventListener('click',  () => {
      if (_markers[wp.id]) { _map.panTo(_markers[wp.id].getLatLng()); _markers[wp.id].openPopup(); }
    });
    const wpText = document.getElementById(`wp-text-${wp.id}`);
    if (wpText) {
      wpText.style.cursor = 'pointer';
      wpText.addEventListener('click', () => {
        if (_markers[wp.id]) { _map.panTo(_markers[wp.id].getLatLng()); _markers[wp.id].openPopup(); }
      });
      attachRowTooltip(wpText, wp.description || null);
    }
  });
}

function _wpRowHtml(wp) {
  const active  = _isActive(wp);
  const expired = _isExpired(wp);
  const deleted = !!wp.deleted_at;
  const color   = _pinColor(wp);
  const emoji   = wp.icon || '📍';
  const expTag  = wp.expires_at
    ? (expired ? `<span class="wp-tag-expired">EXPIRED</span>`
                : `<span class="wp-tag-exp">exp ${_timeAgo(wp.expires_at)}</span>`)
    : '';

  return `
    <div class="wp-row${deleted ? ' wp-row-deleted' : expired ? ' wp-row-expired' : ''}" id="wp-row-${wp.id}">
      <div class="wp-row-main">
        <span class="wp-row-icon" style="color:${color}">${emoji}</span>
        <div class="wp-row-text" id="wp-text-${wp.id}">
          <div class="wp-row-name">${_esc(wp.name || 'Waypoint')}</div>
          ${wp.description ? `<div class="wp-row-desc">${_esc(wp.description)}</div>` : ''}
          <div class="wp-row-meta">${_timeAgo(wp.created_at)} · ${_esc(wp.source_name || wp.source_type || '?')} ${expTag}</div>
        </div>
        <div class="wp-row-btns">
          ${active ? `<button id="wp-pan-${wp.id}" class="wp-btn-sm" title="Pan to">⌖</button>` : ''}
          <button id="wp-edit-${wp.id}" class="wp-btn-sm" title="Edit">✏</button>
          ${deleted
            ? `<button id="wp-res-${wp.id}" class="wp-btn-sm wp-btn-restore" title="Restore">↩</button>`
            : `<button id="wp-del-${wp.id}" class="wp-btn-sm wp-btn-del" title="Remove from map">✕</button>`}
        </div>
      </div>
      <div id="wp-editor-${wp.id}" class="wp-editor" style="display:none;"></div>
    </div>`;
}

// ── Inline editor ──────────────────────────────────────────────────────────────

function _openEditor(id) {
  if (_openEditorId && _openEditorId !== id) {
    const prev = document.getElementById(`wp-editor-${_openEditorId}`);
    if (prev) prev.style.display = 'none';
  }
  const wp = _wps[id];
  const ed = document.getElementById(`wp-editor-${id}`);
  if (!ed || !wp) return;

  if (_openEditorId === id && ed.style.display !== 'none') {
    ed.style.display = 'none';
    _openEditorId = null;
    return;
  }
  _openEditorId = id;

  const expVal = wp.expires_at ? wp.expires_at.slice(0, 16).replace('T', ' ') : '';
  ed.style.display = '';
  ed.innerHTML = `
    <div class="wp-ed-row">
      <label class="wp-ed-label">Name</label>
      <input id="wped-name-${id}" class="wp-ed-input" value="${_esc(wp.name || '')}">
    </div>
    <div class="wp-ed-row">
      <label class="wp-ed-label">Note</label>
      <input id="wped-desc-${id}" class="wp-ed-input" value="${_esc(wp.description || '')}">
    </div>
    <div class="wp-ed-row">
      <label class="wp-ed-label">Expires</label>
      <div class="wp-presets">
        <button class="wp-preset" data-h="1">+1h</button>
        <button class="wp-preset" data-h="6">+6h</button>
        <button class="wp-preset" data-h="24">+24h</button>
        <button class="wp-preset" data-h="0">None</button>
      </div>
      <input id="wped-exp-${id}" class="wp-ed-input" placeholder="YYYY-MM-DD HH:MM UTC" value="${expVal}">
    </div>
    <div class="wp-ed-actions">
      <button id="wped-save-${id}"   class="wp-btn-save">Save</button>
      <button id="wped-cancel-${id}" class="wp-btn-cancel">Cancel</button>
    </div>`;

  ed.querySelectorAll('.wp-preset').forEach(btn => {
    btn.addEventListener('click', () => {
      const h   = parseInt(btn.dataset.h);
      const inp = document.getElementById(`wped-exp-${id}`);
      if (h === 0) { inp.value = ''; return; }
      inp.value = new Date(Date.now() + h * 3600000).toISOString().slice(0, 16).replace('T', ' ');
    });
  });
  document.getElementById(`wped-save-${id}`).addEventListener('click',   () => _saveEdit(id));
  document.getElementById(`wped-cancel-${id}`).addEventListener('click', () => {
    ed.style.display = 'none';
    _openEditorId = null;
  });
}

async function _saveEdit(id) {
  const name   = document.getElementById(`wped-name-${id}`)?.value.trim();
  const desc   = document.getElementById(`wped-desc-${id}`)?.value.trim() || null;
  const expRaw = document.getElementById(`wped-exp-${id}`)?.value.trim();

  let expires_at = '';
  if (expRaw) {
    const d = new Date(expRaw.includes('T') ? expRaw : expRaw.replace(' ', 'T') + ':00Z');
    if (isNaN(d)) { alert('Invalid date. Use YYYY-MM-DD HH:MM'); return; }
    expires_at = d.toISOString();
  }

  try {
    const r = await fetch(`${API}/waypoints/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description: desc, expires_at }),
    });
    if (!r.ok) throw new Error(await r.text());
    const wp = await r.json();
    _wps[id] = wp;
    _renderMarker(wp);
    _openEditorId = null;
    _renderPanel();
  } catch (err) {
    alert('Save failed: ' + err.message);
  }
}

async function _deleteWp(id) {
  if (!confirm('Remove from map?\n(Kept in database — restore via All tab)')) return;
  try {
    const r = await fetch(`${API}/waypoints/${id}`, { method: 'DELETE' });
    if (!r.ok) throw new Error(await r.text());
    // WS broadcast will update state; update locally too for instant feedback
    _wps[id] = { ..._wps[id], deleted_at: new Date().toISOString() };
    _renderMarker(_wps[id]);
    _renderPanel();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

async function _restoreWp(id) {
  try {
    const r = await fetch(`${API}/waypoints/${id}/restore`, { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
    const wp = await r.json();
    _wps[id] = wp;
    _renderMarker(wp);
    _renderPanel();
  } catch (err) {
    alert('Restore failed: ' + err.message);
  }
}

// ── Global hook — popup "Edit" button ─────────────────────────────────────────

window._wpEdit = function (id) {
  const panel = document.querySelector('[data-panel="waypoints"]');
  if (panel?.classList.contains('collapsed')) panel.classList.remove('collapsed');
  _openEditor(id);
  setTimeout(() => {
    document.getElementById(`wp-row-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, 50);
};


// ── Helpers ────────────────────────────────────────────────────────────────────

function _timeAgo(iso) {
  if (!iso) return '—';
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) {
    // Future timestamp (expiry)
    const m = Math.round(-diff / 60000);
    if (m < 60)  return `in ${m}m`;
    const h = Math.round(m / 60);
    if (h < 24)  return `in ${h}h`;
    return `in ${Math.round(h / 24)}d`;
  }
  const m = Math.round(diff / 60000);
  if (m < 1)   return 'just now';
  if (m < 60)  return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24)  return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function _esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
