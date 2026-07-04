/* jTAK IAP — Incident Action Plans frontend */
"use strict";

const API = "/jtak/api";

let _incidents   = [];
let _frequencies = null;
let _trauma      = null;
let _selectedIdx = null;

// ── Boot ─────────────────────────────────────────────────────────────────────
async function init() {
  document.getElementById("btn-refresh").addEventListener("click", loadIncidents);
  document.getElementById("freq-zone-select").addEventListener("change", onZoneChange);
  document.getElementById("inc-search").addEventListener("input", renderIncidentList);

  await Promise.all([loadIncidents(), loadFrequencies(), loadTrauma()]);
}

// ── Data loaders ──────────────────────────────────────────────────────────────
async function loadIncidents() {
  setStatus("Fetching incidents…");
  try {
    const r = await fetch(`${API}/iap/incidents`);
    const d = await r.json();
    _incidents = d.incidents || [];
    renderIncidentList();
    const age = d.cached_age_s || 0;
    const loc = d.hub_lat ? `${d.hub_lat.toFixed(3)}, ${d.hub_lon.toFixed(3)}` : "no GPS";
    const stateTag = d.hub_state ? ` · ${d.hub_state}` : "";
    setStatus(`${_incidents.length} incidents · hub: ${loc}${stateTag} · cache ${age}s ago`);
  } catch (e) {
    setStatus(`Error: ${e.message || e}`);
    console.error(e);
  }
}

async function loadFrequencies() {
  try {
    const r = await fetch(`${API}/iap/frequencies`);
    _frequencies = await r.json();
    populateZoneSelect();
  } catch (e) { console.error("freq load error", e); }
}

async function loadTrauma() {
  try {
    const r = await fetch(`${API}/iap/trauma-centers`);
    _trauma = await r.json();
    renderTraumaTable(null); // render without distance filtering initially
  } catch (e) { console.error("trauma load error", e); }
}

// ── Incident list ─────────────────────────────────────────────────────────────
function renderIncidentList() {
  const el = document.getElementById("incident-list");
  const q = (document.getElementById("inc-search").value || "").trim().toLowerCase();

  const visible = q
    ? _incidents.filter(inc =>
        (inc.name  || "").toLowerCase().includes(q) ||
        (inc.state || "").toLowerCase().includes(q) ||
        (inc.county || "").toLowerCase().includes(q) ||
        (inc.dispatch_center || "").toLowerCase().includes(q))
    : _incidents;

  document.getElementById("inc-count").textContent = visible.length !== _incidents.length
    ? `${visible.length}/${_incidents.length}` : _incidents.length;

  if (!_incidents.length) {
    el.innerHTML = `<div class="inc-no-data">No active incidents found.</div>`;
    return;
  }
  if (!visible.length) {
    el.innerHTML = `<div class="inc-no-data">No matches for "${esc(q)}"</div>`;
    return;
  }

  el.innerHTML = visible.map((inc, i) => {
    const origIdx = _incidents.indexOf(inc);
    const acres = inc.acres_daily != null
      ? `${Number(inc.acres_daily).toLocaleString()} ac`
      : (inc.acres_discovery != null ? `${Number(inc.acres_discovery).toLocaleString()} ac` : "?");
    const pct   = inc.contained_pct != null ? `${inc.contained_pct}%` : "";
    const dist  = inc.dist_mi != null ? `${inc.dist_mi} mi` : "";

    return `<div class="inc-item" data-idx="${origIdx}" onclick="selectIncident(${origIdx})">
      <div class="inc-item-name">${esc(inc.name)}</div>
      <div class="inc-item-sub">
        <span class="inc-item-acres">${acres}</span>
        ${pct ? `<span class="inc-item-pct">${pct} ctnd</span>` : ""}
        ${dist ? `<span class="inc-item-dist">${dist}</span>` : ""}
        <span>${esc(inc.state || "")} ${esc(inc.dispatch_center || "")}</span>
      </div>
    </div>`;
  }).join("");
}

function selectIncident(idx) {
  _selectedIdx = idx;
  // highlight selected
  document.querySelectorAll(".inc-item").forEach(el => {
    el.classList.toggle("selected", parseInt(el.dataset.idx) === idx);
  });
  renderDetail(_incidents[idx]);
}

// ── Detail panel ──────────────────────────────────────────────────────────────
function renderDetail(inc) {
  document.getElementById("iap-placeholder").style.display = "none";
  const d = document.getElementById("iap-inc-detail");
  d.style.display = "block";

  document.getElementById("det-name").textContent = inc.name || "Unknown";

  const discovered = inc.discovery_ts
    ? new Date(inc.discovery_ts).toLocaleDateString("en-US", {month:"short",day:"numeric",year:"numeric"})
    : "unknown date";
  document.getElementById("det-meta").textContent =
    `${inc.county ? inc.county + " Co., " : ""}${inc.state || ""}  ·  Discovered ${discovered}`;

  // Badges
  const badges = [];
  if (inc.type) badges.push(`<span class="inc-badge type-wf">${esc(inc.type)}</span>`);
  if (inc.unified_cmd) badges.push(`<span class="inc-badge unified">UNIFIED CMD</span>`);
  if (inc.multi_juris) badges.push(`<span class="inc-badge multi">MULTI-JURIS</span>`);
  if (inc.complexity)  badges.push(`<span class="inc-badge">${esc(inc.complexity)}</span>`);
  document.getElementById("det-badges").innerHTML = badges.join("");

  // Stats
  const acres = inc.acres_daily != null ? Number(inc.acres_daily).toLocaleString()
              : inc.acres_discovery != null ? Number(inc.acres_discovery).toLocaleString()
              : "—";
  document.getElementById("det-acres").textContent = acres;
  document.getElementById("det-contained").textContent =
    inc.contained_pct != null ? `${inc.contained_pct}%` : "—";
  document.getElementById("det-dist").textContent =
    inc.dist_mi != null ? `${inc.dist_mi} mi` : "—";
  document.getElementById("det-disp").textContent = inc.dispatch_center || "—";

  // Links
  const links = [];
  if (inc.name) {
    const inciwebSearch = `https://www.google.com/search?q=${encodeURIComponent(inc.name + ' wildfire ' + (inc.state || ''))}`;
    links.push(`<a class="inc-link-btn" href="${inciwebSearch}" target="_blank" rel="noopener">&#127760; InciWeb</a>`);
  }
  if (inc.lat && inc.lon) {
    links.push(`<a class="inc-link-btn" href="https://www.google.com/maps/search/?api=1&query=${inc.lat},${inc.lon}" target="_blank" rel="noopener">&#128205; Google Maps</a>`);
  }
  document.getElementById("det-links").innerHTML = links.join("");

  // Re-render trauma with current incident lat/lon
  renderTraumaTable(inc);
}

// ── Frequency zone select ──────────────────────────────────────────────────────
function populateZoneSelect() {
  if (!_frequencies) return;
  const sel = document.getElementById("freq-zone-select");
  const zones = _frequencies.zones || [];
  sel.innerHTML = `<option value="">Select zone…</option>` +
    zones.map((z, i) => `<option value="${i}">${esc(z.zone)} — ${esc(z.name)}</option>`).join("");
}

function onZoneChange() {
  const sel = document.getElementById("freq-zone-select");
  const idx = sel.value;
  if (idx === "" || !_frequencies) {
    document.getElementById("freq-table-wrap").innerHTML =
      `<div class="iap-hint">Select a dispatch zone above to view fire frequencies.</div>`;
    return;
  }
  const zone = _frequencies.zones[parseInt(idx)];
  renderFreqTable(zone);
}

function renderFreqTable(zone) {
  if (!zone) return;
  const rows = zone.channels.map(ch => `
    <tr>
      <td class="freq-name">${esc(ch.name)}</td>
      <td class="freq-mhz">${ch.rx_mhz.toFixed(4)}</td>
      <td class="freq-tone">${ch.tone_hz != null ? ch.tone_hz.toFixed(1) : "—"}</td>
      <td class="freq-use">${esc(ch.use)}</td>
    </tr>`).join("");

  document.getElementById("freq-table-wrap").innerHTML = `
    <table class="freq-table">
      <thead><tr>
        <th>CHANNEL</th><th>FREQ (MHz)</th><th>TONE (Hz)</th><th>USE</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── Trauma centers ─────────────────────────────────────────────────────────────
function renderTraumaTable(inc) {
  if (!_trauma) return;
  const wrap = document.getElementById("trauma-table-wrap");

  let centers = _trauma.centers || [];

  // Annotate with distance from incident if we have coords
  if (inc && inc.lat && inc.lon) {
    centers = centers.map(c => ({
      ...c,
      dist_mi: haversine_mi(inc.lat, inc.lon, c.lat, c.lon),
    })).sort((a, b) => a.dist_mi - b.dist_mi);
  } else {
    centers = [...centers];
  }

  const rows = centers.map(c => {
    const lvlClass = c.level === 1 ? "trauma-lvl-1" : c.level === 2 ? "trauma-lvl-2" : "trauma-lvl-3";
    const dist = c.dist_mi != null ? `<span class="trauma-dist">${c.dist_mi.toFixed(0)} mi</span>` : "";
    const heli = c.helipad ? `<span class="trauma-heli">&#128641;</span>` : "&#8212;";
    return `<tr>
      <td class="${lvlClass}">L${c.level}</td>
      <td>${esc(c.name)}</td>
      <td>${esc(c.city)}</td>
      <td>${dist}</td>
      <td>${heli}</td>
      <td>${c.phone ? `<a href="tel:${c.phone}" style="color:var(--cyan);text-decoration:none">${c.phone}</a>` : "—"}</td>
    </tr>`;
  }).join("");

  wrap.innerHTML = `
    <table class="trauma-table">
      <thead><tr>
        <th>LVL</th><th>FACILITY</th><th>CITY</th><th>DIST</th><th>HELI</th><th>PHONE</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── Utilities ──────────────────────────────────────────────────────────────────
function setStatus(msg) {
  document.getElementById("iap-status").textContent = msg;
}

function esc(s) {
  if (!s) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function haversine_mi(lat1, lon1, lat2, lon2) {
  const R = 3958.8;
  const dlat = (lat2 - lat1) * Math.PI / 180;
  const dlon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dlat/2)**2 +
            Math.cos(lat1 * Math.PI/180) * Math.cos(lat2 * Math.PI/180) * Math.sin(dlon/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// ── Start ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);
