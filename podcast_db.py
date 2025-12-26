"""SQLite index for @Podcast outputs.

This module maintains a small SQLite DB that makes it easy for a web server to
list and serve podcast artifacts stored under `Podcast/{date}/`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


DEFAULT_DB_FILENAME = "podcast.db"
DEFAULT_TABLE_NAME = "podcasts"


def get_default_db_path(repo_root: Path) -> Path:
    return repo_root / "Podcast" / DEFAULT_DB_FILENAME


def utc_iso_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_schema(conn: sqlite3.Connection, *, table_name: str = DEFAULT_TABLE_NAME) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          date TEXT PRIMARY KEY,
          nutshell TEXT NOT NULL DEFAULT '',
          user_tickers TEXT NOT NULL DEFAULT '[]',
          script_saved_at TEXT,
          tts_done INTEGER NOT NULL DEFAULT 0,
          final_saved_at TEXT
        );
        """
    )
    conn.commit()


def upsert_script_row(
    *,
    db_path: Path,
    date: str,
    nutshell: str,
    user_tickers: Sequence[str],
    script_saved_at: str,
    table_name: str = DEFAULT_TABLE_NAME,
) -> None:
    user_tickers_json = json.dumps(list(user_tickers), ensure_ascii=False)
    with _connect(db_path) as conn:
        _ensure_schema(conn, table_name=table_name)
        conn.execute(
            f"""
            INSERT INTO {table_name} (date, nutshell, user_tickers, script_saved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
              nutshell = excluded.nutshell,
              user_tickers = excluded.user_tickers,
              script_saved_at = excluded.script_saved_at;
            """,
            (date, str(nutshell or ""), user_tickers_json, script_saved_at),
        )
        conn.commit()


def update_tts_row(
    *,
    db_path: Path,
    date: str,
    final_saved_at: str,
    nutshell: Optional[str] = None,
    user_tickers: Optional[Sequence[str]] = None,
    script_saved_at: Optional[str] = None,
    table_name: str = DEFAULT_TABLE_NAME,
) -> None:
    with _connect(db_path) as conn:
        _ensure_schema(conn, table_name=table_name)
        conn.execute(f"INSERT INTO {table_name} (date) VALUES (?) ON CONFLICT(date) DO NOTHING;", (date,))

        set_exprs = ["tts_done = 1", "final_saved_at = ?"]
        params: list[object] = [final_saved_at]

        if nutshell is not None:
            set_exprs.append("nutshell = ?")
            params.append(str(nutshell))
        if user_tickers is not None:
            set_exprs.append("user_tickers = ?")
            params.append(json.dumps(list(user_tickers), ensure_ascii=False))
        if script_saved_at is not None:
            set_exprs.append("script_saved_at = ?")
            params.append(script_saved_at)

        params.append(date)
        conn.execute(f"UPDATE {table_name} SET {', '.join(set_exprs)} WHERE date = ?;", params)
        conn.commit()

