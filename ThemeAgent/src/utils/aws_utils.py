"""AWS 세션/클라이언트 유틸리티.

SSO 프로파일 기반으로 boto3 세션을 초기화하고 DynamoDB/S3 핸들을 생성한다.
"""

from __future__ import annotations

import os
from typing import Optional

import boto3
from botocore.config import Config


def get_boto3_session(
    profile_name: Optional[str] = None, region_name: Optional[str] = None
) -> boto3.session.Session:
    """SSO 프로파일로 boto3 세션을 생성한다.

    - 기본 프로파일은 env `AWS_PROFILE` → 인자 → "Admins" 순으로 결정.
    - region은 env `AWS_REGION` → 인자 순.
    - SSO 설정을 읽도록 `AWS_SDK_LOAD_CONFIG=1` 설정을 권장한다.
    """
    profile = profile_name or os.getenv("AWS_PROFILE") or "Admins"
    region = region_name or os.getenv("AWS_REGION")
    return boto3.Session(profile_name=profile, region_name=region)


def get_dynamo_table(
    table_name: str, profile_name: Optional[str] = None, region_name: Optional[str] = None
):
    """DynamoDB Table 리소스를 반환한다."""
    session = get_boto3_session(profile_name, region_name)
    dynamodb = session.resource("dynamodb", config=Config(retries={"max_attempts": 3}))
    return dynamodb.Table(table_name)


def get_s3_client(
    profile_name: Optional[str] = None, region_name: Optional[str] = None
):
    """S3 클라이언트를 반환한다."""
    session = get_boto3_session(profile_name, region_name)
    return session.client("s3", config=Config(retries={"max_attempts": 3}))
