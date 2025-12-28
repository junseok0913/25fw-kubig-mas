"""AWS session/client utilities."""

from __future__ import annotations

import os
from typing import Optional

import boto3
from botocore.config import Config


def get_boto3_session(profile_name: Optional[str] = None, region_name: Optional[str] = None) -> boto3.session.Session:
    """Create boto3 session with optional profile/region overrides."""
    profile = profile_name or os.getenv("AWS_PROFILE") or "Admins"
    region = region_name or os.getenv("AWS_REGION")
    return boto3.Session(profile_name=profile, region_name=region)


def get_dynamo_table(table_name: str, profile_name: Optional[str] = None, region_name: Optional[str] = None):
    """Return DynamoDB Table resource."""
    session = get_boto3_session(profile_name, region_name)
    dynamodb = session.resource("dynamodb", config=Config(retries={"max_attempts": 3}))
    return dynamodb.Table(table_name)


def get_s3_client(profile_name: Optional[str] = None, region_name: Optional[str] = None):
    """Return S3 client."""
    session = get_boto3_session(profile_name, region_name)
    return session.client("s3", config=Config(retries={"max_attempts": 3}))
