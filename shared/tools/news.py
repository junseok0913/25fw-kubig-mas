"""News tools using shared cache."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from langchain_core.tools import tool

from shared.config import get_bodies_dir, get_news_list_path, get_titles_path
from shared.utils.aws import get_s3_client

logger = logging.getLogger(__name__)

DEFAULT_NEWS_BODY_MAX_CHARS = 8000


def _load_news_list() -> Dict[str, Any]:
    """Load news_list.json from cache."""
    path = get_news_list_path()
    if not path.exists():
        raise FileNotFoundError(f"news_list.json이 없습니다: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _iter_articles() -> Iterable[Dict[str, Any]]:
    payload = _load_news_list()
    for art in payload.get("articles", []):
        yield art


@tool
def get_news_list(tickers: Optional[List[str]] = None, keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    """Filter cached news list.

    Args:
        tickers: ticker filter (AND)
        keywords: title keyword filter (AND)
    """
    logger.info("get_news_list 호출: tickers=%s, keywords=%s", tickers, keywords)
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
    logger.info("get_news_list 결과: %d건 반환", len(filtered))
    return {
        "count": len(filtered),
        "filters": {"tickers": tickers, "keywords": keywords},
        "articles": filtered,
    }


def _body_path(pk: str) -> Path:
    return get_bodies_dir() / f"{pk}.txt"


def _read_cached_body(pk: str) -> Optional[str]:
    path = _body_path(pk)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _write_body(pk: str, text: str) -> None:
    bodies_dir = get_bodies_dir()
    bodies_dir.mkdir(parents=True, exist_ok=True)
    _body_path(pk).write_text(text, encoding="utf-8")


def _clean_body_for_llm(text: str) -> str:
    if "<" in text and ">" in text:
        text = re.sub(r"(?s)<[^>]*>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _truncate_for_llm(text: str) -> tuple[str, bool]:
    raw = os.getenv("NEWS_BODY_MAX_CHARS", str(DEFAULT_NEWS_BODY_MAX_CHARS)).strip()
    try:
        limit = int(raw)
    except ValueError:
        limit = DEFAULT_NEWS_BODY_MAX_CHARS
    if limit <= 0:
        return text, False
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n...[truncated]", True


def _guess_s3_bucket() -> str:
    return os.getenv("NEWS_BUCKET") or os.getenv("BUCKET_NAME") or ""


def _fetch_body_from_s3(pk: str, obj_key: str, bucket: Optional[str] = None) -> str:
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
    except Exception:
        return data.decode("utf-8", errors="ignore")


@tool
def get_news_content(pks: List[str], bucket: Optional[str] = None) -> Dict[str, Any]:
    """Fetch news bodies from cache or S3."""
    logger.info("get_news_content 호출: pks=%s, bucket=%s", pks, bucket)
    articles: List[Dict[str, Any]] = []
    news_index = {a.get("pk"): a for a in _iter_articles()}

    for pk in pks:
        meta = news_index.get(pk)
        if not meta:
            logger.warning("news_list.json에서 %s을 찾을 수 없습니다.", pk)
            continue

        cached_body = _read_cached_body(pk)
        if cached_body is not None:
            body_llm, truncated = _truncate_for_llm(_clean_body_for_llm(cached_body))
            articles.append(
                {
                    "pk": pk,
                    "title": meta.get("title"),
                    "body": body_llm,
                    "cached": True,
                    "body_truncated": truncated,
                }
            )
            continue

        body_text = _fetch_body_from_s3(pk, meta.get("path"), bucket)
        _write_body(pk, body_text)
        body_llm, truncated = _truncate_for_llm(_clean_body_for_llm(body_text))
        articles.append(
            {
                "pk": pk,
                "title": meta.get("title"),
                "body": body_llm,
                "cached": False,
                "body_truncated": truncated,
            }
        )

    logger.info("get_news_content 결과: %d건 반환", len(articles))
    return {"count": len(articles), "articles": articles}


def _count_in_text(text: str, keyword: str) -> int:
    return len(re.findall(re.escape(keyword), text, flags=re.IGNORECASE))


@tool
def count_keyword_frequency(
    keywords: List[str],
    source: str = "titles",
    news_pks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Count keyword occurrences in titles or bodies."""
    logger.info("count_keyword_frequency 호출: keywords=%s, source=%s, news_pks=%s", keywords, source, news_pks)
    if source not in {"titles", "bodies"}:
        raise ValueError("source는 'titles' 또는 'bodies'만 허용합니다.")

    results: Dict[str, Dict[str, Any]] = {}
    if source == "titles":
        titles_path = get_titles_path()
        if not titles_path.exists():
            raise FileNotFoundError(f"titles.txt가 없습니다: {titles_path}")
        text = titles_path.read_text(encoding="utf-8")
        for kw in keywords:
            results[kw] = {"count": _count_in_text(text, kw), "article_pks": []}
        logger.info("count_keyword_frequency 결과(titles): %s", {k: v["count"] for k, v in results.items()})
        return results

    bodies_dir = get_bodies_dir()
    targets = news_pks or [f.stem for f in bodies_dir.glob("*.txt")]
    for kw in keywords:
        results[kw] = {"count": 0, "article_pks": []}

    for pk in targets:
        path = bodies_dir / f"{pk}.txt"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for kw in keywords:
            count = _count_in_text(text, kw)
            if count > 0:
                results[kw]["count"] += count
                results[kw]["article_pks"].append(pk)

    logger.info("count_keyword_frequency 결과(bodies): %s", {k: v["count"] for k, v in results.items()})
    return results
