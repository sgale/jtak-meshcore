import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ingest.csv_watcher import broadcast_queue

router = APIRouter()

# Per-client asyncio queues — broadcaster task fans out to each
_clients: dict[WebSocket, asyncio.Queue] = {}


async def _broadcaster():
    """Single task: drain broadcast_queue and push to every client's queue."""
    while True:
        msg = await broadcast_queue.get()
        dead = []
        for ws, q in list(_clients.items()):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(ws)
        for ws in dead:
            _clients.pop(ws, None)


# Started once from main.py lifespan
broadcaster_task: asyncio.Task | None = None


def start_broadcaster():
    global broadcaster_task
    broadcaster_task = asyncio.create_task(_broadcaster())


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _clients[ws] = q
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20.0)
                await ws.send_text(json.dumps(msg))
            except asyncio.TimeoutError:
                # Keep-alive ping
                await ws.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _clients.pop(ws, None)
