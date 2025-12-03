"""news_tools 기능 테스트."""

from __future__ import annotations

import json

from src.tools import news_tools


def _setup_news_files(tmp_path):
    """임시 news_list/titles/bodies 파일을 구성한다."""
    data_dir = tmp_path / "opening"
    news_tools.DATA_DIR = data_dir
    news_tools.NEWS_LIST_PATH = data_dir / "news_list.json"
    news_tools.TITLES_PATH = data_dir / "titles.txt"
    news_tools.BODIES_DIR = data_dir / "bodies"
    news_tools.BODIES_DIR.mkdir(parents=True, exist_ok=True)

    sample_articles = [
        {
            "pk": "h#aaa",
            "title": "NVIDIA surges on AI demand",
            "tickers": ["NVDA"],
            "path": "UTC#2025-11-25/h#aaa.xml",
        },
        {
            "pk": "h#bbb",
            "title": "Fed pauses rates",
            "tickers": ["FED"],
            "path": "UTC#2025-11-25/h#bbb.xml",
        },
    ]
    payload = {"articles": sample_articles}
    news_tools.NEWS_LIST_PATH.write_text(json.dumps(payload), encoding="utf-8")
    news_tools.TITLES_PATH.write_text("\n".join(a["title"] for a in sample_articles), encoding="utf-8")
    return sample_articles


def test_get_news_list_filters(tmp_path):
    _setup_news_files(tmp_path)
    res = news_tools.get_news_list(tickers=["NVDA"])
    assert res["count"] == 1
    assert res["articles"][0]["pk"] == "h#aaa"

    res_kw = news_tools.get_news_list(keywords=["Fed"])
    assert res_kw["count"] == 1
    assert res_kw["articles"][0]["pk"] == "h#bbb"


def test_get_news_content_cached(tmp_path, monkeypatch):
    articles = _setup_news_files(tmp_path)
    # 캐시된 본문 파일 준비
    (news_tools.BODIES_DIR / "h#aaa.txt").write_text("cached body", encoding="utf-8")

    # S3 호출을 막기 위해 페이크 fetch 주입
    monkeypatch.setattr(news_tools, "_fetch_body_from_s3", lambda pk, key, bucket=None: "fresh body")

    res = news_tools.get_news_content(["h#aaa", "h#bbb"], bucket="dummy")
    assert res["count"] == 2
    # 첫 번째는 캐시 사용
    assert res["articles"][0]["cached"] is True
    # 두 번째는 S3에서 읽었다고 보고
    assert res["articles"][1]["cached"] is False
    assert res["articles"][1]["body"] == "fresh body"


def test_count_keyword_frequency(tmp_path):
    _setup_news_files(tmp_path)
    (news_tools.BODIES_DIR / "h#aaa.txt").write_text("AI AI data center", encoding="utf-8")
    (news_tools.BODIES_DIR / "h#bbb.txt").write_text("Rates pause by Fed", encoding="utf-8")

    # 제목 기반
    title_freq = news_tools.count_keyword_frequency(["NVIDIA", "Fed"], source="titles")
    assert title_freq["NVIDIA"]["count"] >= 1
    # 본문 기반
    body_freq = news_tools.count_keyword_frequency(["AI", "Fed"], source="bodies")
    assert body_freq["AI"]["count"] == 2
    assert "h#bbb" in body_freq["Fed"]["article_pks"]
