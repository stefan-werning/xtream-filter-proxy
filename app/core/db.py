from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  kind          TEXT NOT NULL,
  item_id       TEXT NOT NULL,
  name          TEXT NOT NULL,
  category_id   TEXT,
  container_ext TEXT,
  raw_json      TEXT,
  first_seen    INTEGER NOT NULL,
  last_seen     INTEGER NOT NULL,
  removed_at    INTEGER,
  PRIMARY KEY (kind, item_id)
);

CREATE TABLE IF NOT EXISTS audio_tracks (
  kind       TEXT NOT NULL,
  item_id    TEXT NOT NULL,
  track_idx  INTEGER NOT NULL,
  language   TEXT,
  title      TEXT,
  codec      TEXT,
  channels   INTEGER,
  match_text TEXT NOT NULL,
  PRIMARY KEY (kind, item_id, track_idx)
);

CREATE TABLE IF NOT EXISTS probe_state (
  kind        TEXT NOT NULL,
  item_id     TEXT NOT NULL,
  status      TEXT NOT NULL,
  source      TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_try    INTEGER,
  next_try    INTEGER,
  error       TEXT,
  priority    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (kind, item_id)
);

CREATE TABLE IF NOT EXISTS categories (
  kind          TEXT NOT NULL,
  category_id   TEXT NOT NULL,
  category_name TEXT NOT NULL,
  parent_id     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (kind, category_id)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_overrides (
  kind      TEXT NOT NULL,
  item_id   TEXT NOT NULL,
  added_at  INTEGER NOT NULL,
  PRIMARY KEY (kind, item_id)
);

CREATE TABLE IF NOT EXISTS crawl_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  level TEXT,
  message TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_kind_removed ON items(kind, removed_at);
CREATE INDEX IF NOT EXISTS idx_items_kind_category ON items(kind, category_id, removed_at);
CREATE INDEX IF NOT EXISTS idx_probe_status_next ON probe_state(status, next_try);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate(self) -> None:
        with self._init_lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                self._add_column_if_missing(conn, "items", "raw_json", "TEXT")
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return self._local.conn

    @contextmanager
    def cursor(self):
        conn = self.conn
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        cur = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def log(self, level: str, message: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO crawl_log (ts, level, message) VALUES (?, ?, ?)",
                (int(time.time()), level, message),
            )

    def recent_logs(self, limit: int = 50) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM crawl_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()

    def rotate_logs(self, max_age_days: int = 30, max_rows: int = 5000) -> int:
        """Deletes crawl_log rows older than max_age_days, then -- if still
        over max_rows -- trims down to the most recent max_rows. Returns the
        number of rows deleted.
        """
        cutoff = int(time.time()) - max_age_days * 86400
        with self.cursor() as cur:
            cur.execute("DELETE FROM crawl_log WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount

            total = cur.execute("SELECT COUNT(*) c FROM crawl_log").fetchone()["c"]
            if total > max_rows:
                cur.execute(
                    "DELETE FROM crawl_log WHERE id NOT IN ("
                    "SELECT id FROM crawl_log ORDER BY id DESC LIMIT ?)",
                    (max_rows,),
                )
                deleted += cur.rowcount
        return deleted
