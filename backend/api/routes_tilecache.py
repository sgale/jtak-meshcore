"""
jTAK Tile Cache API
  GET  /api/tilecache/caches             — list all caches + disk stats
  POST /api/tilecache/caches             — create + start download
  DELETE /api/tilecache/caches/{id}      — delete cache (manifests only; prune to reclaim tiles)
  POST /api/tilecache/caches/{id}/stop   — stop in-progress download
  POST /api/tilecache/caches/{id}/refresh — re-download existing cache
  GET  /api/tilecache/caches/{id}/progress — SSE progress stream
  GET  /api/tilecache/estimate           — tile count + size estimate (no download)
  POST /api/tilecache/home               — create/refresh home-area cache from hub position
  POST /api/tilecache/prune              — delete tiles not covered by any manifest
  GET  /api/tiles/{z}/{x}/{y}.png        — serve a cached tile (404 if not cached)
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from utils.config import get
from tilecache import worker as W

router = APIRouter()

TILE_DIR     = Path("/opt/jtak/data/tilecache/tiles")
MANIFEST_DIR = Path("/opt/jtak/data/tilecache/manifests")

ZOOM_PRESETS = {
    "overview": (1, 13),
    "street":   (1, 15),
    "tactical": (1, 17),
    "detail":   (1, 18),
}


# ── Models ────────────────────────────────────────────────────────────────────

class NewCacheRequest(BaseModel):
    name:        str
    center_lat:  float
    center_lon:  float
    radius_mi:   float = 10.0
    zoom_preset: str   = "tactical"   # overview | street | tactical | detail
    cache_type:  str   = "mission"    # mission | home


# ── List / stats ──────────────────────────────────────────────────────────────

@router.get("/tilecache/caches")
async def list_caches():
    manifests = W.list_manifests()
    for m in manifests:
        if W.is_active(m["id"]):
            m["status"] = "downloading"
    return {"caches": manifests, "disk": W.disk_usage()}


# ── Estimate (no download) ────────────────────────────────────────────────────

@router.get("/tilecache/estimate")
async def estimate(center_lat: float, center_lon: float,
                   radius_mi: float = 10.0, zoom_preset: str = "tactical"):
    z_min, z_max = ZOOM_PRESETS.get(zoom_preset, (1, 17))
    bbox = W.bbox_from_center_radius(center_lat, center_lon, radius_mi)
    n = W.count_tiles(bbox, z_min, z_max)
    size_bytes = W._estimate_size(n)
    return {"tile_count": n, "size_bytes": size_bytes,
            "zoom_min": z_min, "zoom_max": z_max}


# ── Create cache ──────────────────────────────────────────────────────────────

@router.post("/tilecache/caches")
async def create_cache(req: NewCacheRequest):
    z_min, z_max = ZOOM_PRESETS.get(req.zoom_preset, (1, 17))
    bbox = W.bbox_from_center_radius(req.center_lat, req.center_lon, req.radius_mi)
    n    = W.count_tiles(bbox, z_min, z_max)

    if n > 600_000:
        raise HTTPException(400, f"Too many tiles ({n:,}). Reduce radius or zoom level.")

    safe_name = req.name.lower().replace(" ", "-").replace("/", "-")[:30]
    cache_id  = f"{safe_name}-{datetime.now(timezone.utc).strftime('%m%d%H%M')}"

    manifest = {
        "id":               cache_id,
        "name":             req.name,
        "type":             req.cache_type,
        "center":           [req.center_lat, req.center_lon],
        "radius_mi":        req.radius_mi,
        "bbox":             list(bbox),
        "zoom_min":         z_min,
        "zoom_max":         z_max,
        "zoom_preset":      req.zoom_preset,
        "total_tiles":      n,
        "downloaded_tiles": 0,
        "error_tiles":      0,
        "size_bytes":       0,
        "status":           "pending",
        "created":          datetime.now(timezone.utc).isoformat(),
    }
    W.save_manifest(manifest)
    await W.start_download(cache_id, manifest)
    return manifest


# ── Home area cache ───────────────────────────────────────────────────────────

@router.post("/tilecache/home")
async def create_home_cache():
    """Create or refresh the home-area cache using hub GPS or config center."""
    from ingest.csv_watcher import hub_position
    pos = hub_position if hub_position.get("latitude") else None
    if pos:
        lat = pos["latitude"]
        lon = pos["longitude"]
    else:
        center = get("map.default_center", [40.5729, -111.9941])
        lat, lon = center[0], center[1]

    # Remove existing home cache if present
    for m in W.list_manifests():
        if m.get("type") == "home":
            W.stop_download(m["id"])
            (MANIFEST_DIR / f"{m['id']}.json").unlink(missing_ok=True)

    req = NewCacheRequest(
        name="Home Area",
        center_lat=lat,
        center_lon=lon,
        radius_mi=get("tilecache.home_radius_mi", 25.0),
        zoom_preset=get("tilecache.home_zoom_preset", "tactical"),
        cache_type="home",
    )
    return await create_cache(req)


# ── Stop / refresh / delete ───────────────────────────────────────────────────

@router.post("/tilecache/caches/{cache_id}/pause")
async def pause_cache(cache_id: str):
    W.stop_download(cache_id)
    m = W.load_manifest(cache_id)
    if m:
        m["status"] = "paused"
        W.save_manifest(m)
    return {"paused": cache_id}


@router.post("/tilecache/caches/{cache_id}/refresh")
async def refresh_cache(cache_id: str):
    m = W.load_manifest(cache_id)
    if not m:
        raise HTTPException(404, "Cache not found")
    if W.is_active(cache_id):
        raise HTTPException(409, "Already downloading")
    m["status"] = "downloading"
    await W.start_download(cache_id, m)
    return m


@router.delete("/tilecache/caches/{cache_id}")
async def delete_cache(cache_id: str):
    W.stop_download(cache_id)
    path = MANIFEST_DIR / f"{cache_id}.json"
    if not path.exists():
        raise HTTPException(404, "Cache not found")
    path.unlink()
    return {"deleted": cache_id}


# ── SSE progress ──────────────────────────────────────────────────────────────

@router.get("/tilecache/caches/{cache_id}/progress")
async def cache_progress(cache_id: str):
    q = W._progress.get(cache_id)

    if not q:
        m = W.load_manifest(cache_id)
        if not m:
            raise HTTPException(404)
        done  = m.get("downloaded_tiles", 0)
        total = m.get("total_tiles", 0)
        pct   = round(done / total * 100, 1) if total else 0.0
        snap = json.dumps({"done": done, "total": total, "pct": pct,
                           "status": m.get("status", "complete")})
        async def once():
            yield f"data: {snap}\n\n"
        return StreamingResponse(once(), media_type="text/event-stream")

    async def stream():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20.0)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("status") not in ("downloading", "pending"):
                    break
            except asyncio.TimeoutError:
                yield 'data: {"ping":1}\n\n'

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── Prune orphaned tiles ──────────────────────────────────────────────────────

@router.post("/tilecache/prune")
async def prune_tiles():
    """Delete tiles on disk not covered by any active manifest."""
    manifests = W.list_manifests()

    # Build full set of tiles that should be kept
    keep: set[tuple] = set()
    for m in manifests:
        lat1, lon1, lat2, lon2 = m["bbox"]
        for z in range(m["zoom_min"], m["zoom_max"] + 1):
            for x, y in W.tiles_for_bbox(lat1, lon1, lat2, lon2, z):
                keep.add((z, x, y))

    deleted = 0
    if TILE_DIR.exists():
        for z_dir in TILE_DIR.iterdir():
            if not z_dir.is_dir():
                continue
            z = int(z_dir.name)
            for x_dir in z_dir.iterdir():
                if not x_dir.is_dir():
                    continue
                x = int(x_dir.name)
                for tile_file in list(x_dir.iterdir()):
                    y = int(tile_file.stem)
                    if (z, x, y) not in keep:
                        tile_file.unlink()
                        deleted += 1

    return {"deleted_tiles": deleted, "disk": W.disk_usage()}


# ── Tile serving ──────────────────────────────────────────────────────────────

@router.get("/tiles/{z}/{x}/{y}.png")
async def serve_tile(z: int, x: int, y: int):
    path = TILE_DIR / str(z) / str(x) / f"{y}.png"
    if path.exists():
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=2592000"})
    return Response(status_code=404)
