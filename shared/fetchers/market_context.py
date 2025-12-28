"""Market context generator (yfinance)."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import date as date_type
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pytz
import yfinance as yf
from dotenv import load_dotenv

from shared.config import ROOT_DIR

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")
ET = pytz.timezone("America/New_York")


@dataclass
class TickerSpec:
    name: str
    ticker: str


INDEX_SPECS = [
    TickerSpec("S&P 500", "^GSPC"),
    TickerSpec("Nasdaq Composite", "^IXIC"),
    TickerSpec("Nasdaq 100", "^NDX"),
    TickerSpec("Dow Jones Industrial", "^DJI"),
    TickerSpec("Russell 2000", "^RUT"),
    TickerSpec("NYSE Composite", "^NYA"),
]

YIELD_SPECS = [
    TickerSpec("US 10Y Treasury Yield", "^TNX"),
    TickerSpec("US 30Y Treasury Yield", "^TYX"),
    TickerSpec("US 2Y Treasury Yield", "^IRX"),
]

OTHER_SPECS = [TickerSpec("Dollar Index", "DX-Y.NYB")]

COMMODITY_SPECS = [
    TickerSpec("WTI Crude Oil", "CL=F"),
    TickerSpec("Natural Gas", "NG=F"),
    TickerSpec("Gold", "GC=F"),
    TickerSpec("Silver", "SI=F"),
]

CRYPTO_SPEC = TickerSpec("Bitcoin", "BTC-USD")


def _load_env() -> None:
    load_dotenv(ROOT_DIR / ".env", override=False)


def _to_utc_iso(ts: Any) -> str:
    p = pd.to_datetime(ts)
    if p.tzinfo is None:
        p = p.tz_localize(ET)
    return p.tz_convert("UTC").isoformat()


def _to_et(ts: Any) -> pd.Timestamp:
    p = pd.to_datetime(ts)
    if p.tzinfo is None:
        p = p.tz_localize(ET)
    else:
        p = p.tz_convert(ET)
    return p


def _normalize_ohlc_columns(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        normalized_cols = []
        for col in df.columns:
            if isinstance(col, tuple):
                parts = [str(p) for p in col if p is not None]
                field = next(
                    (p for p in reversed(parts) if p.lower() in {"open", "high", "low", "close", "adj close", "volume"}),
                    parts[-1] if parts else None,
                )
                normalized_cols.append(field)
            else:
                normalized_cols.append(col)
        df.columns = normalized_cols

    df.columns = [str(c).strip() for c in df.columns]

    col_map = {c.lower(): c for c in df.columns}
    if "close" not in col_map and "adj close" in col_map:
        df = df.rename(columns={col_map["adj close"]: "Close"})
    return df


def _as_of_fields(index_value: Any) -> Dict[str, str]:
    et_ts = _to_et(index_value)
    return {
        "as_of_utc": _to_utc_iso(index_value),
        "as_of_et": et_ts.isoformat(),
        "as_of_et_date": et_ts.date().isoformat(),
    }


def _save_raw_csv(name: str, df: pd.DataFrame, tmp_dir: Optional[Path]) -> None:
    if tmp_dir is None:
        return
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(tmp_dir / f"{name}.csv")
    except Exception as exc:
        logger.warning("Failed to save raw CSV for %s: %s", name, exc)


def _cleanup_tmp(tmp_dir: Optional[Path]) -> None:
    if tmp_dir and tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _fetch_daily_frame(ticker: str, days: int = 5) -> pd.DataFrame:
    df = yf.download(
        ticker,
        period=f"{days}d",
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    df = _normalize_ohlc_columns(df, ticker)
    if "Close" not in df.columns:
        logger.warning("Close column missing for %s; skipping", ticker)
        return pd.DataFrame()
    df = df.dropna(subset=["Close"])
    return df


def _latest_row_by_et(df: pd.DataFrame, target_date: date_type) -> tuple[pd.Series, Optional[pd.Series], Any]:
    if df.empty:
        return df.iloc[0], None, None

    idx_et = pd.to_datetime(df.index)
    if idx_et.tz is None:
        idx_et = idx_et.tz_localize(ET)
    else:
        idx_et = idx_et.tz_convert(ET)
    idx_utc = idx_et.tz_convert("UTC")

    mask_today = idx_et.date == target_date
    if mask_today.any():
        last_pos = [i for i, v in enumerate(mask_today) if v][-1]
    else:
        last_pos = len(df) - 1

    latest = df.iloc[last_pos]
    prev = df.iloc[last_pos - 1] if last_pos - 1 >= 0 else None
    latest_idx = idx_utc[last_pos]
    return latest, prev, latest_idx


def _build_ohlc_payload(spec: TickerSpec, df: pd.DataFrame, target_date: date_type) -> Optional[Dict[str, Any]]:
    if df.empty:
        return None
    latest, prev, latest_idx = _latest_row_by_et(df, target_date)

    close = latest.get("Close")
    prev_close = prev.get("Close") if prev is not None else None
    change_pt = None
    change_pct = None
    if prev_close not in (None, 0):
        change_pt = float(close) - float(prev_close)
        change_pct = (float(close) / float(prev_close) - 1.0) * 100.0

    payload = {
        "name": spec.name,
        "ticker": spec.ticker,
        "open": float(latest.get("Open")) if pd.notna(latest.get("Open")) else None,
        "high": float(latest.get("High")) if pd.notna(latest.get("High")) else None,
        "low": float(latest.get("Low")) if pd.notna(latest.get("Low")) else None,
        "close": float(close) if pd.notna(close) else None,
        "change_pt": change_pt,
        "change_pct": change_pct,
    }
    payload.update(_as_of_fields(latest_idx))
    return payload


def _build_yield_payload(spec: TickerSpec, df: pd.DataFrame, target_date: date_type) -> Optional[Dict[str, Any]]:
    payload = _build_ohlc_payload(spec, df, target_date)
    if not payload or payload["close"] is None:
        return None

    yield_pct = payload["close"] / 10.0
    prev_yield_pct = None
    if payload["change_pt"] is not None and payload["change_pt"] != 0:
        prev_yield_pct = (payload["close"] - payload["change_pt"]) / 10.0

    change_bp = None
    if prev_yield_pct is not None:
        change_bp = (yield_pct - prev_yield_pct) * 100.0

    result = {
        "name": spec.name,
        "ticker": spec.ticker,
        "yield_pct": yield_pct,
        "change_bp": change_bp,
    }
    result.update({k: payload[k] for k in payload if k.startswith("as_of")})
    return result


def _build_btc_payload(spec: TickerSpec, tmp_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    df = yf.download(
        spec.ticker,
        period="2d",
        interval="1h",
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    df = _normalize_ohlc_columns(df, spec.ticker)
    if "Close" not in df.columns:
        logger.warning("Close column missing for %s; skipping", spec.ticker)
        return None
    df = df.dropna(subset=["Close"])
    if df.empty:
        return None

    last_idx = df.index[-1]
    window_start = last_idx - pd.Timedelta(hours=24)
    window_df = df[df.index >= window_start]
    if window_df.empty:
        window_df = df

    close = float(window_df["Close"].iloc[-1])
    open_ = float(window_df["Open"].iloc[0])
    high = float(window_df["High"].max())
    low = float(window_df["Low"].min())

    prev_price = float(window_df["Close"].iloc[0]) if len(window_df) >= 2 else None
    change_pct_24h = None
    if prev_price not in (None, 0):
        change_pct_24h = (close / prev_price - 1.0) * 100.0

    _save_raw_csv(spec.ticker.replace("=", "_"), df, tmp_dir)

    as_of = _as_of_fields(window_df.index[-1])
    window_start_iso = _to_utc_iso(window_df.index[0])

    return {
        "name": spec.name,
        "ticker": spec.ticker,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "change_pct_24h": change_pct_24h,
        "as_of_utc": as_of["as_of_utc"],
        "as_of_et": as_of["as_of_et"],
        "as_of_et_date": as_of["as_of_et_date"],
        "window_start_utc": window_start_iso,
    }


def build_context(anchor_date: Optional[date_type] = None, tmp_dir: Optional[Path] = None) -> Dict[str, Any]:
    target_date = anchor_date or pd.Timestamp.now(tz=ET).date()
    results: Dict[str, Any] = {
        "generated_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "generated_at_kst": pd.Timestamp.now(tz=KST).isoformat(),
        "indices": [],
        "yields": [],
        "dollar_index": [],
        "commodities": [],
        "crypto": [],
    }

    for spec in INDEX_SPECS:
        df = _fetch_daily_frame(spec.ticker)
        _save_raw_csv(spec.ticker.replace("^", ""), df, tmp_dir)
        payload = _build_ohlc_payload(spec, df, target_date)
        if payload:
            results["indices"].append(payload)

    for spec in YIELD_SPECS:
        df = _fetch_daily_frame(spec.ticker)
        _save_raw_csv(spec.ticker.replace("^", ""), df, tmp_dir)
        payload = _build_yield_payload(spec, df, target_date)
        if payload:
            results["yields"].append(payload)

    for spec in OTHER_SPECS:
        df = _fetch_daily_frame(spec.ticker)
        _save_raw_csv(spec.ticker.replace("=", "_"), df, tmp_dir)
        payload = _build_ohlc_payload(spec, df, target_date)
        if payload:
            results["dollar_index"].append(payload)

    for spec in COMMODITY_SPECS:
        df = _fetch_daily_frame(spec.ticker)
        _save_raw_csv(spec.ticker.replace("=", "_"), df, tmp_dir)
        payload = _build_ohlc_payload(spec, df, target_date)
        if payload:
            results["commodities"].append(payload)

    btc_payload = _build_btc_payload(CRYPTO_SPEC, tmp_dir)
    if btc_payload:
        results["crypto"].append(btc_payload)

    return results


def generate(anchor_date: date_type, cache_dir: Path) -> Dict[str, Any]:
    """Generate market context JSON into cache_dir."""
    _load_env()
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = cache_dir / "_tmp_csv"

    context = build_context(anchor_date=anchor_date, tmp_dir=tmp_dir)
    output_path = cache_dir / "market_context.json"
    output_path.write_text(json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote market context to %s", output_path)

    _cleanup_tmp(tmp_dir)
    return context
