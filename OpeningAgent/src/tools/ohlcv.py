"""yfinance 기반 OHLCV 조회 Tool."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pandas as pd
import yfinance as yf
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """컬럼 대소문자/멀티인덱스 정리를 수행한다."""
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[-1] if isinstance(c, tuple) else c for c in df.columns]
    df.columns = [str(c).title() for c in df.columns]
    return df


@tool
def get_ohlcv(ticker: str, period: str = "1mo", interval: str = "1d") -> Dict[str, Any]:
    """yfinance를 통해 과거 OHLCV(시가/고가/저가/종가/거래량) 데이터를 조회한다.

    Args:
        ticker: Yahoo Finance 티커 (예: "NVDA", "^GSPC", "CL=F")
        period: 조회 기간 ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max")
        interval: 봉 간격 ("1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo")

    Returns:
        {ticker, period, interval, rows[{ts, open, high, low, close, volume}]}
    """
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    df = _normalize(df)
    if df.empty:
        logger.warning("OHLCV 결과가 비어 있습니다: %s", ticker)
        return {"ticker": ticker, "period": period, "interval": interval, "rows": []}

    rows = []
    for idx, row in df.iterrows():
        rows.append(
            {
                "ts": pd.to_datetime(idx).isoformat(),
                "open": row.get("Open"),
                "high": row.get("High"),
                "low": row.get("Low"),
                "close": row.get("Close"),
                "volume": row.get("Volume"),
            }
        )

    return {"ticker": ticker, "period": period, "interval": interval, "rows": rows}
