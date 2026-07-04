"""
Waypoint API
  GET    /api/waypoints              — active waypoints (not expired, not deleted)
  GET    /api/waypoints/all          — all incl. expired/deleted (for editor)
  POST   /api/waypoints              — create manual waypoint
  PUT    /api/waypoints/{id}         — update name / description / icon / expires_at
  DELETE /api/waypoints/{id}         — soft-delete (sets deleted_at)
  POST   /api/waypoints/{id}/restore — clear deleted_at
  GET    /api/history/waypoints      — waypoints active in a history window (TRACKS)
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from store.db import get_db
from ingest.csv_watcher import broadcast_queue

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _row_to_dict(row) -> dict:
    return {
        "id":            row["id"],
        "meshtastic_id": row["meshtastic_id"],
        "name":          row["name"],
        "description":   row["description"],
        "lat":           row["latitude"],
        "lon":           row["longitude"],
        "altitude":      row["altitude"],
        "icon":          row["icon"],
        "source_id":     row["source_id"],
        "source_name":   row["source_name"],
        "source_type":   row["source_type"],
        "created_at":    row["created_at"],
        "expires_at":    row["expires_at"],
        "deleted_at":    row["deleted_at"],
        "hub_id":        row["hub_id"],
    }


def _broadcast(wp: dict):
    msg = {"type": "waypoint", **wp}
    try:
        broadcast_queue.put_nowait(msg)
    except Exception:
        pass


async def _enqueue_mesh_send(wp: dict):
    """Queue this waypoint for broadcast over Meshtastic mesh."""
    db = await get_db()
    await db.execute(
        """INSERT INTO waypoint_send_queue
             (wp_id, action, name, description, latitude, longitude, icon, expires_at)
           VALUES (?, 'send', ?, ?, ?, ?, ?, ?)""",
        (wp["id"], wp["name"], wp.get("description"), wp["lat"], wp["lon"],
         wp.get("icon"), wp.get("expires_at"))
    )
    await db.commit()


async def _enqueue_mesh_delete(wp_id: int, meshtastic_id: int):
    """Queue a waypoint delete over Meshtastic mesh."""
    if not meshtastic_id:
        return
    db = await get_db()
    await db.execute(
        """INSERT INTO waypoint_send_queue
             (wp_id, action, meshtastic_id)
           VALUES (?, 'delete', ?)""",
        (wp_id, meshtastic_id)
    )
    await db.commit()


# ── Pydantic models ───────────────────────────────────────────────────────────

class WaypointCreate(BaseModel):
    name:        str
    description: Optional[str]   = None
    lat:         float
    lon:         float
    altitude:    Optional[float] = None
    icon:        Optional[str]   = None   # emoji character
    expires_at:  Optional[str]   = None   # ISO UTC string or null


class WaypointUpdate(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None
    icon:        Optional[str] = None
    expires_at:  Optional[str] = None    # empty string = clear expiry


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/waypoints")
async def get_waypoints():
    """Active waypoints — not deleted, not expired."""
    now = _now_utc()
    db = await get_db()
    async with db.execute(
        """SELECT * FROM waypoints
           WHERE deleted_at IS NULL
             AND (expires_at IS NULL OR expires_at > ?)
           ORDER BY created_at DESC""",
        (now,)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/waypoints/all")
async def get_all_waypoints():
    """All waypoints including expired/deleted — for editor panel."""
    db = await get_db()
    async with db.execute(
        "SELECT * FROM waypoints ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/waypoints")
async def create_waypoint(req: WaypointCreate):
    now = _now_utc()
    db = await get_db()
    async with db.execute(
        """INSERT INTO waypoints
             (name, description, latitude, longitude, altitude, icon,
              source_type, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?)""",
        (req.name, req.description, req.lat, req.lon, req.altitude,
         req.icon, now, req.expires_at or None)
    ) as cur:
        wp_id = cur.lastrowid
    # Use the row id as the Meshtastic waypoint id so deletes can reference it
    await db.execute("UPDATE waypoints SET meshtastic_id=? WHERE id=?", (wp_id, wp_id))
    await db.commit()
    async with db.execute("SELECT * FROM waypoints WHERE id=?", (wp_id,)) as cur:
        row = await cur.fetchone()
    wp = _row_to_dict(row)
    _broadcast(wp)
    await _enqueue_mesh_send(wp)
    return wp


@router.put("/waypoints/{wp_id}")
async def update_waypoint(wp_id: int, req: WaypointUpdate):
    db = await get_db()
    async with db.execute("SELECT * FROM waypoints WHERE id=?", (wp_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Waypoint not found")

    name        = req.name        if req.name        is not None else row["name"]
    description = req.description if req.description is not None else row["description"]
    icon        = req.icon        if req.icon        is not None else row["icon"]
    if req.expires_at is not None:
        expires_at = req.expires_at or None  # empty string clears it
    else:
        expires_at = row["expires_at"]

    await db.execute(
        "UPDATE waypoints SET name=?, description=?, icon=?, expires_at=? WHERE id=?",
        (name, description, icon, expires_at, wp_id)
    )
    await db.commit()
    async with db.execute("SELECT * FROM waypoints WHERE id=?", (wp_id,)) as cur:
        row = await cur.fetchone()
    wp = _row_to_dict(row)
    _broadcast(wp)
    return wp


@router.delete("/waypoints/{wp_id}")
async def delete_waypoint(wp_id: int):
    db = await get_db()
    async with db.execute("SELECT id FROM waypoints WHERE id=?", (wp_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Waypoint not found")
    now = _now_utc()
    # Fetch meshtastic_id before soft-delete so we can send the mesh delete
    async with db.execute("SELECT meshtastic_id FROM waypoints WHERE id=?", (wp_id,)) as cur:
        wp_row = await cur.fetchone()
    await db.execute("UPDATE waypoints SET deleted_at=? WHERE id=?", (now, wp_id))
    await db.commit()
    _broadcast({"id": wp_id, "deleted_at": now})
    if wp_row and wp_row["meshtastic_id"]:
        await _enqueue_mesh_delete(wp_id, wp_row["meshtastic_id"])
    return {"deleted": wp_id}


@router.post("/waypoints/{wp_id}/restore")
async def restore_waypoint(wp_id: int):
    db = await get_db()
    async with db.execute("SELECT * FROM waypoints WHERE id=?", (wp_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Waypoint not found")
    await db.execute("UPDATE waypoints SET deleted_at=NULL WHERE id=?", (wp_id,))
    await db.commit()
    async with db.execute("SELECT * FROM waypoints WHERE id=?", (wp_id,)) as cur:
        row = await cur.fetchone()
    wp = _row_to_dict(row)
    _broadcast(wp)
    return wp


@router.get("/history/waypoints")
async def history_waypoints(
    date:     str,
    start_ts: Optional[str] = None,
    end_ts:   Optional[str] = None,
):
    """Waypoints active during a history window — for TRACKS playback.
    A waypoint is 'active' if: created_at <= end AND (expires_at IS NULL OR expires_at >= start)
    AND (deleted_at IS NULL OR deleted_at > start).
    """
    if not start_ts:
        start_ts = f"{date}T00:00:00Z"
    if not end_ts:
        end_ts = f"{date}T23:59:59Z"
    db = await get_db()
    async with db.execute(
        """SELECT * FROM waypoints
           WHERE created_at <= ?
             AND (expires_at IS NULL OR expires_at >= ?)
             AND (deleted_at IS NULL OR deleted_at > ?)
           ORDER BY created_at ASC""",
        (end_ts, start_ts, start_ts)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]
