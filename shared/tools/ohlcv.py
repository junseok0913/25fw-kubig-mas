"""yfinance-based OHLCV tool using shared config."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any, Dict, Optional

import pandas as pd
import yfinance as yf
from langchain_core.tools import tool

from shared.config import get_briefing_date

logger = logging.getLogger(__name__)

MAX_OHLCV_ROWS = 200
INTERVAL_OPTIONS = ("1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo")


def _round3(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return round(float(value), 3)
    except Exception:
        return value


def _as_int(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return int(value)
    except Exception:
        return value


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df.columns = [str(c).title() for c in df.columns]
    return df


def _get_briefing_date() -> date:
    briefing_date = get_briefing_date()
    return datetime.strptime(briefing_date, "%Y%m%d").date()


def _parse_date(date_str: str) -> date:
    return datetime.strptime(date_str, "%Y-%m-%d").date()


@tool
def get_ohlcv(
    ticker: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "1d",
) -> Dict[str, Any]:
    """Fetch OHLCV data from yfinance."""
    if end_date:
        end_dt = _parse_date(end_date)
    else:
        end_dt = _get_briefing_date()

    if start_date:
        start_dt = _parse_date(start_date)
    else:
        start_dt = end_dt - timedelta(days=30)

    yf_end = end_dt + timedelta(days=1)

    logger.info(
        "get_ohlcv 호출: ticker=%s, start=%s, end=%s, interval=%s",
        ticker,
        start_dt,
        end_dt,
        interval,
    )

    df = yf.download(
        ticker,
        start=start_dt.isoformat(),
        end=yf_end.isoformat(),
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    df = _normalize(df)

    result_base = {
        "ticker": ticker,
        "start_date": start_dt.isoformat(),
        "end_date": end_dt.isoformat(),
        "interval": interval,
    }

    if df.empty:
        logger.warning("OHLCV 결과가 비어 있습니다: %s", ticker)
        return {**result_base, "rows": []}

    cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    if not cols:
        logger.warning("OHLCV 컬럼이 부족합니다: %s", df.columns)
        return {**result_base, "rows": []}
    df = df.dropna(subset=cols, how="all")
    if df.empty:
        logger.warning("OHLCV 유효 행이 없습니다(모두 NaN): %s", ticker)
        return {**result_base, "rows": []}

    if len(df) > MAX_OHLCV_ROWS:
        logger.warning(
            "OHLCV 결과가 너무 큽니다: %s (%d rows, interval=%s, %s~%s)",
            ticker,
            len(df),
            interval,
            start_dt,
            end_dt,
        )
        return {
            **result_base,
            "rows": [],
            "too_many_rows": True,
            "row_count": int(len(df)),
            "max_rows": MAX_OHLCV_ROWS,
            "message": (
                f"OHLCV rows가 {len(df)}개로 너무 많습니다(최대 {MAX_OHLCV_ROWS}). "
                "기간을 더 짧게 하거나 interval을 더 크게 해서 다시 조회하세요."
            ),
            "suggested_intervals": list(INTERVAL_OPTIONS),
        }

    rows = []
    for idx, row in df.iterrows():
        rows.append(
            {
                "ts": pd.to_datetime(idx).isoformat(),
                "open": _round3(row.get("Open")),
                "high": _round3(row.get("High")),
                "low": _round3(row.get("Low")),
                "close": _round3(row.get("Close")),
                "volume": _as_int(row.get("Volume")),
            }
        )

    logger.info("get_ohlcv 결과: %d개 행 반환", len(rows))
    return {**result_base, "rows": rows}
