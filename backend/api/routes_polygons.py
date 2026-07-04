"""
Polygon / Zone API
  GET    /api/polygons              — active zones
  POST   /api/polygons              — create zone
  PUT    /api/polygons/{id}         — update name / description / color / type
  DELETE /api/polygons/{id}         — soft-delete
  GET    /api/polygons/export.kml   — KML export of all active zones
  GET    /api/polygons/qr           — QR code PNG for the KML URL
"""

import io
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from store.db import get_db
from ingest.csv_watcher import broadcast_queue
from utils.config import get as _cfg

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _row_to_dict(row) -> dict:
    return {
        "id":          row["id"],
        "name":        row["name"],
        "description": row["description"],
        "type":        row["type"],
        "color":       row["color"],
        "geojson":     json.loads(row["geojson"]) if isinstance(row["geojson"], str) else row["geojson"],
        "created_at":  row["created_at"],
        "updated_at":  row["updated_at"],
        "deleted_at":  row["deleted_at"],
        "hub_id":      row["hub_id"],
    }


def _broadcast(z: dict):
    try:
        broadcast_queue.put_nowait({"type": "zone", **z})
    except Exception:
        pass


def _hex_to_kml(hex_color: str, alpha: str = "ff") -> str:
    """Convert #RRGGBB → KML aaBBGGRR."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"{alpha}{b}{g}{r}"


# ── Pydantic models ───────────────────────────────────────────────────────────

class ZoneCreate(BaseModel):
    name:        str
    description: Optional[str] = None
    type:        str = "polygon"   # polygon | polyline
    color:       str = "#f97316"
    geojson:     dict              # GeoJSON geometry object


class ZoneUpdate(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None
    color:       Optional[str] = None
    type:        Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/polygons")
async def get_polygons():
    db = await get_db()
    async with db.execute(
        "SELECT * FROM polygons WHERE deleted_at IS NULL ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/polygons")
async def create_polygon(req: ZoneCreate):
    now    = _now_utc()
    hub_id = _cfg("hub.id", "hub")
    db     = await get_db()
    async with db.execute(
        """INSERT INTO polygons (name, description, type, color, geojson, created_at, hub_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (req.name, req.description, req.type, req.color,
         json.dumps(req.geojson), now, hub_id)
    ) as cur:
        zone_id = cur.lastrowid
    await db.commit()
    async with db.execute("SELECT * FROM polygons WHERE id=?", (zone_id,)) as cur:
        row = await cur.fetchone()
    z = _row_to_dict(row)
    _broadcast(z)
    return z


@router.put("/polygons/{zone_id}")
async def update_polygon(zone_id: int, req: ZoneUpdate):
    db = await get_db()
    async with db.execute("SELECT * FROM polygons WHERE id=?", (zone_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Zone not found")
    name        = req.name        if req.name        is not None else row["name"]
    description = req.description if req.description is not None else row["description"]
    color       = req.color       if req.color       is not None else row["color"]
    zone_type   = req.type        if req.type        is not None else row["type"]
    await db.execute(
        "UPDATE polygons SET name=?, description=?, color=?, type=?, updated_at=? WHERE id=?",
        (name, description, color, zone_type, _now_utc(), zone_id)
    )
    await db.commit()
    async with db.execute("SELECT * FROM polygons WHERE id=?", (zone_id,)) as cur:
        row = await cur.fetchone()
    z = _row_to_dict(row)
    _broadcast(z)
    return z


@router.delete("/polygons/{zone_id}")
async def delete_polygon(zone_id: int):
    db = await get_db()
    async with db.execute("SELECT id FROM polygons WHERE id=?", (zone_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Zone not found")
    now = _now_utc()
    await db.execute("UPDATE polygons SET deleted_at=? WHERE id=?", (now, zone_id))
    await db.commit()
    _broadcast({"id": zone_id, "deleted_at": now})
    return {"deleted": zone_id}


@router.get("/polygons/export.kml")
async def export_kml(request: Request):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM polygons WHERE deleted_at IS NULL ORDER BY created_at ASC"
    ) as cur:
        rows = await cur.fetchall()

    hub_name = _cfg("hub.name", "jTAK Hub")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '<Document>',
        f'  <name>jTAK Zones — {hub_name}</name>',
        '  <description>Live zone/region overlays from jTAK</description>',
    ]

    for row in rows:
        z       = _row_to_dict(row)
        name    = z["name"] or "Zone"
        desc    = z["description"] or ""
        color   = z.get("color", "#f97316")
        geo     = z["geojson"]
        kml_line = _hex_to_kml(color, "ff")
        kml_fill = _hex_to_kml(color, "66")
        coords_raw = geo.get("coordinates", [])

        # Flatten coordinate list depending on geometry type
        geo_type = geo.get("type", "")
        if geo_type == "Polygon":
            coord_list = coords_raw[0] if coords_raw else []
        elif geo_type == "LineString":
            coord_list = coords_raw
        else:
            continue

        coord_str = " ".join(f"{c[0]},{c[1]},0" for c in coord_list)

        lines += [
            "  <Placemark>",
            f"    <name>{name}</name>",
            f"    <description>{desc}</description>",
            "    <Style>",
            "      <LineStyle>",
            f"        <color>{kml_line}</color>",
            "        <width>3</width>",
            "      </LineStyle>",
            "      <PolyStyle>",
            f"        <color>{kml_fill}</color>",
            "        <fill>1</fill>",
            "      </PolyStyle>",
            "    </Style>",
        ]

        if geo_type == "Polygon":
            lines += [
                "    <Polygon>",
                "      <outerBoundaryIs><LinearRing>",
                f"        <coordinates>{coord_str}</coordinates>",
                "      </LinearRing></outerBoundaryIs>",
                "    </Polygon>",
            ]
        else:
            lines += [
                "    <LineString>",
                f"      <coordinates>{coord_str}</coordinates>",
                "    </LineString>",
            ]

        lines.append("  </Placemark>")

    lines += ["</Document>", "</kml>"]
    kml_body = "\n".join(lines)
    return Response(
        content=kml_body,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": "attachment; filename=jtak_zones.kml"},
    )


@router.get("/polygons/qr")
async def polygon_qr(request: Request):
    """Return a QR code PNG for the KML export URL."""
    import qrcode
    base = str(request.base_url).rstrip("/")
    # Strip /jtak/api prefix added by root_path — reconstruct public URL
    host = str(request.headers.get("host", base))
    url  = f"http://{host}/jtak/api/polygons/export.kml"
    qr   = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")
