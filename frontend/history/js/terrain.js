// ── Terrain elevation profile + LoS/Fresnel SVG chart ────────────────────────

const API = '/jtak/api';

// signal is optional — callers pass their own AbortController.signal
export async function renderTerrain(contact, container, signal = null) {
  if (!container) return;

  const { hub_lat, hub_lon, hub_alt_m, node_lat, node_lon, node_alt_m } = contact;
  if (!hub_lat || !hub_lon || !node_lat || !node_lon) {
    container.innerHTML = '<div class="terrain-na">No position data for terrain analysis</div>';
    return;
  }

  container.innerHTML = '<div class="terrain-loading">Loading terrain profile…</div>';

  try {
    const params = new URLSearchParams({
      lat1: hub_lat, lon1: hub_lon, alt1: hub_alt_m || 0,
      lat2: node_lat, lon2: node_lon, alt2: node_alt_m || 0,
      n: 24,
    });
    const fetchOpts = signal ? { signal } : {};
    const r = await fetch(`${API}/history/terrain?${params}`, fetchOpts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    _drawSvg(data, container, contact);
    return data;   // caller can use hub_alt_m / node_alt_m for elev calculations
  } catch (e) {
    if (e.name === 'AbortError') return null;
    container.innerHTML = `<div class="terrain-na">Terrain unavailable: ${e.message}</div>`;
    return null;
  }
}

// ── SVG drawing ───────────────────────────────────────────────────────────────

function _drawSvg(data, container, contact) {
  const { profile, dist_mi, los_blocked, max_intrusion_m, hub_alt_m, node_alt_m } = data;
  if (!profile || profile.length < 2) {
    container.innerHTML = '<div class="terrain-na">No profile data</div>';
    return;
  }

  const W = 300, H = 140, PAD = { top: 14, right: 8, bottom: 24, left: 40 };
  const IW = W - PAD.left - PAD.right;
  const IH = H - PAD.top  - PAD.bottom;

  // Value ranges
  const allElev = profile.flatMap(p => [p.elev_m, p.los_elev_m]);
  const fresnelTop = profile.map(p => p.los_elev_m + p.r_fresnel_m);
  allElev.push(...fresnelTop);
  const minE = Math.min(...allElev) - 5;
  const maxE = Math.max(...allElev) + 5;
  const eRange = maxE - minE || 1;

  const sx = d => PAD.left + (d / dist_mi) * IW;
  const sy = e => PAD.top  + IH - ((e - minE) / eRange) * IH;

  // Build polygon points for terrain fill
  const terrainPts = profile.map(p => `${sx(p.dist_mi)},${sy(p.elev_m)}`);
  const baseL = `${sx(0)},${sy(minE)} `;
  const baseR = ` ${sx(dist_mi)},${sy(minE)}`;
  const terrainPoly = baseL + terrainPts.join(' ') + baseR;

  // LoS line
  const losPts = profile.map(p => `${sx(p.dist_mi)},${sy(p.los_elev_m)}`).join(' ');

  // Fresnel zone upper/lower bands
  const fresnelUpper = profile.map(p => `${sx(p.dist_mi)},${sy(p.los_elev_m + p.r_fresnel_m)}`);
  const fresnelLower = profile.map(p => `${sx(p.dist_mi)},${sy(p.los_elev_m - p.r_fresnel_m)}`).reverse();
  const fresnelPoly  = [...fresnelUpper, ...fresnelLower].join(' ');

  // Blocked segments (red overlay)
  const blockedSegs = [];
  for (let i = 0; i < profile.length - 1; i++) {
    const p = profile[i];
    if (p.clearance_m < 0) {
      const x1 = sx(p.dist_mi);
      const x2 = sx(profile[i + 1].dist_mi);
      const yT  = sy(p.elev_m);
      const yB  = sy(minE);
      blockedSegs.push(`<rect x="${x1}" y="${yT}" width="${x2 - x1}" height="${yB - yT}" fill="rgba(255,60,60,0.3)"/>`);
    }
  }

  // Y-axis ticks
  const yTicks = 4;
  const yTickLines = [];
  for (let i = 0; i <= yTicks; i++) {
    const e   = minE + (eRange * i / yTicks);
    const yy  = sy(e);
    const lbl = Math.round(e);
    yTickLines.push(`<line x1="${PAD.left}" y1="${yy}" x2="${W - PAD.right}" y2="${yy}" stroke="rgba(255,255,255,0.07)"/>`);
    yTickLines.push(`<text x="${PAD.left - 3}" y="${yy + 4}" fill="#888" font-size="11" text-anchor="end">${lbl}</text>`);
  }

  // X-axis label
  const midX  = sx(dist_mi / 2);
  const xLbl  = `${dist_mi.toFixed(1)} mi`;

  const status = los_blocked
    ? `<text x="${W / 2}" y="${H - 3}" fill="#ff4444" font-size="11" text-anchor="middle">⚠ LoS BLOCKED — ${max_intrusion_m}m intrusion</text>`
    : `<text x="${W / 2}" y="${H - 3}" fill="#00e676" font-size="11" text-anchor="middle">✓ Line of sight clear</text>`;

  const svg = `
<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="0 0 ${W} ${H}">
  <defs>
    <linearGradient id="tgrd" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="#5d4037"/>
      <stop offset="100%" stop-color="#3e2723"/>
    </linearGradient>
  </defs>
  <!-- Grid -->
  ${yTickLines.join('\n  ')}
  <!-- Terrain fill -->
  <polygon points="${terrainPoly}" fill="url(#tgrd)" stroke="#8d6e63" stroke-width="1"/>
  <!-- Fresnel zone -->
  <polygon points="${fresnelPoly}" fill="rgba(0,200,255,0.08)" stroke="none"/>
  <!-- Blocked sections -->
  ${blockedSegs.join('\n  ')}
  <!-- LoS line -->
  <polyline points="${losPts}" fill="none" stroke="#00bcd4" stroke-width="1.5" stroke-dasharray="5,3"/>
  <!-- Fresnel boundary lines -->
  <polyline points="${fresnelUpper.join(' ')}" fill="none" stroke="rgba(0,200,255,0.35)" stroke-width="0.8" stroke-dasharray="3,3"/>
  <polyline points="${fresnelLower.join(' ')}" fill="none" stroke="rgba(0,200,255,0.35)" stroke-width="0.8" stroke-dasharray="3,3"/>
  <!-- Hub/Node labels -->
  <circle cx="${sx(0)}" cy="${sy(hub_alt_m || profile[0].elev_m)}" r="3" fill="${document.documentElement.dataset.theme === 'day' ? '#004a8f' : '#ffd740'}"/>
  <text x="${PAD.left + 2}" y="${PAD.top + 10}" fill="${document.documentElement.dataset.theme === 'day' ? '#004a8f' : '#ffd740'}" font-size="11" font-weight="bold">HUB</text>
  <circle cx="${sx(dist_mi)}" cy="${sy(node_alt_m || profile[profile.length-1].elev_m)}" r="3" fill="${document.documentElement.dataset.theme === 'day' ? '#004a8f' : '#69f0ae'}"/>
  <text x="${W - PAD.right - 2}" y="${PAD.top + 10}" fill="${document.documentElement.dataset.theme === 'day' ? '#004a8f' : '#69f0ae'}" font-size="11" font-weight="bold" text-anchor="end">${contact.source_name || 'NODE'}</text>
  <!-- Distance label -->
  <text x="${midX}" y="${PAD.top + IH + 14}" fill="#666" font-size="10" text-anchor="middle">${xLbl}</text>
  <!-- Elevation unit -->
  <text x="5" y="${PAD.top + IH / 2}" fill="#555" font-size="9" text-anchor="middle" transform="rotate(-90,5,${PAD.top + IH / 2})">m ASL</text>
  <!-- Status -->
  ${status}
</svg>`;

  container.innerHTML = svg;
}
