from __future__ import annotations
import os
import sys
import hashlib
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Iterable, Tuple, Optional

try:
    from .yahoo_fetch import fetch_news_list
    from .aws_dynamo import put_items_idempotent, resolve_region
except ImportError:  # direct script execution: python upload_db.py
    from yahoo_fetch import fetch_news_list
    from aws_dynamo import put_items_idempotent, resolve_region




def _compute_pk(url: str) -> str:
    """원본 URL로부터 DynamoDB 파티션 키를 생성합니다.

    - URL에 숫자형 기사 ID(연속 6자리 이상)가 .html 앞에 있으면 접두사 `id#`
    - 아니면 접두사 `h#`
    - 해시는 sha256(url)의 앞 16자리 hex 사용
    """
    # Yahoo Finance의 숫자형 기사 ID 패턴 감지(경로 끝부분)
    has_numeric_id = bool(re.search(r"/news/[^?]*?(\d{6,})\.html", url))
    prefix = "id#" if has_numeric_id else "h#"
    digest16 = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{digest16}"


def compute_pk(url: str) -> str:
    """외부 모듈에서 재사용할 수 있는 pk 계산기.

    내부 구현은 _compute_pk와 동일합니다.
    """
    return _compute_pk(url)


def _now_fields() -> dict:
    """최근 24시간 조회를 위한 최소 타임스탬프 필드를 생성합니다.

    - utc_ms: 정렬/범위 조회용 epoch ms(UTC)
    - gsi_utc_pk: GSI 파티션 키로 사용할 UTC 일자 버킷(String)
    - et_iso: 표출용 America/New_York ISO 문자열(DST 반영)
    """
    now_utc = datetime.now(timezone.utc)
    est_tz = ZoneInfo("America/New_York")
    now_est = now_utc.astimezone(est_tz)

    return {
        "utc_ms": int(now_utc.timestamp() * 1000),
        "gsi_utc_pk": f"UTC#{now_utc.date().isoformat()}",
        "et_iso": now_est.isoformat(),
    }


def build_items(rows: Iterable[dict]) -> list[dict]:
    """크롤링 결과 행 리스트를 DynamoDB 아이템 문서로 변환합니다."""
    ts = _now_fields()
    items = []
    for r in rows:
        url = r.get("url", "").strip()
        title = (r.get("title") or "").strip()
        tickers = r.get("tickers") or []
        if not url or not title:
            continue
        pk = _compute_pk(url)
        doc = {
            "pk": pk,
            "title": title,
            "url": url,
            "tickers": list(tickers),
            # 본문 수집 단계에서 채워질 필드들
            "path": "",
            "publish_et_iso": "",
            "provider": "",
            "related_articles": [],
            **ts,
        }
        items.append(doc)
    return items


def put_items(table_name: str, region: str, items: Iterable[dict]) -> Tuple[int, int, int]:
    """아이템을 멱등적으로 삽입합니다."""
    return put_items_idempotent(table_name, region, items)


def _require_table_name(name: Optional[str]) -> str:
    """테이블명을 필수로 요구합니다. (앞뒤 공백/따옴표 제거)

    - 인자로 전달되거나, 환경변수 TABLE_NAME에서 가져옵니다.
    - 둘 다 없으면 예외를 발생시킵니다.
    """
    val = (name or os.getenv("TABLE_NAME") or "").strip().strip("'\"")
    if not val:
        raise RuntimeError("TABLE_NAME 환경변수가 설정되어 있지 않습니다.")
    return val


def run_upload(table_name: Optional[str] = None, region: Optional[str] = None) -> dict:
    """수집 → 변환 → 업로드를 수행하고 요약 결과를 반환합니다.

    Lambda와 CLI 양쪽에서 재사용 가능합니다.
    """
    target_table = _require_table_name(table_name)
    target_region = resolve_region(region)

    rows = fetch_news_list()
    docs = build_items(rows)
    inserted, dups, errs = put_items(target_table, target_region, docs)
    return {
        "table": target_table,
        "region": target_region,
        "rows": len(rows),
        "docs": len(docs),
        "inserted": inserted,
        "duplicates": dups,
        "errors": errs,
    }


def main(argv: list[str]) -> int:
    """로컬 실행용 진입점."""
    res = run_upload()
    print(
        f"가져온 행: {res['rows']} · 준비된 아이템: {res['docs']} · "
        f"테이블: '{res['table']}' · 리전: {res['region']}"
    )
    print(f"Inserted: {res['inserted']}, Duplicates: {res['duplicates']}, Errors: {res['errors']}")
    return 0 if res["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
