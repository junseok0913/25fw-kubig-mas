"""ohlcv Tool 테스트."""

from __future__ import annotations

import pandas as pd

from src.tools import ohlcv


def test_get_ohlcv_monkeypatched(monkeypatch):
    # yfinance.download을 목업해 고정된 프레임을 반환
    def fake_download(ticker, start, end, interval, progress, auto_adjust, threads):
        idx = pd.to_datetime(["2025-11-24", "2025-11-25"])
        df = pd.DataFrame(
            {"Open": [1, 2], "High": [2, 3], "Low": [0.5, 1.5], "Close": [1.5, 2.5], "Volume": [100, 200]},
            index=idx,
        )
        return df

    monkeypatch.setattr(ohlcv.yf, "download", fake_download)
    res = ohlcv.get_ohlcv.invoke({
        "ticker": "NVDA",
        "start_date": "2025-11-24",
        "end_date": "2025-11-25",
        "interval": "1d",
    })
    assert res["ticker"] == "NVDA"
    assert res["start_date"] == "2025-11-24"
    assert res["end_date"] == "2025-11-25"
    assert len(res["rows"]) == 2
    assert res["rows"][0]["open"] == 1


def test_get_ohlcv_default_dates(monkeypatch):
    """start_date/end_date 미지정 시 기본값 테스트."""
    def fake_download(ticker, start, end, interval, progress, auto_adjust, threads):
        idx = pd.to_datetime(["2025-11-01"])
        df = pd.DataFrame(
            {"Open": [100], "High": [110], "Low": [95], "Close": [105], "Volume": [1000]},
            index=idx,
        )
        return df

    monkeypatch.setattr(ohlcv.yf, "download", fake_download)
    # TODAY 환경변수 설정
    monkeypatch.setenv("TODAY", "20251125")

    res = ohlcv.get_ohlcv.invoke({"ticker": "AAPL"})
    assert res["ticker"] == "AAPL"
    assert res["end_date"] == "2025-11-25"  # TODAY 환경변수 기준
    assert res["start_date"] == "2025-10-26"  # 30일 전
    assert len(res["rows"]) == 1
