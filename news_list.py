from __future__ import annotations
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote

URL = "https://finance.yahoo.com/topic/latest-news/"
HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}

def fetch_latest() -> list[dict]:
    """
    한 번의 request로 Latest News(US) 페이지에서
    1) 제목, 2) 기사 링크, 3) 관련 티커(/quote/{SYMBOL}/)만 추출.
    - 폴백/디버그/동의쿠키 처리 없음. (페이지 구조 변경 시 깨질 수 있음)
    """
    html = requests.get(URL, headers=HDRS, timeout=20).text
    soup = BeautifulSoup(html, "html.parser")

    # 각 기사 섹션: ul.stream-items > li.stream-item.story-item > section[data-testid="storyitem"]
    sections = soup.select('ul.stream-items li.stream-item.story-item section[data-testid="storyitem"]')

    rows = []
    for sec in sections[:50]:
        # 기사 링크(제목 포함): /news/*.html 이면서 aria-label/title 보유 앵커
        a = sec.select_one('a[href*="/news/"][aria-label], a[href*="/news/"][title]')
        if not a:
            continue
        url = a.get("href")
        url = url if url.startswith("http") else urljoin("https://finance.yahoo.com", url)
        title = a.get("title") or a.get("aria-label") or a.get_text(strip=True)

        # 관련 티커: 섹션 내부의 /quote/{SYMBOL}/ 앵커 수집
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
    items = fetch_latest()
    print(f"총 {len(items)}건")
    for i, it in enumerate(items, 1):
        tks = ", ".join(it["tickers"]) if it["tickers"] else "-"
        print(f"{i:02d}. {it['title']}\n    {it['url']}\n    Tickers: {tks}")