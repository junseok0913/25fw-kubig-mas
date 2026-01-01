"""
멀티에이전트 환경 초기화
- .env 파일 로드
- LangSmith(LangChain) 트레이싱 기본 설정
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def init_env():
    project_root = Path(__file__).resolve().parents[1]
    dotenv_path = project_root / ".env"
    load_dotenv(dotenv_path=dotenv_path if dotenv_path.exists() else None)

    # LangSmith / LangGraph 추적 기본값
    if os.getenv("LANGCHAIN_API_KEY") or os.getenv("LANGSMITH_API_KEY"):
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGCHAIN_PROJECT", "stock-morning")


init_env()
