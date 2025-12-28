"""Shared LLM builder."""

from __future__ import annotations

import logging
import os
from typing import Optional

from langchain_openai import ChatOpenAI

from .tracing import configure_tracing


def _getenv_nonempty(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def build_llm(prefix: str, *, logger: Optional[logging.Logger] = None) -> ChatOpenAI:
    """Build ChatOpenAI with prefix-specific overrides.

    Example prefixes:
    - OPENING
    - THEME_WORKER
    - THEME_REFINER
    - CLOSING
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY가 설정되지 않았습니다. .env 또는 환경변수를 확인하세요.")

    prefix_key = f"{prefix.strip('_')}_" if prefix else ""

    def cfg(key: str, default_env_key: str, default: str) -> str:
        return _getenv_nonempty(f"{prefix_key}{key}", _getenv_nonempty(default_env_key, default))

    model_name = cfg("OPENAI_MODEL", "OPENAI_MODEL", "gpt-5.1")
    reasoning_effort_raw = cfg("OPENAI_REASONING_EFFORT", "OPENAI_REASONING_EFFORT", "")
    temperature = float(cfg("OPENAI_TEMPERATURE", "OPENAI_TEMPERATURE", "0.0"))
    timeout = float(cfg("OPENAI_TIMEOUT", "OPENAI_TIMEOUT", "120"))
    max_retries = int(cfg("OPENAI_MAX_RETRIES", "OPENAI_MAX_RETRIES", "2"))

    configure_tracing(logger=logger)

    reasoning_effort_norm = (reasoning_effort_raw or "").strip().lower()
    llm_kwargs: dict[str, object] = {
        "model": model_name,
        "temperature": temperature,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if reasoning_effort_norm and reasoning_effort_norm not in {"none", "null", "off", "false"}:
        llm_kwargs["reasoning_effort"] = reasoning_effort_raw

    return ChatOpenAI(**llm_kwargs)
