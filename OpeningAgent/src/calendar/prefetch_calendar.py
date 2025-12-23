"""TradingEconomics 경제 캘린더 프리페치 (Option B).

OpeningAgent 실행 시 state.date(YYYYMMDD, ET 기준)를 입력으로 받아:
- `data/opening/calendar.csv` (id, est_date, title)
- `data/opening/calendar.json` (상세 이벤트)

를 생성한다.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Python zoneinfo unavailable: {exc}")

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # OpeningAgent/
DATA_DIR = BASE_DIR / "data/opening"

CALENDAR_CSV_PATH = DATA_DIR / "calendar.csv"
CALENDAR_JSON_PATH = DATA_DIR / "calendar.json"

CALENDAR_URL = "https://tradingeconomics.com/calendar"
BASE_URL = "https://tradingeconomics.com"

WINDOW_TZ = "America/New_York"
WINDOW_DAYS_BACK = 7
WINDOW_DAYS_FORWARD = 7
COUNTRY_COOKIE = "usa"
IMPACTS_ALL = "1,2,3"
IMPACTS_HIGH = "3"
SITE_TIMEZONE_HINT = "UTC"

SECTION_SLUG_TO_LABEL: Dict[str, str] = {
    "interest-rate": "Interest Rate",
    "inflation": "Prices & Inflation",
    "labour": "Labour Market",
    "gdp": "GDP Growth",
    "trade": "Foreign Trade",
    "government": "Government",
    "business": "Business Confidence",
    "consumer": "Consumer Sentiment",
    "housing": "Housing Market",
    "bonds": "Bond Auctions",
    "energy": "Energy",
    "holidays": "Holidays",
    "earnings": "Earnings",
}

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def _normalize_cell(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    cleaned = " ".join(text.split()).strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in {"--", "n/a", "na"}:
        return None
    return cleaned


def _end_of_next_month(d: date) -> date:
    first_next = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    first_after = (first_next + timedelta(days=32)).replace(day=1)
    return first_after - timedelta(days=1)


def _window_for_anchor_date(anchor_date_et: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(WINDOW_TZ)
    start = datetime.combine(anchor_date_et - timedelta(days=WINDOW_DAYS_BACK), time.min).replace(tzinfo=tz)
    end = datetime.combine(anchor_date_et + timedelta(days=WINDOW_DAYS_FORWARD), time.max).replace(tzinfo=tz)
    return start, end


def _extract_row_date_from_time_cell_class(time_td) -> Optional[date]:
    class_attr = time_td.get("class")
    if isinstance(class_attr, list):
        class_str = " ".join(class_attr)
    else:
        class_str = class_attr or ""
    match = _DATE_PATTERN.search(class_str)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _extract_importance_from_time_span(time_td) -> Optional[int]:
    span = time_td.find("span")
    if span is None:
        return None
    class_attr = span.get("class", [])
    tokens = class_attr.split() if isinstance(class_attr, str) else class_attr
    for token in tokens:
        if token.startswith("importance-"):
            suffix = token.split("-")[-1]
            if suffix.isdigit():
                return int(suffix)
        if token.startswith("calendar-date-"):
            suffix = token.split("-")[-1]
            if suffix.isdigit():
                return int(suffix)
    return None


def _parse_time_utc(raw_time: Optional[str], d: Optional[date], site_tz: str) -> Optional[datetime]:
    raw_time = _normalize_cell(raw_time)
    if raw_time is None or d is None:
        return None

    lowered = raw_time.lower()
    if lowered in {"all day", "all-day", "tentative"}:
        return None

    try:
        t = datetime.strptime(raw_time, "%I:%M %p").time()
    except Exception:
        return None

    tzinfo = ZoneInfo(site_tz) if site_tz else timezone.utc
    dt_local = datetime.combine(d, t).replace(tzinfo=tzinfo)
    return dt_local.astimezone(timezone.utc)


def _find_title_td_index(tds: Sequence) -> Optional[int]:
    for idx, td in enumerate(tds):
        if td.select_one("a.calendar-event") is not None:
            return idx
    for idx, td in enumerate(tds):
        style = td.get("style") or ""
        if "max-width" in style and "overflow" in style:
            return idx
    return None


def _extract_value_cells(tds: Sequence, title_td_idx: Optional[int]):
    if title_td_idx is None:
        return None, None, None, None
    start = title_td_idx + 1
    if len(tds) <= start:
        return None, None, None, None
    actual_td = tds[start] if len(tds) > start else None
    previous_td = tds[start + 1] if len(tds) > start + 1 else None
    consensus_td = tds[start + 2] if len(tds) > start + 2 else None
    forecast_td = tds[start + 3] if len(tds) > start + 3 else None
    return actual_td, previous_td, consensus_td, forecast_td


@dataclass(frozen=True)
class CalendarRequest:
    start_date: date
    end_date: date
    country_cookie: str
    importance_cookie: str

    def cookies(self) -> Dict[str, str]:
        return {
            "calendar-countries": self.country_cookie,
            "calendar-importance": self.importance_cookie,
            "calendar-range": "0",
            "cal-custom-range": f"{self.start_date:%Y-%m-%d}|{self.end_date:%Y-%m-%d}",
        }


def _fetch_html(url: str, cookies: Dict[str, str]) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, cookies=cookies, timeout=30)
    resp.raise_for_status()
    return resp.text


def _fetch_calendar_html(req: CalendarRequest) -> str:
    return _fetch_html(CALENDAR_URL, req.cookies())


def _fetch_calendar_html_for_path(path: str, req: CalendarRequest) -> str:
    return _fetch_html(f"{BASE_URL}{path}", req.cookies())


def _build_section_map(req: CalendarRequest) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for slug, label in SECTION_SLUG_TO_LABEL.items():
        html = _fetch_calendar_html_for_path(f"/calendar/{slug}", req)
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("#calendar tr[data-id]"):
            te_id = _normalize_cell(tr.get("data-id"))
            if te_id and te_id not in out:
                out[te_id] = label
    return out


def _parse_calendar_rows(html: str, section_by_te_id: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, Any]] = []

    last_event_date: Optional[date] = None
    for tr in soup.select("#calendar tr[data-id]"):
        tds = tr.find_all("td")
        if not tds:
            continue

        time_td = tds[0]
        event_date = _extract_row_date_from_time_cell_class(time_td)
        if event_date is None:
            event_date = last_event_date
        else:
            last_event_date = event_date

        raw_time = _normalize_cell(time_td.get_text(" ", strip=True))
        dt_utc = _parse_time_utc(raw_time, event_date, SITE_TIMEZONE_HINT)
        # 취소/무효: 시간 파싱 불가능하면 제외
        if dt_utc is None:
            continue

        dt_est = dt_utc.astimezone(ZoneInfo(WINDOW_TZ))

        te_id = _normalize_cell(tr.get("data-id"))
        series = _normalize_cell(tr.get("data-category"))
        importance = _extract_importance_from_time_span(time_td)

        # Title: prefer data-event
        title = _normalize_cell(tr.get("data-event"))
        if not title:
            anchor = tr.select_one("a.calendar-event")
            title = _normalize_cell(anchor.get_text(" ", strip=True) if anchor else None) or series

        title_td_idx = _find_title_td_index(tds)
        actual_td, previous_td, consensus_td, forecast_td = _extract_value_cells(tds, title_td_idx)

        actual_node = tr.select_one("span#actual")
        actual = _normalize_cell(actual_node.get_text(" ", strip=True) if actual_node else (actual_td.get_text(" ", strip=True) if actual_td else None))

        previous_node = tr.select_one("span#previous")
        previous = _normalize_cell(previous_node.get_text(" ", strip=True) if previous_node else (previous_td.get_text(" ", strip=True) if previous_td else None))
        if previous:
            previous = previous.replace("®", "").strip() or None

        consensus_node = tr.select_one("a#consensus")
        consensus = _normalize_cell(consensus_node.get_text(" ", strip=True) if consensus_node else (consensus_td.get_text(" ", strip=True) if consensus_td else None))

        forecast = _normalize_cell(forecast_td.get_text(" ", strip=True) if forecast_td else None)

        event: Dict[str, Any] = {
            "event_id": te_id
            or hashlib.sha1(
                "|".join(
                    [
                        event_date.isoformat() if event_date else "",
                        raw_time or "",
                        title or "",
                        series or "",
                        str(importance or ""),
                    ]
                ).encode("utf-8")
            ).hexdigest()[:16],
            "utc": dt_utc.isoformat(),
            "est": dt_est.isoformat(),
            "title": title,
            "category": section_by_te_id.get(te_id) if (section_by_te_id and te_id) else None,
            "series": series,
            "importance": importance,
        }

        if actual:
            event["actual"] = actual
        if previous:
            event["previous"] = previous
        if consensus:
            event["consensus"] = consensus
        if forecast:
            event["forecast"] = forecast

        rows.append(event)

    rows.sort(key=lambda e: (e.get("utc") is None, e.get("utc") or "", e.get("title") or ""))
    return rows


def _dedupe_by_id(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for e in events:
        eid = str(e.get("event_id") or "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        out.append(e)
    return out


def _write_calendar_csv(events: Sequence[Dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CALENDAR_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "est_date", "title"])
        writer.writeheader()
        for e in events:
            est_iso = e.get("est") or ""
            try:
                est_date = datetime.fromisoformat(est_iso).strftime("%Y%m%d") if est_iso else ""
            except ValueError:
                est_date = ""
            writer.writerow({"id": e.get("event_id"), "est_date": est_date, "title": e.get("title")})


def _write_calendar_json(meta: Dict[str, Any], events: Sequence[Dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {**meta, "events": list(events)}
    CALENDAR_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def prefetch_calendar(date_yyyymmdd: str) -> Dict[str, Any]:
    """캘린더를 스크래핑하고 파일로 저장한다."""
    anchor = datetime.strptime(date_yyyymmdd, "%Y%m%d").date()
    start_et, end_et = _window_for_anchor_date(anchor)
    end_next_month = _end_of_next_month(end_et.date())

    base_req = CalendarRequest(
        start_date=start_et.date(),
        end_date=end_et.date(),
        country_cookie=COUNTRY_COOKIE,
        importance_cookie=IMPACTS_ALL,
    )
    base_html = _fetch_calendar_html(base_req)
    base_section_map = _build_section_map(base_req)
    base_events = _parse_calendar_rows(base_html, section_by_te_id=base_section_map)

    hi_req = CalendarRequest(
        start_date=start_et.date(),
        end_date=end_next_month,
        country_cookie=COUNTRY_COOKIE,
        importance_cookie=IMPACTS_HIGH,
    )
    hi_html = _fetch_calendar_html(hi_req)
    hi_section_map = _build_section_map(hi_req)
    hi_events = [e for e in _parse_calendar_rows(hi_html, section_by_te_id=hi_section_map) if e.get("importance") == 3]

    merged = _dedupe_by_id([*base_events, *hi_events])

    meta = {
        "schema_version": "1.0",
        "source": "tradingeconomics",
        "target_url": CALENDAR_URL,
        "country": "United States",
        "window": {"start_et": start_et.isoformat(), "end_et": end_et.isoformat()},
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    _write_calendar_csv(merged)
    _write_calendar_json(meta, merged)

    logger.info("calendar prefetch 완료: %d events -> %s, %s", len(merged), CALENDAR_CSV_PATH, CALENDAR_JSON_PATH)
    return {"count": len(merged), "csv": str(CALENDAR_CSV_PATH), "json": str(CALENDAR_JSON_PATH)}

