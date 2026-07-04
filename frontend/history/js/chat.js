// ── RF Analyst Chat — modal popup with image paste support ────────────────────

const API = '/jtak/api';

let _apiHistory  = [];   // [{role, content}] sent to Anthropic
let _ctx         = {};   // session context merged with each request
let _stagedImage = null; // {dataUrl, mediaType, data} — waiting to send
let _typingSeq   = 0;

function _chatSave() {
  // Only save text-based turns (skip image data — too large)
  const saveable = _apiHistory.map(m => ({
    role:    m.role,
    content: typeof m.content === 'string' ? m.content
      : (Array.isArray(m.content)
          ? m.content.filter(b => b.type === 'text').map(b => b.text).join(' ') || '[image]'
          : String(m.content)),
  }));
  try { sessionStorage.setItem('jtak_chat_history', JSON.stringify(saveable)); } catch {}
}

function _chatRestore() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('jtak_chat_history') || 'null');
    if (!Array.isArray(saved) || saved.length === 0) return;
    _apiHistory = saved;
    // Re-render saved turns (skip the greeting, it was already added)
    const box = document.getElementById('chat-messages');
    if (box) box.innerHTML = '';  // clear greeting
    for (const m of saved) {
      if (typeof m.content === 'string') _addBubble(m.role, m.content);
    }
  } catch {}
}

// ── Public API ────────────────────────────────────────────────────────────────

export function initChat() {
  _bindModal();
  _greet();        // adds greeting bubble
  _chatRestore();  // if prior session exists, replaces messages with saved turns
}

export function updateChatContext(ctx) {
  _ctx = { ..._ctx, ...ctx };
}

// ── Modal open / close ────────────────────────────────────────────────────────

function _bindModal() {
  document.getElementById('btn-chat-bubble')
    .addEventListener('click', _openModal);
  document.getElementById('chat-modal-close')
    .addEventListener('click', _closeModal);
  // Click backdrop to close
  document.getElementById('chat-modal')
    .addEventListener('click', e => { if (e.target.id === 'chat-modal') _closeModal(); });
  // Escape key
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') _closeModal();
  });

  const input = document.getElementById('chat-input');
  document.getElementById('chat-send').addEventListener('click', _send);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _send(); }
  });
  input.addEventListener('paste', _handlePaste);
}

function _openModal() {
  document.getElementById('chat-modal').classList.remove('hidden');
  document.getElementById('chat-input').focus();
}

function _closeModal() {
  document.getElementById('chat-modal').classList.add('hidden');
}

// ── Greeting ──────────────────────────────────────────────────────────────────

function _greet() {
  _addBubble('assistant',
    "I'm your **RF Analyst**. Load a session to get started, then ask me about signal strength, " +
    "terrain effects, antenna performance, or anything you see on the map.\n\n" +
    "You can also paste a screenshot (`Ctrl+V`) and I'll analyse what I see.");
}

// ── Image paste ───────────────────────────────────────────────────────────────

function _handlePaste(e) {
  const items = e.clipboardData?.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault();
      const blob = item.getAsFile();
      // Normalize to PNG via canvas — Anthropic only accepts jpeg/png/gif/webp
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width  = img.naturalWidth;
        canvas.height = img.naturalHeight;
        canvas.getContext('2d').drawImage(img, 0, 0);
        URL.revokeObjectURL(url);
        const dataUrl   = canvas.toDataURL('image/png');
        const mediaType = 'image/png';
        const data      = dataUrl.split(',')[1];
        _stageImage({ dataUrl, mediaType, data });
      };
      img.src = url;
      break;
    }
  }
}

function _stageImage({ dataUrl, mediaType, data }) {
  _stagedImage = { dataUrl, mediaType, data };
  const preview = document.getElementById('chat-image-preview');
  preview.innerHTML = '';
  preview.classList.remove('hidden');

  const img = document.createElement('img');
  img.src       = dataUrl;
  img.className = 'staged-img';
  preview.appendChild(img);

  const clearBtn = document.createElement('button');
  clearBtn.className   = 'clear-img-btn';
  clearBtn.title       = 'Remove image';
  clearBtn.textContent = '✕';
  clearBtn.addEventListener('click', _clearStagedImage);
  preview.appendChild(clearBtn);
}

function _clearStagedImage() {
  _stagedImage = null;
  const preview = document.getElementById('chat-image-preview');
  preview.innerHTML = '';
  preview.classList.add('hidden');
}

// ── Send ──────────────────────────────────────────────────────────────────────

async function _send() {
  const input = document.getElementById('chat-input');
  const text  = input.value.trim();
  if (!text && !_stagedImage) return;
  input.value = '';

  // Build API content block (text + optional image)
  let apiContent;
  if (_stagedImage) {
    const parts = [];
    if (text) parts.push({ type: 'text', text });
    parts.push({
      type: 'image',
      source: { type: 'base64', media_type: _stagedImage.mediaType, data: _stagedImage.data },
    });
    apiContent = parts;
    _addBubble('user', text || null, _stagedImage.dataUrl);
    _clearStagedImage();
  } else {
    apiContent = text;
    _addBubble('user', text);
  }

  _apiHistory.push({ role: 'user', content: apiContent });

  const typingId = _addTyping();
  _setStatus('thinking…');

  try {
    const r = await fetch(`${API}/history/chat`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ messages: _apiHistory, context: _ctx }),
    });
    _removeTyping(typingId);
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      _addBubble('error', err.detail || 'Request failed');
      _setStatus('error');
      _apiHistory.pop();
      return;
    }
    const { reply } = await r.json();
    _addBubble('assistant', reply);
    _apiHistory.push({ role: 'assistant', content: reply });
    _chatSave();
    _setStatus('');
  } catch (e) {
    _removeTyping(typingId);
    _addBubble('error', `Network error: ${e.message}`);
    _setStatus('error');
    _apiHistory.pop();
  }
}

// ── Bubble rendering ──────────────────────────────────────────────────────────

function _addBubble(role, text, imageDataUrl = null) {
  const box = document.getElementById('chat-messages');
  if (!box) return;

  const div = document.createElement('div');
  div.className = `chat-msg ${role === 'error' ? 'system' : role}`;

  if (imageDataUrl) {
    const img = document.createElement('img');
    img.src       = imageDataUrl;
    img.className = 'msg-img';
    div.appendChild(img);
  }

  if (text) {
    const p = document.createElement('div');
    p.className = 'msg-text';
    p.innerHTML = _renderMarkdown(text);
    div.appendChild(p);
  }

  // Copy button on assistant messages
  if (role === 'assistant' && text) {
    const btn = document.createElement('button');
    btn.className = 'msg-copy';
    btn.title     = 'Copy';
    btn.innerHTML = '&#9113;';
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(text).then(() => {
        btn.innerHTML = '&#10003;';
        setTimeout(() => { btn.innerHTML = '&#9113;'; }, 1500);
      });
    });
    div.appendChild(btn);
  }

  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function _addTyping() {
  const id  = ++_typingSeq;
  const box = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-msg assistant typing-indicator';
  div.id        = `typing-${id}`;
  div.innerHTML = '<span></span><span></span><span></span>';
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return id;
}

function _removeTyping(id) {
  document.getElementById(`typing-${id}`)?.remove();
}

function _setStatus(msg) {
  const el = document.getElementById('chat-status');
  if (el) el.textContent = msg;
}

// ── Markdown-lite renderer ────────────────────────────────────────────────────

function _renderMarkdown(text) {
  // Escape HTML first
  let h = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Inline: bold, code
  h = h
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');

  // Block: split into lines
  const lines  = h.split('\n');
  const out    = [];
  let inList   = false;

  for (const line of lines) {
    const li = line.match(/^[-•*] (.+)$/);
    const h3 = line.match(/^### (.+)$/);
    const h2 = line.match(/^## (.+)$/);
    if (h2) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<h4>${h2[1]}</h4>`);
    } else if (h3) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<h5>${h3[1]}</h5>`);
    } else if (li) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${li[1]}</li>`);
    } else {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(line === '' ? '<br>' : `<p>${line}</p>`);
    }
  }
  if (inList) out.push('</ul>');
  return out.join('');
}
