"""Calendar tool using shared cache."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from langchain_core.tools import tool

from shared.config import get_calendar_json_path

logger = logging.getLogger(__name__)


def _load_calendar_json() -> Dict[str, Any]:
    path = get_calendar_json_path()
    if not path.exists():
        logger.warning("calendar.json 없음: %s", path)
        raise FileNotFoundError(f"calendar.json이 없습니다: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_dates(value: Union[str, List[str]]) -> List[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = [part.strip() for part in str(value).split(",")]
    out: List[str] = []
    for d in raw:
        if not d:
            continue
        datetime.strptime(d, "%Y%m%d")
        out.append(d)
    return out


def _event_est_date_yyyymmdd(event: Dict[str, Any]) -> Optional[str]:
    est_iso = event.get("est")
    if not isinstance(est_iso, str) or not est_iso:
        return None
    try:
        dt = datetime.fromisoformat(est_iso)
    except ValueError:
        return None
    return dt.strftime("%Y%m%d")


@tool
def get_calendar(id: Optional[str] = None, date: Optional[Union[str, List[str]]] = None) -> Dict[str, Any]:
    """Fetch calendar events by id or date(s)."""
    if not id and date is None:
        raise ValueError("id 또는 date 중 하나는 반드시 지정해야 합니다.")

    logger.info("get_calendar 호출: id=%s, date=%s", id, date)
    payload = _load_calendar_json()
    events: List[Dict[str, Any]] = payload.get("events", []) or []

    if id:
        for e in events:
            if str(e.get("event_id")) == str(id):
                logger.info("get_calendar 결과: id=%s found", id)
                return {"mode": "id", "found": True, "event": e}
        logger.info("get_calendar 결과: id=%s not found", id)
        return {"mode": "id", "found": False, "event": None}

    dates = _normalize_dates(date)  # type: ignore[arg-type]
    date_set = set(dates)
    filtered: List[Dict[str, Any]] = []
    for e in events:
        d = _event_est_date_yyyymmdd(e)
        if d and d in date_set:
            filtered.append(e)

    logger.info("get_calendar 결과: dates=%s count=%d", dates, len(filtered))
    return {"mode": "date", "count": len(filtered), "dates": dates, "events": filtered}
