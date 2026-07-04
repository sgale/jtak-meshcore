// ── jTAK Sound Engine — Web Audio API, no files, fully offline ───────────────

const PRESETS = {
  // Warm two-note ascending chime — direct messages
  chime: [
    { freq: 880,  type: 'sine',   attack: 0.005, sustain: 0.08, release: 0.35, gain: 0.55, delay: 0.00 },
    { freq: 1108, type: 'sine',   attack: 0.005, sustain: 0.08, release: 0.40, gain: 0.50, delay: 0.13 },
  ],
  // Single clean ping — channel messages
  ping: [
    { freq: 1480, type: 'sine',   attack: 0.003, sustain: 0.04, release: 0.30, gain: 0.45, delay: 0.00 },
  ],
  // Three-note descending drop — waypoints
  drop: [
    { freq: 784,  type: 'sine',   attack: 0.005, sustain: 0.06, release: 0.20, gain: 0.50, delay: 0.00 },
    { freq: 659,  type: 'sine',   attack: 0.005, sustain: 0.06, release: 0.22, gain: 0.48, delay: 0.11 },
    { freq: 523,  type: 'sine',   attack: 0.005, sustain: 0.08, release: 0.30, gain: 0.55, delay: 0.22 },
  ],
  // Crisp square beep — utility
  beep: [
    { freq: 660,  type: 'square', attack: 0.003, sustain: 0.06, release: 0.12, gain: 0.18, delay: 0.00 },
  ],
  // Double-tap — alert
  alert: [
    { freq: 440,  type: 'sine',   attack: 0.004, sustain: 0.07, release: 0.15, gain: 0.50, delay: 0.00 },
    { freq: 440,  type: 'sine',   attack: 0.004, sustain: 0.07, release: 0.15, gain: 0.50, delay: 0.22 },
  ],
  none: [],
};

let _ctx       = null;
let _volume    = 0.6;
let _enabled   = true;
let _soundMap  = { direct: 'chime', channel: 'ping', waypoint: 'drop' };
let _listening = false;

function _getCtx() {
  if (!_ctx) {
    try { _ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (_) {}
  }
  return _ctx;
}

function _ensureListeners() {
  if (_listening) return;
  _listening = true;
  const resume = () => { if (_ctx && _ctx.state === 'suspended') _ctx.resume(); };
  document.addEventListener('click',    resume);
  document.addEventListener('keydown',  resume);
  document.addEventListener('touchend', resume);
}

// ── Init ──────────────────────────────────────────────────────────────────────

export function initSounds(cfg = {}) {
  _enabled  = cfg.enabled !== false;
  _volume   = Math.max(0, Math.min(1, cfg.volume ?? 0.6));
  _soundMap = {
    direct:  cfg.direct_message  || 'chime',
    channel: cfg.channel_message || 'ping',
    waypoint: cfg.waypoint       || 'drop',
  };
  _getCtx();          // create context now
  _ensureListeners(); // resume on any user gesture
}

// ── Public ────────────────────────────────────────────────────────────────────

export function playEvent(event) {
  if (!_enabled) return;
  _play(_soundMap[event] || 'none');
}

export function playPreset(name) { _play(name); }
export const PRESET_NAMES = Object.keys(PRESETS);

/** Toggle mute on/off. Returns new enabled state. */
export function toggleMute() {
  _enabled = !_enabled;
  return _enabled;
}

export function isSoundEnabled() { return _enabled; }

// ── Core synth ────────────────────────────────────────────────────────────────

function _play(presetName) {
  const ctx = _getCtx();
  console.log('[sounds] _play', presetName, 'ctx=', ctx?.state, 'enabled=', _enabled, 'volume=', _volume);
  if (!ctx) return;
  const layers = PRESETS[presetName];
  if (!layers || !layers.length) return;

  const doPlay = () => {
    console.log('[sounds] doPlay', presetName, 'state=', ctx.state);
    const master = ctx.createGain();
    master.gain.value = _volume;
    master.connect(ctx.destination);
    const now = ctx.currentTime;
    for (const l of layers) {
      const osc  = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = l.type;
      osc.frequency.value = l.freq;
      osc.connect(gain);
      gain.connect(master);
      const t0 = now + l.delay;
      gain.gain.setValueAtTime(0, t0);
      gain.gain.linearRampToValueAtTime(l.gain, t0 + l.attack);
      gain.gain.setValueAtTime(l.gain, t0 + l.attack + l.sustain);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + l.attack + l.sustain + l.release);
      osc.start(t0);
      osc.stop(t0 + l.attack + l.sustain + l.release + 0.05);
    }
  };

  if (ctx.state === 'suspended') {
    ctx.resume().then(doPlay).catch(() => {});
  } else {
    doPlay();
  }
}
