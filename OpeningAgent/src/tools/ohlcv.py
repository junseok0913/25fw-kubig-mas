"""yfinance 기반 OHLCV 조회 Tool."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """컬럼 대소문자/멀티인덱스 정리를 수행한다."""
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[-1] if isinstance(c, tuple) else c for c in df.columns]
    df.columns = [str(c).title() for c in df.columns]
    return df


def get_ohlcv(ticker: str, period: str = "1mo", interval: str = "1d") -> Dict[str, Any]:
    """지정한 티커의 과거 OHLCV를 조회한다."""
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
