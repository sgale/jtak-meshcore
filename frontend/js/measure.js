// ── Measure Tool — two-point distance / elevation / ETA ──────────────────────
// Click MEASURE → click point A → click point B → results panel appears.
// Elevation via USGS Elevation Point Query Service (no key, CORS-enabled).

let _map    = null;
let _active = false;
let _ptA    = null;
let _ptB    = null;
let _markers = [];
let _line    = null;
let _panel   = null;
let _btn     = null;

const USGS_EPQS = 'https://epqs.nationalmap.gov/v1/json';

export function stopMeasure() { if (_active) _stop(); }
export function isMeasureActive() { return _active; }

export function initMeasure(map) {
  _map = map;
  _btn = document.getElementById('measure-btn');
  _panel = document.getElementById('measure-panel');

  _btn.addEventListener('click', () => _active ? _stop() : _start());

  // ESC cancels
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && _active) _stop();
  });
}

// ── Mode control ──────────────────────────────────────────────────────────────

function _start() {
  _active = true;
  _ptA = _ptB = null;
  _btn.classList.add('active');
  _btn.textContent = '📐 PICK A';
  _map.getContainer().style.cursor = 'crosshair';
  _map.on('click', _onClick);
  _clearDrawing();
  _hidePanel();
}

function _stop() {
  _active = false;
  _btn.classList.remove('active');
  _btn.textContent = '📐 MEASURE';
  _map.getContainer().style.cursor = '';
  _map.off('click', _onClick);
  _clearDrawing();
  _hidePanel();
}

// ── Click handler ─────────────────────────────────────────────────────────────

function _onClick(e) {
  if (!_ptA) {
    _clearDrawing();   // clear previous measurement when picking new A
    _hidePanel();
    _ptA = e.latlng;
    _addMarker(_ptA, 'A');
    _btn.textContent = '📐 PICK B';
  } else if (!_ptB) {
    _ptB = e.latlng;
    _addMarker(_ptB, 'B');
    _drawLine();
    _btn.textContent = '📐 …';
    _map.getContainer().style.cursor = '';
    _map.off('click', _onClick);
    _showResults();
  }
}

// ── Drawing ───────────────────────────────────────────────────────────────────

function _addMarker(latlng, label) {
  const icon = L.divIcon({
    className: '',
    html: `<div class="msr-pin">${label}</div>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
  _markers.push(L.marker(latlng, { icon }).addTo(_map));
}

function _drawLine() {
  _line = L.polyline([_ptA, _ptB], {
    color: '#00e5ff',
    weight: 2,
    dashArray: '6 5',
    opacity: 0.85,
  }).addTo(_map);
}

function _clearDrawing() {
  _markers.forEach(m => m.remove());
  _markers = [];
  if (_line) { _line.remove(); _line = null; }
}

// ── Elevation fetch ───────────────────────────────────────────────────────────

async function _getElev(lat, lon) {
  try {
    const url = `${USGS_EPQS}?x=${lon}&y=${lat}&units=Feet&includeDate=false`;
    const r = await fetch(url);
    if (!r.ok) return null;
    const d = await r.json();
    // Response: { value: "1234.56" } or nested under properties
    const raw = d?.value ?? d?.properties?.value ?? null;
    return raw != null ? parseFloat(raw) : null;
  } catch {
    return null;
  }
}

// ── Results ───────────────────────────────────────────────────────────────────

function _haversineMi(a, b) {
  const R = 3958.8;
  const dLat = (b.lat - a.lat) * Math.PI / 180;
  const dLon = (b.lng - a.lng) * Math.PI / 180;
  const s = Math.sin(dLat/2)**2 +
            Math.cos(a.lat * Math.PI/180) * Math.cos(b.lat * Math.PI/180) *
            Math.sin(dLon/2)**2;
  return R * 2 * Math.asin(Math.sqrt(s));
}

// Tobler's hiking function — returns minutes for given distance + elevation change
// W = 6 * exp(-3.5 * |slope + 0.05|) km/h,  slope = dh_m / dh_dist_m
function _toblerMin(distMi, elevDeltaFt) {
  const distM   = distMi * 1609.34;
  const elevM   = elevDeltaFt * 0.3048;
  const slope   = distM > 0 ? elevM / distM : 0;
  const speedKmh = 6 * Math.exp(-3.5 * Math.abs(slope + 0.05));
  const distKm   = distMi * 1.60934;
  return (distKm / speedKmh) * 60;
}

function _fmtMin(min) {
  if (min < 60) return `${Math.round(min)} min`;
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function _fmtDist(mi) {
  if (mi < 0.189) return `${Math.round(mi * 5280)} ft`;  // <1000 ft → show feet
  return `${mi.toFixed(2)} mi`;
}

async function _showResults() {
  _panel.style.display = 'block';
  _panel.innerHTML = `<div class="msr-loading"><span class="msr-spinner"></span>Fetching elevation…</div>`;

  const distMi = _haversineMi(_ptA, _ptB);

  // Fetch elevations in parallel
  const [elevA, elevB] = await Promise.all([
    _getElev(_ptA.lat, _ptA.lng),
    _getElev(_ptB.lat, _ptB.lng),
  ]);

  const hasElev = elevA != null && elevB != null;
  const deltaFt = hasElev ? Math.round(elevB - elevA) : null;
  const slopePct = (hasElev && distMi > 0)
    ? Math.round((deltaFt * 0.3048) / (distMi * 1609.34) * 100)
    : null;

  // ETAs
  const etaFlat    = _toblerMin(distMi, 0);
  const etaTerrain = hasElev ? _toblerMin(distMi, deltaFt) : null;

  const elevSign = deltaFt == null ? '' : deltaFt > 0 ? '+' : '';
  const deltaStr = deltaFt != null
    ? `<span class="msr-highlight">${elevSign}${deltaFt} ft</span>`
    : '<span class="msr-na">—</span>';

  _panel.innerHTML = `
    <div class="msr-close" id="msr-close">✕</div>
    <div class="msr-title">📐 MEASURE</div>

    <div class="msr-row">
      <span class="msr-key">Distance</span>
      <span class="msr-val">${_fmtDist(distMi)}</span>
    </div>

    ${hasElev ? `
    <div class="msr-row">
      <span class="msr-key">Elevation A</span>
      <span class="msr-val">${Math.round(elevA).toLocaleString()} ft</span>
    </div>
    <div class="msr-row">
      <span class="msr-key">Elevation B</span>
      <span class="msr-val">${Math.round(elevB).toLocaleString()} ft</span>
    </div>
    <div class="msr-row">
      <span class="msr-key">Change</span>
      <span class="msr-val">${deltaStr}</span>
    </div>
    <div class="msr-row">
      <span class="msr-key">Slope</span>
      <span class="msr-val">${slopePct != null ? slopePct + '%' : '—'}</span>
    </div>` : `
    <div class="msr-row msr-na-row">
      <span class="msr-key">Elevation</span>
      <span class="msr-val msr-na">unavailable</span>
    </div>`}

    <div class="msr-divider"></div>

    <div class="msr-row">
      <span class="msr-key">ETA foot (flat)</span>
      <span class="msr-val">${_fmtMin(etaFlat)}</span>
    </div>
    ${etaTerrain != null ? `
    <div class="msr-row">
      <span class="msr-key">ETA foot (terrain)</span>
      <span class="msr-val msr-highlight">${_fmtMin(etaTerrain)}</span>
    </div>` : ''}

    <div class="msr-note">Tobler's hiking function</div>
    <button class="msr-new-btn" id="msr-new">New measurement</button>
  `;

  document.getElementById('msr-close').addEventListener('click', _stop);
  document.getElementById('msr-new').addEventListener('click', () => {
    _clearDrawing();
    _hidePanel();
    _start();
  });

  // Auto-restart — keep drawing visible, immediately wait for next point A
  _ptA = _ptB = null;
  _btn.textContent = '📐 PICK A';
  _map.getContainer().style.cursor = 'crosshair';
  _map.on('click', _onClick);
}

function _hidePanel() {
  if (_panel) _panel.style.display = 'none';
}
