"""LangSmith tracing 설정 유틸.

환경변수(`LANGSMITH_*`)만 사용해 트레이싱 설정을 확인/보정하고, 로그에 현재 상태를 남긴다.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def _mask(secret: str | None) -> str:
    if not secret:
        return ""
    if len(secret) <= 6:
        return "***"
    return f"{secret[:3]}***{secret[-3:]}"


def configure_tracing(logger: logging.Logger | None = None) -> None:
    """LANGSMITH_* 환경변수만 사용해 트레이싱 설정을 확인/보정하고 로그를 남긴다."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log = logger or logging.getLogger(__name__)

    api_key = os.getenv("LANGSMITH_API_KEY")
    project = os.getenv("LANGSMITH_PROJECT")
    tracing_flag = os.getenv("LANGSMITH_TRACING_V2")
    endpoint = os.getenv("LANGSMITH_ENDPOINT")

    if tracing_flag is not None:
        normalized = str(tracing_flag).lower()
        if normalized != tracing_flag:
            os.environ["LANGSMITH_TRACING_V2"] = normalized
            tracing_flag = normalized

    log.info(
        "LangSmith tracing 설정 확인: tracing_v2=%s, project=%s, endpoint=%s, api_key(masked)=%s",
        tracing_flag,
        project or "(unset)",
        endpoint or "(default)",
        _mask(api_key) or "(missing)",
    )

    if not api_key:
        log.warning("LangSmith API 키가 설정되지 않았습니다. .env의 LANGSMITH_API_KEY를 확인하세요.")
    if str(tracing_flag).lower() != "true":
        log.warning("LANGSMITH_TRACING_V2가 true가 아니면 트레이싱이 기록되지 않을 수 있습니다.")

    _CONFIGURED = True
