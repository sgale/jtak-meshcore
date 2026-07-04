"""
db_pruner.py — Periodic pruning of high-volume DB tables.

Retention is configured in jtak.yaml:
  database:
    retention_days_rf_metrics: 30
    retention_days_positions: 30

Runs 2 min after startup, then every 24 hours. After pruning, checkpoints
the WAL. VACUUM is intentionally skipped — it requires exclusive DB access
and is too disruptive for daily runs. Run manually with services stopped if needed.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from store.db import get_db
from utils.config import get

PRUNE_INTERVAL_SEC = 86400  # 24 hours


async def prune_once():
    db = await get_db()
    now = datetime.now(timezone.utc)

    rf_days  = get("database.retention_days_rf_metrics", 30)
    pos_days = get("database.retention_days_positions",  30)

    rf_cutoff  = (now - timedelta(days=rf_days)).strftime("%Y-%m-%d %H:%M:%S")
    pos_cutoff = (now - timedelta(days=pos_days)).strftime("%Y-%m-%d %H:%M:%S")

    cur = await db.execute(
        "DELETE FROM rf_metrics WHERE timestamp < ?", (rf_cutoff,)
    )
    rf_deleted = cur.rowcount

    cur = await db.execute(
        "DELETE FROM positions WHERE timestamp < ?", (pos_cutoff,)
    )
    pos_deleted = cur.rowcount

    await db.commit()
    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    print(
        f"[db_pruner] rf_metrics: -{rf_deleted:,} rows (>{rf_days}d)  "
        f"positions: -{pos_deleted:,} rows (>{pos_days}d)"
    )


async def run():
    # Delay first run so push_agent/tactical_monitor settle before competing for write lock
    await asyncio.sleep(120)
    while True:
        try:
            await prune_once()
        except Exception as e:
            print(f"[db_pruner] ERROR: {e} — will retry in 10 min")
            await asyncio.sleep(600)
            continue
        await asyncio.sleep(PRUNE_INTERVAL_SEC)
