"""
시간 관련 유틸리티
오전 6시(한국 기준) 배치 사이클에 맞춰 날짜/시간 윈도우를 계산합니다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

KST = timezone(timedelta(hours=9))


def _ensure_kst(dt: datetime) -> datetime:
    """datetime이 timezone 정보를 갖지 않으면 KST를 부여하고, 있으면 KST로 변환"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def get_korea_batch_window(
    now: Optional[datetime] = None,
    base_hour: int = 6,
) -> Tuple[datetime, datetime]:
    """
    오전 base_hour 기준으로 하루 단위 배치 구간(시작, 종료)을 반환합니다.
    예) base_hour=6이면 [어제 06:00, 오늘 06:00) 범위를 돌려줌.
    """
    kst_now = _ensure_kst(now or datetime.now(KST))
    cutoff = kst_now.replace(hour=base_hour, minute=0, second=0, microsecond=0)

    if kst_now >= cutoff:
        start = cutoff - timedelta(days=1)
        end = cutoff
    else:
        start = cutoff - timedelta(days=2)
        end = cutoff - timedelta(days=1)

    return start, end


def get_last_24h_window(
    now: Optional[datetime] = None,
) -> Tuple[datetime, datetime]:
    """
    현재 시각 기준 직전 24시간 구간(시작, 종료)을 반환합니다.
    """
    kst_now = _ensure_kst(now or datetime.now(KST))
    start = kst_now - timedelta(hours=24)
    return start, kst_now


def get_korea_batch_yesterday(
    now: Optional[datetime] = None,
    base_hour: int = 6,
) -> str:
    """
    오전 base_hour 배치를 기준으로 '어제' 날짜(YYYY-MM-DD)를 반환합니다.
    """
    kst_now = _ensure_kst(now or datetime.now(KST))
    if kst_now.hour >= base_hour:
        target = (kst_now - timedelta(days=1)).date()
    else:
        target = (kst_now - timedelta(days=2)).date()
    return target.isoformat()


def parse_iso_datetime(raw: Optional[str]) -> Optional[datetime]:
    """
    ISO 형식의 datetime 문자열을 파싱합니다.
    'Z'는 '+00:00'으로 정규화됩니다.
    """
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def utc_to_korea_batch_date(
    utc_datetime_str: str,
    base_hour: int = 6,
) -> str:
    """
    UTC datetime 문자열을 한국 배치 날짜(YYYY-MM-DD)로 변환합니다.
    base_hour 이전이면 전날로 간주합니다.
    """
    try:
        utc_dt = datetime.fromisoformat(utc_datetime_str.replace("Z", "+00:00"))
        korea_dt = utc_dt.astimezone(KST)
        if korea_dt.hour < base_hour:
            korea_dt -= timedelta(days=1)
        return korea_dt.date().isoformat()
    except Exception:
        try:
            dt = datetime.fromisoformat(utc_datetime_str.replace("Z", "+00:00"))
            return dt.date().isoformat()
        except Exception:
            return datetime.now(KST).date().isoformat()


def to_kst(dt: datetime) -> datetime:
    """주어진 datetime을 KST로 변환"""
    return _ensure_kst(dt)
