"""
jTAK Tile Cache — download engine.
Tiles stored flat: /opt/jtak/data/tilecache/tiles/{z}/{x}/{y}.png
Manifests:        /opt/jtak/data/tilecache/manifests/{id}.json
"""

import asyncio
import json
import math
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

TILE_DIR     = Path("/opt/jtak/data/tilecache/tiles")
MANIFEST_DIR = Path("/opt/jtak/data/tilecache/manifests")

TILE_PROVIDERS = [
    "https://tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
]

HEADERS = {"User-Agent": "jTAK-field-hub/1.0 (emergency-management; rate-limited)"}

# active download tasks  cache_id → Task
_active:   dict[str, asyncio.Task]  = {}
# progress queues  cache_id → Queue
_progress: dict[str, asyncio.Queue] = {}


# ── Tile math ─────────────────────────────────────────────────────────────────

def deg2num(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    lat_r = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def bbox_from_center_radius(lat: float, lon: float, radius_mi: float) -> tuple:
    dlat = radius_mi / 69.0
    dlon = radius_mi / (69.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def tiles_for_bbox(lat1: float, lon1: float, lat2: float, lon2: float, zoom: int):
    x1, y1 = deg2num(max(lat1, lat2), min(lon1, lon2), zoom)
    x2, y2 = deg2num(min(lat1, lat2), max(lon1, lon2), zoom)
    for x in range(min(x1, x2), max(x1, x2) + 1):
        for y in range(min(y1, y2), max(y1, y2) + 1):
            yield x, y


def count_tiles(bbox: tuple, zoom_min: int, zoom_max: int) -> int:
    lat1, lon1, lat2, lon2 = bbox
    return sum(
        sum(1 for _ in tiles_for_bbox(lat1, lon1, lat2, lon2, z))
        for z in range(zoom_min, zoom_max + 1)
    )


# ── Manifest helpers ──────────────────────────────────────────────────────────

def load_manifest(cache_id: str) -> dict | None:
    path = MANIFEST_DIR / f"{cache_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def save_manifest(m: dict):
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    (MANIFEST_DIR / f"{m['id']}.json").write_text(json.dumps(m, indent=2))


def list_manifests() -> list[dict]:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(MANIFEST_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            pass
    return out


# ── Disk usage ────────────────────────────────────────────────────────────────

def disk_usage() -> dict:
    total_bytes = 0
    tile_count  = 0
    if TILE_DIR.exists():
        for f in TILE_DIR.rglob("*.png"):
            try:
                total_bytes += f.stat().st_size
                tile_count  += 1
            except OSError:
                pass
    disk = shutil.disk_usage("/opt/jtak/data")
    return {
        "cache_bytes":      total_bytes,
        "cache_tiles":      tile_count,
        "disk_free_bytes":  disk.free,
        "disk_total_bytes": disk.total,
    }


# ── Download lifecycle ────────────────────────────────────────────────────────

def is_active(cache_id: str) -> bool:
    t = _active.get(cache_id)
    return t is not None and not t.done()


async def start_download(cache_id: str, manifest: dict) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _progress[cache_id] = q
    task = asyncio.create_task(_download(cache_id, manifest, q))
    _active[cache_id] = task
    return q


def stop_download(cache_id: str):
    t = _active.get(cache_id)
    if t and not t.done():
        t.cancel()


async def _download(cache_id: str, manifest: dict, q: asyncio.Queue):
    lat1, lon1, lat2, lon2 = manifest["bbox"]
    zoom_min, zoom_max = manifest["zoom_min"], manifest["zoom_max"]

    all_tiles = [
        (z, x, y)
        for z in range(zoom_min, zoom_max + 1)
        for x, y in tiles_for_bbox(lat1, lon1, lat2, lon2, z)
    ]
    total = len(all_tiles)
    done = errors = 0

    manifest["total_tiles"] = total
    manifest["status"]      = "downloading"
    save_manifest(manifest)

    TILE_DIR.mkdir(parents=True, exist_ok=True)

    paused = False
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            for z, x, y in all_tiles:
                path = TILE_DIR / str(z) / str(x) / f"{y}.png"

                if path.exists():
                    done += 1
                    if done % 100 == 0:
                        _push(q, done, total, errors, "downloading")
                    continue

                path.parent.mkdir(parents=True, exist_ok=True)
                ok = False
                for url_tpl in TILE_PROVIDERS:
                    url = url_tpl.format(z=z, x=x, y=y)
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                            if r.status == 200:
                                path.write_bytes(await r.read())
                                ok = True
                                break
                            if r.status == 429:
                                await asyncio.sleep(3.0)
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        continue

                if not ok:
                    errors += 1

                done += 1
                # Flush progress to manifest every 500 tiles so state survives browser refresh
                if done % 500 == 0 or done == total:
                    manifest["downloaded_tiles"] = done - errors
                    manifest["error_tiles"]      = errors
                    manifest["size_bytes"]       = _estimate_size(done - errors)
                    save_manifest(manifest)

                if done % 50 == 0 or done == total:
                    _push(q, done, total, errors, "downloading")

                await asyncio.sleep(0.15)   # ~6-7 req/s — polite

    except asyncio.CancelledError:
        paused = True

    status = "complete" if (done >= total and errors == 0) else (
             "complete_with_errors" if done >= total else
             "paused" if paused else "stopped")
    manifest.update({
        "status":            status,
        "downloaded_tiles":  done - errors,
        "error_tiles":       errors,
        "completed":         datetime.now(timezone.utc).isoformat(),
        "size_bytes":        _estimate_size(done - errors),
    })
    save_manifest(manifest)
    _push(q, done, total, errors, status)
    _active.pop(cache_id, None)
    _progress.pop(cache_id, None)


def _estimate_size(tile_count: int) -> int:
    """Estimate bytes from a sample of actual tiles on disk."""
    sample_bytes = sample_n = 0
    if TILE_DIR.exists():
        for p in list(TILE_DIR.rglob("*.png"))[:200]:
            try:
                sample_bytes += p.stat().st_size
                sample_n += 1
            except OSError:
                pass
    avg = sample_bytes / max(sample_n, 1) if sample_n else 18_000
    return int(tile_count * avg)


def _push(q: asyncio.Queue, done: int, total: int, errors: int, status: str):
    pct = round(done / max(total, 1) * 100, 1)
    msg = {"done": done, "total": total, "errors": errors,
           "pct": pct, "status": status}
    try:
        q.put_nowait(msg)
    except asyncio.QueueFull:
        pass
