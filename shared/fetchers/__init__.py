"""Shared prefetch helpers."""

from __future__ import annotations

from datetime import date as date_type
from pathlib import Path

from shared.config import ensure_cache_dir

from . import calendar, market_context, news


def prefetch_all(anchor_date: date_type, cache_dir: Path | None = None) -> None:
    """Run all prefetchers into cache_dir."""
    target_dir = cache_dir or ensure_cache_dir(anchor_date.strftime("%Y%m%d"))
    target_dir.mkdir(parents=True, exist_ok=True)

    news.prefetch_news(today=anchor_date, cache_dir=target_dir)
    calendar.prefetch_calendar(anchor_date, cache_dir=target_dir, days_back=7, days_forward=7)
    market_context.generate(anchor_date, cache_dir=target_dir)


__all__ = ["prefetch_all", "news", "calendar", "market_context"]
