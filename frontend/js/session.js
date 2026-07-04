// ── Session state — persists across page refreshes, clears on browser close ───
const PREFIX = 'jtak_';

export function sessionGet(key, fallback = null) {
  try {
    const v = sessionStorage.getItem(PREFIX + key);
    return v === null ? fallback : JSON.parse(v);
  } catch { return fallback; }
}

export function sessionSet(key, value) {
  try { sessionStorage.setItem(PREFIX + key, JSON.stringify(value)); } catch {}
}

// ── Local state — persists permanently across browser sessions ────────────────
function localGet(key, fallback = null) {
  try {
    const v = localStorage.getItem(PREFIX + key);
    return v === null ? fallback : JSON.parse(v);
  } catch { return fallback; }
}

function localSet(key, value) {
  try { localStorage.setItem(PREFIX + key, JSON.stringify(value)); } catch {}
}

// ── Stable browser fingerprint — no cookies or localStorage needed ───────────
async function _getClientId() {
  const raw = [
    screen.width, screen.height, screen.colorDepth,
    Intl.DateTimeFormat().resolvedOptions().timeZone,
    navigator.language,
    navigator.platform,
  ].join('|');
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2,'0')).join('').slice(0, 32);
}

// ── Server prefs sync ─────────────────────────────────────────────────────────
function _allPrefKeys() {
  const keys = ['panel_order', 'sidebar_collapsed', 'hud_collapsed', 'sidebar_panels', 'hud_chips'];
  document.querySelectorAll('#sidebar .panel[data-panel]').forEach(p => {
    keys.push(`panel_${p.dataset.panel}_collapsed`);
  });
  return keys;
}

function _collectPrefs() {
  const out = {};
  _allPrefKeys().forEach(k => {
    const v = localStorage.getItem(PREFIX + k);
    if (v !== null) out[k] = v;
  });
  return out;
}

function _applyServerPrefs(serverPrefs) {
  if (!serverPrefs || !Object.keys(serverPrefs).length) return;
  Object.entries(serverPrefs).forEach(([k, v]) => {
    try { localStorage.setItem(PREFIX + k, v); } catch {}
  });
}

let _clientId = null;
let _syncTimer = null;

function _scheduleSyncToServer() {
  clearTimeout(_syncTimer);
  _syncTimer = setTimeout(async () => {
    if (!_clientId) return;
    try {
      await fetch('/jtak/api/ui-prefs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Client-ID': _clientId },
        body: JSON.stringify({ prefs: _collectPrefs() }),
      });
    } catch {}
  }, 1500);
}

export async function initUiPrefs() {
  _clientId = await _getClientId();
  try {
    const resp = await fetch('/jtak/api/ui-prefs', { headers: { 'X-Client-ID': _clientId } });
    const serverPrefs = resp.ok ? await resp.json() : {};
    _applyServerPrefs(serverPrefs);
  } catch(e) {
    console.warn('[jtak-prefs] load error:', e);
  }
}

// Call after any state change to persist locally + sync to server
function _afterChange() {
  _scheduleSyncToServer();
}

// ── Sidebar collapse ──────────────────────────────────────────────────────────
export function initSidebarToggle() {
  const sidebar = document.getElementById('sidebar');
  const btn     = document.getElementById('sidebar-toggle');
  if (!sidebar || !btn) return;

  const smallScreen = window.innerWidth < 600;
  const savedCollapsed = localGet('sidebar_collapsed', smallScreen);

  const apply = (collapsed) => {
    sidebar.classList.toggle('collapsed', collapsed);
    btn.classList.toggle('collapsed', collapsed);
    const mobile = window.innerWidth <= 600;
    btn.innerHTML = collapsed
      ? (mobile ? '&#9650;' : '&#9654;')
      : (mobile ? '&#9660;' : '&#9664;');
    localSet('sidebar_collapsed', collapsed);
    _afterChange();
    setTimeout(() => { const m = window._jtakMap; if (m) m.invalidateSize(); }, 300);
  };

  apply(savedCollapsed);
  btn.addEventListener('click', () => apply(!sidebar.classList.contains('collapsed')));
}

// ── HUD collapse ──────────────────────────────────────────────────────────────
export function initHudToggle() {
  const btn   = document.getElementById('hud-toggle');
  const chips = document.getElementById('hud-chips');
  if (!btn || !chips) return;

  const apply = (collapsed) => {
    chips.classList.toggle('collapsed', collapsed);
    btn.innerHTML = collapsed ? '&#9650; HUD' : '&#9660; HUD';
    localSet('hud_collapsed', collapsed);
    _afterChange();
  };

  apply(localGet('hud_collapsed', false));
  btn.addEventListener('click', () => apply(!chips.classList.contains('collapsed')));
}

// ── Accordion panels ──────────────────────────────────────────────────────────
export function initAccordions() {
  document.querySelectorAll('.panel[data-panel]').forEach(panel => {
    const key    = panel.dataset.panel;
    const header = panel.querySelector('.panel-header');
    if (!header) return;

    if (localGet(`panel_${key}_collapsed`, false)) {
      panel.classList.add('collapsed');
    }

    header.addEventListener('click', (e) => {
      if (e.target.closest('.panel-drag-handle')) return;
      const collapsed = panel.classList.toggle('collapsed');
      localSet(`panel_${key}_collapsed`, collapsed);
      _afterChange();
    });
  });
}

// ── Draggable panels ──────────────────────────────────────────────────────────
export function initDraggablePanels() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar || typeof Sortable === 'undefined') return;

  document.querySelectorAll('#sidebar .panel[data-panel]').forEach(panel => {
    const header = panel.querySelector('.panel-header');
    if (!header || header.querySelector('.panel-drag-handle')) return;
    const handle = document.createElement('span');
    handle.className = 'panel-drag-handle';
    handle.innerHTML = '&#9776;';
    handle.title = 'Drag to reorder';
    header.insertBefore(handle, header.firstChild);
  });

  const savedOrder = localGet('panel_order', null);
  if (Array.isArray(savedOrder)) {
    savedOrder.forEach(key => {
      const panel = sidebar.querySelector(`.panel[data-panel="${key}"]`);
      if (panel) sidebar.appendChild(panel);
    });
  }

  Sortable.create(sidebar, {
    handle:      '.panel-drag-handle',
    animation:   150,
    ghostClass:  'panel-drag-ghost',
    chosenClass: 'panel-drag-chosen',
    onEnd: () => {
      const order = [...sidebar.querySelectorAll('.panel[data-panel]')]
        .map(p => p.dataset.panel);
      localSet('panel_order', order);
      _afterChange();
    },
  });
}
