// ── RF History Analysis — main controller ─────────────────────────────────────

import { initPlayback, startPlayback, pausePlayback,
         seekPlayback, setSpeedPlayback, clearPlayback, setMapMode, openNodePopup, pruneExcluded } from './playback.js';
import { renderHeatmap, clearHeatmap, updateHeatmapTime, highlightHeatmapContact, openHeatmapPopup } from './heatmap.js';
import { initChat, updateChatContext } from './chat.js';

const API = '/jtak/api';

// ── Session storage helpers (shared key prefix with live view) ────────────────
const CACHE_TTL = 30 * 60 * 1000;  // 30 min

function _sGet(key, fallback = null) {
  try {
    const v = sessionStorage.getItem('jtak_' + key);
    return v === null ? fallback : JSON.parse(v);
  } catch { return fallback; }
}

function _sSet(key, value) {
  try { sessionStorage.setItem('jtak_' + key, JSON.stringify(value)); } catch {}
}

function _saveHistState() {
  _sSet('hist', {
    savedAt:    Date.now(),
    date:       S.date,
    hubId:      S.hubId,
    winStart:   document.getElementById('win-start')?.value || '',
    winEnd:     document.getElementById('win-end')?.value   || '',
    mode:       S.mode,
    allContacts: S.allContacts,
    stats:      S.stats,
  });
}

// ── App state ─────────────────────────────────────────────────────────────────
const S = {
  mode:         'replay',    // 'replay' | 'heatmap'
  date:         null,
  hubId:        null,        // null = all hubs
  contacts:     [],          // filtered set for current view
  allContacts:  [],          // raw from API for current date
  stats:        null,
  map:          null,
  playing:      false,
  speed:        30,
  startMs:      0,
  endMs:        0,
  virtualMs:    0,
  selectedIdx:  null,        // index into S.contacts
  winStartMs:   null,        // time-window filter start (ms of day)
  winEndMs:     null,        // time-window filter end   (ms of day)
  excludedNodes: new Set(),  // source_ids unchecked in node filter
  hubLayer:     null,        // L.layerGroup for static hub pins
  previewLayer: null,        // dim background dots in replay mode (cleared on play)
};

// ── Boot ──────────────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', () => {
  _initMap();
  initChat();
  _bindControls();
  _loadDates();   // always fetch date list; may restore cached contacts after
  fetch('/jtak/api/status').then(r => r.json()).then(s => {
    const name = s.hub_short_name || s.hub_name || 'jTAK';
    document.title = `${name} — RF History`;
  }).catch(() => {});
});

const BASEMAPS = {
  'Dark':      L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
                 { subdomains: 'abcd', maxZoom: 19, attribution: '© OpenStreetMap © CARTO' }),
  'Satellite': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                 { maxZoom: 19, attribution: '© Esri © USGS' }),
  'Hybrid':    L.layerGroup([
                 L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                   { maxZoom: 19, attribution: '© Esri' }),
                 L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
                   { maxZoom: 19, opacity: 0.8 }),
               ]),
  'Topo':      L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
                 { subdomains: 'abc', maxZoom: 17, attribution: '© OpenTopoMap © OpenStreetMap' }),
  'USGS Topo': L.tileLayer('https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',
                 { maxZoom: 16, attribution: '© USGS National Map' }),
  'Street':    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
                 { subdomains: 'abcd', maxZoom: 19, attribution: '© OpenStreetMap © CARTO' }),
};

function _initMap() {
  const center = [40.5729, -111.9941];
  S.map = L.map('hist-map', { center, zoom: 12, zoomControl: true });

  const saved = sessionStorage.getItem('jtak_basemap') || 'Dark';
  const active = BASEMAPS[saved] || BASEMAPS['Dark'];
  active.addTo(S.map);
  setMapMode(saved === 'Dark' ? 'dark' : 'light');

  L.control.layers(BASEMAPS, {}, { position: 'topright', collapsed: true })
    .addTo(S.map);

  S.map.on('baselayerchange', e => {
    sessionStorage.setItem('jtak_basemap', e.name);
    setMapMode(e.name === 'Dark' ? 'dark' : 'light');
  });
}

// ── Date / Hub selects ────────────────────────────────────────────────────────

async function _loadDates() {
  _setStatus('Loading dates…');
  try {
    const r = await fetch(`${API}/history/dates`);
    const { dates } = await r.json();
    const sel = document.getElementById('date-select');
    sel.innerHTML = dates.length
      ? '<option value="">— Select date —</option>' +
        dates.map(d => `<option value="${d}">${d}</option>`).join('')
      : '<option value="">No logs found</option>';

    // Restore cached session if recent and date still exists
    const cache = _sGet('hist');
    if (cache && cache.date && (Date.now() - cache.savedAt) < CACHE_TTL && dates.includes(cache.date)) {
      _restoreHistState(cache, dates);
    } else {
      _setStatus(dates.length ? 'Select a date' : 'No log files found');
    }
  } catch (e) {
    _setStatus('Error loading dates');
  }
}

function _restoreHistState(cache, dates) {
  // Restore selects
  document.getElementById('date-select').value = cache.date;
  S.date = cache.date;

  if (cache.hubId) {
    S.hubId = cache.hubId;
  }

  // Restore contacts + stats
  S.allContacts = (cache.allContacts || []).map(c => ({
    ...c,
    _ms: c._ms || new Date(c.ts.replace(' ', 'T')).getTime(),
  }));
  S.stats = cache.stats;

  // Restore time window inputs
  if (cache.winStart) document.getElementById('win-start').value = cache.winStart;
  if (cache.winEnd)   document.getElementById('win-end').value   = cache.winEnd;
  if (cache.winStart || cache.winEnd) document.getElementById('window-group').style.display = '';

  S.winStartMs = cache.winStart ? _timeToMs(cache.winStart) : null;
  S.winEndMs   = cache.winEnd   ? _timeToMs(cache.winEnd)   : null;

  // Restore mode
  if (cache.mode) _setMode(cache.mode);


  _applyFilters();
  _renderStats();
  document.getElementById('window-group').style.display = '';
  _setStatus(`${S.allContacts.length} contacts restored`);

  updateChatContext({
    date: S.date, mode: S.mode,
    total:      S.stats?.total,
    time_range: S.stats?.time_range,
    rssi:       S.stats?.rssi,
    snr:        S.stats?.snr,
    distance:   S.stats?.distance,
    nodes:      S.stats?.nodes,
  });
}

async function _onDateChange(date) {
  if (!date) { _setStatus('Select a date'); return; }
  S.date    = date;
  S.hubId   = null;
  S.selectedIdx = null;
  _setStatus('Loading…');

  try {
    const [cResp, sResp] = await Promise.all([
      fetch(`${API}/history/contacts?date=${date}`),
      fetch(`${API}/history/stats?date=${date}`),
    ]);
    const cData = await cResp.json();
    const sData = await sResp.json();

    // Attach _ms to each contact for timeline math
    // Normalize space-separated timestamps ("2026-03-07 14:37:37") to ISO T format
    S.allContacts = cData.contacts.map(c => ({
      ...c,
      _ms: new Date(c.ts.replace(' ', 'T')).getTime(),
    })).sort((a, b) => a._ms - b._ms);

    S.stats = sData;
    S.winStartMs = null;
    S.winEndMs   = null;
    S.excludedNodes = new Set();

    // Pre-fill time window inputs with session bounds
    if (S.allContacts.length) {
      const first = new Date(S.allContacts[0]._ms);
      const last  = new Date(S.allContacts[S.allContacts.length - 1]._ms);
      const fmt   = d => `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
      document.getElementById('win-start').value = fmt(first);
      document.getElementById('win-end').value   = fmt(last);
    }
    document.getElementById('window-group').style.display = '';

    _applyFilters();
    _renderStats();
    _setStatus(`${S.allContacts.length} contacts loaded`);
    setTimeout(() => _setStatus(''), 3000);
    _saveHistState();

    updateChatContext({
      date, mode: S.mode,
      total:      sData.total,
      time_range: sData.time_range,
      rssi:       sData.rssi,
      snr:        sData.snr,
      distance:   sData.distance,
      nodes:      sData.nodes,
    });
  } catch (e) {
    _setStatus(`Error: ${e.message}`);
  }
}

function _populateHubSelect() {
  const hubs = {};
  for (const c of S.allContacts) {
    if (c.hub_id && !hubs[c.hub_id]) hubs[c.hub_id] = c.hub_name || c.hub_id;
  }
  const sel = document.getElementById('hub-select');
  sel.innerHTML = '<option value="">All hubs</option>' +
    Object.entries(hubs).map(([id, name]) => `<option value="${id}">${name}</option>`).join('');
}

function _applyFilters({ preserveView = false } = {}) {
  let base = S.hubId ? S.allContacts.filter(c => c.hub_id === S.hubId) : S.allContacts.slice();

  // Apply time window if set
  if (S.winStartMs !== null || S.winEndMs !== null) {
    base = base.filter(c => {
      const d = new Date(c._ms);
      const dayMs = (d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds()) * 1000;
      if (S.winStartMs !== null && dayMs < S.winStartMs) return false;
      if (S.winEndMs   !== null && dayMs > S.winEndMs)   return false;
      return true;
    });
  }

  // Apply node exclusion for the map/contacts, but keep `base` intact for stats
  S.contacts = S.excludedNodes.size > 0
    ? base.filter(c => !S.excludedNodes.has(c.source_id))
    : base;

  _renderContactList();
  _clearDetail();

  if (preserveView && S.mode === 'replay') {
    // Surgical path: remove only excluded-node markers, re-init playback, restore position
    pruneExcluded([...S.excludedNodes]);
    const savedMs = S.virtualMs;
    const { startMs, endMs } = initPlayback(S.map, S.contacts, {
      onTick:    _onPlaybackTick,
      onContact: _onPlaybackContact,
    });
    S.startMs = startMs;
    S.endMs   = endMs;
    S.playing = false;
    document.getElementById('tl-play').textContent = '▶';
    const targetMs = Math.max(startMs, Math.min(endMs, savedMs));
    seekPlayback(targetMs);
    S.virtualMs = targetMs;
    const frac = endMs > startMs ? (targetMs - startMs) / (endMs - startMs) : 0;
    document.getElementById('tl-slider').value = Math.round(frac * 1000);
    _updateTimeLabels(startMs, endMs, targetMs);
    _showHubPins(S.contacts);
    _clearPreview();
    _buildPreview();
  } else if (preserveView && S.mode === 'heatmap') {
    clearHeatmap(S.map);
    renderHeatmap(S.map, S.contacts, { onSelect: idx => _selectContact(idx) });
    _showHubPins(S.contacts);
    updateHeatmapTime(S.virtualMs);
  } else {
    _resetView();
  }

  _saveHistState();
  _setStatus(`${S.contacts.length} contacts in window`);

  // Stats always computed from pre-exclusion base so all nodes stay visible in the table
  const isFiltered = (S.winStartMs !== null || S.winEndMs !== null || S.hubId !== null);
  const stats = _computeStats(base);
  if (stats && S.winStartMs !== null && S.winEndMs !== null) {
    stats.duration_ms = S.winEndMs - S.winStartMs;
  }
  _renderStats(stats, isFiltered);
}

function _applyTimeWindow() {
  const startVal = document.getElementById('win-start').value;  // "HH:MM"
  const endVal   = document.getElementById('win-end').value;
  S.winStartMs = startVal ? _timeToMs(startVal) : null;
  S.winEndMs   = endVal   ? _timeToMs(endVal)   : null;
  _applyFilters();
}

function _timeToMs(hhmm) {
  const [h, m] = hhmm.split(':').map(Number);
  return (h * 3600 + m * 60) * 1000;
}

// ── Mode swithing ─────────────────────────────────────────────────────────────

function _setMode(mode) {
  S.mode = mode;
  document.getElementById('btn-replay').classList.toggle('active',  mode === 'replay');
  document.getElementById('btn-heatmap').classList.toggle('active', mode === 'heatmap');

  _clearPreview();
  _clearDetail();
  _resetView({ skipFit: true });   // preserve map pan/zoom when toggling modes
  updateChatContext({ mode });
  _saveHistState();
}

// ── Static hub pins (always visible, independent of playback) ────────────────

function _showHubPins(contacts) {
  _clearHubPins();
  S.hubLayer = L.layerGroup().addTo(S.map);

  // Collect unique hubs → average position across all their contacts
  const hubs = {};
  for (const c of contacts) {
    if (!c.hub_id || !c.hub_lat || !c.hub_lon) continue;
    if (!hubs[c.hub_id]) hubs[c.hub_id] = { name: c.hub_name || c.hub_id, lats: [], lons: [] };
    hubs[c.hub_id].lats.push(c.hub_lat);
    hubs[c.hub_id].lons.push(c.hub_lon);
  }

  for (const [, hub] of Object.entries(hubs)) {
    const lat = hub.lats.reduce((a, b) => a + b, 0) / hub.lats.length;
    const lon = hub.lons.reduce((a, b) => a + b, 0) / hub.lons.length;
    const icon = L.divIcon({
      className: '',
      html: `<div class="marker-hub" title="${hub.name}"></div>`,
      iconSize: [28, 28], iconAnchor: [6, 22],
    });
    const m = L.marker([lat, lon], { icon, zIndexOffset: 1000 });
    m.bindTooltip(hub.name, { permanent: true, direction: 'top', className: 'hub-label' });
    S.hubLayer.addLayer(m);
  }
}

function _clearHubPins() {
  if (S.hubLayer && S.map) S.map.removeLayer(S.hubLayer);
  S.hubLayer = null;
}

// ── Replay preview layer (dim background dots so map isn't blank) ─────────────

function _buildPreview() {
  _clearPreview();
  if (!S.contacts.length) return;
  S.previewLayer = L.layerGroup().addTo(S.map);
  for (const c of S.contacts) {
    if (!c.node_lat || !c.node_lon) continue;
    const color = _rssiColorHex(c.rssi);
    L.circleMarker([c.node_lat, c.node_lon], {
      radius: 6,
      fillColor: color, fillOpacity: 0.45,
      color: color, weight: 1.5, opacity: 0.65,
      interactive: false,
    }).addTo(S.previewLayer);
  }
}

function _clearPreview() {
  if (S.previewLayer && S.map) S.map.removeLayer(S.previewLayer);
  S.previewLayer = null;
}

function _resetView({ skipFit = false } = {}) {
  if (S.mode === 'replay') {
    clearHeatmap(S.map);
    clearPlayback();
    // Sync play state — clearPlayback() stops the engine but S.playing may still be true
    S.playing = false;
    document.getElementById('tl-play').textContent = '▶';
    if (S.contacts.length === 0) {
      _setStatus('No contacts in selected window');
      return;
    }

    const { startMs, endMs } = initPlayback(S.map, S.contacts, {
      onTick:    _onPlaybackTick,
      onContact: _onPlaybackContact,
    });
    S.startMs   = startMs;
    S.endMs     = endMs;
    S.virtualMs = startMs;
    S.playing   = false;

    const slider = document.getElementById('tl-slider');
    slider.value = 0;
    _updateTimeLabels(startMs, endMs, startMs);

    // Static hub pins
    _showHubPins(S.contacts);

    // Auto-fit map to contact + hub positions (skip when just toggling modes)
    if (!skipFit) {
      const fitPts = S.contacts
        .flatMap(c => {
          const pts = [];
          if (c.node_lat && c.node_lon) pts.push([c.node_lat, c.node_lon]);
          if (c.hub_lat  && c.hub_lon)  pts.push([c.hub_lat,  c.hub_lon]);
          return pts;
        });
      if (fitPts.length > 0) {
        try { S.map.fitBounds(L.latLngBounds(fitPts).pad(0.25)); } catch(_) {}
      }
    }

    // Show dim preview dots so map isn't blank before play
    _buildPreview();

  } else {
    _clearHubPins();
    clearPlayback();
    S.playing = false;
    document.getElementById('tl-play').textContent = '▶';
    if (!S.contacts.length) {
      _setStatus('No contacts in selected window');
      return;
    }

    // Render heatmap with bidirectional click sync
    renderHeatmap(S.map, S.contacts, {
      onSelect: idx => _selectContact(idx),
    });
    _showHubPins(S.contacts);

    if (!skipFit) {
      const fitPts = S.contacts
        .flatMap(c => {
          const pts = [];
          if (c.node_lat && c.node_lon) pts.push([c.node_lat, c.node_lon]);
          if (c.hub_lat  && c.hub_lon)  pts.push([c.hub_lat,  c.hub_lon]);
          return pts;
        });
      if (fitPts.length > 0) {
        try { S.map.fitBounds(L.latLngBounds(fitPts).pad(0.25)); } catch(_) {}
      }
    }

    // Wire up playback engine for timeline (heatmap mode — no node markers)
    const { startMs, endMs } = initPlayback(S.map, S.contacts, {
      onTick:       _onPlaybackTick,
      heatmapMode:  true,
    });
    S.startMs   = startMs;
    S.endMs     = endMs;
    S.playing   = false;

    // Start fully revealed (all contacts visible)
    S.virtualMs = endMs;
    const slider = document.getElementById('tl-slider');
    slider.value = 1000;
    _updateTimeLabels(startMs, endMs, endMs);
    updateHeatmapTime(endMs);   // all circles full opacity
  }
}

// ── Timeline callbacks ────────────────────────────────────────────────────────

function _onPlaybackTick(virtualMs) {
  S.virtualMs = virtualMs;
  const frac   = S.endMs > S.startMs ? (virtualMs - S.startMs) / (S.endMs - S.startMs) : 0;
  const slider = document.getElementById('tl-slider');
  slider.value = Math.round(frac * 1000);
  document.getElementById('tl-current').textContent = _fmtTime(new Date(virtualMs));

  // In heatmap mode, dim future contacts
  if (S.mode === 'heatmap') updateHeatmapTime(virtualMs);

  if (virtualMs >= S.endMs) {
    S.playing = false;
    document.getElementById('tl-play').textContent = '▶';
    _setStatus(S.mode === 'heatmap' ? 'All contacts revealed' : 'Playback complete');
  }
}

function _onPlaybackContact(c) {
  // Highlight contact in the list as it fires during playback
  const idx = S.contacts.indexOf(c);
  if (idx >= 0) {
    _highlightListItem(idx);
    // Expand SESSION SUMMARY accordion if collapsed (so user can see stats update)
  }
}

// ── Contact list ──────────────────────────────────────────────────────────────

function _renderContactList() {
  const list  = document.getElementById('contact-list');
  const badge = document.getElementById('contact-count');
  if (badge) badge.textContent = S.contacts.length;

  if (S.contacts.length === 0) {
    list.innerHTML = '<div class="contact-empty">No contacts for selection</div>';
    return;
  }

  list.innerHTML = S.contacts.map((c, i) => {
    const rssiClass = _rssiClass(c.rssi);
    const rssiStr   = c.rssi != null ? `${c.rssi}` : '—';
    const distStr   = c.distance_mi != null ? `${c.distance_mi.toFixed(1)}mi` : '';
    const timeStr   = c.ts ? c.ts.slice(11, 19) : '';
    return `<div class="contact-item" data-idx="${i}">
      <span class="ci-name">${c.source_name || c.source_id}</span>
      <span class="ci-time">${timeStr}</span>
      <span class="ci-rssi rssi-${rssiClass}">${rssiStr}</span>
      <span class="ci-dist">${distStr}</span>
    </div>`;
  }).join('');

  list.querySelectorAll('.contact-item').forEach(el => {
    el.addEventListener('click', () => _selectContact(+el.dataset.idx));
  });
}

function _highlightListItem(idx) {
  const list = document.getElementById('contact-list');
  list.querySelectorAll('.contact-item').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
  });
  // Scroll into view without scrolling page
  const active = list.querySelector('.contact-item.active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

// ── Contact detail panel ──────────────────────────────────────────────────────

async function _selectContact(idx) {
  S.selectedIdx = idx;
  const c = S.contacts[idx];
  _highlightListItem(idx);

  if (!c.node_lat || !c.node_lon) {
    updateChatContext({ selected: c, current_time: c.ts });
    return;
  }

  if (S.mode === 'replay') {
    seekPlayback(c._ms);
    // After seek, pan then open the marker popup
    S.map.panTo([c.node_lat, c.node_lon]);
    // Small delay so Leaflet finishes panning before opening popup
    setTimeout(() => openNodePopup(c.source_id, c), 250);
  } else {
    highlightHeatmapContact(idx);
    S.map.panTo([c.node_lat, c.node_lon]);
    setTimeout(() => openHeatmapPopup(S.map, idx, c), 250);
  }

  updateChatContext({ selected: c, current_time: c.ts });
}

function _clearDetail() {
  S.selectedIdx = null;
  const list = document.getElementById('contact-list');
  list.querySelectorAll('.contact-item.active').forEach(el => el.classList.remove('active'));
}

// ── Session stats ─────────────────────────────────────────────────────────────

function _computeStats(contacts) {
  if (!contacts.length) return null;
  const _n  = v => { const n = +v; return (v == null || v === '' || isNaN(n)) ? null : n; };
  const avg = arr => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;
  const r1  = v => v != null ? Math.round(v * 10) / 10 : null;

  const rssiVals = contacts.map(c => _n(c.rssi)).filter(v => v != null);
  const snrVals  = contacts.map(c => _n(c.snr)).filter(v => v != null);
  const sorted   = contacts.filter(c => c._ms != null && !isNaN(c._ms)).sort((a, b) => a._ms - b._ms);
  if (!sorted.length) return null;

  const nodeMap = {};
  for (const c of contacts) {
    const id = c.source_id;
    if (!nodeMap[id]) nodeMap[id] = { id, name: c.source_name || id, count: 0, rssis: [], dists: [] };
    nodeMap[id].count++;
    const r = _n(c.rssi);        if (r != null) nodeMap[id].rssis.push(r);
    const d = _n(c.distance_mi); if (d != null) nodeMap[id].dists.push(d);
  }
  const nodes = Object.values(nodeMap)
    .map(n => ({
      id:       n.id,
      name:     n.name,
      count:    n.count,
      avg_rssi: r1(avg(n.rssis)),
      avg_dist: n.dists.length ? r1(avg(n.dists)) : null,
    }))
    .sort((a, b) => b.count - a.count);

  const spanMs = sorted.length > 1 ? sorted[sorted.length - 1]._ms - sorted[0]._ms : 0;
  return {
    total:       contacts.length,
    time_range:  { start: sorted[0].ts, end: sorted[sorted.length - 1].ts },
    duration_ms: spanMs,
    rssi:        { avg: r1(avg(rssiVals)), min: rssiVals.length ? Math.min(...rssiVals) : null, max: rssiVals.length ? Math.max(...rssiVals) : null },
    snr:         { avg: r1(avg(snrVals)) },
    nodes,
  };
}

function _renderStats(s = S.stats, filtered = false) {
  if (!s || !s.total) return;
  const sec   = document.getElementById('session-stats');
  const inner = document.getElementById('stats-content');
  sec.style.display = '';   // reveal the accordion section

  // Update accordion header to indicate filtered vs full session
  const hdr = sec.querySelector('.section-header');
  if (hdr) {
    const arrow = hdr.querySelector('.acc-arrow')?.outerHTML || '';
    hdr.innerHTML = filtered
      ? `SESSION SUMMARY <span class="stats-filtered-badge">FILTERED</span> ${arrow}`
      : `SESSION SUMMARY ${arrow}`;
  }

  let timeStr = '—';
  if (s.time_range?.start) {
    const t1 = s.time_range.start.slice(11,19);
    const t2 = (s.time_range.end || '').slice(11,19);
    timeStr = `${t1} → ${t2}`;
    if (filtered && s.duration_ms != null) {
      const h = Math.floor(s.duration_ms / 3600000);
      const m = Math.floor((s.duration_ms % 3600000) / 60000);
      const dur = h > 0 ? `${h}h ${m}m` : `${m}m`;
      timeStr += ` <span class="stat-duration">(${dur})</span>`;
    }
  }

  inner.innerHTML = `
    <div class="stat-row"><span class="sk">Total</span><span class="sv">${s.total} contacts</span></div>
    <div class="stat-row"><span class="sk">Time</span><span class="sv">${timeStr}</span></div>
    <div class="stat-row"><span class="sk">RSSI avg</span><span class="sv">${s.rssi?.avg ?? '—'} dBm</span></div>
    <div class="stat-row"><span class="sk">RSSI range</span><span class="sv">${s.rssi?.min ?? '—'} → ${s.rssi?.max ?? '—'}</span></div>
    <div class="stat-row"><span class="sk">SNR avg</span><span class="sv">${s.snr?.avg ?? '—'} dB</span></div>
    ${(s.nodes||[]).length ? `
    <table class="node-table">
      <thead><tr><th></th><th>Node</th><th>Pkts</th><th>dBm avg</th><th>Dist avg</th></tr></thead>
      <tbody>
        ${(s.nodes||[]).map(n => {
          const excluded = S.excludedNodes.has(n.id);
          const rssiClass = excluded || n.avg_rssi == null ? 'rssi-na'
            : n.avg_rssi >= -65 ? 'rssi-ex'
            : n.avg_rssi >= -75 ? 'rssi-good'
            : n.avg_rssi >= -85 ? 'rssi-marg'
            : n.avg_rssi >= -100 ? 'rssi-poor'
            : 'rssi-na';
          return `<tr class="${excluded ? 'nt-excluded' : ''}">
            <td class="nt-cb-cell"><input type="checkbox" class="nt-cb" data-id="${n.id}" ${excluded ? '' : 'checked'}></td>
            <td class="nt-name">${n.name}</td>
            <td class="nt-num">${n.count}</td>
            <td class="nt-num ${rssiClass}">${n.avg_rssi ?? '—'}</td>
            <td class="nt-num">${n.avg_dist != null ? n.avg_dist + ' mi' : '—'}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>` : ''}
    <div class="rssi-legend">
      <div class="legend-title">RSSI SIGNAL</div>
      <div class="legend-row"><span class="legend-label">Excellent</span><span class="legend-range">≥ −65</span><span class="legend-swatch" style="background:#00e5ff"></span></div>
      <div class="legend-row"><span class="legend-label">Good</span><span class="legend-range">−65→−75</span><span class="legend-swatch" style="background:#00e676"></span></div>
      <div class="legend-row"><span class="legend-label">Marginal</span><span class="legend-range">−75→−85</span><span class="legend-swatch" style="background:#ffea00"></span></div>
      <div class="legend-row"><span class="legend-label">Poor</span><span class="legend-range">−85→−100</span><span class="legend-swatch" style="background:#ff6d00"></span></div>
      <div class="legend-row"><span class="legend-label">Very Poor</span><span class="legend-range">< −100</span><span class="legend-swatch" style="background:#f44336"></span></div>
    </div>
  `;

  // Wire node filter checkboxes
  inner.querySelectorAll('.nt-cb').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) S.excludedNodes.delete(cb.dataset.id);
      else            S.excludedNodes.add(cb.dataset.id);
      _applyFilters({ preserveView: true });
    });
  });
}

// ── Timeline controls ─────────────────────────────────────────────────────────

function _bindControls() {
  // Date select
  document.getElementById('date-select').addEventListener('change', e => _onDateChange(e.target.value));

  // Time window
  document.getElementById('btn-apply-window').addEventListener('click', _applyTimeWindow);

  // Mode buttons
  document.getElementById('btn-replay').addEventListener('click',  () => _setMode('replay'));
  document.getElementById('btn-heatmap').addEventListener('click', () => _setMode('heatmap'));

  // Timeline play/pause
  document.getElementById('tl-play').addEventListener('click', () => {
    if (S.playing) {
      pausePlayback();
      S.playing = false;
      document.getElementById('tl-play').textContent = '▶';
    } else {
      if (S.virtualMs >= S.endMs) seekPlayback(S.startMs);
      _clearPreview();   // hide dim dots — let the real replay paint markers
      startPlayback();
      S.playing = true;
      document.getElementById('tl-play').textContent = '⏸';
    }
  });

  document.getElementById('tl-stop').addEventListener('click', () => {
    pausePlayback();
    S.playing = false;
    document.getElementById('tl-play').textContent = '▶';
  });

  document.getElementById('tl-back').addEventListener('click', () => {
    seekPlayback(Math.max(S.startMs, S.virtualMs - 5 * 60 * 1000));
  });

  document.getElementById('tl-fwd').addEventListener('click', () => {
    seekPlayback(Math.min(S.endMs, S.virtualMs + 5 * 60 * 1000));
  });

  // Slider
  const slider = document.getElementById('tl-slider');
  slider.addEventListener('input', e => {
    const frac  = e.target.value / 1000;
    const ms    = S.startMs + frac * (S.endMs - S.startMs);
    seekPlayback(ms);
  });

  // Speed buttons
  document.querySelectorAll('.speed-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      S.speed = +btn.dataset.speed;
      setSpeedPlayback(S.speed);
    });
  });

  // Sidebar collapse toggle
  document.getElementById('sidebar-toggle').addEventListener('click', () => {
    const collapsed = document.body.classList.toggle('sidebar-collapsed');
    document.getElementById('sidebar-toggle').textContent = collapsed ? '◀' : '▶';
    setTimeout(() => S.map.invalidateSize(), 220);
  });

  // Accordions — click section-header to collapse/expand
  document.querySelectorAll('.sidebar-section.accordion > .section-header').forEach(hdr => {
    hdr.addEventListener('click', e => {
      // Don't collapse if clicking a button inside the header
      if (e.target.closest('button')) return;
      const sec = hdr.parentElement;
      sec.classList.toggle('collapsed');
      const arrow = hdr.querySelector('.acc-arrow');
      if (arrow) arrow.textContent = sec.classList.contains('collapsed') ? '▶' : '▼';
    });
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _setStatus(msg) {
  const el = document.getElementById('load-status');
  if (el) el.textContent = msg;
}

function _updateTimeLabels(startMs, endMs, nowMs) {
  document.getElementById('tl-start-label').textContent = _fmtTime(new Date(startMs));
  document.getElementById('tl-end-label').textContent   = _fmtTime(new Date(endMs));
  document.getElementById('tl-current').textContent     = _fmtTime(new Date(nowMs));
}

function _fmtTime(d) {
  if (!(d instanceof Date) || isNaN(d)) return '--:--:--';
  return d.toTimeString().slice(0, 8);
}

function _rssiClass(rssi) {
  if (rssi == null)  return 'na';
  if (rssi >= -65)   return 'ex';
  if (rssi >= -75)   return 'good';
  if (rssi >= -85)   return 'marg';
  return 'poor';
}

function _rssiColorHex(rssi) {
  if (rssi == null)  return '#9e9e9e';
  if (rssi >= -65)   return '#00e5ff';
  if (rssi >= -75)   return '#00e676';
  if (rssi >= -85)   return '#ffea00';
  return '#f44336';
}
