"""
UI Preferences API — per-browser persistence via stable browser fingerprint
Client sends X-Client-ID header (SHA-256 hash of stable browser attributes).
No cookies required — survives cookie/localStorage clearing.
  GET  /api/ui-prefs  — load prefs for this browser fingerprint
  POST /api/ui-prefs  — save prefs for this browser fingerprint
"""

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from typing import Optional
from pydantic import BaseModel

from store.db import get_db

router = APIRouter()

_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def _valid_id(client_id: Optional[str]) -> Optional[str]:
    if client_id and _ID_RE.match(client_id):
        return client_id
    return None


@router.get("/ui-prefs")
async def get_ui_prefs(x_client_id: Optional[str] = Header(default=None)):
    client_id = _valid_id(x_client_id)
    if not client_id:
        return {}
    db = await get_db()
    async with db.execute(
        "SELECT prefs FROM ui_prefs WHERE client_id = ?", (client_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["prefs"])
    except Exception:
        return {}


class PrefsPayload(BaseModel):
    prefs: dict


@router.post("/ui-prefs")
async def save_ui_prefs(payload: PrefsPayload, x_client_id: Optional[str] = Header(default=None)):
    client_id = _valid_id(x_client_id)
    if not client_id:
        return {"ok": False, "error": "missing or invalid X-Client-ID"}
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    await db.execute(
        """INSERT INTO ui_prefs(client_id, prefs, updated_at) VALUES(?,?,?)
           ON CONFLICT(client_id) DO UPDATE SET prefs=excluded.prefs, updated_at=excluded.updated_at""",
        (client_id, json.dumps(payload.prefs), now),
    )
    await db.commit()
    return {"ok": True}
