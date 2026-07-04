"""
History — RF session playback and analysis.

GET  /api/history/dates       — list log dates available on this hub
GET  /api/history/contacts    — all RF contacts for a date/time window (from CSV)
GET  /api/history/stats       — summary statistics for a session
GET  /api/history/terrain     — elevation profile + LoS between two points
POST /api/history/chat        — Claude RF analyst (requires anthropic.api_key in jtak.yaml)
"""

import csv, glob, math, os, time
from typing import Optional

import httpx
from fastapi import APIRouter, Query, HTTPException

from utils.config import get

router = APIRouter()

LOG_DIR           = get("logs.base_path", "/opt/jtak/logs/rf")
TOPO_URL          = "https://api.opentopodata.org/v1/srtm30m"
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL   = "claude-sonnet-4-6"
FREQ_MHZ_DEFAULT  = 902.0

# In-memory CSV cache: date_str → (rows, loaded_at)
_csv_cache: dict[str, tuple[list, float]] = {}
CSV_CACHE_TTL = 60


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _log_files() -> list[str]:
    return sorted(glob.glob(os.path.join(LOG_DIR, "rf_log_*.csv")), reverse=True)


def _date_from_path(path: str) -> str | None:
    name = os.path.basename(path).removesuffix(".csv")
    parts = name.rsplit("_", 1)
    return parts[1] if len(parts) == 2 and len(parts[1]) == 10 else None


def _load_csv(date_str: str) -> list[dict]:
    now = time.time()
    if date_str in _csv_cache:
        rows, ts = _csv_cache[date_str]
        if now - ts < CSV_CACHE_TTL:
            return rows
    files = glob.glob(os.path.join(LOG_DIR, f"rf_log_*_{date_str}.csv"))
    if not files:
        return []
    rows = []
    try:
        with open(files[0], newline="") as f:
            for row in csv.DictReader(f):
                if row.get("timestamp"):
                    rows.append(dict(row))
    except Exception:
        pass
    _csv_cache[date_str] = (rows, now)
    return rows


def _f(row: dict, key: str) -> float | None:
    v = row.get(key, "")
    try:
        return float(v) if v not in ("", "None", None) else None
    except (ValueError, TypeError):
        return None


def _contact(row: dict) -> dict:
    freq = _f(row, "freq_mhz") or FREQ_MHZ_DEFAULT
    dist = _f(row, "distance_mi")
    path = _f(row, "path_loss_db")
    predicted = None
    excess = None
    if dist and dist > 0 and freq:
        dist_km = dist * 1.60934
        predicted = round(20 * math.log10(dist_km) + 20 * math.log10(freq) + 32.44, 1)
        if path:
            excess = round(path - predicted, 1)
    return {
        "ts":              row.get("timestamp", ""),
        "source_id":       row.get("node_id", ""),
        "source_name":     row.get("node_name", row.get("node_id", "")),
        "hub_id":          row.get("hub_id", ""),
        "hub_name":        row.get("hub_name", ""),
        "packet_type":     row.get("packet_type", ""),
        "direct_or_relay": row.get("direct_or_relay", ""),
        "hop_count":       row.get("hop_count"),
        "rssi":            _f(row, "rssi"),
        "snr":             _f(row, "snr"),
        "distance_mi":     dist,
        "bearing_deg":     _f(row, "bearing_deg"),
        "path_loss_db":    path,
        "predicted_fspl":  predicted,
        "excess_loss_db":  excess,
        "elev_delta_m":    _f(row, "elev_delta_m"),
        "elev_angle_deg":  _f(row, "elev_angle_deg"),
        "freq_mhz":        freq,
        "node_lat":        _f(row, "node_lat"),
        "node_lon":        _f(row, "node_lon"),
        "node_alt_m":      _f(row, "node_alt_m"),
        "hub_lat":         _f(row, "hub_lat"),
        "hub_lon":         _f(row, "hub_lon"),
        "hub_alt_m":       _f(row, "hub_alt_m"),
        "hub_sats":        _f(row, "hub_sats"),
        "hub_hdop":        _f(row, "hub_hdop"),
        "battery_pct":     _f(row, "battery_pct"),
        "channel_util":    _f(row, "channel_util_pct"),
        "cpu_temp_c":      _f(row, "cpu_temp_c"),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/history/dates")
async def history_dates():
    dates = [d for p in _log_files() if (d := _date_from_path(p))]
    return {"dates": dates}


@router.get("/history/contacts")
async def history_contacts(
    date:     str           = Query(...),
    hub_id:   Optional[str] = None,
    start_ts: Optional[str] = None,
    end_ts:   Optional[str] = None,
):
    rows = _load_csv(date)
    out = []
    for row in rows:
        ts = row.get("timestamp", "")
        if start_ts and ts < start_ts:
            continue
        if end_ts and ts > end_ts:
            continue
        if hub_id and row.get("hub_id") != hub_id:
            continue
        out.append(_contact(row))
    return {"contacts": out, "count": len(out), "date": date}


@router.get("/history/stats")
async def history_stats(date: str = Query(...), hub_id: Optional[str] = None):
    rows = _load_csv(date)
    if hub_id:
        rows = [r for r in rows if r.get("hub_id") == hub_id]
    if not rows:
        return {}

    rssi_vals = [v for r in rows if (v := _f(r, "rssi")) is not None]
    snr_vals  = [v for r in rows if (v := _f(r, "snr"))  is not None]
    dist_vals = [v for r in rows if (v := _f(r, "distance_mi")) is not None]
    ts_vals   = [r["timestamp"] for r in rows if r.get("timestamp")]

    nodes: dict[str, dict] = {}
    for r in rows:
        nid = r.get("node_id", "unknown")
        if nid not in nodes:
            nodes[nid] = {"name": r.get("node_name", nid), "count": 0, "rssi": [], "dist": []}
        nodes[nid]["count"] += 1
        rssi = _f(r, "rssi")
        if rssi is not None:
            nodes[nid]["rssi"].append(rssi)
        dist = _f(r, "distance_mi")
        if dist is not None:
            nodes[nid]["dist"].append(dist)

    node_list = [
        {
            "id": nid, "name": v["name"], "count": v["count"],
            "avg_rssi": round(sum(v["rssi"]) / len(v["rssi"]), 1) if v["rssi"] else None,
            "best_rssi": max(v["rssi"]) if v["rssi"] else None,
            "avg_dist": round(sum(v["dist"]) / len(v["dist"]), 2) if v["dist"] else None,
        }
        for nid, v in nodes.items()
    ]
    node_list.sort(key=lambda n: n["avg_rssi"] or -999, reverse=True)

    def _stat(vals):
        if not vals:
            return {"min": None, "max": None, "avg": None}
        return {
            "min": round(min(vals), 1),
            "max": round(max(vals), 1),
            "avg": round(sum(vals) / len(vals), 1),
        }

    return {
        "date":       date,
        "total":      len(rows),
        "time_range": {"start": min(ts_vals) if ts_vals else None,
                       "end":   max(ts_vals) if ts_vals else None},
        "rssi":       _stat(rssi_vals),
        "snr":        _stat(snr_vals),
        "distance":   _stat(dist_vals),
        "nodes":      node_list,
    }


@router.get("/history/terrain")
async def history_terrain(
    lat1: float, lon1: float, alt1: float = 0,
    lat2: float = 0, lon2: float = 0, alt2: float = 0,
    n: int = 24,
):
    """Elevation profile + line-of-sight analysis between two points."""
    n = max(8, min(n, 40))
    points = [
        (i / (n - 1), lat1 + (i / (n - 1)) * (lat2 - lat1),
                       lon1 + (i / (n - 1)) * (lon2 - lon1))
        for i in range(n)
    ]
    locs = "|".join(f"{lat},{lon}" for _, lat, lon in points)

    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = await c.get(TOPO_URL, params={"locations": locs})
        elevs = [res.get("elevation") or 0 for res in r.json().get("results", [])]
    except Exception:
        elevs = [0] * n

    # Distance calculation
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    dist_mi = math.sqrt(dlat ** 2 + (dlon * math.cos(math.radians((lat1 + lat2) / 2))) ** 2) * 69.0

    # LoS line: interpolate elevation between hub_alt and node_alt at each point
    h_alt = alt1 if alt1 > 0 else (elevs[0] if elevs else 0)
    n_alt = alt2 if alt2 > 0 else (elevs[-1] if elevs else 0)

    freq = FREQ_MHZ_DEFAULT
    lam  = 299_792_458 / (freq * 1e6)     # wavelength in meters
    dist_m = dist_mi * 1609.34

    profile = []
    blocked = False
    max_intrusion = 0.0

    for i, (frac, lat, lon) in enumerate(points):
        elev = elevs[i] if i < len(elevs) else 0
        los_elev = h_alt + frac * (n_alt - h_alt)

        # First Fresnel zone radius at this point
        x = frac * dist_m
        if 0 < x < dist_m:
            r_f1 = math.sqrt(lam * x * (dist_m - x) / dist_m)
        else:
            r_f1 = 0.0

        clearance = los_elev - elev        # positive = terrain below LoS
        fresnel_margin = clearance - 0.6 * r_f1   # < 0 means Fresnel blocked

        if clearance < 0:
            blocked = True
        if fresnel_margin < max_intrusion:
            max_intrusion = fresnel_margin

        profile.append({
            "frac":           round(frac, 4),
            "dist_mi":        round(frac * dist_mi, 3),
            "lat":            lat, "lon": lon,
            "elev_m":         round(elev, 1),
            "los_elev_m":     round(los_elev, 1),
            "r_fresnel_m":    round(r_f1, 1),
            "clearance_m":    round(clearance, 1),
            "fresnel_ok":     fresnel_margin >= 0,
        })

    return {
        "profile":        profile,
        "dist_mi":        round(dist_mi, 3),
        "los_blocked":    blocked,
        "max_intrusion_m": round(abs(max_intrusion), 1) if max_intrusion < 0 else 0,
        "hub_alt_m":      h_alt,
        "node_alt_m":     n_alt,
    }


@router.post("/history/chat")
async def history_chat(body: dict):
    """Claude RF analyst chat with dataset context."""
    api_key = get("anthropic.api_key", "")
    if not api_key:
        raise HTTPException(503, "Set anthropic.api_key in /opt/jtak/config/jtak.yaml to enable the RF Analyst")

    messages = body.get("messages", [])
    ctx      = body.get("context", {})

    # Build context paragraph injected into system prompt
    ctx_lines = []
    if ctx.get("date"):
        ctx_lines.append(f"Session date: {ctx['date']}")
    if ctx.get("hub_name"):
        ctx_lines.append(f"Hub: {ctx['hub_name']}")
    if ctx.get("total"):
        ctx_lines.append(f"Total RF contacts in session: {ctx['total']}")
    if ctx.get("time_range"):
        tr = ctx["time_range"]
        ctx_lines.append(f"Time range: {tr.get('start','?')} → {tr.get('end','?')}")
    if ctx.get("rssi"):
        r = ctx["rssi"]
        ctx_lines.append(f"RSSI — min: {r.get('min')} dBm, max: {r.get('max')} dBm, avg: {r.get('avg')} dBm")
    if ctx.get("snr"):
        s = ctx["snr"]
        ctx_lines.append(f"SNR  — min: {s.get('min')} dB, max: {s.get('max')} dB, avg: {s.get('avg')} dB")
    if ctx.get("distance"):
        d = ctx["distance"]
        ctx_lines.append(f"Distance — min: {d.get('min')} mi, max: {d.get('max')} mi, avg: {d.get('avg')} mi")
    if ctx.get("nodes"):
        node_strs = [f"{n['name']} ({n['count']} pkts, avg {n.get('avg_rssi')} dBm)" for n in ctx["nodes"]]
        ctx_lines.append("Nodes seen: " + ", ".join(node_strs))
    if ctx.get("mode"):
        ctx_lines.append(f"Current view mode: {ctx['mode']}")
    if ctx.get("current_time"):
        ctx_lines.append(f"Playback position: {ctx['current_time']}")
    if ctx.get("selected"):
        sc = ctx["selected"]
        ctx_lines.append(
            f"\nUser has selected contact: {sc.get('source_name')} at {sc.get('ts')}\n"
            f"  RSSI={sc.get('rssi')} dBm  SNR={sc.get('snr')} dB  "
            f"Distance={sc.get('distance_mi')} mi  Bearing={sc.get('bearing_deg')}°\n"
            f"  Path loss={sc.get('path_loss_db')} dB  Predicted FSPL={sc.get('predicted_fspl')} dB  "
            f"Excess={sc.get('excess_loss_db')} dB\n"
            f"  Link type={sc.get('direct_or_relay')}  Hops={sc.get('hop_count')}\n"
            f"  Elevation delta={sc.get('elev_delta_m')} m  Elevation angle={sc.get('elev_angle_deg')}°"
        )
    if ctx.get("terrain"):
        t = ctx["terrain"]
        if t.get("los_blocked"):
            ctx_lines.append(f"  ⚠ Terrain blocks line of sight! Max intrusion: {t.get('max_intrusion_m')} m into path")
        else:
            ctx_lines.append(f"  Line of sight: clear (all Fresnel zones OK)")

    system = (
        "You are an expert RF analyst and educator embedded in jTAK — a tactical LoRa/Meshtastic "
        "mesh radio analysis platform used for field operations, SAR, and antenna testing.\n\n"
        "Your role:\n"
        "- Teach and mentor the user about RF propagation, LoRa link budgets, Fresnel zones, "
        "terrain effects, and antenna performance in practical, field-relevant terms\n"
        "- Make specific observations about the loaded dataset — patterns, anomalies, unusually "
        "good/bad links, what the data suggests about terrain and antenna performance\n"
        "- Help the user understand what the numbers mean (RSSI, SNR, path loss, excess loss)\n"
        "- Compare antenna configurations and predict performance differences\n"
        "- Flag concerns: near-threshold links, high excess path loss suggesting obstruction, "
        "multipath signatures (good RSSI but poor SNR), etc.\n"
        "- Reference specific data values from the session when making observations\n"
        "- Be concise — 2-4 sentences for simple questions, up to a short paragraph for complex ones\n\n"
        "Key reference values (915 MHz LoRa / Meshtastic):\n"
        "- Free-space path loss at 1 mi (902 MHz): ~96 dB\n"
        "- Typical LoRa sensitivity: −130 dBm\n"
        "- Typical TX power: +20–27 dBm → link budget ~150 dB\n"
        "- RSSI thresholds: ≥−65 excellent, −65 to −75 good, −75 to −85 marginal, <−85 poor\n"
        "- Excess path loss >10 dB suggests terrain/obstruction; >20 dB is severe\n"
        "- SNR < −5 dB means signal below noise floor (LoRa can still decode down to ~−20 dB)\n\n"
        + ("Current dataset context:\n" + "\n".join(ctx_lines) if ctx_lines else "No session loaded yet.")
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                ANTHROPIC_URL,
                json={"model": ANTHROPIC_MODEL, "max_tokens": 600,
                      "system": system, "messages": messages},
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
            )
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text[:300])
        return {"reply": r.json()["content"][0]["text"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))
