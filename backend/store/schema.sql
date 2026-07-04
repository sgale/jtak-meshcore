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
    source_type TEXT NOT NULL,   -- 'hub', 't114', 'meshtastic_node'
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    altitude    REAL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_positions_time        ON positions(timestamp);
CREATE INDEX IF NOT EXISTS idx_positions_source      ON positions(source_id);
CREATE INDEX IF NOT EXISTS idx_positions_source_time ON positions(source_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS rf_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   DATETIME NOT NULL,
    source_id   TEXT NOT NULL,
    source_name TEXT,
    hub_id      TEXT,
    rssi        REAL,
    snr         REAL,
    frequency   REAL,
    hop_count   INTEGER,
    direct_or_relay TEXT,
    path_loss_db    REAL,
    distance_mi     REAL,
    bearing_deg     REAL,
    packet_type     TEXT,
    battery_pct     REAL,
    channel_util_pct REAL,
    air_util_tx_pct  REAL,
    cpu_temp_c      REAL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rf_time        ON rf_metrics(timestamp);
CREATE INDEX IF NOT EXISTS idx_rf_source      ON rf_metrics(source_id);
CREATE INDEX IF NOT EXISTS idx_rf_source_time ON rf_metrics(source_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS sensors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   DATETIME NOT NULL,
    source_id   TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sensors_time ON sensors(timestamp);

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
CREATE INDEX IF NOT EXISTS idx_mesh_msg_from ON mesh_messages(from_id);
CREATE INDEX IF NOT EXISTS idx_mesh_msg_time ON mesh_messages(timestamp);

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

CREATE TABLE IF NOT EXISTS waypoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meshtastic_id   INTEGER,           -- original Meshtastic waypoint ID (dedup)
    name            TEXT NOT NULL DEFAULT 'Waypoint',
    description     TEXT,
    latitude        REAL NOT NULL,
    longitude       REAL NOT NULL,
    altitude        REAL,
    icon            TEXT,              -- emoji character
    source_id       TEXT,              -- sender node ID
    source_name     TEXT,              -- sender node name
    source_type     TEXT DEFAULT 'mesh',  -- 'mesh' | 'manual' | 'cot'
    created_at      TEXT NOT NULL,     -- ISO UTC
    expires_at      TEXT,              -- ISO UTC — NULL = permanent
    deleted_at      TEXT,              -- soft delete timestamp
    hub_id          TEXT               -- which hub received/created it
);
CREATE INDEX IF NOT EXISTS idx_waypoints_created ON waypoints(created_at);
CREATE INDEX IF NOT EXISTS idx_waypoints_expires ON waypoints(expires_at);
CREATE INDEX IF NOT EXISTS idx_waypoints_source  ON waypoints(source_id);

CREATE TABLE IF NOT EXISTS ui_prefs (
    client_id  TEXT PRIMARY KEY,
    prefs      TEXT NOT NULL DEFAULT '{}',  -- JSON blob of all UI state
    updated_at TEXT NOT NULL
);
