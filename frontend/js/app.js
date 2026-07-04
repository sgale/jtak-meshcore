// ── jTAK App — main entrypoint ────────────────────────────────────────────
import { initMap, updateNode, fitAllNodes, startMarkerRefresh, updateSelfMarker, switchToOffline, restoreBasemap, centerOnHub } from './map.js';
import { renderNodes, updateRFStats, updateHealth, updateSensors, logEvent } from './sidebar.js';
import { initWeather, startWeather, stopWeather, setHubPosition, updateLocalSensors, toggleEmberSpot, isEmberActive, setEmberOrigin, deactivateEmber } from './weather.js';
import { initFireSpread, stopFireSpread, isFireSpreadActive } from './firespread.js';
import { initFireLayers, startFireLayers, stopFireLayers, setFireHubPos, fitToFires } from './firelayers.js';
import { initAircraft, onInternetOn, onInternetOff } from './aircraft.js';
import { initTileCache } from './tilecache.js';
import { startAtmo, stopAtmo } from './atmo.js';
import { initLightning, startLightning, stopLightning } from './lightning.js';
import { initMeasure, stopMeasure, isMeasureActive } from './measure.js';
import { initMesh, updateMeshNodes } from './mesh.js';
import { initWaypoints, onWaypointMessage, stopDrop as stopWpDrop } from './waypoints.js';
import { initZones, onZoneMessage, cancelDraw as cancelZoneDraw } from './zones.js';
import { sessionGet, sessionSet, initAccordions, initSidebarToggle, initHudToggle, initDraggablePanels, initUiPrefs } from './session.js';
import { initSounds, playEvent, toggleMute, isSoundEnabled } from './sounds.js';

const API   = '/jtak/api';
const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/jtak/api/ws/live`;

const connDot       = document.getElementById('conn-indicator');
let _wsClass  = '';   // '', 'live', 'error'
let _gpsClass = '';   // '', 'gps', 'gps-fair', 'gps-poor'
function setConnDot(ws, gps) {
  if (ws  !== undefined) _wsClass  = ws;
  if (gps !== undefined) _gpsClass = gps;
  connDot.className = [_wsClass, _gpsClass].filter(Boolean).join(' ');
}
const clockEl       = document.getElementById('clock');
const satCount      = document.getElementById('sat-count');
const hubId         = document.getElementById('hub-id');
const hudHub        = document.getElementById('hud-hub');
const hudZtIp       = document.getElementById('hud-zt-ip');
const hudZtRow      = document.getElementById('hud-zt-row');
const hudGuid       = document.getElementById('hud-guid');
const hudGuidRow    = document.getElementById('hud-guid-row');
const inetIndicator  = document.getElementById('inet-indicator');
const fedWrap        = document.getElementById('fed-toggle-wrap');
const fedCb          = document.getElementById('fed-toggle-cb');
const meshDebugWrap  = document.getElementById('mesh-debug-wrap');
const meshDebugCb    = document.getElementById('mesh-debug-cb');
let   _meshDebugEnabled = false;   // true = feature visible on this hub
let   _selfIds = new Set();        // hub's own source_ids — filtered from nodes list

let ws, reconnectTimer;
let _hudOrdered = false;

const _HUD_CHIP_IDS = {
  hub:         'hub-chip',
  atmo:        'atmo-hud-chip',
  wind:        'wind-hud',
  fire_data:   'fire-data-hud',
  aircraft:    'aircraft-hud',
  fire_spread: 'fire-btn',
  ember_spot:  'ember-btn',
  measure:     'measure-btn',
  hq_feed:     'fed-toggle-wrap',
  waypoint:    'wp-drop-btn',
  zones:       'zones-btn',
  led_ctrl:    'led-hud',
};

function _applyHudRules(container, allowed, chips) {
  const anchor = document.getElementById('center-hub-btn');
  if (!anchor) return;
  Object.entries(_HUD_CHIP_IDS).forEach(([key, id]) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (!allowed.has(key)) el.classList.add('hud-omitted');
  });
  for (const key of chips) {
    const el = document.getElementById(_HUD_CHIP_IDS[key]);
    if (el) container.insertBefore(el, anchor);
  }
}

function _applyHudOrder(chips) {
  if (_hudOrdered || !chips?.length) return;
  _hudOrdered = true;
  const container = document.getElementById('hud-chips');
  if (!container) return;
  const allowed = new Set(chips);
  // Apply immediately for static chips, then retry for dynamic chips
  // (fire_data is created async after internet + fire fetch completes)
  _applyHudRules(container, allowed, chips);
  const t = setInterval(() => {
    _applyHudRules(container, allowed, chips);
    // Stop once all expected chips are present in the DOM
    const allPresent = chips.every(k => !_HUD_CHIP_IDS[k] || !!document.getElementById(_HUD_CHIP_IDS[k]));
    if (allPresent) clearInterval(t);
  }, 500);
  setTimeout(() => clearInterval(t), 30000); // safety cutoff at 30s
}
let _sidebarApplied = false;
function _applySidebarPanels(panels) {
  if (_sidebarApplied) return;
  _sidebarApplied = true;
  localStorage.setItem('jtak_sidebar_panels', JSON.stringify(panels));
  const allowed = new Set(panels);
  document.querySelectorAll('#sidebar .panel[data-panel]').forEach(el => {
    if (!allowed.has(el.dataset.panel)) el.style.display = 'none';
  });
}

let nodeCache = {};
let _hubPos = null;
let _fedRestored   = false;
let _statusFails   = 0;
let _mapCentered   = false;

// ── Map loader ────────────────────────────────────────────────────────────────
const _mapLoader = document.getElementById('map-loader');
let _loaderDismissed = false;
const _loaderFallback = setTimeout(_dismissLoader, 10000);
function _dismissLoader() {
  if (_loaderDismissed) return;
  _loaderDismissed = true;
  clearTimeout(_loaderFallback);
  if (_mapLoader) {
    _mapLoader.classList.add('fade-out');
    setTimeout(() => { _mapLoader.style.display = 'none'; }, 650);
  }
}

// ── Internet state ────────────────────────────────────────────────────────────
export let onlineMode = false;

function _applyInetState(on) {
  onlineMode = on;
  inetIndicator.style.display = on ? 'flex' : 'none';
  document.dispatchEvent(new CustomEvent('onlineMode', { detail: on }));
  if (on) { startWeather(); startFireLayers(); onInternetOn();  startAtmo(); startLightning(); _showFedToggle(); restoreBasemap(); }
  else    { stopWeather();  stopFireLayers();  onInternetOff(); stopAtmo();  stopLightning();  _hideFedToggle(); switchToOffline(); }
}

// ── Federation state ──────────────────────────────────────────────────────────
async function _loadFedState() {
  try {
    const r = await fetch(`${API}/federation`);
    const s = await r.json();
    fedCb.checked = s.enabled;
  } catch(e) { /* silently ignore */ }
}

async function _setFedState(on) {
  fedCb.disabled = true;
  try {
    const r = await fetch(`${API}/federation`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: on }),
    });
    const s = await r.json();
    fedCb.checked = s.enabled;
    logEvent(s.enabled ? '&#x1F6F0; HQ Feed ENABLED — streaming telemetry' : 'HQ Feed disabled', s.enabled);
  } catch(e) {
    logEvent(`Federation toggle failed: ${e.message}`);
  } finally {
    fedCb.disabled = false;
  }
}

function _showFedToggle() {
  fedWrap.style.display = 'flex';
  if (!_fedRestored) {
    _fedRestored = true;
    _loadFedState();
  }
}

function _hideFedToggle() {
  fedWrap.style.display = 'none';
}

fedCb.addEventListener('change', () => {
  _setFedState(fedCb.checked);
});

// ── Meshtastic debug toggle ───────────────────────────────────────────────────
async function _syncMeshDebugState() {
  try {
    const r = await fetch(`${API}/meshtastic/debug`);
    if (!r.ok) return;
    const d = await r.json();
    meshDebugCb.checked = d.debug_enabled;
  } catch(e) { /* silent */ }
}

async function _setMeshDebug(enable) {
  meshDebugCb.disabled = true;
  try {
    const r = await fetch(`${API}/meshtastic/debug`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enable}),
    });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    meshDebugCb.checked = d.debug_enabled;
    logEvent(enable
      ? 'Mesh monitor stopped — Meshtastic app can now connect'
      : 'Mesh monitor started — live data resuming');
  } catch(e) {
    logEvent(`Mesh debug toggle failed: ${e.message}`);
    await _syncMeshDebugState();   // revert to actual state
  } finally {
    meshDebugCb.disabled = false;
  }
}

meshDebugCb.addEventListener('change', () => _setMeshDebug(meshDebugCb.checked));

// ── Clock ────────────────────────────────────────────────────────────────────
function tickClock() {
  const now = new Date();
  clockEl.textContent = now.toLocaleTimeString([], {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'}) + ' Z';
}
setInterval(tickClock, 1000);
tickClock();

// ── Header height sync — keeps sidebar flush on all screen sizes ──────────────
function _syncHeaderHeight() {
  const h = document.getElementById('header');
  if (h) document.documentElement.style.setProperty('--header-h', h.getBoundingClientRect().height + 'px');
}
_syncHeaderHeight();
window.addEventListener('resize', _syncHeaderHeight);

// ── Map init ─────────────────────────────────────────────────────────────────
const map = initMap([40.5729, -111.9941], 15);
window._jtakMap = map;
initLightning(map);
initMeasure(map);
initMesh(map);

// ── Sound mute toggle ─────────────────────────────────────────────────────────
// _updateSoundBtn is called after mute state is restored from localStorage
function _updateSoundBtn() {
  const btn = document.getElementById('sound-test-btn');
  if (!btn) return;
  const on = isSoundEnabled();
  btn.textContent = on ? '🔔' : '🔕';
  btn.title = on ? 'Sound ON — click to mute' : 'Sound MUTED — click to unmute';
  btn.classList.toggle('muted', !on);
}
(function () {
  const btn = document.getElementById('sound-test-btn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    toggleMute();
    localStorage.setItem('jtak_sound_muted', isSoundEnabled() ? '0' : '1');
    _updateSoundBtn();
  });
})();

// ── Fetch initial data ────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const r = await fetch(`${API}/status`);
    const s = await r.json();
    _selfIds = new Set([s.hub_id, s.hub_guid ? '!' + s.hub_guid : null].filter(Boolean));
    if (hubId)   hubId.textContent   = s.hub_short_name || s.hub_id;
    if (hudHub)  hudHub.textContent  = s.hub_name;
    if (s.hub_short_name) document.title = `${s.hub_short_name} — jTAK`;
    if (s.hub_guid && hudGuid) {
      hudGuid.textContent     = s.hub_guid;
      hudGuidRow.style.display = '';
    }
    if (s.zt_ip && hudZtIp) {
      hudZtIp.textContent    = s.zt_ip;
      hudZtRow.style.display = '';
    }
    updateHealth(s);
    // GPS quality class based on HDOP
    let gpsClass = '';
    if (s.hub_position) {
      const hdop = s.hub_hdop;
      gpsClass = hdop == null || hdop < 2.0 ? 'gps'
               : hdop < 5.0                 ? 'gps-fair'
               :                              'gps-poor';
    }
    setConnDot(undefined, gpsClass);
    // Sat count + quality label
    if (satCount) {
      const qual = gpsClass === 'gps-fair' ? ' FAIR'
                 : gpsClass === 'gps-poor' ? ' POOR' : '';
      satCount.textContent = s.hub_sats != null ? `${s.hub_sats} SAT${qual}` : '0 SAT';
    }
    if (s.hub_position) {
      _hubPos = s.hub_position;
      updateSelfMarker(s.hub_position, s);
      setHubPosition(s.hub_position);
      setFireHubPos(s.hub_position);
      if (!_mapCentered && s.hub_position.latitude && s.hub_position.longitude) {
        map.setView([s.hub_position.latitude, s.hub_position.longitude], 19);
        _mapCentered = true;
      }
    }
    // Feed local BME data to weather module for FDFM calculation
    updateLocalSensors(s.bme_temp_f ?? null, s.bme_humidity_pct ?? null);

    // Inject hub's own BME sensor into the Sensors panel so it appears alongside mesh nodes
    if (s.bme_temp_c != null) {
      updateSensors({
        source_id:    s.hub_id,
        source_name:  (s.hub_short_name || s.hub_name) + ' (this hub)',
        temp_c:       s.bme_temp_c,
        temp_f:       s.bme_temp_f,
        humidity_pct: s.bme_humidity_pct,
        pressure_hpa: s.bme_pressure_hpa ?? null,
      });
    }
    logEvent(`Hub: ${s.hub_name} — CPU ${s.cpu_pct?.toFixed(0)}% Temp ${s.cpu_temp_c}°C`);
    if (s.hud_chips) { localStorage.setItem('jtak_hud_chips', JSON.stringify(s.hud_chips)); _applyHudOrder(s.hud_chips); }
    if (s.sidebar_panels) _applySidebarPanels(s.sidebar_panels);
    if (s.sounds) {
      initSounds(s.sounds);
      // Restore mute preference — overrides yaml default
      const muted = localStorage.getItem('jtak_sound_muted');
      if (muted === '1' && isSoundEnabled()) toggleMute();
      else if (muted === '0' && !isSoundEnabled()) toggleMute();
      _updateSoundBtn();
    }

    // Meshtastic debug toggle visibility
    if (s.meshtastic_debug && !_meshDebugEnabled) {
      _meshDebugEnabled = true;
      meshDebugWrap.style.display = 'flex';
      _syncMeshDebugState();
    }

    // Auto-enable/disable online features on any internet state transition
    _statusFails = 0;
    if (s.internet !== onlineMode) _applyInetState(s.internet);
  } catch(e) {
    logEvent(`Status fetch failed: ${e.message}`);
    // 2 consecutive failures = ~30s grace period before going offline (tolerates quick reboots)
    if (onlineMode && ++_statusFails >= 2) _applyInetState(false);
  }
}

async function loadPositions() {
  try {
    const r = await fetch(`${API}/nodes`);
    const nodes = await r.json();
    // Replace cache with authoritative DB snapshot — removes ghost entries
    Object.keys(nodeCache).forEach(k => delete nodeCache[k]);
    nodes.forEach(n => {
      nodeCache[n.source_id] = n;
      updateNode(n);
    });
    { const all = Object.values(nodeCache); renderNodes(all.filter(n => !_selfIds.has(n.source_id))); updateMeshNodes(all); }
    if (nodes.length > 0 && !_mapCentered) fitAllNodes();
    logEvent(`Loaded ${nodes.length} nodes`, true);
  } catch(e) {
    logEvent(`Nodes fetch failed: ${e.message}`);
  }
}

// ── WebSocket ────────────────────────────────────────────────────────────────
function connectWS() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    setConnDot('live');
    logEvent('WebSocket connected — live feed active', true);
    clearTimeout(reconnectTimer);
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'ping') return;
    _dismissLoader();

    if (msg.type === 'zone') {
      onZoneMessage(msg);
      return;
    }
    if (msg.type === 'waypoint') {
      if (!msg.deleted_at) playEvent('waypoint');
      onWaypointMessage(msg);
      return;
    }

    if (msg.type === 'rf') {
      updateRFStats(msg);
      // Merge into node cache
      const cached = nodeCache[msg.source_id] || {};
      nodeCache[msg.source_id] = {
        ...cached,
        source_id:    msg.source_id,
        source_name:  msg.source_name,
        latitude:     msg.lat         ?? cached.latitude,
        longitude:    msg.lon         ?? cached.longitude,
        rssi:         msg.rssi        ?? cached.rssi,
        snr:          msg.snr         ?? cached.snr,
        distance_mi:  msg.distance_mi ?? cached.distance_mi,
        packet_type:  msg.packet_type,
        temp_c:       msg.temp_c      ?? cached.temp_c,
        temp_f:       msg.temp_f      ?? cached.temp_f,
        humidity_pct: msg.humidity_pct ?? cached.humidity_pct,
        pressure_hpa: msg.pressure_hpa ?? cached.pressure_hpa,
        last_position: msg.timestamp,
      };
      updateSensors(nodeCache[msg.source_id]);
      if (msg.lat && msg.lon) updateNode(nodeCache[msg.source_id]);
      { const all = Object.values(nodeCache); renderNodes(all.filter(n => !_selfIds.has(n.source_id))); updateMeshNodes(all); }
      logEvent(`${msg.source_name || msg.source_id}  ${msg.packet_type || ''}  RSSI:${msg.rssi ?? '—'}`);
      // Persist node cache so returning from /history restores contacts
      try { sessionStorage.setItem('jtak_live_nodes', JSON.stringify(nodeCache)); } catch {}
    }
  };

  ws.onerror = () => {
    setConnDot('error');
    // onclose always fires after onerror — reconnect handled there
  };

  ws.onclose = () => {
    setConnDot('');
    clearTimeout(reconnectTimer);
    logEvent('WebSocket closed — reconnecting in 5s…');
    reconnectTimer = setTimeout(connectWS, 5000);
  };
}

// ── Status polling (every 15s) ────────────────────────────────────────────────
setInterval(loadStatus, 15000);

// ── Beacon toggle ─────────────────────────────────────────────────────────────
(function () {
  const btn = document.getElementById('beacon-btn');
  if (!btn) return;
  let active = false;

  function setBeacon(val) {
    active = val;
    btn.classList.toggle('active', active);
    fetch(`${API}/led/beacon`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({active})
    }).catch(() => {});
  }

  btn.addEventListener('click', () => setBeacon(!active));

  // restore state from server on load
  fetch(`${API}/led/beacon`).then(r => r.json()).then(d => {
    active = d.active ?? false;
    btn.classList.toggle('active', active);
  }).catch(() => {});
})();

// ── LED brightness slider ─────────────────────────────────────────────────────
(function () {
  const slider = document.getElementById('led-slider');
  const label  = document.getElementById('led-slider-label');
  const ctrl   = document.getElementById('led-ctrl');
  if (!slider) return;

  let debounce = null;

  function applyValue(pct) {
    if (pct === 0) {
      label.textContent = 'LIGHT OFF';
      ctrl.classList.add('led-off');
    } else {
      label.textContent = pct + '%';
      ctrl.classList.remove('led-off');
    }
    slider.style.background =
      `linear-gradient(to right, var(--orange) 0%, var(--orange) ${pct}%, var(--border) ${pct}%, var(--border) 100%)`;
  }

  function sendBrightness(pct) {
    fetch(`${API}/led/brightness`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({brightness: pct})
    }).catch(() => {});
  }

  slider.addEventListener('input', () => {
    const pct = parseInt(slider.value, 10);
    applyValue(pct);
    clearTimeout(debounce);
    debounce = setTimeout(() => sendBrightness(pct), 150);
  });

  // init state from server on load
  fetch(`${API}/led/brightness`).then(r => r.json()).then(d => {
    const pct = d.brightness ?? 100;
    slider.value = pct;
    applyValue(pct);
  }).catch(() => applyValue(100));
})();

// ── Boot sequence ─────────────────────────────────────────────────────────────
(async () => {
  await initUiPrefs();     // load server prefs into localStorage on fresh browser

  // Apply cached sidebar/hud config immediately so panels don't flash on first poll
  const _cachedPanels = localStorage.getItem('jtak_sidebar_panels');
  const _cachedChips  = localStorage.getItem('jtak_hud_chips');
  if (_cachedPanels) try { _applySidebarPanels(JSON.parse(_cachedPanels)); } catch {}
  if (_cachedChips)  try { _applyHudOrder(JSON.parse(_cachedChips)); } catch {}

  initDraggablePanels();   // order restored + Sortable bound before accordions
  initAccordions();
  initSidebarToggle();
  initHudToggle();
  initWeather(map, null);
  initFireSpread(map);
  document.getElementById('center-hub-btn').addEventListener('click', () => centerOnHub(_hubPos));

  // ── Tool mutual exclusion — deactivate all map tools except the one named ──
  let _stopEmber = () => {}; // filled in by ember IIFE below

  function _deactivateOtherTools(except) {
    if (except !== 'fire'     && isFireSpreadActive()) stopFireSpread();
    if (except !== 'ember')                            _stopEmber();
    if (except !== 'measure'  && isMeasureActive())    stopMeasure();
    if (except !== 'waypoint') stopWpDrop();
  }

  // Patch fire-spread button to deactivate others when activating
  document.getElementById('fire-btn').addEventListener('click', () => {
    if (!isFireSpreadActive()) _deactivateOtherTools('fire');
  }, true);

  // Patch measure button to deactivate others when activating
  document.getElementById('measure-btn').addEventListener('click', () => {
    if (!isMeasureActive()) _deactivateOtherTools('measure');
  }, true);

  // ── ZONES HUD button — open zones sidebar panel ──────────────────────────
  document.getElementById('zones-btn')?.addEventListener('click', () => {
    const panel = document.querySelector('[data-panel="zones"]');
    if (!panel) return;
    const sidebar = document.getElementById('sidebar');
    // Open sidebar if collapsed
    if (sidebar?.classList.contains('collapsed')) sidebar.classList.remove('collapsed');
    // Expand the zones panel if collapsed
    if (panel.classList.contains('collapsed')) panel.classList.remove('collapsed');
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // ── EMBER SPOT HUD button ────────────────────────────────────────────────
  (function () {
    const btn = document.getElementById('ember-btn');
    let _awaitingClick = false;

    function _setStatus(msg) {
      let el = document.getElementById('ember-status-hud');
      if (!msg) { if (el) el.remove(); return; }
      if (!el) {
        el = document.createElement('div');
        el.id = 'ember-status-hud';
        el.className = 'hud-chip';
        document.getElementById('map-hud').appendChild(el);
      }
      el.textContent = msg;
    }

    function _setAwait(on) {
      _awaitingClick = on;
      btn.classList.toggle('awaiting', on);
      map.getContainer().style.cursor = on ? 'crosshair' : '';
      _setStatus(on ? 'Click map to place ember origin' : null);
    }

    function _onMapClick(e) {
      if (isEmberActive()) {
        // Already active — just move the origin, keep listening
        setEmberOrigin(e.latlng.lat, e.latlng.lng);
      } else {
        // First click — activate ember at this point
        toggleEmberSpot(e.latlng.lat, e.latlng.lng);
        btn.classList.add('active');
        _setAwait(false);   // transition from awaiting → active
        _setStatus('Click map to move ember origin');
      }
    }

    // Expose stop function for mutual exclusion
    _stopEmber = function () {
      deactivateEmber();           // explicit set-to-false, safe to call anytime
      btn.classList.remove('active');
      map.off('click', _onMapClick);
      _setAwait(false);
    };

    btn.addEventListener('click', () => {
      if (isEmberActive() || _awaitingClick) {
        _stopEmber();
      } else {
        // Activate — deactivate other tools first, then wait for first map click
        _deactivateOtherTools('ember');
        _setAwait(true);
        map.on('click', _onMapClick);
      }
    });
  })();
  initFireLayers(map, null);
  initAircraft(map);   // SDR polling starts immediately inside initAircraft
  initTileCache(map);
  initWaypoints(map);
  initZones(map);

  // Restore cached nodes immediately so map isn't blank when returning from /history
  try {
    const cached = JSON.parse(sessionStorage.getItem('jtak_live_nodes') || 'null');
    if (cached && typeof cached === 'object') {
      Object.assign(nodeCache, cached);
      Object.values(nodeCache).forEach(n => { if (n.latitude && n.longitude) updateNode(n); });
      { const all = Object.values(nodeCache); renderNodes(all.filter(n => !_selfIds.has(n.source_id))); updateMeshNodes(all); }
    }
  } catch {}

  await loadStatus();
  await loadPositions();  // fresh positions overlay/update the cache
  connectWS();
  startMarkerRefresh(nodeCache);
})();
