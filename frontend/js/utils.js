// ── Utilities ──────────────────────────────────────────────────────────────

export function rssiClass(rssi) {
  if (rssi === null || rssi === undefined) return 'rssi-none';
  if (rssi >= -70) return 'rssi-good';
  if (rssi >= -85) return 'rssi-ok';
  return 'rssi-weak';
}

export function rssiLabel(rssi) {
  if (rssi === null || rssi === undefined) return '—';
  return `${rssi} dBm`;
}

export function timeAgo(ts) {
  if (!ts) return '—';
  const diff = (Date.now() - new Date(ts).getTime()) / 1000;
  if (diff < 60)   return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff/60)}m ago`;
  return `${Math.round(diff/3600)}h ago`;
}

export function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
}

export function isStale(ts, thresholdSec = 300) {
  if (!ts) return true;
  return (Date.now() - new Date(ts).getTime()) / 1000 > thresholdSec;
}

export function fmtDist(d) {
  if (d === null || d === undefined) return '—';
  return `${parseFloat(d).toFixed(2)} mi`;
}

export function fmtBearing(b) {
  if (b === null || b === undefined) return '—';
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  return `${Math.round(b)}° ${dirs[Math.round(b/45)%8]}`;
}

export function attachRowTooltip(el, text) {
  if (!text) return;
  let tip = null;
  el.addEventListener('mouseenter', () => {
    tip = document.createElement('div');
    tip.className = 'jtak-row-tip';
    tip.textContent = text;
    document.body.appendChild(tip);
    const r = el.getBoundingClientRect();
    tip.style.left = Math.max(4, r.left) + 'px';
    tip.style.top  = (r.top - tip.offsetHeight - 6) + 'px';
  });
  el.addEventListener('mouseleave', () => { tip?.remove(); tip = null; });
}
