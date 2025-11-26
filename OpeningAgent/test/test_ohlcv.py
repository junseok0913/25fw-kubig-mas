"""ohlcv Tool 테스트."""

from __future__ import annotations

import pandas as pd

from src.tools import ohlcv


def test_get_ohlcv_monkeypatched(monkeypatch):
    # yfinance.download을 목업해 고정된 프레임을 반환
    def fake_download(ticker, period, interval, progress, auto_adjust, threads):
        idx = pd.to_datetime(["2025-11-24", "2025-11-25"])
        df = pd.DataFrame(
            {"Open": [1, 2], "High": [2, 3], "Low": [0.5, 1.5], "Close": [1.5, 2.5], "Volume": [100, 200]},
            index=idx,
        )
        return df

    monkeypatch.setattr(ohlcv.yf, "download", fake_download)
    res = ohlcv.get_ohlcv("NVDA", period="2d", interval="1d")
    assert res["ticker"] == "NVDA"
    assert len(res["rows"]) == 2
    assert res["rows"][0]["open"] == 1
