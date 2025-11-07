from __future__ import annotations
import os
import sys
import hashlib
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Iterable, Tuple

import boto3
from botocore.exceptions import ClientError

from news_list import fetch_news_list


SOURCE = "finance.yahoo.com/topic/latest-news/"
DEFAULT_TABLE = os.getenv("NEWS_TABLE")
DEFAULT_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"


def _compute_pk(url: str) -> str:
    """Compute DynamoDB partition key from original URL per spec.

    - If URL contains a numeric article id (≥6 consecutive digits) before .html → prefix `id#`
    - Else → prefix `h#`
    - Hash is sha256(url) first 16 hex chars.
    """
    # Heuristic for Yahoo Finance numeric article ID near end of path
    has_numeric_id = bool(re.search(r"/news/[^?]*?(\d{6,})\.html", url))
    prefix = "id#" if has_numeric_id else "h#"
    digest16 = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{digest16}"


def _now_fields() -> dict:
    """Build timezone fields for upload timestamp per spec."""
    now_utc = datetime.now(timezone.utc)
    est_tz = ZoneInfo("America/New_York")
    kst_tz = ZoneInfo("Asia/Seoul")

    now_est = now_utc.astimezone(est_tz)
    now_kst = now_utc.astimezone(kst_tz)

    tz_est_abbr = now_est.strftime("%Z")  # 'EST' or 'EDT'
    tz_est_is_dst = (now_est.dst() or timedelta(0)) != timedelta(0)

    return {
        "uploaded_at_utc_iso": now_utc.isoformat().replace("+00:00", "Z"),
        "uploaded_at_utc_ms": int(now_utc.timestamp() * 1000),
        "uploaded_at_est_iso": now_est.isoformat(),
        "uploaded_at_kst_iso": now_kst.isoformat(),
        "dt_utc": now_utc.date().isoformat(),
        "dt_est": now_est.date().isoformat(),
        "dt_kst": now_kst.date().isoformat(),
        "tz_est_abbr": tz_est_abbr,
        "tz_est_is_dst": tz_est_is_dst,
    }


def build_items(rows: Iterable[dict]) -> list[dict]:
    """Transform scraped rows into DynamoDB item documents."""
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
            "source": SOURCE,
            "title": title,
            "url": url,
            "tickers": list(tickers),
            **ts,
        }
        items.append(doc)
    return items


def put_items(table_name: str, region: str, items: Iterable[dict]) -> Tuple[int, int, int]:
    """Insert items idempotently. Returns (inserted, duplicates, errors)."""
    dynamo = boto3.resource("dynamodb", region_name=region)
    table = dynamo.Table(table_name)
    inserted = dup = errs = 0
    for it in items:
        try:
            table.put_item(Item=it, ConditionExpression="attribute_not_exists(pk)")
            inserted += 1
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                dup += 1
            else:
                errs += 1
                print(f"Error for pk={it.get('pk')}: {code} - {e}", file=sys.stderr)
        except Exception as e:  # Catch-all for unexpected issues
            errs += 1
            print(f"Error for pk={it.get('pk')}: {e}", file=sys.stderr)
    return inserted, dup, errs


def main(argv: list[str]) -> int:
    table_name = os.getenv("NEWS_TABLE", DEFAULT_TABLE)
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_REGION

    rows = fetch_news_list()
    docs = build_items(rows)

    print(f"Fetched {len(rows)} rows; prepared {len(docs)} items for table '{table_name}' in {region}.")
    if not docs:
        return 0

    ins, dups, errs = put_items(table_name, region, docs)
    print(f"Inserted: {ins}, Duplicates: {dups}, Errors: {errs}")
    return 0 if errs == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

