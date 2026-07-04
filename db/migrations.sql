-- jTAK DB Migrations — idempotent, safe to run on any hub at any time
-- Run via: sqlite3 /opt/jtak/data/jtak.db < /opt/jtak/db/migrations.sql
-- Add new migrations at the bottom. Never modify existing ones.

-- ── 001: core tables ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hub_info (
    hub_id    TEXT PRIMARY KEY,
    hub_name  TEXT NOT NULL,
    role      TEXT DEFAULT 'edge',
    last_boot DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   DATETIME NOT NULL,
    source_id   TEXT NOT NULL,
    source_name TEXT,
    source_type TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    altitude    REAL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rf_metrics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        DATETIME NOT NULL,
    source_id        TEXT NOT NULL,
    source_name      TEXT,
    hub_id           TEXT,
    rssi             REAL,
    snr              REAL,
    frequency        REAL,
    hop_count        INTEGER,
    direct_or_relay  TEXT,
    path_loss_db     REAL,
    distance_mi      REAL,
    bearing_deg      REAL,
    packet_type      TEXT,
    battery_pct      REAL,
    channel_util_pct REAL,
    air_util_tx_pct  REAL,
    cpu_temp_c       REAL,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sensors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   DATETIME NOT NULL,
    source_id   TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── 002: HQ sync columns ──────────────────────────────────────────────────────

ALTER TABLE positions  ADD COLUMN synced_to_hq INTEGER DEFAULT 0;
ALTER TABLE rf_metrics ADD COLUMN synced_to_hq INTEGER DEFAULT 0;
ALTER TABLE sensors    ADD COLUMN synced_to_hq INTEGER DEFAULT 0;

-- ── 003: mesh messages ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mesh_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    direction     TEXT NOT NULL,
    from_id       TEXT,
    from_name     TEXT,
    to_id         TEXT,
    to_name       TEXT,
    channel_index INTEGER DEFAULT 0,
    channel_name  TEXT,
    message       TEXT NOT NULL,
    want_ack      INTEGER DEFAULT 0,
    ack_received  INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    synced_to_hq  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mesh_send_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    to_id         TEXT NOT NULL,
    to_name       TEXT,
    channel_index INTEGER DEFAULT 0,
    channel_name  TEXT,
    message       TEXT NOT NULL,
    want_ack      INTEGER DEFAULT 1,
    status        TEXT DEFAULT 'pending',
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ── 004: aircraft DB ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS aircraft_db (
    icao24       TEXT PRIMARY KEY,
    registration TEXT,
    manufacturer TEXT,
    model        TEXT,
    icao_type    TEXT,
    operator     TEXT,
    category     TEXT
);

-- ── 005: jTAK agent messages ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS jtak_agent_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL,
    thread_id   TEXT,
    subject     TEXT,
    body        TEXT,
    msg_type    TEXT DEFAULT 'message',
    priority    INTEGER DEFAULT 5,
    status      TEXT DEFAULT 'unread',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    read_at     DATETIME
);

-- ── 007: polygons / zones ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS polygons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT 'Zone',
    description TEXT,
    type        TEXT    NOT NULL DEFAULT 'polygon',  -- polygon | polyline
    color       TEXT    NOT NULL DEFAULT '#f97316',
    geojson     TEXT    NOT NULL,                    -- GeoJSON geometry JSON string
    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT,
    deleted_at  TEXT,
    hub_id      TEXT
);

-- ── 006: waypoints ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS waypoint_send_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wp_id         INTEGER NOT NULL,         -- local waypoints.id
    action        TEXT NOT NULL DEFAULT 'send',  -- 'send' or 'delete'
    name          TEXT,
    description   TEXT,
    latitude      REAL,
    longitude     REAL,
    icon          TEXT,
    expires_at    TEXT,                     -- ISO UTC or NULL
    meshtastic_id INTEGER,                  -- set after send, for deletes
    channel_index INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'pending',   -- pending / sent / failed
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS waypoints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    meshtastic_id INTEGER,
    name          TEXT NOT NULL DEFAULT 'Waypoint',
    description   TEXT,
    latitude      REAL NOT NULL,
    longitude     REAL NOT NULL,
    altitude      REAL,
    icon          TEXT,
    source_id     TEXT,
    source_name   TEXT,
    source_type   TEXT DEFAULT 'mesh',
    created_at    TEXT NOT NULL,
    expires_at    TEXT,
    deleted_at    TEXT,
    hub_id        TEXT
);
