from __future__ import annotations
import xml.etree.ElementTree as ET
from typing import Dict

import boto3


def build_article_xml(doc: Dict) -> str:
    """메인 기사 정보를 XML 문자열로 직렬화합니다.

    기대 입력 필드:
    - pk, url, provider, author, publish_iso_utc, publish_et_iso, body_text
    """
    root = ET.Element("article")

    def _add(tag: str, text: str | None) -> None:
        el = ET.SubElement(root, tag)
        if text is not None:
            el.text = text

    _add("pk", doc.get("pk"))
    _add("url", doc.get("url"))
    _add("provider", doc.get("provider"))
    _add("author", doc.get("author"))
    _add("publish_iso_utc", doc.get("publish_iso_utc"))
    _add("publish_et_iso", doc.get("publish_et_iso"))

    body_text = doc.get("body_text") or ""
    body_el = ET.SubElement(root, "body")
    body_el.text = f"<![CDATA[\n{body_text}\n]]>"

    return ET.tostring(root, encoding="utf-8").decode("utf-8")


def put_article_xml(bucket: str, key: str, article_doc: Dict) -> None:
    """기사 정보를 XML로 직렬화하여 S3에 업로드합니다."""
    xml_str = build_article_xml(article_doc)
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=xml_str.encode("utf-8"),
        ContentType="application/xml",
    )

