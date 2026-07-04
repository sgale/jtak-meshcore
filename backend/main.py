"""
jTAK FastAPI backend — main entrypoint
Runs on port 8420, served via NGINX at /jtak/api/
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure local modules resolve
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from utils.config import get, load_config
from utils.identity import init_identity
from store.db import get_db, close_db
from ingest import csv_watcher
from ingest.bme280 import run_bme280_loop
from ingest.csv_watcher import latest_hub_env
from ingest import led_monitor
from ingest import sdr_logger
from ingest import rf_logger
from ingest import waypoint_watcher
from ingest import db_pruner

from api.routes_status  import router as status_router, _poll_gps_sats
from api.routes_map     import router as map_router
from api.routes_rf           import router as rf_router
from api.routes_rf_bakeoff   import router as rf_bakeoff_router
from api.routes_sensors import router as sensors_router
from api.routes_ws      import router as ws_router, start_broadcaster
from api.routes_weather    import router as weather_router
from api.routes_firespread import router as firespread_router
from api.routes_fire       import router as fire_router
from api.routes_aircraft   import router as aircraft_router
from api.routes_federation import router as federation_router
from api.routes_history    import router as history_router
from api.routes_tilecache  import router as tilecache_router
from api.routes_led        import router as led_router
from api.routes_atmo       import router as atmo_router, _background_refresh as _atmo_prefetch
from api.routes_lightning  import router as lightning_router, start_lightning
from api.routes_mesh       import router as mesh_router
from api.routes_iap        import router as iap_router
from api.routes_waypoints  import router as waypoints_router
from api.routes_polygons   import router as polygons_router
from api.routes_ui_prefs           import router as ui_prefs_router
from api.routes_meshtastic_debug import router as mesh_debug_router
from api.routes_auth        import router as auth_router
from api.routes_mesh_config import router as mesh_config_router

load_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    ident = await loop.run_in_executor(None, init_identity)
    await get_db()
    start_broadcaster()
    task = asyncio.create_task(csv_watcher.run())
    bme_task = asyncio.create_task(run_bme280_loop(latest_hub_env))
    led_task = asyncio.create_task(led_monitor.run())
    gps_task     = asyncio.create_task(_poll_gps_sats())
    sdr_log_task = asyncio.create_task(sdr_logger.run())
    rf_log_task  = asyncio.create_task(rf_logger.run())
    wp_task      = asyncio.create_task(waypoint_watcher.run())
    prune_task   = asyncio.create_task(db_pruner.run())
    await start_lightning()
    # Pre-warm atmo cache in background so first user request is never cold
    asyncio.create_task(_atmo_prefetch(
        get("map.default_center", [0, 0])[0],
        get("map.default_center", [0, 0])[1],
    ))
    print(f"[jTAK] {ident['hub_name']} ({ident['hub_id']}) — API ready on port {get('api.port', 8420)}")
    yield
    task.cancel()
    bme_task.cancel()
    led_task.cancel()
    gps_task.cancel()
    sdr_log_task.cancel()
    rf_log_task.cancel()
    wp_task.cancel()
    try:
        await asyncio.gather(task, bme_task, led_task, gps_task, wp_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    await close_db()


app = FastAPI(title="jTAK API", version="0.1.0", root_path="/jtak/api", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get("api.cors_origins", ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(status_router,  prefix="/api")
app.include_router(map_router,     prefix="/api")
app.include_router(rf_router,          prefix="/api")
app.include_router(rf_bakeoff_router,  prefix="/api")
app.include_router(sensors_router, prefix="/api")
app.include_router(ws_router,      prefix="/api")
app.include_router(weather_router,    prefix="/api")
app.include_router(firespread_router, prefix="/api")
app.include_router(fire_router,       prefix="/api")
app.include_router(aircraft_router,   prefix="/api")
app.include_router(federation_router, prefix="/api")
app.include_router(history_router,    prefix="/api")
app.include_router(tilecache_router,  prefix="/api")
app.include_router(led_router,        prefix="/api")
app.include_router(atmo_router,       prefix="/api")
app.include_router(lightning_router,  prefix="/api")
app.include_router(mesh_router,       prefix="/api")
app.include_router(iap_router,        prefix="/api")
app.include_router(waypoints_router,  prefix="/api")
app.include_router(polygons_router,   prefix="/api")
app.include_router(ui_prefs_router,    prefix="/api")
app.include_router(mesh_debug_router,  prefix="/api")
app.include_router(auth_router,        prefix="/api")
app.include_router(mesh_config_router, prefix="/api")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=get("api.host", "0.0.0.0"),
        port=get("api.port", 8420),
        reload=False,
        log_level="info",
    )
