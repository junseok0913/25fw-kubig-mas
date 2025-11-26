"""yfinance 기반 OHLCV 조회 Tool."""

from __future__ import annotations

from datetime import datetime, date, timedelta
import logging
import os
from typing import Any, Dict

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


def _parse_today() -> date:
    """환경변수 TODAY(YYYYMMDD)를 날짜로 파싱."""
    raw = os.getenv("TODAY")
    if raw:
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except Exception:  # noqa: BLE001
            logger.warning("TODAY 파싱 실패(%s), 시스템 날짜 사용", raw)
    return datetime.utcnow().date()


def _period_to_days(period: str) -> int:
    """yfinance period 문자열을 일수로 근사."""
    mapping = {
        "1d": 1,
        "5d": 5,
        "1mo": 30,
        "3mo": 90,
        "6mo": 180,
        "1y": 365,
        "2y": 730,
        "5y": 1825,
        "10y": 3650,
    }
    if period == "ytd":
        anchor = _parse_today()
        start = date(anchor.year, 1, 1)
        return (anchor - start).days + 1
    return mapping.get(period, 30)


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
    anchor = _parse_today()
    days = _period_to_days(period)
    end_date = anchor + timedelta(days=1)  # end는 비포 방식, 당일 포함
    start_date = end_date - timedelta(days=days)

    logger.info(
        "get_ohlcv 호출: ticker=%s, period=%s(%sd), interval=%s, start=%s, end=%s",
        ticker,
        period,
        days,
        interval,
        start_date,
        end_date,
    )

    df = yf.download(
        ticker,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    df = _normalize(df)
    if df.empty:
        logger.warning("OHLCV 결과가 비어 있습니다: %s", ticker)
        return {"ticker": ticker, "period": period, "interval": interval, "rows": []}

    # 사용할 수 있는 컬럼만으로 NaN 행 제거
    cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    if not cols:
        logger.warning("OHLCV 컬럼이 부족합니다: %s", df.columns)
        return {"ticker": ticker, "period": period, "interval": interval, "rows": []}
    df = df.dropna(subset=cols, how="all")
    if df.empty:
        logger.warning("OHLCV 유효 행이 없습니다(모두 NaN): %s", ticker)
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
