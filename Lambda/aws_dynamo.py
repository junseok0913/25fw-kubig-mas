from __future__ import annotations
import os
import sys
from typing import Iterable, Tuple, Optional

import boto3
from botocore.exceptions import ClientError


def resolve_region(region: Optional[str] = None) -> str:
    """리전을 해석합니다. (명시 인자 → AWS_REGION)

    설정이 없으면 예외를 발생시킵니다.
    """
    value = region or os.getenv("AWS_REGION")
    if not value:
        raise RuntimeError("AWS_REGION 환경변수가 설정되어 있지 않습니다.")
    return value


def get_table(table_name: str, region: Optional[str] = None):
    """DynamoDB Table 리소스를 반환합니다."""
    dynamo = boto3.resource("dynamodb", region_name=resolve_region(region))
    return dynamo.Table(table_name)


def put_items_idempotent(table_name: str, region: Optional[str], items: Iterable[dict]) -> Tuple[int, int, int]:
    """`attribute_not_exists(pk)` 조건으로 멱등 삽입을 수행합니다.

    반환: (inserted, duplicates, errors)
    """
    table = get_table(table_name, region)
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
        except Exception as e:
            errs += 1
            print(f"Error for pk={it.get('pk')}: {e}", file=sys.stderr)
    return inserted, dup, errs
