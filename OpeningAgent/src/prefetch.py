"""DynamoDB에서 뉴스 메타데이터를 사전 수집하는 유틸리티.

- 지정된 날짜 기준으로 전일 16:00 ET ~ 당일 18:00 ET 범위의 뉴스를 조회
- gsi_utc_pk 파티션은 지정 날짜부터 3일치(오늘, 전일, 전전일)만 쿼리
- 결과를 data/opening/news_list.json, titles.txt로 저장

Note:
    날짜는 orchestrator에서 CLI 인자로 받아 prefetch_news(today=...)로 전달됩니다.
"""

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

from src.utils.aws_utils import get_dynamo_table

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")
BASE_DIR = Path(__file__).resolve().parent.parent  # OpeningAgent/
ROOT_DIR = BASE_DIR.parent  # repo root

DATA_DIR = BASE_DIR / "data/opening"
NEWS_LIST_PATH = DATA_DIR / "news_list.json"
TITLES_PATH = DATA_DIR / "titles.txt"
BODIES_DIR = DATA_DIR / "bodies"


def _ensure_dirs() -> None:
    """필요한 로컬 디렉터리를 생성한다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BODIES_DIR.mkdir(parents=True, exist_ok=True)


def _get_current_et_date() -> date:
    """현재 ET(미국 동부 시간) 날짜를 반환한다.
    
    Note:
        이 함수는 prefetch_news()에 today 파라미터가 전달되지 않았을 때만 사용됩니다.
        정상적인 실행에서는 orchestrator가 항상 날짜를 전달하므로 호출되지 않습니다.
    """
    return datetime.now(tz=ET).date()


def _time_window_et(today: date) -> tuple[datetime, datetime]:
    """전일 16:00 ET ~ 당일 18:00 ET 범위를 반환한다."""
    start = datetime.combine(today - timedelta(days=1), time(16, 0, tzinfo=ET))
    end = datetime.combine(today, time(18, 0, tzinfo=ET))
    return start, end


def _to_utc_ms(dt_et: datetime) -> int:
    """ET datetime을 UTC epoch milliseconds로 변환한다."""
    dt_utc = dt_et.astimezone(timezone.utc)
    return int(dt_utc.timestamp() * 1000)


def _partition_keys(today: date) -> List[str]:
    """지정 날짜 포함 최근 3일 UTC 파티션 키를 생성한다."""
    return [f"UTC#{(today - timedelta(days=offset)).isoformat()}" for offset in range(0, 3)]


def _load_env() -> None:
    """리포 루트 .env를 로드한다 (독립 실행 시 필요)."""
    load_dotenv(ROOT_DIR / ".env", override=False)


def _normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Decimal 등을 json 직렬화 가능한 타입으로 변환한다."""
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
    """news_list.json에 필요한 필드만 추린다."""
    fields = _normalize_item(item)
    return {
        "pk": fields.get("pk"),
        "title": fields.get("title"),
        "url": fields.get("url"),
        "tickers": fields.get("tickers") or [],
        "publish_et_iso": fields.get("publish_et_iso"),
        "gsi_utc_pk": fields.get("gsi_utc_pk"),
        "utc_ms": fields.get("utc_ms"),
        "path": fields.get("path"),  # S3 객체 키
    }


def _query_single_partition(table, gsi_pk: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    """지정한 GSI 파티션을 시간 범위로 Query한다."""
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
    table_name: Optional[str] = None,
    profile_name: Optional[str] = None,
    region_name: Optional[str] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """DynamoDB에서 뉴스 메타데이터를 조회해 로컬 캐시에 저장한다."""
    _load_env()
    _ensure_dirs()
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

    NEWS_LIST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    TITLES_PATH.write_text("\n".join(a.get("title", "") for a in articles if a.get("title")), encoding="utf-8")
    logger.info("뉴스 메타데이터 %d건을 저장했습니다: %s", len(articles), NEWS_LIST_PATH)
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    prefetch_news()
