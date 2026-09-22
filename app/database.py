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
    max_temp_c    REAL,
    cell_count      INTEGER,
    cell_columns    TEXT,
    cell_rows_used  INTEGER,
    cell_rows_skipped INTEGER,
    cell_skip_reasons TEXT,
    cell_problems   TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id      INTEGER NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
    t           REAL NOT NULL,
    voltage     REAL NOT NULL,
    current     REAL NOT NULL,
    temperature REAL NOT NULL,
    cell_voltages TEXT
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
    flags          TEXT NOT NULL DEFAULT '[]',
    cell_count       INTEGER,
    cell_rows_used   INTEGER,
    cell_rows_skipped INTEGER,
    max_cell_spread_v REAL,
    over_threshold_s  REAL,
    cell_intervals    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_cycles_battery ON cycles(battery_id, id);
"""

# 旧版本数据库增量迁移（CREATE TABLE IF NOT EXISTS 不会为既有表补列）
_MIGRATIONS = {
    "logs": [
        ("cell_count", "INTEGER"), ("cell_columns", "TEXT"),
        ("cell_rows_used", "INTEGER"), ("cell_rows_skipped", "INTEGER"),
        ("cell_skip_reasons", "TEXT"), ("cell_problems", "TEXT"),
    ],
    "samples": [("cell_voltages", "TEXT")],
    "cycles": [
        ("cell_count", "INTEGER"), ("cell_rows_used", "INTEGER"),
        ("cell_rows_skipped", "INTEGER"), ("max_cell_spread_v", "REAL"),
        ("over_threshold_s", "REAL"), ("cell_intervals", "TEXT DEFAULT '[]'"),
    ],
}

# 建表脚本中的索引：旧库表结构不完整时建索引可能失败，需容错
_INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_samples_log ON samples(log_id, t)",
    "CREATE INDEX IF NOT EXISTS idx_cycles_battery ON cycles(battery_id, id)",
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """启动时调用：确保数据目录存在并建表，并为旧库补齐单体分析相关列。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # executescript 遇索引语句失败会整体中止；逐条执行 CREATE TABLE
        # 可让旧库中已存在的表（CREATE TABLE IF NOT EXISTS）安全跳过。
        for stmt in SCHEMA.split(";"):
            head = stmt.strip().upper()
            if head.startswith("CREATE TABLE"):
                conn.execute(stmt)
        # 旧库增量补列（CREATE TABLE IF NOT EXISTS 不会为既有表补列）
        _migrate_columns(conn)
        for stmt in _INDEX_STATEMENTS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()
