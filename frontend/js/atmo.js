// ── ATMO — Atmospheric Intelligence ──────────────────────────────────────────
// Polls /api/atmo every 20 minutes when internet is online.
// Renders into the ATMO sidebar panel and the ATMO HUD chip.

const API      = '/jtak/api';
const POLL_MS  = 20 * 60 * 1000;

let _pollTimer = null;
let _last      = null;

const _panel   = () => document.getElementById('atmo-panel');
const _body    = () => document.getElementById('atmo-body');
const _chip    = () => document.getElementById('atmo-hud-chip');

// ── Public API ────────────────────────────────────────────────────────────────

export function startAtmo() {
  _pollNow();
  _pollTimer = setInterval(_pollNow, POLL_MS);
}

export function stopAtmo() {
  clearInterval(_pollTimer);
  _pollTimer = null;
  _last = null;
  _renderOffline();
}

// ── Fetch ─────────────────────────────────────────────────────────────────────

async function _pollNow() {
  try {
    const r = await fetch(`${API}/atmo`);
    if (!r.ok) { _renderError(`HTTP ${r.status}`); return; }
    _last = await r.json();
    _render(_last);
  } catch(e) {
    _renderError(e.message);
  }
}

// ── Render helpers ────────────────────────────────────────────────────────────

function _bar(pct, color = '#2ecc71') {
  const p = Math.round(pct ?? 0);
  return `<div class="atmo-bar-wrap">
    <div class="atmo-bar" style="width:${p}%;background:${color}"></div>
    <span class="atmo-bar-label">${p}%</span>
  </div>`;
}

function _ccIcon(pct) {
  if (pct == null) return '—';
  if (pct < 25)   return '☀';
  if (pct < 50)   return '🌤';
  if (pct < 75)   return '⛅';
  return '☁';
}

function _invBadge(inv) {
  if (!inv) return '<span class="atmo-badge atmo-badge-muted">N/A</span>';
  if (inv.detected)
    return `<span class="atmo-badge atmo-badge-warn">YES +${inv.delta_c}°C</span>`;
  return `<span class="atmo-badge atmo-badge-ok">NONE (${inv.delta_c}°C)</span>`;
}

// ── Main render ───────────────────────────────────────────────────────────────

function _render(d) {
  const panel = _panel();
  const body  = _body();
  const chip  = _chip();
  if (!panel || !body || !chip) return;

  panel.style.display = '';

  const score = d.lightning_score ?? 0;
  const color = d.lightning_color ?? '#2ecc71';
  const risk  = d.lightning_risk  ?? 'LOW';

  // Sidebar panel body
  const ccTotal     = d.cloudcover_pct ?? 0;
  const precip2     = d.precip_pct_2hr ?? 0;
  const precipColor = precip2 > 70 ? '#e74c3c' : precip2 > 40 ? '#e6a817' : '#2ecc71';

  // HUD chip — lightning score + cloud icon + rain chance
  const _dayMode  = document.documentElement.dataset.theme === 'day';
  const rainColor = precip2 > 70 ? '#e74c3c' : precip2 > 40 ? '#e6a817' : (_dayMode ? '#004a8f' : '#7ecfff');
  chip.innerHTML = `
    <span class="atmo-chip-bolt" style="color:${color}">⚡</span>
    <span class="atmo-chip-risk" style="color:${color}">${risk}</span>
    <span class="atmo-chip-sep">|</span>
    <span class="atmo-chip-cc">${_ccIcon(ccTotal)}</span>
    <span class="atmo-chip-cc-val">${ccTotal != null ? Math.round(ccTotal)+'%' : '—'}</span>
    <span class="atmo-chip-sep">|</span>
    <span class="atmo-chip-rain" style="color:${rainColor}">🌧 ${precip2}%</span>
  `;
  chip.style.display = 'flex';

  const wsStr   = d.wind_850hPa_mph != null
    ? `${d.wind_850hPa_cardinal || '?'} @ ${d.wind_850hPa_mph} mph`
    : '—';

  const capeStr = d.cape_jkg != null ? `${d.cape_jkg} J/kg` : '—';
  const liStr   = d.lifted_index != null
    ? `${d.lifted_index > 0 ? '+' : ''}${d.lifted_index}`
    : '—';

  const t2    = d.temp_2m_c    != null ? `${d.temp_2m_c}°C` : '—';
  const t925  = d.temp_925hPa_c != null ? `${d.temp_925hPa_c}°C` : '—';
  const t850  = d.temp_850hPa_c != null ? `${d.temp_850hPa_c}°C` : '—';
  const t700  = d.temp_700hPa_c != null ? `${d.temp_700hPa_c}°C` : '—';

  const hoursStr = d.hours_shown ? d.hours_shown.join(' – ') : '';
  const ageMin   = d.cache_age_s ? Math.round(d.cache_age_s / 60) : 0;
  const freshStr = d.cached ? `cached ${ageMin}m ago` : 'fresh';

  body.innerHTML = `
  <div class="atmo-section">
    <div class="atmo-row atmo-row-big">
      <span class="atmo-icon" style="color:${color}">⚡</span>
      <span class="atmo-key">Lightning Risk</span>
      <span class="atmo-val atmo-val-score" style="color:${color}">${risk} (${score}/100)</span>
    </div>
    <div class="atmo-subrow">
      <span class="atmo-key">CAPE</span><span class="atmo-val">${capeStr}</span>
      <span class="atmo-key atmo-key-pad">Lifted Index</span><span class="atmo-val">${liStr}</span>
    </div>
  </div>

  <div class="atmo-section">
    <div class="atmo-row">
      <span class="atmo-icon">🌧</span>
      <span class="atmo-key">Rain (2 hr)</span>
      <span class="atmo-val">${precip2}%</span>
    </div>
    ${_bar(precip2, precipColor)}
  </div>

  <div class="atmo-section">
    <div class="atmo-row">
      <span class="atmo-icon">☁</span>
      <span class="atmo-key">Cloud Cover</span>
      <span class="atmo-val">${_ccIcon(ccTotal)} ${ccTotal != null ? Math.round(ccTotal)+'%' : '—'}</span>
    </div>
    <div class="atmo-cloud-grid">
      <div class="atmo-cloud-level">
        <span class="atmo-cloud-lbl">LOW</span>
        ${_bar(d.cloudcover_low_pct, '#7ecbff')}
      </div>
      <div class="atmo-cloud-level">
        <span class="atmo-cloud-lbl">MID</span>
        ${_bar(d.cloudcover_mid_pct, '#a0d4f5')}
      </div>
      <div class="atmo-cloud-level">
        <span class="atmo-cloud-lbl">HIGH</span>
        ${_bar(d.cloudcover_high_pct, '#c8e8ff')}
      </div>
    </div>
  </div>

  <div class="atmo-section">
    <div class="atmo-row">
      <span class="atmo-icon">🌡</span>
      <span class="atmo-key">Inversion</span>
      ${_invBadge(d.inversion)}
    </div>
    <div class="atmo-subrow atmo-temp-profile">
      <span class="atmo-key">SFC</span><span class="atmo-val">${t2}</span>
      <span class="atmo-key atmo-key-pad">925hPa</span><span class="atmo-val">${t925}</span>
      <span class="atmo-key atmo-key-pad">850hPa</span><span class="atmo-val">${t850}</span>
      <span class="atmo-key atmo-key-pad">700hPa</span><span class="atmo-val">${t700}</span>
    </div>
  </div>

  <div class="atmo-section">
    <div class="atmo-row">
      <span class="atmo-icon">💨</span>
      <span class="atmo-key">Cloud Steering (850hPa ~5kft)</span>
    </div>
    <div class="atmo-subrow"><span class="atmo-val atmo-val-wind">${wsStr}</span></div>
  </div>

  <div class="atmo-meta">
    <span class="online-globe">&#127760;</span> Open-Meteo GFS &nbsp;|&nbsp;
    ${hoursStr} UTC &nbsp;|&nbsp; ${freshStr}
  </div>`;
}

function _renderError(msg) {
  const body = _body();
  if (body) body.innerHTML = `<div class="rf-row"><span class="rf-label atmo-err">ATMO error: ${msg}</span></div>`;
  const chip = _chip();
  if (chip) { chip.innerHTML = '<span class="atmo-chip-bolt">⚡</span><span>—</span>'; }
}

function _renderOffline() {
  const panel = _panel();
  if (panel) panel.style.display = 'none';
  const chip  = _chip();
  if (chip)  chip.style.display = 'none';
}
