"""News prefetcher (DynamoDB)."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz
from boto3.dynamodb.conditions import Key
from dotenv import load_dotenv

from shared.config import ROOT_DIR
from shared.utils.aws import get_dynamo_table

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


def _ensure_dirs(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    bodies_dir = cache_dir / "bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)
    return bodies_dir


def _get_current_et_date() -> date:
    return datetime.now(tz=ET).date()


def _time_window_et(today: date) -> tuple[datetime, datetime]:
    start = datetime.combine(today - timedelta(days=1), time(16, 0, tzinfo=ET))
    end = datetime.combine(today, time(18, 0, tzinfo=ET))
    return start, end


def _to_utc_ms(dt_et: datetime) -> int:
    dt_utc = dt_et.astimezone(timezone.utc)
    return int(dt_utc.timestamp() * 1000)


def _partition_keys(today: date) -> List[str]:
    return [f"UTC#{(today - timedelta(days=offset)).isoformat()}" for offset in range(0, 3)]


def _load_env() -> None:
    load_dotenv(ROOT_DIR / ".env", override=False)


def _normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    def convert(val: Any) -> Any:
        if isinstance(val, list):
            return [convert(v) for v in val]
        if isinstance(val, dict):
            return {k: convert(v) for k, v in val.items()}
        if isinstance(val, Decimal):
            as_float = float(val)
            return int(as_float) if as_float.is_integer() else as_float
        return val

    return {k: convert(v) for k, v in item.items()}


def _extract_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    fields = _normalize_item(item)
    return {
        "pk": fields.get("pk"),
        "title": fields.get("title"),
        "url": fields.get("url"),
        "tickers": fields.get("tickers") or [],
        "publish_et_iso": fields.get("publish_et_iso"),
        "gsi_utc_pk": fields.get("gsi_utc_pk"),
        "utc_ms": fields.get("utc_ms"),
        "path": fields.get("path"),
    }


def _query_single_partition(table, gsi_pk: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs = {
        "IndexName": "gsi_latest_utc",
        "KeyConditionExpression": Key("gsi_utc_pk").eq(gsi_pk) & Key("utc_ms").between(start_ms, end_ms),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def prefetch_news(
    today: Optional[date] = None,
    cache_dir: Optional[Path] = None,
    table_name: Optional[str] = None,
    profile_name: Optional[str] = None,
    region_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch news metadata from DynamoDB and write to cache."""
    _load_env()
    if cache_dir is None:
        raise ValueError("cache_dir가 필요합니다.")
    _ensure_dirs(cache_dir)

    table = get_dynamo_table(table_name or os.getenv("NEWS_TABLE", "kubig-YahoofinanceNews"), profile_name, region_name)

    today_date = today or _get_current_et_date()
    start_et, end_et = _time_window_et(today_date)
    start_ms = _to_utc_ms(start_et)
    end_ms = _to_utc_ms(end_et)
    partitions = _partition_keys(today_date)

    all_items: List[Dict[str, Any]] = []
    for pk in partitions:
        part_items = _query_single_partition(table, pk, start_ms, end_ms)
        logger.info("Partition %s: %d items", pk, len(part_items))
        all_items.extend(part_items)

    articles = [_extract_fields(item) for item in all_items]
    payload = {
        "count": len(articles),
        "filters": {
            "today": today_date.isoformat(),
            "date_start_et": start_et.isoformat(),
            "date_end_et": end_et.isoformat(),
            "gsi_partitions": partitions,
        },
        "articles": articles,
    }

    news_list_path = cache_dir / "news_list.json"
    titles_path = cache_dir / "titles.txt"

    news_list_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    titles_path.write_text("\n".join(a.get("title", "") for a in articles if a.get("title")), encoding="utf-8")
    logger.info("뉴스 메타데이터 %d건을 저장했습니다: %s", len(articles), news_list_path)
    return payload
