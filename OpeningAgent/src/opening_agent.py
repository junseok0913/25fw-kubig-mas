"""오프닝 에이전트 (LangGraph 기반)

목표:
- yfinance로 미리 수집해둔 `data/market_context.json`과 뉴스 데이터를 활용해
  진행자-해설자 형식의 오프닝 대본을 작성한다.
- ReAct 패턴으로 Tool을 사용하여 뉴스 분석 및 테마 도출을 수행한다.

주의:
- OpenAI API 키는 .env 또는 환경변수 `OPENAI_API_KEY`로 주입해야 한다.
- 모델은 5.1 계열 `reasoning_effort='medium'` 설정을 사용한다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Dict, Sequence, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
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
TITLES_PATH = Path("data/opening/titles.txt")

# LangChain Tool 리스트 (LLM에 바인딩할 도구들)
TOOLS = [
    get_news_list,
    get_news_content,
    list_downloaded_bodies,
    count_keyword_frequency,
    get_ohlcv,
]


def _get_tools_description() -> str:
    """Tool 목록을 사람이 읽기 쉬운 설명으로 반환한다."""
    descriptions = []
    for tool in TOOLS:
        name = tool.name
        desc = tool.description or ""
        descriptions.append(f"- {name}: {desc}")
    return "\n".join(descriptions)


def _top_words_from_titles(limit: int = 30) -> list[dict[str, Any]]:
    """titles.txt에서 단어 빈도 상위 N개를 계산한다."""
    if not Path("data/opening/titles.txt").exists():
        return []
    text = Path("data/opening/titles.txt").read_text(encoding="utf-8")
    tokens = re.findall(r"[A-Za-z0-9$%+\\-']+", text.lower())
    counter = Counter(tokens)
    top = counter.most_common(limit)
    return [{"word": w, "count": c} for w, c in top]


class OpeningState(TypedDict, total=False):
    """그래프 상태 정의: 메시지 기반 ReAct 패턴."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    context_json: Dict[str, Any]
    news_meta: Dict[str, Any]
    script_markdown: str


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
    # 제목 상위 30개 단어 빈도 추가
    context["title_top_words"] = _top_words_from_titles(limit=30)
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
    )


def _prepare_initial_messages(state: OpeningState) -> OpeningState:
    """초기 시스템/사용자 메시지를 준비한다."""
    context = state.get("context_json")
    if not context:
        raise ValueError("context_json이 비어 있습니다. load_context_node를 확인하세요.")

    prompt_cfg = load_prompt()

    # {{tools}} 플레이스홀더를 실제 도구 설명으로 대체
    system_prompt = prompt_cfg["system"].replace(
        "{{tools}}", _get_tools_description()
    )

    # {{context_json}} 플레이스홀더를 컨텍스트로 대체
    user_prompt = prompt_cfg["user_template"].replace(
        "{{context_json}}", json.dumps(context, ensure_ascii=False, indent=2)
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]

    return {**state, "messages": messages}


def agent_node(state: OpeningState) -> OpeningState:
    """Tool이 바인딩된 LLM을 호출하여 응답을 생성한다."""
    llm = _build_llm()
    llm_with_tools = llm.bind_tools(TOOLS)

    messages = state.get("messages", [])
    logger.info("Agent 호출: %d개 메시지", len(messages))

    response = llm_with_tools.invoke(messages)
    return {**state, "messages": [response]}


def should_continue(state: OpeningState) -> str:
    """Tool 호출이 필요한지 판단하여 다음 노드를 결정한다."""
    messages = state.get("messages", [])
    if not messages:
        return "end"

    last_message = messages[-1]
    # AIMessage가 tool_calls를 포함하면 tools 노드로
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        logger.info("Tool 호출 감지: %d개", len(last_message.tool_calls))
        return "tools"
    return "end"


def extract_script_node(state: OpeningState) -> OpeningState:
    """최종 메시지에서 대본을 추출한다."""
    messages = state.get("messages", [])
    script = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            script = msg.content
            break
    return {**state, "script_markdown": script}


def build_graph():
    """LangGraph 그래프를 구성하고 반환.

    ReAct 패턴:
    START → prefetch_news → load_context → prepare_messages
          → agent ⇄ tools (반복)
          → extract_script → END
    """
    graph = StateGraph(OpeningState)

    # 노드 정의
    graph.add_node("prefetch_news", prefetch_node)
    graph.add_node("load_context", load_context_node)
    graph.add_node("prepare_messages", _prepare_initial_messages)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("extract_script", extract_script_node)

    # 엣지 정의: 초기화 흐름
    graph.add_edge(START, "prefetch_news")
    graph.add_edge("prefetch_news", "load_context")
    graph.add_edge("load_context", "prepare_messages")
    graph.add_edge("prepare_messages", "agent")

    # ReAct 루프: agent → (tools → agent) 또는 → extract_script
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            "end": "extract_script",
        },
    )
    graph.add_edge("tools", "agent")

    # 최종 대본 추출 후 종료
    graph.add_edge("extract_script", END)

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
