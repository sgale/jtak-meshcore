import aiosqlite
from pathlib import Path
from utils.config import get

DB_PATH = get("database.path", "/opt/jtak/data/jtak.db")
SCHEMA = Path(__file__).parent / "schema.sql"

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.executescript(SCHEMA.read_text())
        await _db.commit()
    return _db


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
