"""
routes_rf_bakeoff.py — RF bakeoff API

Endpoints:
  GET  /rf-bakeoff/tag        — get current passive test config tag
  POST /rf-bakeoff/tag        — set config tag  { "tag": "..." }
  GET  /rf-bakeoff/passive    — recent rows from rf_bakeoff.csv as JSON
  GET  /rf-bakeoff/summary    — per-node stats from rf_metrics over last N hours
"""

import csv
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from store.db import get_db

router = APIRouter()

TAG_PATH = Path("/opt/jtak/data/rf_config_tag.txt")
LOG_PATH = Path("/opt/jtak/data/rf_bakeoff.csv")


# ── Tag ───────────────────────────────────────────────────────────────────────

@router.get("/rf-bakeoff/tag")
async def get_tag():
    try:
        tag = TAG_PATH.read_text().strip() or "untagged"
    except FileNotFoundError:
        tag = "untagged"
    return {"tag": tag}


class TagRequest(BaseModel):
    tag: str


@router.post("/rf-bakeoff/tag")
async def set_tag(req: TagRequest):
    tag = req.tag.strip() or "untagged"
    TAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    TAG_PATH.write_text(tag + "\n")
    return {"tag": tag}


# ── Passive log data ──────────────────────────────────────────────────────────

@router.get("/rf-bakeoff/passive")
async def get_passive(limit: int = Query(500, le=5000), tag: Optional[str] = None):
    if not LOG_PATH.exists():
        return []
    rows = []
    try:
        with open(LOG_PATH, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if tag and row.get("config_tag") != tag:
                    continue
                rows.append(row)
    except Exception as e:
        raise HTTPException(500, str(e))
    # Return most recent N rows
    return rows[-limit:]


# ── Live summary from rf_metrics DB ───────────────────────────────────────────

@router.get("/rf-bakeoff/summary")
async def get_summary(hours: float = Query(6.0, ge=0.1, le=72.0)):
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    db = await get_db()
    async with db.execute("""
        SELECT
            COALESCE(source_name, source_id)        AS node,
            COUNT(*)                                 AS packets,
            ROUND(AVG(rssi), 1)                      AS rssi_avg,
            ROUND(MIN(rssi), 1)                      AS rssi_min,
            ROUND(MAX(rssi), 1)                      AS rssi_max,
            ROUND(AVG(snr),  1)                      AS snr_avg,
            ROUND(MIN(snr),  1)                      AS snr_min,
            ROUND(MAX(snr),  1)                      AS snr_max,
            ROUND(AVG(hop_count), 2)                 AS hop_avg,
            ROUND(
                SUM(CASE WHEN hop_count = 0 THEN 1.0 ELSE 0.0 END)
                / COUNT(*) * 100.0, 1
            )                                        AS direct_pct,
            ROUND(AVG(distance_mi), 2)               AS distance_mi_avg,
            MAX(timestamp)                           AS last_seen
        FROM rf_metrics
        WHERE timestamp >= ?
          AND rssi IS NOT NULL
        GROUP BY COALESCE(source_name, source_id)
        ORDER BY packets DESC
    """, (cutoff,)) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
