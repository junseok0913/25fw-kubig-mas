"""뉴스 조회/본문 캐시/키워드 분석 Tool 구현."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.utils import get_s3_client

logger = logging.getLogger(__name__)

DATA_DIR = Path("data/opening")
NEWS_LIST_PATH = DATA_DIR / "news_list.json"
TITLES_PATH = DATA_DIR / "titles.txt"
BODIES_DIR = DATA_DIR / "bodies"


def _load_news_list() -> Dict[str, Any]:
    """news_list.json을 로드한다."""
    if not NEWS_LIST_PATH.exists():
        raise FileNotFoundError(f"news_list.json이 없습니다: {NEWS_LIST_PATH}")
    with open(NEWS_LIST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _iter_articles() -> Iterable[Dict[str, Any]]:
    payload = _load_news_list()
    for art in payload.get("articles", []):
        yield art


def get_news_list(
    tickers: Optional[List[str]] = None, keywords: Optional[List[str]] = None
) -> Dict[str, Any]:
    """로컬 캐시된 뉴스 목록을 필터링하여 반환한다."""
    payload = _load_news_list()
    articles = list(_iter_articles())

    def matches(article: Dict[str, Any]) -> bool:
        if tickers:
            article_tickers = [t.lower() for t in article.get("tickers", [])]
            if not all(t.lower() in article_tickers for t in tickers):
                return False
        if keywords:
            title = (article.get("title") or "").lower()
            if not all(k.lower() in title for k in keywords):
                return False
        return True

    filtered = [a for a in articles if matches(a)]
    return {
        "count": len(filtered),
        "filters": {"tickers": tickers, "keywords": keywords},
        "articles": filtered,
    }


def _body_path(pk: str) -> Path:
    return BODIES_DIR / f"{pk}.txt"


def _read_cached_body(pk: str) -> Optional[str]:
    path = _body_path(pk)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _write_body(pk: str, text: str) -> None:
    BODIES_DIR.mkdir(parents=True, exist_ok=True)
    _body_path(pk).write_text(text, encoding="utf-8")


def _guess_s3_bucket() -> str:
    return os.getenv("NEWS_BUCKET") or os.getenv("BUCKET_NAME") or ""


def _fetch_body_from_s3(pk: str, obj_key: str, bucket: Optional[str] = None) -> str:
    """S3에서 XML을 읽어 문자열로 반환한다."""
    if not obj_key:
        raise ValueError(f"{pk}에 대한 S3 key(path)가 없습니다.")
    bucket_name = bucket or _guess_s3_bucket()
    if not bucket_name:
        raise EnvironmentError("NEWS_BUCKET(또는 BUCKET_NAME) 환경변수가 필요합니다.")

    s3 = get_s3_client()
    resp = s3.get_object(Bucket=bucket_name, Key=obj_key)
    data = resp["Body"].read()
    try:
        return data.decode("utf-8")
    except Exception:  # noqa: BLE001
        return data.decode("utf-8", errors="ignore")


def get_news_content(
    pks: List[str], bucket: Optional[str] = None
) -> Dict[str, Any]:
    """S3에서 본문을 조회하거나 로컬 캐시를 반환한다."""
    articles: List[Dict[str, Any]] = []
    news_index = {a.get("pk"): a for a in _iter_articles()}

    for pk in pks:
        meta = news_index.get(pk)
        if not meta:
            logger.warning("news_list.json에서 %s을 찾을 수 없습니다.", pk)
            continue

        cached_body = _read_cached_body(pk)
        if cached_body is not None:
            articles.append({"pk": pk, "title": meta.get("title"), "body": cached_body, "cached": True})
            continue

        body_text = _fetch_body_from_s3(pk, meta.get("path"), bucket)
        _write_body(pk, body_text)
        articles.append({"pk": pk, "title": meta.get("title"), "body": body_text, "cached": False})

    return {"count": len(articles), "articles": articles}


def list_downloaded_bodies() -> Dict[str, Any]:
    """로컬에 저장된 본문 파일 목록을 반환한다."""
    BODIES_DIR.mkdir(parents=True, exist_ok=True)
    news_index = {a.get("pk"): a for a in _iter_articles()}
    entries = []
    for file in sorted(BODIES_DIR.glob("*.txt")):
        pk = file.stem
        title = news_index.get(pk, {}).get("title")
        entries.append({"pk": pk, "title": title})
    return {"count": len(entries), "articles": entries}


def _count_in_text(text: str, keyword: str) -> int:
    """대소문자 무시 빈도 카운트."""
    return len(re.findall(re.escape(keyword), text, flags=re.IGNORECASE))


def count_keyword_frequency(
    keywords: List[str],
    source: str = "titles",
    news_pks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """제목 또는 본문에서 키워드 빈도를 계산한다."""
    if source not in {"titles", "bodies"}:
        raise ValueError("source는 'titles' 또는 'bodies'만 허용합니다.")

    results: Dict[str, Dict[str, Any]] = {}
    if source == "titles":
        if not TITLES_PATH.exists():
            raise FileNotFoundError(f"titles.txt가 없습니다: {TITLES_PATH}")
        text = TITLES_PATH.read_text(encoding="utf-8")
        for kw in keywords:
            results[kw] = {"count": _count_in_text(text, kw), "article_pks": []}
        return results

    # bodies
    targets = news_pks or [f.stem for f in BODIES_DIR.glob("*.txt")]
    for kw in keywords:
        results[kw] = {"count": 0, "article_pks": []}

    for pk in targets:
        body = _read_cached_body(pk)
        if body is None:
            continue
        for kw in keywords:
            cnt = _count_in_text(body, kw)
            if cnt > 0:
                results[kw]["count"] += cnt
                results[kw].setdefault("article_pks", []).append(pk)

    return results
