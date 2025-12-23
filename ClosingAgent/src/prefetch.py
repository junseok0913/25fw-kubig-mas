"""ClosingAgent prefetch 유틸리티.

경제 캘린더(TradingEconomics)만 프리페치한다.
- state.date(YYYYMMDD, ET 기준)를 앵커로 `오늘~+7일`(impact 1~3) + `impact=3`은 다음달 말까지 확장 수집
- 결과를 ClosingAgent/data/closing/calendar.csv (id, est_date, title) + calendar.json (상세)로 저장

주의:
- 프롬프트로 주입할 컨텍스트는 "오늘과 이후" 리스트만 필요하므로, 저장 결과도 기본적으로 오늘 이후 이벤트만 포함한다.
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
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Python zoneinfo unavailable: {exc}")

BASE_DIR = Path(__file__).resolve().parent.parent  # ClosingAgent/
ROOT_DIR = BASE_DIR.parent  # repo root

DATA_DIR = BASE_DIR / "data/closing"
CALENDAR_CSV_PATH = DATA_DIR / "calendar.csv"
CALENDAR_JSON_PATH = DATA_DIR / "calendar.json"


def _load_env() -> None:
    """리포 루트 .env를 로드한다 (독립 실행 시 필요)."""
    load_dotenv(ROOT_DIR / ".env", override=False)


#
# TradingEconomics 경제 캘린더 프리페치
#

CALENDAR_URL = "https://tradingeconomics.com/calendar"
CALENDAR_BASE_URL = "https://tradingeconomics.com"

CAL_WINDOW_TZ = "America/New_York"
CAL_WINDOW_DAYS_BACK = 0
CAL_WINDOW_DAYS_FORWARD = 7
CAL_COUNTRY_COOKIE = "usa"
CAL_IMPACTS_ALL = "1,2,3"
CAL_IMPACTS_HIGH = "3"
CAL_SITE_TIMEZONE_HINT = "UTC"

CAL_SECTION_SLUG_TO_LABEL: Dict[str, str] = {
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

_CAL_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def _cal_normalize_cell(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    cleaned = " ".join(text.split()).strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in {"--", "n/a", "na"}:
        return None
    return cleaned


def _cal_end_of_next_month(d: date) -> date:
    first_next = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    first_after = (first_next + timedelta(days=32)).replace(day=1)
    return first_after - timedelta(days=1)


def _cal_window_for_anchor_date(anchor_date_et: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(CAL_WINDOW_TZ)
    start = datetime.combine(anchor_date_et - timedelta(days=CAL_WINDOW_DAYS_BACK), time.min).replace(tzinfo=tz)
    end = datetime.combine(anchor_date_et + timedelta(days=CAL_WINDOW_DAYS_FORWARD), time.max).replace(tzinfo=tz)
    return start, end


def _cal_extract_row_date_from_time_cell_class(time_td) -> Optional[date]:
    class_attr = time_td.get("class")
    if isinstance(class_attr, list):
        class_str = " ".join(class_attr)
    else:
        class_str = class_attr or ""
    match = _CAL_DATE_PATTERN.search(class_str)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _cal_extract_importance_from_time_span(time_td) -> Optional[int]:
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


def _cal_parse_time_utc(raw_time: Optional[str], d: Optional[date], site_tz: str) -> Optional[datetime]:
    raw_time = _cal_normalize_cell(raw_time)
    if raw_time is None or d is None:
        return None

    lowered = raw_time.lower()
    if lowered in {"all day", "all-day", "tentative"}:
        return None

    try:
        parsed_time = datetime.strptime(raw_time, "%I:%M %p").time()
    except Exception:
        return None

    tzinfo = ZoneInfo(site_tz) if site_tz else timezone.utc
    dt_local = datetime.combine(d, parsed_time).replace(tzinfo=tzinfo)
    return dt_local.astimezone(timezone.utc)


def _cal_find_title_td_index(tds: Sequence) -> Optional[int]:
    for idx, td in enumerate(tds):
        if td.select_one("a.calendar-event") is not None:
            return idx
    for idx, td in enumerate(tds):
        style = td.get("style") or ""
        if "max-width" in style and "overflow" in style:
            return idx
    return None


def _cal_extract_value_cells(tds: Sequence, title_td_idx: Optional[int]):
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
class _CalendarRequest:
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


def _cal_fetch_html(url: str, cookies: Dict[str, str]) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, cookies=cookies, timeout=30)
    resp.raise_for_status()
    return resp.text


def _cal_fetch_calendar_html(req: _CalendarRequest) -> str:
    return _cal_fetch_html(CALENDAR_URL, req.cookies())


def _cal_fetch_calendar_html_for_path(path: str, req: _CalendarRequest) -> str:
    return _cal_fetch_html(f"{CALENDAR_BASE_URL}{path}", req.cookies())


def _cal_build_section_map(req: _CalendarRequest) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for slug, label in CAL_SECTION_SLUG_TO_LABEL.items():
        html = _cal_fetch_calendar_html_for_path(f"/calendar/{slug}", req)
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("#calendar tr[data-id]"):
            te_id = _cal_normalize_cell(tr.get("data-id"))
            if te_id and te_id not in out:
                out[te_id] = label
    return out


def _cal_parse_calendar_rows(html: str, section_by_te_id: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, Any]] = []

    last_event_date: Optional[date] = None
    for tr in soup.select("#calendar tr[data-id]"):
        tds = tr.find_all("td")
        if not tds:
            continue

        time_td = tds[0]
        event_date = _cal_extract_row_date_from_time_cell_class(time_td)
        if event_date is None:
            event_date = last_event_date
        else:
            last_event_date = event_date

        raw_time = _cal_normalize_cell(time_td.get_text(" ", strip=True))
        dt_utc = _cal_parse_time_utc(raw_time, event_date, CAL_SITE_TIMEZONE_HINT)
        if dt_utc is None:
            continue

        dt_est = dt_utc.astimezone(ZoneInfo(CAL_WINDOW_TZ))

        te_id = _cal_normalize_cell(tr.get("data-id"))
        series = _cal_normalize_cell(tr.get("data-category"))
        importance = _cal_extract_importance_from_time_span(time_td)

        title = _cal_normalize_cell(tr.get("data-event"))
        if not title:
            anchor = tr.select_one("a.calendar-event")
            title = _cal_normalize_cell(anchor.get_text(" ", strip=True) if anchor else None) or series

        title_td_idx = _cal_find_title_td_index(tds)
        actual_td, previous_td, consensus_td, forecast_td = _cal_extract_value_cells(tds, title_td_idx)

        actual_node = tr.select_one("span#actual")
        actual = _cal_normalize_cell(
            actual_node.get_text(" ", strip=True) if actual_node else (actual_td.get_text(" ", strip=True) if actual_td else None)
        )

        previous_node = tr.select_one("span#previous")
        previous = _cal_normalize_cell(
            previous_node.get_text(" ", strip=True) if previous_node else (previous_td.get_text(" ", strip=True) if previous_td else None)
        )
        if previous:
            previous = previous.replace("®", "").strip() or None

        consensus_node = tr.select_one("a#consensus")
        consensus = _cal_normalize_cell(
            consensus_node.get_text(" ", strip=True) if consensus_node else (consensus_td.get_text(" ", strip=True) if consensus_td else None)
        )

        forecast = _cal_normalize_cell(forecast_td.get_text(" ", strip=True) if forecast_td else None)

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


def _cal_dedupe_by_id(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for e in events:
        eid = str(e.get("event_id") or "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        out.append(e)
    return out


def _cal_event_est_date_yyyymmdd(event: Dict[str, Any]) -> Optional[str]:
    est_iso = event.get("est") or ""
    if not isinstance(est_iso, str) or not est_iso:
        return None
    try:
        return datetime.fromisoformat(est_iso).strftime("%Y%m%d")
    except ValueError:
        return None


def _cal_write_calendar_csv(events: Sequence[Dict[str, Any]], min_est_date_yyyymmdd: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CALENDAR_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "est_date", "title"])
        writer.writeheader()
        for e in events:
            est_date = _cal_event_est_date_yyyymmdd(e) or ""
            if est_date and est_date < min_est_date_yyyymmdd:
                continue
            writer.writerow({"id": e.get("event_id"), "est_date": est_date, "title": e.get("title")})


def _cal_write_calendar_json(meta: Dict[str, Any], events: Sequence[Dict[str, Any]], min_est_date_yyyymmdd: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    filtered = []
    for e in events:
        est_date = _cal_event_est_date_yyyymmdd(e) or ""
        if est_date and est_date < min_est_date_yyyymmdd:
            continue
        filtered.append(e)
    payload = {**meta, "events": filtered}
    CALENDAR_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def prefetch_calendar(today: date) -> Dict[str, Any]:
    """TradingEconomics 경제 캘린더를 스크래핑하고 파일로 저장한다."""
    _load_env()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    start_et, end_et = _cal_window_for_anchor_date(today)
    end_next_month = _cal_end_of_next_month(end_et.date())
    min_est_date = today.strftime("%Y%m%d")

    base_req = _CalendarRequest(
        start_date=start_et.date(),
        end_date=end_et.date(),
        country_cookie=CAL_COUNTRY_COOKIE,
        importance_cookie=CAL_IMPACTS_ALL,
    )
    base_html = _cal_fetch_calendar_html(base_req)
    base_section_map = _cal_build_section_map(base_req)
    base_events = _cal_parse_calendar_rows(base_html, section_by_te_id=base_section_map)

    hi_req = _CalendarRequest(
        start_date=start_et.date(),
        end_date=end_next_month,
        country_cookie=CAL_COUNTRY_COOKIE,
        importance_cookie=CAL_IMPACTS_HIGH,
    )
    hi_html = _cal_fetch_calendar_html(hi_req)
    hi_section_map = _cal_build_section_map(hi_req)
    hi_events = [e for e in _cal_parse_calendar_rows(hi_html, section_by_te_id=hi_section_map) if e.get("importance") == 3]

    merged = _cal_dedupe_by_id([*base_events, *hi_events])

    meta = {
        "schema_version": "1.0",
        "source": "tradingeconomics",
        "target_url": CALENDAR_URL,
        "country": "United States",
        "window": {"start_et": start_et.isoformat(), "end_et": end_et.isoformat()},
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    _cal_write_calendar_csv(merged, min_est_date_yyyymmdd=min_est_date)
    _cal_write_calendar_json(meta, merged, min_est_date_yyyymmdd=min_est_date)

    logger.info("calendar prefetch 완료: %d events -> %s, %s", len(merged), CALENDAR_CSV_PATH, CALENDAR_JSON_PATH)
    return {"count": len(merged), "csv": str(CALENDAR_CSV_PATH), "json": str(CALENDAR_JSON_PATH)}


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    prefetch_calendar(date.today())

