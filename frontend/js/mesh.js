// ── Meshtastic messaging — compose modal, chat panel, map toasts ──────────────
import { playEvent } from './sounds.js';

const API      = '/jtak/api/mesh';
const MAX_BYTES = 200;   // safe under 237-byte LoRa limit

const _LAST_ID_KEY    = 'jtak_last_msg_id';
let _map            = null;
let _sse            = null;
let _lastMsgId      = (() => {
  try { return parseInt(localStorage.getItem(_LAST_ID_KEY) || '0') || 0; } catch { return 0; }
})();
let _allMessages    = [];
let _channels       = [];
let _nodeCache      = [];
let _nameMap        = {};           // id → best known name (built from messages + nodeCache)
let _unread         = {};           // nodeId → unread count
let _pollTimer      = null;
let _composeMode    = 'direct';     // 'direct' | 'channel'
let _recipients     = [];           // [{id, name}] or [{index, name}]
let _filterNodeId   = null;
let _dropdownOpen   = false;

// ── Init ──────────────────────────────────────────────────────────────────────

export function initMesh(map) {
  _map = map;
  _initModal();
  _initChatPanel();
  _loadChannels().then(() => _loadHistory().then(() => _startSSE()));

  // Compose trigger from sidebar chat-bubble clicks
  document.addEventListener('mesh:compose', e => {
    openCompose(e.detail.nodeId, e.detail.nodeName);
  });
}

export function updateMeshNodes(nodes) {
  _nodeCache = nodes || [];
  // Seed name map from node cache
  for (const n of _nodeCache) {
    if (n.source_id && n.source_name) _nameMap[n.source_id] = n.source_name;
  }
  _refreshFilterCounts();
}

function _indexMsgNames(msg) {
  if (msg.from_id && msg.from_name) _nameMap[msg.from_id] = msg.from_name;
  if (msg.to_id   && msg.to_name)   _nameMap[msg.to_id]   = msg.to_name;
}

function _refreshFilterCounts() {
  const sel = document.getElementById('mesh-filter-node');
  if (!sel) return;

  // Count messages per node (keyed by from_id / to_id)
  const counts = {};
  for (const m of _allMessages) {
    const peer = m.direction === 'rx' ? m.from_id : m.to_id;
    if (peer && peer !== '^all') counts[peer] = (counts[peer] || 0) + 1;
  }
  const total = _allMessages.length;

  const prev = sel.value;
  sel.innerHTML = `<option value="">All nodes (${total})</option>`;
  for (const n of _nodeCache) {
    const c = counts[n.source_id] || 0;
    const opt = document.createElement('option');
    opt.value = n.source_id;
    opt.textContent = c > 0
      ? `${n.source_name || n.source_id} (${c})`
      : n.source_name || n.source_id;
    sel.appendChild(opt);
  }
  // Also add any nodes seen in messages but not in nodeCache
  for (const [id, c] of Object.entries(counts)) {
    if (!_nodeCache.find(n => n.source_id === id)) {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = `${id} (${c})`;
      sel.appendChild(opt);
    }
  }
  sel.value = prev;
}

// ── Modal init ────────────────────────────────────────────────────────────────

function _initModal() {
  document.getElementById('mesh-modal').addEventListener('click', e => {
    if (e.target.id === 'mesh-modal') _closeCompose();
  });
  document.getElementById('mesh-modal-close').addEventListener('click', _closeCompose);
  document.getElementById('mesh-mode-direct').addEventListener('click',  () => _setMode('direct'));
  document.getElementById('mesh-mode-channel').addEventListener('click', () => _setMode('channel'));
  document.getElementById('mesh-add-btn').addEventListener('click', _toggleDropdown);
  document.getElementById('mesh-msg-input').addEventListener('input', _updateCounter);
  document.getElementById('mesh-send-btn').addEventListener('click', _handleSend);
  document.addEventListener('click', e => {
    if (_dropdownOpen && !e.target.closest('#mesh-add-dropdown') && !e.target.closest('#mesh-add-btn')) {
      _hideDropdown();
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.getElementById('mesh-modal').style.display !== 'none') {
      _closeCompose();
    }
  });
}

function _initChatPanel() {
  document.getElementById('mesh-clear-btn').addEventListener('click', async () => {
    if (!confirm('Clear all message history?')) return;
    await fetch(`${API}/messages`, { method: 'DELETE' });
    _allMessages = [];
    _renderChatPanel();
    _refreshFilterCounts();
  });
  document.getElementById('mesh-filter-node').addEventListener('change', e => {
    _filterNodeId = e.target.value || null;
    _renderChatPanel();
  });
  document.getElementById('mesh-channel-compose-btn').addEventListener('click', () => {
    _openChannelCompose();
  });
}

// ── Open compose ──────────────────────────────────────────────────────────────

export function openCompose(nodeId, nodeName) {
  _composeMode = 'direct';  // set before _setMode to avoid clearing
  _recipients = [{ id: nodeId, name: nodeName }];
  _setMode('direct', true);
  _renderRecipients();
  document.getElementById('mesh-msg-input').value = '';
  _updateCounter();
  document.getElementById('mesh-modal').style.display = 'flex';
  requestAnimationFrame(() => document.getElementById('mesh-msg-input').focus());
  _markRead(nodeId);
}

function _openChannelCompose() {
  _setMode('channel');
  _recipients = _channels.length ? [{ index: _channels[0].index, name: _channels[0].name }] : [];
  _renderRecipients();
  document.getElementById('mesh-msg-input').value = '';
  _updateCounter();
  document.getElementById('mesh-modal').style.display = 'flex';
  requestAnimationFrame(() => document.getElementById('mesh-msg-input').focus());
}

function _closeCompose() {
  document.getElementById('mesh-modal').style.display = 'none';
  _hideDropdown();
}

// ── Mode toggle ───────────────────────────────────────────────────────────────

function _setMode(mode, keepRecipients = false) {
  const modeChanged = mode !== _composeMode;
  _composeMode = mode;
  document.getElementById('mesh-mode-direct').classList.toggle('active', mode === 'direct');
  document.getElementById('mesh-mode-channel').classList.toggle('active', mode === 'channel');
  document.getElementById('mesh-add-btn').textContent = mode === 'direct' ? '+ Node' : '+ Channel';
  const ackRow = document.getElementById('mesh-ack-row');
  if (ackRow) ackRow.style.display = mode === 'direct' ? '' : 'none';
  // Clear recipients when switching modes so stale chips don't carry over
  if (modeChanged && !keepRecipients) _recipients = [];
  // Auto-seed first channel when entering channel mode with no recipients
  if (mode === 'channel' && _recipients.length === 0 && _channels.length) {
    _recipients = [{ index: _channels[0].index, name: _channels[0].name }];
  }
  _renderRecipients();
}

// ── Recipients ────────────────────────────────────────────────────────────────

function _renderRecipients() {
  const box = document.getElementById('mesh-recipients');
  box.innerHTML = '';
  for (const r of _recipients) {
    const chip = document.createElement('span');
    chip.className = 'mesh-chip';
    chip.textContent = r.name;
    const x = document.createElement('button');
    x.className = 'mesh-chip-remove';
    x.textContent = '✕';
    const captured = r;
    x.addEventListener('click', () => {
      _recipients = _recipients.filter(rx =>
        _composeMode === 'direct' ? rx.id !== captured.id : rx.index !== captured.index
      );
      _renderRecipients();
    });
    chip.appendChild(x);
    box.appendChild(chip);
  }
}

// ── Add dropdown ──────────────────────────────────────────────────────────────

function _toggleDropdown() {
  _dropdownOpen ? _hideDropdown() : _showDropdown();
}

function _showDropdown() {
  const dd = document.getElementById('mesh-add-dropdown');
  dd.innerHTML = '';
  let items = [];
  if (_composeMode === 'direct') {
    const selIds = new Set(_recipients.map(r => r.id));
    items = _nodeCache
      .filter(n => !selIds.has(n.source_id))
      .map(n => ({ label: n.source_name || n.source_id, id: n.source_id, name: n.source_name || n.source_id }));
  } else {
    const selIdx = new Set(_recipients.map(r => r.index));
    items = _channels
      .filter(c => !selIdx.has(c.index))
      .map(c => ({ label: c.name, index: c.index, name: c.name }));
  }
  if (!items.length) {
    dd.innerHTML = '<div class="mesh-dd-empty">No more options</div>';
  } else {
    items.forEach(item => {
      const row = document.createElement('div');
      row.className = 'mesh-dd-item';
      row.textContent = item.label;
      row.addEventListener('click', () => {
        _recipients.push(_composeMode === 'direct'
          ? { id: item.id, name: item.name }
          : { index: item.index, name: item.name });
        _renderRecipients();
        _hideDropdown();
      });
      dd.appendChild(row);
    });
  }
  dd.style.display = 'block';
  _dropdownOpen = true;
}

function _hideDropdown() {
  const dd = document.getElementById('mesh-add-dropdown');
  if (dd) dd.style.display = 'none';
  _dropdownOpen = false;
}

// ── Character counter ─────────────────────────────────────────────────────────

function _updateCounter() {
  const input = document.getElementById('mesh-msg-input');
  const bytes = new TextEncoder().encode(input.value).length;
  const el    = document.getElementById('mesh-char-count');
  el.textContent = `${bytes} / ${MAX_BYTES}`;
  el.className = bytes > MAX_BYTES ? 'mesh-char over'
               : bytes > MAX_BYTES * 0.85 ? 'mesh-char warn'
               : bytes > MAX_BYTES * 0.65 ? 'mesh-char caution'
               : 'mesh-char ok';
  document.getElementById('mesh-send-btn').disabled = bytes === 0 || bytes > MAX_BYTES;
}

// ── Send ──────────────────────────────────────────────────────────────────────

async function _handleSend() {
  const msg     = document.getElementById('mesh-msg-input').value.trim();
  const wantAck = document.getElementById('mesh-want-ack')?.checked ?? true;
  if (!msg || !_recipients.length) return;

  const btn = document.getElementById('mesh-send-btn');
  btn.disabled = true;
  btn.textContent = 'Sending…';

  const sends = _recipients.map(r => {
    const body = _composeMode === 'direct'
      ? { to_id: r.id, to_name: r.name, channel_index: 0, message: msg, want_ack: wantAck }
      : { to_id: '^all', channel_index: r.index, channel_name: r.name, message: msg, want_ack: false };
    return fetch(`${API}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  });

  try {
    await Promise.all(sends);
    _closeCompose();
  } catch (e) {
    btn.textContent = 'Error — retry';
    btn.disabled = false;
    return;
  }
  btn.textContent = 'Send →';
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function _loadChannels() {
  try {
    const r = await fetch(`${API}/channels`);
    _channels = await r.json();
  } catch (e) {
    _channels = [{ index: 0, name: 'Primary', role: 1 }];
  }
}

async function _loadHistory() {
  try {
    const r = await fetch(`${API}/messages?limit=200`);
    _allMessages = await r.json();
    if (_allMessages.length) {
      _lastMsgId = _allMessages[_allMessages.length - 1].id;
      try { localStorage.setItem(_LAST_ID_KEY, _lastMsgId); } catch {}
    }
    // Build name map from all historical messages
    for (const m of _allMessages) _indexMsgNames(m);
    _renderChatPanel();
    _refreshFilterCounts();
  } catch (e) {}
}

// ── SSE stream ────────────────────────────────────────────────────────────────

function _startSSE() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(_pollMessages, 3000);
}

async function _pollMessages() {
  try {
    const r = await fetch(`${API}/messages?after_id=${_lastMsgId}`);
    if (!r.ok) return;
    const msgs = await r.json();
    for (const msg of msgs) {
      _allMessages.push(msg);
      _lastMsgId = msg.id;
      try { localStorage.setItem(_LAST_ID_KEY, _lastMsgId); } catch {}
      _indexMsgNames(msg);
      _onNewMessage(msg);
    }
    if (msgs.length) _refreshFilterCounts();
  } catch (_) {}
}

function _onNewMessage(msg) {
  if (msg.direction === 'rx') {
    const isChannel = msg.to_id === '^all';
    try { playEvent(isChannel ? 'channel' : 'direct'); } catch (_) {}
    try { _showMapToast(msg); } catch (e) { console.warn('[mesh] toast error:', e); }
    _incrementUnread(msg.from_id);
  }
  _appendToChatPanel(msg);
  _refreshFilterCounts();
}

// ── Map toast ─────────────────────────────────────────────────────────────────

function _showMapToast(msg) {
  const container = document.getElementById('mesh-toast-container');
  if (!container) return;
  const from    = _esc(msg.from_name || msg.from_id || '?');
  const channel = msg.to_id === '^all'
    ? ` → ${msg.channel_name ? '#' + _esc(msg.channel_name) : 'Channel'}`
    : '';
  const toast = document.createElement('div');
  toast.className = 'mesh-toast';
  toast.title = 'Click to reply';
  toast.innerHTML = `
    <div class="toast-header-row">
      <span class="toast-from">💬 <strong>${from}</strong><span class="toast-channel">${channel}</span></span>
      <span class="toast-meta">
        <span class="toast-ts">${_fmtMsgTs(msg.timestamp)}</span>
        <button class="toast-dismiss" title="Dismiss">✕</button>
      </span>
    </div>
    <div class="toast-body">${_esc(msg.message)}</div>
  `;
  toast.querySelector('.toast-dismiss').addEventListener('click', e => {
    e.stopPropagation();
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  });
  toast.addEventListener('click', () => openCompose(msg.from_id, msg.from_name || msg.from_id));
  container.prepend(toast);   // prepend = first DOM child = bottom in column-reverse
  requestAnimationFrame(() => {
    requestAnimationFrame(() => toast.classList.add('show'));
  });
  // No auto-dismiss — stays until ✕ is clicked
}

// ── Unread badges ─────────────────────────────────────────────────────────────

function _incrementUnread(nodeId) {
  _unread[nodeId] = (_unread[nodeId] || 0) + 1;
  _updateBadge(nodeId);
}

function _markRead(nodeId) {
  _unread[nodeId] = 0;
  _updateBadge(nodeId);
}

function _updateBadge(nodeId) {
  const item = document.querySelector(`.node-item[data-id="${nodeId}"]`);
  if (!item) return;
  let badge = item.querySelector('.node-chat-badge');
  const count = _unread[nodeId] || 0;
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'node-chat-badge';
    const btn = item.querySelector('.node-chat-btn');
    if (btn) btn.style.position = 'relative';
    if (btn) btn.appendChild(badge);
    else item.appendChild(badge);
  }
  badge.textContent = count > 9 ? '9+' : count || '';
  badge.style.display = count > 0 ? 'flex' : 'none';
}

// ── Chat panel ────────────────────────────────────────────────────────────────

function _fmtMsgTs(timestamp) {
  if (!timestamp) return '';
  const d = new Date(timestamp);
  if (isNaN(d)) return `<span class="msg-ts-time">${timestamp.slice(11, 16)}</span>`;
  const month = d.getMonth() + 1;
  const day   = d.getDate();
  let   h     = d.getHours();
  const min   = String(d.getMinutes()).padStart(2, '0');
  const ampm  = h >= 12 ? 'pm' : 'am';
  h = h % 12 || 12;
  return `<span class="msg-ts-date">${month}/${day}</span>&nbsp;&nbsp;<span class="msg-ts-time">${h}:${min}${ampm}</span>`;
}

function _renderChatPanel() {
  const list = document.getElementById('mesh-chat-list');
  if (!list) return;
  list.innerHTML = '';
  const msgs = _filterNodeId
    ? _allMessages.filter(m => m.from_id === _filterNodeId || m.to_id === _filterNodeId)
    : _allMessages;
  // Newest first — iterate in reverse and append so newest lands at top
  for (let i = msgs.length - 1; i >= 0; i--) list.appendChild(_makeMsgEl(msgs[i]));
}

function _appendToChatPanel(msg) {
  const list = document.getElementById('mesh-chat-list');
  if (!list) return;
  if (_filterNodeId && msg.from_id !== _filterNodeId && msg.to_id !== _filterNodeId) return;
  list.prepend(_makeMsgEl(msg));  // newest at top
}

function _makeMsgEl(msg) {
  const isTx   = msg.direction === 'tx';
  const from   = _esc(_resolveName(msg.from_id, msg.from_name));
  const toDisp = msg.to_id === '^all'
    ? (msg.channel_name ? `#${msg.channel_name}` : `CH${msg.channel_index ?? 0}`)
    : _esc(_resolveName(msg.to_id, msg.to_name));
  const ts = _fmtMsgTs(msg.timestamp);

  const div = document.createElement('div');
  div.className = `mesh-msg${isTx ? ' tx' : ' rx'}`;
  div.innerHTML = `
    <div class="msg-header">
      <span class="msg-from">${from}</span>
      <span class="msg-arrow">→</span>
      <span class="msg-to">${toDisp}</span>
      <span class="msg-ts">${ts}</span>
    </div>
    <div class="msg-body">${_esc(msg.message)}</div>
  `;
  return div;
}

// ── Util ──────────────────────────────────────────────────────────────────────

function _esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// Resolve a node ID to a human name — prefers name on the message,
// then the accumulated name map (built from all message history + node cache).
function _resolveName(id, nameOnMsg) {
  if (nameOnMsg && nameOnMsg.trim()) return nameOnMsg.trim();
  if (id && _nameMap[id])            return _nameMap[id];
  return id || '?';
}
