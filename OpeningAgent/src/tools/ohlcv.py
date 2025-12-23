"""yfinance 기반 OHLCV 조회 Tool."""

from __future__ import annotations

from datetime import datetime, date, timedelta
import logging
import os
from typing import Any, Dict, Optional

import pandas as pd
import yfinance as yf
from langchain_core.tools import tool

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
    """컬럼 대소문자/멀티인덱스 정리를 수행한다.
    
    yfinance는 멀티인덱스 (Price, Ticker) 형태로 컬럼을 반환할 수 있다.
    첫 번째 레벨(Price: Close, Open, High, Low, Volume)만 추출한다.
    """
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        # 첫 번째 레벨(Price 타입)을 컬럼명으로 사용
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df.columns = [str(c).title() for c in df.columns]
    return df


def _get_briefing_date() -> date:
    """브리핑 날짜를 반환한다.
    
    orchestrator/opening_agent에서 설정한 BRIEFING_DATE 환경변수를 사용합니다.
    이 환경변수가 없으면 ValueError를 발생시킵니다.
    
    Note:
        BRIEFING_DATE는 orchestrator.py 또는 opening_agent.py에서 CLI 인자를 통해 설정됩니다.
        에이전트가 end_date를 명시하지 않으면 이 날짜가 자동으로 사용됩니다.
    """
    briefing_date = os.environ.get("BRIEFING_DATE")
    if not briefing_date:
        raise ValueError(
            "BRIEFING_DATE 환경변수가 설정되지 않았습니다. "
            "orchestrator.py 또는 opening_agent.py를 통해 실행하세요."
        )
    return datetime.strptime(briefing_date, "%Y%m%d").date()


def _parse_date(date_str: str) -> date:
    """YYYY-MM-DD 형식의 문자열을 date로 파싱."""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


@tool
def get_ohlcv(
    ticker: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "1d",
) -> Dict[str, Any]:
    """yfinance를 통해 과거 OHLCV(시가/고가/저가/종가/거래량) 데이터를 조회한다.

    Args:
        ticker: Yahoo Finance 티커 (예: "NVDA", "^GSPC", "CL=F")
        start_date: 조회 시작일 (YYYY-MM-DD 형식, 미지정 시 end_date 기준 30일 전)
        end_date: 조회 종료일 (YYYY-MM-DD 형식, 미지정 시 브리핑 날짜)
        interval: 봉 간격 ("1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo")

    Returns:
        {ticker, start_date, end_date, interval, rows[{ts, open, high, low, close, volume}]}
    """
    # end_date 결정: 미지정 시 브리핑 날짜 (orchestrator에서 입력받은 날짜)
    if end_date:
        end_dt = _parse_date(end_date)
    else:
        end_dt = _get_briefing_date()

    # start_date 결정: 미지정 시 end_date 기준 30일 전
    if start_date:
        start_dt = _parse_date(start_date)
    else:
        start_dt = end_dt - timedelta(days=30)

    # yfinance end는 exclusive이므로 +1일
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

    # 사용할 수 있는 컬럼만으로 NaN 행 제거
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
