from __future__ import annotations
import os
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from .aws_dynamo import get_table, resolve_region
from .article_crawler import crawl_yahoo_finance_page
from .upload_s3 import put_article_xml


def _is_path_empty(item: Dict[str, Any]) -> bool:
    """DynamoDB 아이템에서 path가 비어있는지 확인합니다."""
    v = item.get("path")
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def list_unprocessed_items(table_name: str, region: Optional[str], limit: int = 10) -> List[Dict[str, Any]]:
    """path가 비어있는 레코드들 중 utc_ms 오름차순 기준 상위 limit개를 반환합니다."""
    table = get_table(table_name, region)
    items: List[Dict[str, Any]] = []

    scan_kwargs: Dict[str, Any] = {}
    while True:
        resp = table.scan(**scan_kwargs)
        batch = resp.get("Items", [])
        for it in batch:
            if _is_path_empty(it):
                items.append(it)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    # utc_ms 오름차순(과거순)으로 정렬
    items.sort(key=lambda x: int(x.get("utc_ms", 0)))
    return items[:limit]


def _utc_iso_to_et_iso(iso_utc: Optional[str]) -> Optional[str]:
    """UTC ISO 문자열을 America/New_York ISO 문자열로 변환합니다."""
    if not iso_utc:
        return None
    try:
        s = iso_utc.strip()
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        et = dt.astimezone(ZoneInfo("America/New_York"))
        return et.isoformat()
    except Exception:
        return None


def _build_s3_key(item: Dict[str, Any]) -> str:
    """DynamoDB 아이템에서 S3 키(path)를 생성합니다."""
    gsi_utc_pk = item.get("gsi_utc_pk") or ""
    pk = item.get("pk") or ""
    return f"{gsi_utc_pk}/{pk}.xml"


def process_single_item(table_name: str, region: Optional[str], item: Dict[str, Any], bucket: str) -> Dict[str, Any]:
    """단일 DynamoDB 아이템을 크롤링 및 S3 업로드/업데이트/삭제 처리합니다."""
    pk = item.get("pk")
    url = item.get("url")
    gsi_utc_pk = item.get("gsi_utc_pk")

    if not pk or not url or not gsi_utc_pk:
        msg = "missing_pk_or_url_or_gsi"
        print(f"[detail_crawl] skip item with invalid keys: pk={pk}, url={url}, gsi_utc_pk={gsi_utc_pk} ({msg})", file=sys.stderr)
        return {"pk": pk, "action": "skipped_invalid", "error": msg}

    # 1) 크롤링
    try:
        print(f"[detail_crawl] crawling pk={pk} url={url}", file=sys.stderr)
        page = crawl_yahoo_finance_page(url)
    except Exception as exc:
        print(f"[detail_crawl] crawl_failed pk={pk} url={url}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return {"pk": pk, "action": "crawl_failed", "error": str(exc)}

    main = page.get("main_article") or {}
    provider = (main.get("provider") or "").strip()

    table = get_table(table_name, region)

    # 2) PREMIUM 기사이면 삭제
    if provider.upper() == "PREMIUM":
        try:
            table.delete_item(Key={"pk": pk}, ConditionExpression="attribute_exists(pk)")
            print(f"[detail_crawl] deleted PREMIUM article pk={pk}", file=sys.stderr)
            return {"pk": pk, "action": "deleted_premium"}
        except ClientError as exc:
            print(f"[detail_crawl] delete_failed pk={pk}: {exc}", file=sys.stderr)
            return {"pk": pk, "action": "delete_failed", "error": str(exc)}

    # 3) 일반 기사 처리
    title_crawled = (main.get("title") or "").strip()
    body_text: str = main.get("body_text") or ""
    time_iso_utc = main.get("time_iso_utc")
    publish_et_iso = _utc_iso_to_et_iso(time_iso_utc)

    # S3 업로드용 문서 구성
    article_doc = {
        "pk": pk,
        "url": main.get("url") or url,
        "provider": provider,
        "author": main.get("author") or "",
        "publish_iso_utc": time_iso_utc or "",
        "publish_et_iso": publish_et_iso or "",
        "body_text": body_text,
    }

    s3_key = _build_s3_key(item)

    try:
        put_article_xml(bucket, s3_key, article_doc)
    except Exception as exc:
        print(f"[detail_crawl] s3_failed pk={pk}, key={s3_key}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return {"pk": pk, "action": "s3_failed", "error": str(exc)}

    # 4) related_articles pk 계산 및 존재 여부 확인 (제목 기반)
    related = page.get("related_articles") or []
    related_pks: List[str] = []

    dynamo = boto3.resource("dynamodb", region_name=resolve_region(region))
    tbl = dynamo.Table(table_name)

    for rel in related:
        rel_title = (rel.get("title") or "").strip()
        if not rel_title:
            continue
        try:
            scan_kwargs: Dict[str, Any] = {
                "FilterExpression": Attr("title").eq(rel_title),
                "ProjectionExpression": "pk, utc_ms",
            }
            best_pk = None
            best_utc = -1
            while True:
                resp = tbl.scan(**scan_kwargs)
                items = resp.get("Items", [])
                for it in items:
                    try:
                        u = int(it.get("utc_ms", 0))
                    except Exception:
                        u = 0
                    if u >= best_utc:
                        best_utc = u
                        best_pk = it.get("pk")
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
            if best_pk:
                related_pks.append(str(best_pk))
        except ClientError as exc:
            print(f"[detail_crawl] scan failed for related title='{rel_title}': {exc}", file=sys.stderr)
            continue

    # 중복 제거
    related_pks = list(dict.fromkeys(related_pks))

    # 5) DynamoDB UpdateItem
    new_title = title_crawled or item.get("title") or ""

    try:
        table.update_item(
            Key={"pk": pk},
            UpdateExpression=(
                "SET #title = :title, "
                "#provider = :provider, "
                "#publish_et_iso = :publish_et_iso, "
                "#path = :path, "
                "#related_articles = :related"
            ),
            ExpressionAttributeNames={
                "#title": "title",
                "#provider": "provider",
                "#publish_et_iso": "publish_et_iso",
                "#path": "path",
                "#related_articles": "related_articles",
            },
            ExpressionAttributeValues={
                ":title": new_title,
                ":provider": provider,
                ":publish_et_iso": publish_et_iso or "",
                ":path": s3_key,
                ":related": related_pks,
            },
            ConditionExpression="attribute_exists(pk)",
        )
    except ClientError as exc:
        print(f"[detail_crawl] update_failed pk={pk}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return {"pk": pk, "action": "update_failed", "error": str(exc)}

    return {
        "pk": pk,
        "action": "updated",
        "s3_key": s3_key,
        "related_count": len(related_pks),
    }


def run_detail_crawl(table_name: Optional[str], region: Optional[str], bucket: str, max_items: int = 10) -> Dict[str, Any]:
    """path가 비어있는 기사들 중 과거순 최대 max_items개를 처리합니다."""
    if not bucket:
        raise RuntimeError("BUCKET_NAME 환경변수가 설정되어 있지 않습니다.")

    target_region = resolve_region(region)
    target_table = table_name or os.getenv("TABLE_NAME")
    if not target_table:
        raise RuntimeError("TABLE_NAME 환경변수가 설정되어 있지 않습니다.")

    print(f"[detail_crawl] start: table={target_table}, region={target_region}, bucket={bucket}, max_items={max_items}", file=sys.stderr)
    items = list_unprocessed_items(target_table, target_region, limit=max_items)
    print(f"[detail_crawl] unprocessed items (selected): {len(items)}", file=sys.stderr)

    results: List[Dict[str, Any]] = []
    for it in items:
        res = process_single_item(target_table, target_region, it, bucket)
        results.append(res)
        time.sleep(3)

    deleted_premium = sum(1 for r in results if r.get("action") == "deleted_premium")
    updated = sum(1 for r in results if r.get("action") == "updated")
    errors = sum(1 for r in results if "failed" in (r.get("action") or ""))  # crawl_failed, s3_failed, update_failed 등

    summary = {
        "table": target_table,
        "region": target_region,
        "checked": len(items),
        "processed": len(results),
        "deleted_premium": deleted_premium,
        "updated": updated,
        "errors": errors,
    }
    print(f"[detail_crawl] summary: {summary}", file=sys.stderr)
    return summary
