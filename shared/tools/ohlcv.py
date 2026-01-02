"""yfinance-based OHLCV tool using shared config."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import re
from typing import Any, Dict, Optional

import pandas as pd
import yfinance as yf
from langchain_core.tools import tool

from shared.config import get_briefing_date

logger = logging.getLogger(__name__)

MAX_OHLCV_ROWS = 200
INTERVAL_OPTIONS = ("1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo")

_DATE_YYYY_MM_DD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_YYYYMMDD = re.compile(r"^\d{8}$")
_DATE_YYYY_MM_DD_SEARCH = re.compile(r"(\d{4}-\d{2}-\d{2})")


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


def _coerce_date(value: Any) -> tuple[date | None, str | None]:
    """Best-effort date coercion.

    Accepts:
    - YYYY-MM-DD
    - YYYYMMDD
    - Any string containing a YYYY-MM-DD substring (last-resort for LLM tool calls)
    """
    raw = str(value or "").strip()
    if not raw:
        return None, None

    if _DATE_YYYY_MM_DD.match(raw):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date(), raw
        except ValueError:
            return None, None

    if _DATE_YYYYMMDD.match(raw):
        try:
            dt = datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            return None, None
        return dt, dt.isoformat()

    # Some LLM outputs accidentally include extra prefixes/suffixes. If an ISO date
    # substring exists, use it rather than failing hard.
    m = _DATE_YYYY_MM_DD_SEARCH.search(raw)
    if m:
        candidate = m.group(1)
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").date(), candidate
        except ValueError:
            return None, None

    return None, None


@tool
def get_ohlcv(
    ticker: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "1d",
) -> Dict[str, Any]:
    """Fetch OHLCV data from yfinance.

    This tool is defensive: it returns `{rows: [], error: ...}` instead of raising,
    so LLM tool-calling graphs can continue even when invalid arguments are provided.
    """
    raw_start = str(start_date or "").strip()
    raw_end = str(end_date or "").strip()

    result_base: Dict[str, Any] = {
        "ticker": ticker,
        "start_date": raw_start,
        "end_date": raw_end,
        "interval": interval,
    }

    if interval not in INTERVAL_OPTIONS:
        logger.warning("get_ohlcv invalid interval: %r", interval)
        return {
            **result_base,
            "rows": [],
            "error": "invalid_interval",
            "message": f"interval must be one of {list(INTERVAL_OPTIONS)}",
            "suggested_intervals": list(INTERVAL_OPTIONS),
        }

    if raw_end:
        end_dt, end_norm = _coerce_date(raw_end)
        if end_dt is None:
            logger.warning("get_ohlcv invalid end_date: %r", raw_end)
            return {
                **result_base,
                "rows": [],
                "error": "invalid_end_date",
                "message": "end_date must be YYYY-MM-DD (or YYYYMMDD)",
            }
        result_base["end_date"] = str(end_norm or end_dt.isoformat())
    else:
        try:
            end_dt = _get_briefing_date()
        except Exception as exc:
            logger.warning("get_ohlcv missing/invalid BRIEFING_DATE: %s", exc)
            return {
                **result_base,
                "rows": [],
                "error": "missing_briefing_date",
                "message": "end_date was omitted and BRIEFING_DATE is not set/invalid",
            }
        result_base["end_date"] = end_dt.isoformat()

    if raw_start:
        start_dt, start_norm = _coerce_date(raw_start)
        if start_dt is None:
            logger.warning("get_ohlcv invalid start_date: %r", raw_start)
            return {
                **result_base,
                "rows": [],
                "error": "invalid_start_date",
                "message": "start_date must be YYYY-MM-DD (or YYYYMMDD)",
            }
        result_base["start_date"] = str(start_norm or start_dt.isoformat())
    else:
        start_dt = end_dt - timedelta(days=30)
        result_base["start_date"] = start_dt.isoformat()

    if start_dt > end_dt:
        logger.warning("get_ohlcv invalid range: start_date=%s end_date=%s", start_dt, end_dt)
        return {
            **result_base,
            "rows": [],
            "error": "invalid_date_range",
            "message": "start_date must be <= end_date",
        }

    yf_end = end_dt + timedelta(days=1)

    logger.info(
        "get_ohlcv 호출: ticker=%s, start=%s, end=%s, interval=%s",
        ticker,
        start_dt,
        end_dt,
        interval,
    )

    try:
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
    except Exception as exc:
        logger.warning("get_ohlcv yfinance.download 실패: ticker=%s (%s)", ticker, exc)
        return {**result_base, "rows": [], "error": "yfinance_download_failed", "message": str(exc)}

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
