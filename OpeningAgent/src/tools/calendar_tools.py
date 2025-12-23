"""경제 캘린더 조회 Tool.

Prefetch 단계에서 생성되는 `data/opening/calendar.json`을 대상으로,
id / date(단일/복수) 조회를 하나의 Tool로 제공한다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from langchain_core.tools import tool

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # OpeningAgent/
CALENDAR_JSON_PATH = BASE_DIR / "data/opening/calendar.json"


def _load_calendar_json() -> Dict[str, Any]:
    if not CALENDAR_JSON_PATH.exists():
        raise FileNotFoundError(f"calendar.json이 없습니다: {CALENDAR_JSON_PATH}")
    with open(CALENDAR_JSON_PATH, "r", encoding="utf-8") as f:
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
        datetime.strptime(d, "%Y%m%d")  # validate
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
def get_calendar(
    id: Optional[str] = None,
    date: Optional[Union[str, List[str]]] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """경제 캘린더 이벤트를 조회한다.

    입력은 아래 중 하나를 사용한다:
    - id="417228"
    - date="20251223"
    - date=["20251223","20251224"]
    - date="20251223,20251224"
    """
    if not id and date is None:
        raise ValueError("id 또는 date 중 하나는 반드시 지정해야 합니다.")

    payload = _load_calendar_json()
    events: List[Dict[str, Any]] = payload.get("events", []) or []

    if id:
        for e in events:
            if str(e.get("event_id")) == str(id):
                return {"mode": "id", "found": True, "event": e}
        return {"mode": "id", "found": False, "event": None}

    dates = _normalize_dates(date)  # type: ignore[arg-type]
    date_set = set(dates)
    filtered: List[Dict[str, Any]] = []
    for e in events:
        d = _event_est_date_yyyymmdd(e)
        if d and d in date_set:
            filtered.append(e)
            if len(filtered) >= max(1, int(limit)):
                break

    return {"mode": "date", "count": len(filtered), "dates": dates, "events": filtered}

