"""prefetch 유틸리티 테스트."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src import prefetch


class _FakeTable:
    """DynamoDB Table.query를 흉내 내는 간단한 스텁."""

    def __init__(self, items, pages=1):
        self._items = items
        self._pages = pages
        self.calls = 0

    def query(self, **kwargs):
        self.calls += 1
        page_size = len(self._items) // self._pages or len(self._items)
        start = (self.calls - 1) * page_size
        end = start + page_size
        resp = {"Items": self._items[start:end]}
        if end < len(self._items):
            resp["LastEvaluatedKey"] = {"pk": "next"}
        return resp


def test_prefetch_writes_files(tmp_path, monkeypatch):
    # 임시 경로로 데이터 디렉터리 재지정
    data_dir = tmp_path / "opening"
    monkeypatch.setattr(prefetch, "DATA_DIR", data_dir)
    monkeypatch.setattr(prefetch, "NEWS_LIST_PATH", data_dir / "news_list.json")
    monkeypatch.setattr(prefetch, "TITLES_PATH", data_dir / "titles.txt")
    monkeypatch.setattr(prefetch, "BODIES_DIR", data_dir / "bodies")

    # 가짜 DynamoDB 테이블 주입
    items = [
        {
            "pk": "h#abc",
            "title": "Fed holds rates steady",
            "url": "https://finance.yahoo.com/news/fed",
            "tickers": ["FED"],
            "publish_et_iso": "2025-11-24T17:00:00-05:00",
            "gsi_utc_pk": "UTC#2025-11-25",
            "utc_ms": Decimal(1700000000000),
            "path": "UTC#2025-11-25/h#abc.xml",
        }
    ]
    fake_table = _FakeTable(items)
    monkeypatch.setattr(prefetch, "get_dynamo_table", lambda *a, **k: fake_table)
    monkeypatch.setenv("TODAY", "20251125")

    result = prefetch.prefetch_news(table_name="dummy")
    assert result["count"] == 1
    assert (data_dir / "news_list.json").exists()
    saved = json.loads((data_dir / "news_list.json").read_text())
    assert saved["articles"][0]["pk"] == "h#abc"
    assert (data_dir / "titles.txt").read_text().strip() == "Fed holds rates steady"
