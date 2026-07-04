from fastapi import APIRouter
from ingest.csv_watcher import node_sensors

router = APIRouter()


@router.get("/sensors")
async def all_sensors():
    """Latest sensor reading per node (from mesh telemetry)."""
    return {
        "sensors": list(node_sensors.values()),
        "meta": {
            "units": {
                "temp_c":       "°C",
                "temp_f":       "°F",
                "humidity_pct": "%",
                "pressure_hpa": "hPa",
            }
        },
    }
