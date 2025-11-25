from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup


HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}


def _fetch_html(url: str, timeout: float = 10.0) -> str:
    resp = requests.get(url, headers=HDRS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _parse_provider(article: BeautifulSoup) -> Optional[str]:
    """기사 상단 로고/텍스트에서 제공자명을 추출합니다."""
    # top-header 내 링크 우선
    header = article.select_one(".top-header")
    if header:
        link = header.select_one('a[data-ylk*="logo-provider"], a[aria-label], a[title]')
        if link:
            for attr in ("aria-label", "title"):
                value = link.get(attr)
                if value:
                    return value.strip()
            text = (link.get_text() or "").strip()
            if text:
                return text

    # byline-attr-author 가 제공자만 담고 있는 경우 활용
    by_author = article.select_one(".byline-attr-author")
    if by_author:
        txt = (by_author.get_text() or "").strip()
        if txt:
            # "By " 접두어 제거
            return re.sub(r"^By\s+", "", txt, flags=re.IGNORECASE).strip() or None

    return None


def _parse_author(article: BeautifulSoup) -> Optional[str]:
    """기사 하단의 byline에서 작성자(기자)를 추출합니다."""
    by_author = article.select_one(".byline-attr-author")
    if not by_author:
        return None
    txt = (by_author.get_text() or "").strip()
    if not txt:
        return None
    # "By " 접두어 제거
    cleaned = re.sub(r"^By\s+", "", txt, flags=re.IGNORECASE).strip()
    return cleaned or None


def _parse_time(article: BeautifulSoup) -> Dict[str, Optional[Any]]:
    """byline 영역의 <time> 태그에서 시간 정보를 추출합니다."""
    t = article.select_one("time.byline-attr-meta-time")
    if not t:
        return {"display": None, "iso_utc": None, "utc_ms": None}

    display = (t.get_text() or "").strip() or None
    raw_attr = t.get("datetime") or t.get("data-timestamp")
    if not raw_attr:
        return {"display": display, "iso_utc": None, "utc_ms": None}

    raw_attr = raw_attr.strip()
    iso_utc: Optional[str] = None
    utc_ms: Optional[int] = None

    try:
        if raw_attr.endswith("Z"):
            dt = datetime.fromisoformat(raw_attr.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(raw_attr)
        dt_utc = dt.astimezone(timezone.utc)
        iso_utc = dt_utc.isoformat().replace("+00:00", "Z")
        utc_ms = int(dt_utc.timestamp() * 1000)
    except Exception:
        iso_utc = raw_attr
        utc_ms = None

    return {"display": display, "iso_utc": iso_utc, "utc_ms": utc_ms}


def _is_inside_unwanted(el: BeautifulSoup) -> bool:
    """이미지/비디오/사이드 영역 내부인지 확인합니다."""
    unwanted = {"figure", "figcaption", "aside", "nav", "footer", "header", "button"}
    parent = el.parent
    while parent is not None:
        if getattr(parent, "name", "").lower() in unwanted:
            return True
        parent = parent.parent
    return False


def _extract_body_text(article: BeautifulSoup) -> List[str]:
    """메인 기사 본문 텍스트를 모두 추출합니다."""
    # 우선순위별 본문 컨테이너 후보
    container = (
        article.select_one('[data-test-id="article-content"]')
        or article.select_one("div.caas-body")
        or article.select_one('div[data-testid="article-body"]')
        or article
    )

    tags = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]
    collected: List[str] = []
    for tag in tags:
        for el in container.find_all(tag):
            if _is_inside_unwanted(el):
                continue
            text = (el.get_text() or "").strip()
            if not text:
                continue
            collected.append(text)

    cleaned: List[str] = []
    seen = set()
    for t in collected:
        if t in seen:
            continue
        seen.add(t)
        cleaned.append(t)
    return cleaned


def _find_article_wrappers(soup: BeautifulSoup) -> List[BeautifulSoup]:
    """페이지 내 기사(article) wrapper들을 모두 찾습니다."""
    articles = soup.select('article[data-testid="article-content-wrapper"]')
    if not articles:
        articles = soup.select("article.article-wrap")
    if not articles:
        articles = soup.find_all("article")
    return articles


def crawl_yahoo_finance_page(url: str, timeout: float = 10.0) -> Dict[str, Any]:
    """주어진 Yahoo Finance 기사 URL에서 메인/관련 기사 정보를 수집합니다.

    - Selenium / 브라우저를 사용하지 않고, HTTP + BeautifulSoup만 사용합니다.
    """
    html = _fetch_html(url, timeout=timeout)
    soup = BeautifulSoup(html, "html.parser")

    wrappers = _find_article_wrappers(soup)
    main_wrapper: Optional[BeautifulSoup] = wrappers[0] if wrappers else None

    # 메인 기사 정보
    main_title: Optional[str] = None
    main_provider: Optional[str] = None
    main_author: Optional[str] = None
    time_info: Dict[str, Optional[Any]] = {"display": None, "iso_utc": None, "utc_ms": None}
    body_paragraphs: List[str] = []

    if main_wrapper is not None:
        # 제목
        h1 = main_wrapper.select_one("h1.cover-title") or main_wrapper.find("h1")
        if h1:
            main_title = (h1.get_text() or "").strip() or None

        main_provider = _parse_provider(main_wrapper)
        main_author = _parse_author(main_wrapper)
        time_info = _parse_time(main_wrapper)
        body_paragraphs = _extract_body_text(main_wrapper)

    body_text = "\n\n".join(body_paragraphs)

    # 관련 기사 메타데이터 (제목 + 제공자만)
    related_articles: List[Dict[str, Optional[str]]] = []
    for w in wrappers[1:]:
        title = None
        h = w.select_one("h1.cover-title") or w.find("h1") or w.find("h2")
        if h:
            title = (h.get_text() or "").strip() or None
        provider = _parse_provider(w)
        if not title and not provider:
            continue
        related_articles.append(
            {
                "title": title,
                "provider": provider,
            }
        )

    main_article = {
        "title": main_title,
        "provider": main_provider,
        "author": main_author,
        "time_display": time_info["display"],
        "time_iso_utc": time_info["iso_utc"],
        "time_utc_ms": time_info["utc_ms"],
        "url": url,
        "body_text": body_text,
        "body_paragraph_count": len(body_paragraphs),
    }

    return {
        "page_url": url,
        "main_article": main_article,
        "related_articles": related_articles,
    }

