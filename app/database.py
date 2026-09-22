"""SQLite 存储层：连接管理与建表初始化。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("BATTERY_DB", str(Path("data") / "battery.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS batteries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    rated_capacity_ah  REAL NOT NULL,
    safe_voltage_min   REAL NOT NULL,
    safe_voltage_max   REAL NOT NULL,
    notes              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    battery_id    INTEGER NOT NULL REFERENCES batteries(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    start_time    TEXT,
    duration_s    REAL,
    row_count     INTEGER,
    max_current_a REAL,
    min_voltage   REAL,
    max_voltage   REAL,
    max_temp_c    REAL
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id      INTEGER NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
    t           REAL NOT NULL,
    voltage     REAL NOT NULL,
    current     REAL NOT NULL,
    temperature REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_log ON samples(log_id, t);

CREATE TABLE IF NOT EXISTS cycles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id         INTEGER NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
    battery_id     INTEGER NOT NULL REFERENCES batteries(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    kind           TEXT NOT NULL,              -- discharge | charge
    start_t        REAL NOT NULL,
    end_t          REAL NOT NULL,
    start_iso      TEXT,
    duration_s     REAL,
    capacity_ah    REAL,
    energy_wh      REAL,
    avg_current_a  REAL,
    min_voltage    REAL,
    max_voltage    REAL,
    max_temp_c     REAL,
    temp_rise_c    REAL,
    ir_mohm        REAL,
    closed         INTEGER NOT NULL DEFAULT 1,
    flags          TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_cycles_battery ON cycles(battery_id, id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """启动时调用：确保数据目录存在并建表。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
