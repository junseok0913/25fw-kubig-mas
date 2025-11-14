from __future__ import annotations
import os
from typing import Any, Dict

from .upload_db import run_upload


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """EventBridge에 의해 실행되는 AWS Lambda 엔트리포인트.

    환경변수:
    - TABLE_NAME: DynamoDB 테이블명(기본: latest_news_list)
    - AWS_REGION: 리전
    """
    # 이벤트 페이로드 기반 오버라이드(선택)
    table_override = None
    region_override = None

    if isinstance(event, dict):
        table_override = event.get("table") or event.get("TABLE_NAME")
        region_override = event.get("region") or event.get("AWS_REGION")

    result = run_upload(table_override, region_override)
    return {
        "ok": result["errors"] == 0,
        **result,
    }

