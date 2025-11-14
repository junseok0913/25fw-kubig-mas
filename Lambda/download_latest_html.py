from __future__ import annotations
"""Yahoo Finance Latest(US) HTML 스냅샷 도우미.

Selenium 없이 requests + BeautifulSoup를 사용해서
원격 HTML을 파일로 저장하거나, 저장된 .html에서 다시 파싱할 수 있습니다.

이 파일은 로컬 개발·디버깅 용도이고, Lambda 핸들러와는 분리되어 있습니다.
"""

import pathlib
from typing import List, Dict

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote


URL = "https://finance.yahoo.com/topic/latest-news/"
HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}


def download_latest_html(path: str = "yf_latest.html") -> str:
    """Yahoo Finance Latest 페이지 HTML을 로컬 파일로 저장합니다.

    Selenium 없이 순수 HTTP 요청으로만 HTML을 가져옵니다.
    """
    resp = requests.get(URL, headers=HDRS, timeout=20)
    resp.raise_for_status()

    out_path = pathlib.Path(path).resolve()
    out_path.write_text(resp.text, encoding="utf-8")
    return str(out_path)


def parse_latest_from_file(path: str) -> List[Dict]:
    """저장된 HTML 파일에서 기사 목록을 파싱합니다.

    반환 형식은 `Lambda/yahoo_fetch.py` 의 `fetch_news_list()`와 동일합니다.
    """
    html = pathlib.Path(path).read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    sections = soup.select(
        'ul.stream-items li.stream-item.story-item section[data-testid="storyitem"]'
    )

    rows: List[Dict] = []
    for sec in sections[:50]:
        a = sec.select_one('a[href*="/news/"][aria-label], a[href*="/news/"][title]')
        if not a:
            continue
        url = a.get("href")
        url = url if url.startswith("http") else urljoin("https://finance.yahoo.com", url)
        title = a.get("title") or a.get("aria-label") or a.get_text(strip=True)

        tickers = []
        for t in sec.select('a[href^="/quote/"]'):
            href = t.get("href", "")
            try:
                sym = href.split("/quote/")[1].split("/")[0]
            except Exception:
                continue
            sym = unquote(sym).upper()
            if sym and sym not in tickers:
                tickers.append(sym)

        rows.append({"title": title, "url": url, "tickers": tickers})

    return rows


if __name__ == "__main__":
    # 1) HTML을 한 번 저장하고
    saved = download_latest_html("yf_latest.html")
    print(f"HTML을 저장했습니다: {saved}")

    # 2) 저장된 파일에서 다시 파싱해보기
    articles = parse_latest_from_file(saved)
    print(f"파싱된 기사 수: {len(articles)}")
    for i, it in enumerate(articles[:5], start=1):
        print(f"{i:02d}. {it['title']}\n    {it['url']}\n    tickers={it['tickers']}")

