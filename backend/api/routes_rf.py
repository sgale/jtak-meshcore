from fastapi import APIRouter, Query
from store.db import get_db

router = APIRouter()


@router.get("/rf")
async def latest_rf(limit: int = Query(100, le=1000)):
    db = await get_db()
    async with db.execute(
        """SELECT * FROM rf_metrics ORDER BY timestamp DESC LIMIT ?""",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/rf/node")
async def rf_for_node(source_id: str, limit: int = Query(200, le=2000)):
    db = await get_db()
    async with db.execute(
        """SELECT * FROM rf_metrics WHERE source_id = ?
           ORDER BY timestamp DESC LIMIT ?""",
        (source_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
