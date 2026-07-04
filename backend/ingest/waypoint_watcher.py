"""
waypoint_watcher.py — polls DB for new/updated waypoints and broadcasts via WebSocket.
Runs as a FastAPI background task alongside csv_watcher.
Waypoints are written directly to DB by tactical_monitor (not via CSV pipeline).
"""
import asyncio
from store.db import get_db
from ingest.csv_watcher import broadcast_queue

_last_id = 0
_last_deleted_poll = ""


async def run():
    global _last_id, _last_deleted_poll
    from datetime import datetime, timezone

    def _now_utc():
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Seed _last_id from current max so we don't replay history on startup
    db = await get_db()
    async with db.execute("SELECT COALESCE(MAX(id), 0) FROM waypoints") as cur:
        row = await cur.fetchone()
        _last_id = row[0] if row else 0
    _last_deleted_poll = _now_utc()

    while True:
        try:
            await asyncio.sleep(2)
            db = await get_db()

            # New waypoints
            async with db.execute(
                """SELECT id, meshtastic_id, name, description, latitude, longitude,
                          icon, source_id, source_name, source_type,
                          created_at, expires_at, deleted_at, hub_id
                   FROM waypoints WHERE id > ? ORDER BY id ASC""",
                (_last_id,)
            ) as cur:
                rows = await cur.fetchall()

            for row in rows:
                _last_id = row[0]
                msg = {
                    "type":          "waypoint",
                    "id":            row[0],
                    "meshtastic_id": row[1],
                    "name":          row[2],
                    "description":   row[3],
                    "lat":           row[4],
                    "lon":           row[5],
                    "icon":          row[6],
                    "source_id":     row[7],
                    "source_name":   row[8],
                    "source_type":   row[9],
                    "created_at":    row[10],
                    "expires_at":    row[11],
                    "deleted_at":    row[12],
                    "hub_id":        row[13],
                }
                try:
                    broadcast_queue.put_nowait(msg)
                except Exception:
                    pass

            # Phone-side deletes: existing rows where deleted_at was just set
            poll_time = _last_deleted_poll
            _last_deleted_poll = _now_utc()
            async with db.execute(
                """SELECT id, deleted_at FROM waypoints
                   WHERE deleted_at IS NOT NULL AND deleted_at >= ?""",
                (poll_time,)
            ) as cur:
                deleted_rows = await cur.fetchall()
            for drow in deleted_rows:
                try:
                    broadcast_queue.put_nowait({"type": "waypoint", "id": drow[0], "deleted_at": drow[1]})
                except Exception:
                    pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[waypoint_watcher] error: {e}")
            await asyncio.sleep(5)
