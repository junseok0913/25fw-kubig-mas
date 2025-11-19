from __future__ import annotations
import os
from typing import Any, Dict

from .upload_db import run_upload
from .detail_crawl import run_detail_crawl


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """EventBridge에 의해 실행되는 AWS Lambda 엔트리포인트.

    환경변수:
    - TABLE_NAME: DynamoDB 테이블명(기본: latest_news_list)
    - AWS_REGION: 리전
    - BUCKET_NAME: 본문 XML을 저장할 S3 버킷 이름
    """
    table_override = None
    region_override = None

    if isinstance(event, dict):
        table_override = event.get("table") or event.get("TABLE_NAME")
        region_override = event.get("region") or event.get("AWS_REGION")

    # 1단계: 최신 뉴스 목록 적재 (리스트 수집 단계)
    print(f"[handler] event={event}", flush=True)
    upload_res = run_upload(table_override, region_override)
    print(f"[handler] upload result={upload_res}", flush=True)

    # 2단계: 본문 크롤링 + S3 업로드 + DynamoDB 필드 채우기 (개별 본문 수집 단계)
    bucket = os.getenv("BUCKET_NAME")
    detail_res = run_detail_crawl(
        table_name=upload_res["table"],
        region=upload_res["region"],
        bucket=bucket,
        max_items=100,
    )
    print(f"[handler] detail result={detail_res}", flush=True)

    ok = (upload_res["errors"] == 0) and (detail_res["errors"] == 0)
    return {
        "ok": ok,
        "upload": upload_res,
        "detail": detail_res,
    }
