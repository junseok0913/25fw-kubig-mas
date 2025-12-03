"""Fetch day-end market context for Opening Agent using yfinance.

Outputs a JSON payload to data/context/market_context.json.
Temporary CSVs of raw yfinance responses are written to data/context/_tmp_csv
and cleaned up after context generation.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pytz
import yfinance as yf
from dotenv import load_dotenv


logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")
ET = pytz.timezone("America/New_York")
BASE_DIR = Path(__file__).resolve().parent.parent  # OpeningAgent/
ROOT_DIR = BASE_DIR.parent

CONTEXT_DIR = BASE_DIR / "data"
TMP_DIR = CONTEXT_DIR / "_tmp_csv"
OUTPUT_JSON = CONTEXT_DIR / "market_context.json"


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

OTHER_SPECS = [
    TickerSpec("Dollar Index", "DX-Y.NYB"),
]

COMMODITY_SPECS = [
    TickerSpec("WTI Crude Oil", "CL=F"),
    TickerSpec("Natural Gas", "NG=F"),
    TickerSpec("Gold", "GC=F"),
    TickerSpec("Silver", "SI=F"),
]

CRYPTO_SPEC = TickerSpec("Bitcoin", "BTC-USD")


def _ensure_dirs() -> None:
    # 컨텍스트 출력 폴더와 임시 CSV 폴더 생성
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)


def _to_utc_iso(ts: Any) -> str:
    """인덱스/타임스탬프를 UTC ISO 문자열로 변환."""
    p = pd.to_datetime(ts)
    # yfinance 일봉은 tz 정보가 없는 날짜 인덱스가 많음 → ET 자정으로 간주 후 UTC 변환
    if p.tzinfo is None:
        p = p.tz_localize(ET)
    return p.tz_convert("UTC").isoformat()


def _to_et(ts: Any) -> pd.Timestamp:
    """타임스탬프를 America/New_York(TZ-aware)으로 변환."""
    p = pd.to_datetime(ts)
    if p.tzinfo is None:
        # tz 미지정은 ET 자정으로 간주
        p = p.tz_localize(ET)
    else:
        p = p.tz_convert(ET)
    return p

def _normalize_ohlc_columns(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """yfinance가 멀티인덱스/비표준 컬럼으로 반환할 때 Close 필드를 복구."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    df = df.copy()

    # 멀티인덱스면 필드명 후보를 추출해 평탄화
    if isinstance(df.columns, pd.MultiIndex):
        normalized_cols = []
        for col in df.columns:
            if isinstance(col, tuple):
                parts = [str(p) for p in col if p is not None]
                # 뒤에서부터 open/high/low/close/adj close/volume 찾기
                field = next(
                    (p for p in reversed(parts) if p.lower() in {"open", "high", "low", "close", "adj close", "volume"}),
                    parts[-1] if parts else None,
                )
                normalized_cols.append(field)
            else:
                normalized_cols.append(col)
        df.columns = normalized_cols

    # 대소문자 정규화
    df.columns = [str(c).strip() for c in df.columns]

    # Close가 없고 Adj Close만 있으면 대체
    col_map = {c.lower(): c for c in df.columns}
    if "close" not in col_map and "adj close" in col_map:
        df = df.rename(columns={col_map["adj close"]: "Close"})
    return df


def _as_of_fields(index_value: Any) -> Dict[str, str]:
    """인덱스 값에서 UTC/ET 기준 시각과 ET 날짜를 생성."""
    et_ts = _to_et(index_value)
    return {
        "as_of_utc": _to_utc_iso(index_value),
        "as_of_et": et_ts.isoformat(),
        "as_of_et_date": et_ts.date().isoformat(),
    }


def _save_raw_csv(name: str, df: pd.DataFrame) -> None:
    """Save raw yfinance frame for traceability."""
    # 계산 전 원본 응답을 남겨 디버깅/검증 용도로 활용
    try:
        df.to_csv(TMP_DIR / f"{name}.csv")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to save raw CSV for %s: %s", name, exc)


def _cleanup_tmp() -> None:
    # 컨텍스트 생성 후 임시 CSV 제거
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR, ignore_errors=True)


def _load_env() -> None:
    """리포 루트 .env 로드 (독립 실행 시 사용)."""
    load_dotenv(ROOT_DIR / ".env", override=False)


def _fetch_daily_frame(ticker: str, days: int = 5) -> pd.DataFrame:
    # 최근 N영업일 일봉 조회 (변동폭 계산에 전일 종가 필요)
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


def _latest_row_by_et(df: pd.DataFrame) -> tuple[pd.Series, Optional[pd.Series], Any]:
    """
    ET 날짜 기준으로 당일(row)과 전일(prev) 시리즈를 선택.
    반환: (latest_row, prev_row_or_None, latest_index_value)
    """
    if df.empty:
        return df.iloc[0], None, None  # will not be used; caller handles empties

    idx_et = pd.to_datetime(df.index)
    if idx_et.tz is None:
        # tz 없는 일봉 인덱스 → ET 자정으로 간주
        idx_et = idx_et.tz_localize(ET)
    else:
        idx_et = idx_et.tz_convert(ET)
    idx_utc = idx_et.tz_convert("UTC")
    target_date = pd.Timestamp.now(tz=ET).date()

    mask_today = idx_et.date == target_date
    if mask_today.any():
        # 당일 ET 데이터 중 마지막 행 사용
        last_pos = [i for i, v in enumerate(mask_today) if v][-1]
    else:
        # 당일 데이터가 없으면 가장 마지막 행 사용
        last_pos = len(df) - 1

    latest = df.iloc[last_pos]
    prev = df.iloc[last_pos - 1] if last_pos - 1 >= 0 else None
    latest_idx = idx_utc[last_pos]
    return latest, prev, latest_idx


def _build_ohlc_payload(spec: TickerSpec, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    # 기본 OHLC + 전일 대비 등락폭/등락률 산출
    if df.empty:
        return None
    latest, prev, latest_idx = _latest_row_by_et(df)

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


def _build_yield_payload(spec: TickerSpec, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    # 채권 수익률은 ^TNX 스케일(10배)을 pct·bp로 환산
    payload = _build_ohlc_payload(spec, df)
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
    # as_of 필드는 payload의 기준 시각(ET 기준) 사용
    result.update({k: payload[k] for k in payload if k.startswith("as_of")})
    return result


def _build_btc_payload(spec: TickerSpec) -> Optional[Dict[str, Any]]:
    # 비트코인은 24시간 창을 1시간봉으로 산출(등락률은 24h 기준)
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

    _save_raw_csv(spec.ticker.replace("=", "_"), df)

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


def build_context() -> Dict[str, Any]:
    _ensure_dirs()
    results: Dict[str, Any] = {
        "generated_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "generated_at_kst": pd.Timestamp.now(tz=KST).isoformat(),
        "indices": [],        # 지수: OHLC + 등락폭(pt) + 등락률(%)
        "yields": [],         # 금리: 수익률(%) + 변동폭(bp)
        "dollar_index": [],   # 달러인덱스: OHLC + 등락률(%)
        "commodities": [],    # 원자재: OHLC + 등락률(%)
        "crypto": [],         # 비트코인: 24h 창 기준 OHLC + 24h 등락률(%)
    }

    for spec in INDEX_SPECS:
        df = _fetch_daily_frame(spec.ticker)
        _save_raw_csv(spec.ticker.replace("^", ""), df)
        payload = _build_ohlc_payload(spec, df)
        if payload:
            results["indices"].append(payload)

    for spec in YIELD_SPECS:
        df = _fetch_daily_frame(spec.ticker)
        _save_raw_csv(spec.ticker.replace("^", ""), df)
        payload = _build_yield_payload(spec, df)
        if payload:
            results["yields"].append(payload)

    for spec in OTHER_SPECS:
        df = _fetch_daily_frame(spec.ticker)
        _save_raw_csv(spec.ticker.replace("=", "_"), df)
        payload = _build_ohlc_payload(spec, df)
        if payload:
            results["dollar_index"].append(payload)

    for spec in COMMODITY_SPECS:
        df = _fetch_daily_frame(spec.ticker)
        _save_raw_csv(spec.ticker.replace("=", "_"), df)
        payload = _build_ohlc_payload(spec, df)
        if payload:
            results["commodities"].append(payload)

    btc_payload = _build_btc_payload(CRYPTO_SPEC)
    if btc_payload:
        results["crypto"].append(btc_payload)

    return results


def main() -> None:
    _load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    context = build_context()
    OUTPUT_JSON.write_text(json.dumps(context, indent=2), encoding="utf-8")
    logger.info("Wrote market context to %s", OUTPUT_JSON)
    _cleanup_tmp()


if __name__ == "__main__":
    main()
