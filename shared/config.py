"""Shared path helpers and cache/temp configuration."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache"
TEMP_DIR = ROOT_DIR / "temp"


def normalize_date(date_str: str) -> str:
    """Normalize date to YYYYMMDD."""
    raw = str(date_str).strip()
    if "-" in raw:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y%m%d")
    return datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")


def set_briefing_date(date_str: str) -> str:
    """Set BRIEFING_DATE after normalizing."""
    normalized = normalize_date(date_str)
    os.environ["BRIEFING_DATE"] = normalized
    return normalized


def get_briefing_date() -> str:
    """Return BRIEFING_DATE from environment."""
    date = os.environ.get("BRIEFING_DATE")
    if not date:
        raise ValueError("BRIEFING_DATE 환경변수가 설정되지 않았습니다.")
    return normalize_date(date)


def get_cache_dir(date: Optional[str] = None) -> Path:
    """Return cache/{YYYYMMDD} directory."""
    normalized = normalize_date(date) if date else get_briefing_date()
    return CACHE_DIR / normalized


def ensure_cache_dir(date: Optional[str] = None) -> Path:
    """Ensure cache/{YYYYMMDD} directory exists and return it."""
    cache_dir = get_cache_dir(date)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def cleanup_cache_dir(date: Optional[str] = None) -> None:
    """Remove cache/{YYYYMMDD} directory."""
    cache_dir = get_cache_dir(date)
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)


def ensure_temp_dir() -> Path:
    """Ensure temp directory exists and return it."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR


def get_news_list_path(date: Optional[str] = None) -> Path:
    return get_cache_dir(date) / "news_list.json"


def get_titles_path(date: Optional[str] = None) -> Path:
    return get_cache_dir(date) / "titles.txt"


def get_bodies_dir(date: Optional[str] = None) -> Path:
    return get_cache_dir(date) / "bodies"


def get_calendar_json_path(date: Optional[str] = None) -> Path:
    return get_cache_dir(date) / "calendar.json"


def get_calendar_csv_path(date: Optional[str] = None) -> Path:
    return get_cache_dir(date) / "calendar.csv"


def get_market_context_path(date: Optional[str] = None) -> Path:
    return get_cache_dir(date) / "market_context.json"


def get_temp_opening_path() -> Path:
    return TEMP_DIR / "opening.json"


def get_temp_theme_path() -> Path:
    return TEMP_DIR / "theme.json"


def get_temp_closing_path() -> Path:
    return TEMP_DIR / "closing.json"


def get_temp_ticker_pipeline_path() -> Path:
    return TEMP_DIR / "ticker_pipeline.json"
