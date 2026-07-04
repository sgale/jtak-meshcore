"""
routes_mesh.py — Meshtastic messaging API

Endpoints:
  GET  /mesh/channels        — list configured mesh channels (cached from file)
  POST /mesh/send            — queue a message for delivery by tactical_monitor
  GET  /mesh/send/{id}       — poll send status
  GET  /mesh/messages        — fetch message history (optional ?node_id= filter)
  DELETE /mesh/messages      — clear all message history
  GET  /mesh/stream          — SSE stream of new messages (uses ?after_id=N)
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from store.db import get_db

router = APIRouter()

CHANNELS_FILE = Path("/opt/jtak/data/mesh_channels.json")
MAX_MSG_BYTES = 237


# ── Channels ──────────────────────────────────────────────────────────────────

@router.get("/mesh/channels")
async def get_channels():
    try:
        return json.loads(CHANNELS_FILE.read_text())
    except Exception:
        return [{"index": 0, "name": "Primary", "role": 1}]


# ── Send ──────────────────────────────────────────────────────────────────────

class SendRequest(BaseModel):
    to_id: str
    to_name: Optional[str] = None
    channel_index: int = 0
    channel_name: Optional[str] = None
    message: str
    want_ack: bool = True


@router.post("/mesh/send")
async def send_message(req: SendRequest):
    if len(req.message.encode("utf-8")) > MAX_MSG_BYTES:
        raise HTTPException(400, f"Message exceeds {MAX_MSG_BYTES} byte LoRa limit")
    if not req.message.strip():
        raise HTTPException(400, "Empty message")
    db = await get_db()
    cur = await db.execute(
        """INSERT INTO mesh_send_queue
           (to_id, to_name, channel_index, channel_name, message, want_ack)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (req.to_id, req.to_name, req.channel_index, req.channel_name,
         req.message, 1 if req.want_ack else 0),
    )
    await db.commit()
    return {"id": cur.lastrowid, "status": "queued"}


@router.get("/mesh/send/{send_id}")
async def get_send_status(send_id: int):
    db = await get_db()
    async with db.execute(
        "SELECT id, status, created_at FROM mesh_send_queue WHERE id=?", (send_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)


# ── Messages ──────────────────────────────────────────────────────────────────

@router.get("/mesh/messages")
async def get_messages(node_id: Optional[str] = None, limit: int = 200, after_id: int = 0):
    db = await get_db()
    if after_id:
        sql = "SELECT * FROM mesh_messages WHERE id > ? ORDER BY id ASC LIMIT ?"
        async with db.execute(sql, (after_id, limit)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    if node_id:
        sql = """SELECT * FROM mesh_messages
                 WHERE from_id=? OR to_id=?
                 ORDER BY id DESC LIMIT ?"""
        async with db.execute(sql, (node_id, node_id, limit)) as cur:
            rows = await cur.fetchall()
    else:
        sql = "SELECT * FROM mesh_messages ORDER BY id DESC LIMIT ?"
        async with db.execute(sql, (limit,)) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


@router.delete("/mesh/messages")
async def clear_messages():
    db = await get_db()
    await db.execute("DELETE FROM mesh_messages")
    await db.commit()
    return {"ok": True, "deleted": True}


# ── SSE stream ────────────────────────────────────────────────────────────────

@router.get("/mesh/stream")
async def message_stream(after_id: int = 0):
    async def generate():
        last_id = after_id
        while True:
            try:
                db = await get_db()
                async with db.execute(
                    "SELECT * FROM mesh_messages WHERE id > ? ORDER BY id ASC LIMIT 20",
                    (last_id,),
                ) as cur:
                    rows = await cur.fetchall()
                for row in rows:
                    d = dict(row)
                    last_id = d["id"]
                    yield f"data: {json.dumps(d)}\n\n"
                if not rows:
                    yield ": ping\n\n"
            except Exception as e:
                yield f": error {e}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
