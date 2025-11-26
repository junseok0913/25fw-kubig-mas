"""오프닝 에이전트 (LangGraph 기반)

목표:
- yfinance로 미리 수집해둔 `data/market_context.json`만을 활용해
  진행자-해설자 형식의 오프닝 대본을 작성한다.
- 네트워크 호출이나 추가 Tool 사용 없이, 주어진 컨텍스트만으로 LLM을 실행한다.

주의:
- OpenAI API 키는 .env 또는 환경변수 `OPENAI_API_KEY`로 주입해야 한다.
- 모델은 5.1 계열 `reasoning_effort='medium'` 설정을 사용한다.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, TypedDict

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
import yaml

from src import context_today, prefetch
from src.utils.tracing import configure_tracing
from src.tools import (
    count_keyword_frequency,
    get_news_content,
    get_news_list,
    get_ohlcv,
    list_downloaded_bodies,
)

# 로깅 설정: CLI 실행 시에도 깔끔하게 남도록 INFO 기본값 사용
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 컨텍스트 파일 경로 (yfinance 수집 결과)
CONTEXT_PATH = Path("data/market_context.json")
# 프롬프트 YAML 경로
PROMPT_PATH = Path("prompt/opening_script.yaml")
# 도구 레지스트리
TOOL_REGISTRY: Dict[str, Any] = {
    "prefetch_news": prefetch.prefetch_news,
    "get_news_list": get_news_list,
    "get_news_content": get_news_content,
    "list_downloaded_bodies": list_downloaded_bodies,
    "count_keyword_frequency": count_keyword_frequency,
    "get_ohlcv": get_ohlcv,
}


class OpeningState(TypedDict, total=False):
    """그래프 상태 정의: 컨텍스트와 대본 텍스트를 전달."""

    context_json: Dict[str, Any]
    news_meta: Dict[str, Any]
    tools: Dict[str, Any]
    script_markdown: str


def load_tools_node(state: OpeningState) -> OpeningState:
    """사용 가능한 Tool 레지스트리를 상태에 적재."""
    return {**state, "tools": TOOL_REGISTRY}


def prefetch_node(state: OpeningState) -> OpeningState:
    """DynamoDB에서 뉴스 메타데이터를 사전 수집한다."""
    try:
        payload = prefetch.prefetch_news()
        logger.info("프리페치 완료: %d건", payload.get("count", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("프리페치 실패, 그래프는 계속 진행합니다: %s", exc)
        payload = {}
    return {**state, "news_meta": payload}


def load_context_node(state: OpeningState) -> OpeningState:
    """컨텍스트를 최신 상태로 생성하고 로드한다."""
    # yfinance 기반 컨텍스트를 최신으로 갱신
    try:
        context_today.main()
        logger.info("market_context.json을 갱신했습니다.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("컨텍스트 갱신 실패, 기존 캐시 사용: %s", exc)

    if not CONTEXT_PATH.exists():
        raise FileNotFoundError(f"컨텍스트 파일이 없습니다: {CONTEXT_PATH}")
    with open(CONTEXT_PATH, "r", encoding="utf-8") as f:
        context = json.load(f)
    logger.info("Loaded market context from %s", CONTEXT_PATH)
    return {**state, "context_json": context}


def load_prompt() -> Dict[str, str]:
    """프롬프트 YAML에서 system/user 텍스트를 읽어온다."""
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"프롬프트 파일이 없습니다: {PROMPT_PATH}")
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    system = raw.get("system", "")
    user_template = raw.get("user_template", "")
    if not system or not user_template:
        raise ValueError("프롬프트 YAML에 system 또는 user_template가 비어 있습니다.")
    return {"system": system, "user_template": user_template}


def _build_llm() -> ChatOpenAI:
    """
    OpenAI LLM 인스턴스 생성 (context-7/5.1 스타일 설정).

    - 기본 모델: gpt-5.1 (OPENAI_MODEL로 재정의 가능)
    - reasoning_effort: medium (OPENAI_REASONING_EFFORT로 재정의)
    - LangGraph에서 요구하는 ChatModel 인터페이스를 준수
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY가 설정되지 않았습니다. .env 또는 환경변수를 확인하세요.")

    model_name = os.getenv("OPENAI_MODEL", "gpt-5.1")
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "medium")
    temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.0"))

    configure_tracing(logger=logger)

    return ChatOpenAI(
        model=model_name,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        # responses/chat-completions 공통으로 받는 표준 필드만 전달
        model_kwargs={
            "response_format": {"type": "text"},
        },
    )


def draft_script_node(state: OpeningState) -> OpeningState:
    """컨텍스트 JSON만을 사용해 오프닝 대본을 생성."""
    context = state.get("context_json")
    if not context:
        raise ValueError("context_json이 비어 있습니다. load_context_node를 확인하세요.")

    llm = _build_llm()
    prompt_cfg = load_prompt()

    # YAML 템플릿에 컨텍스트를 삽입해 user 프롬프트 구성
    user_prompt = prompt_cfg["user_template"].replace(
        "{{context_json}}", json.dumps(context, ensure_ascii=False, indent=2)
    )

    logger.info("Invoking LLM with provided market context only.")
    response = llm.invoke(
        [
            {"role": "system", "content": prompt_cfg["system"]},
            {"role": "user", "content": user_prompt},
        ]
    )
    script = response.content if hasattr(response, "content") else str(response)

    return {**state, "script_markdown": script}


def build_graph():
    """LangGraph 그래프를 구성하고 반환."""
    graph = StateGraph(OpeningState)
    graph.add_node("prefetch_news", prefetch_node)
    graph.add_node("load_tools", load_tools_node)
    graph.add_node("load_context", load_context_node)
    graph.add_node("draft_script", draft_script_node)

    graph.add_edge(START, "prefetch_news")
    graph.add_edge("prefetch_news", "load_tools")
    graph.add_edge("load_tools", "load_context")
    graph.add_edge("load_context", "draft_script")
    graph.add_edge("draft_script", END)

    return graph.compile()


def cleanup_cache() -> None:
    """에이전트 실행 후 캐시 파일을 정리한다."""
    # 시장 컨텍스트/뉴스 메타/본문 캐시 제거
    news_files = [
        Path("data/opening/news_list.json"),
        Path("data/opening/titles.txt"),
        Path("data/market_context.json"),
    ]
    bodies_dir = Path("data/opening/bodies")
    for file in news_files:
        if file.exists():
            try:
                file.unlink()
                logger.info("삭제: %s", file)
            except Exception as exc:  # noqa: BLE001
                logger.warning("파일 삭제 실패 %s: %s", file, exc)
    if bodies_dir.exists():
        for body_file in bodies_dir.glob("*.txt"):
            try:
                body_file.unlink()
                logger.info("삭제: %s", body_file)
            except Exception as exc:  # noqa: BLE001
                logger.warning("본문 캐시 삭제 실패 %s: %s", body_file, exc)
    # context_today 임시 CSV 폴더 정리
    tmp_dir = Path("data/_tmp_csv")
    if tmp_dir.exists():
        for item in tmp_dir.glob("*"):
            try:
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    item.rmdir()
                logger.info("삭제: %s", item)
            except Exception as exc:  # noqa: BLE001
                logger.warning("임시 파일 삭제 실패 %s: %s", item, exc)


def main() -> None:
    """그래프 실행 진입점."""
    # .env 로드 (없을 경우 무시)
    load_dotenv()

    workflow = build_graph()
    # 빈 상태로 시작하여 컨텍스트 로드 → 대본 생성 순으로 실행
    result = workflow.invoke({})

    script = result.get("script_markdown", "")
    print("\n=== 오프닝 대본 (컨텍스트 기반) ===\n")
    print(script)

    # 실행이 끝나면 캐시된 파일 정리
    try:
        cleanup_cache()
    except Exception as exc:  # noqa: BLE001
        logger.warning("캐시 삭제 중 오류: %s", exc)


if __name__ == "__main__":
    main()
