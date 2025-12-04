"""ThemeAgent LangGraph 구현.

OpeningAgent가 생성한 테마/오프닝 스크립트를 입력받아
- 테마별 심층 스크립트를 ThemeWorkerGraph × N에서 생성하고
- Opening+Theme 전체를 자연스럽게 이어지도록 Refiner가 편집한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Sequence, TypedDict

import yaml
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from . import prefetch
from .tools import (
    count_keyword_frequency,
    get_news_content,
    get_news_list,
    get_ohlcv,
    list_downloaded_bodies,
)
from .utils.tracing import configure_tracing

# 로깅 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent  # ThemeAgent/
ROOT_DIR = BASE_DIR.parent
PROMPT_PATH = BASE_DIR / "prompt/theme_script.yaml"
OUTPUT_PATH = BASE_DIR / "data/theme_result.json"

# LangChain Tool 리스트 (LLM에 바인딩할 도구들)
TOOLS = [
    get_news_list,
    get_news_content,
    list_downloaded_bodies,
    count_keyword_frequency,
    get_ohlcv,
]


# ==== 공통 유틸 ====
def _load_env() -> None:
    """리포지토리 루트(.env)에서 환경변수를 로드한다."""
    load_dotenv(ROOT_DIR / ".env", override=False)


def _get_tools_description() -> str:
    """Tool 목록을 사람이 읽기 쉬운 설명으로 반환한다."""
    descriptions = []
    for tool in TOOLS:
        name = tool.name
        desc = tool.description or ""
        descriptions.append(f"- {name}: {desc}")
    return "\n".join(descriptions)


def _format_date_korean(date_yyyymmdd: str) -> str:
    """YYYYMMDD를 '11월 25일' 형식으로 변환한다."""
    dt = datetime.strptime(date_yyyymmdd, "%Y%m%d")
    return f"{dt.month}월 {dt.day}일"


def _build_llm() -> ChatOpenAI:
    """OpenAI LLM 인스턴스를 생성한다."""
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


def _parse_json_from_response(content: str) -> Dict[str, Any]:
    """AI 응답에서 JSON을 추출하고 파싱한다."""
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", content)
    if json_match:
        json_str = json_match.group(1)
    else:
        json_str = content.strip()

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as exc:  # noqa: BLE001
        logger.error("JSON 파싱 실패: %s", exc)
        return {}


def _load_prompt() -> Dict[str, str]:
    """theme_script.yaml에서 worker/refiner 프롬프트를 읽어온다."""
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"프롬프트 파일이 없습니다: {PROMPT_PATH}")
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    worker = raw.get("worker", {})
    refiner = raw.get("refiner", {})
    return {
        "worker_system": worker.get("system", ""),
        "worker_user_template": worker.get("user_template", ""),
        "refiner_system": refiner.get("system", ""),
        "refiner_user_template": refiner.get("user_template", ""),
    }


# ==== State 타입 ====
class NewsSource(TypedDict):
    pk: str
    title: str


class Theme(TypedDict):
    headline: str
    description: str
    related_news: List[NewsSource]


class ScriptTurn(TypedDict):
    speaker: Literal["진행자", "해설자"]
    text: str
    sources: List[NewsSource]


class ThemeState(TypedDict, total=False):
    date: str
    nutshell: str
    themes: List[Theme]
    base_scripts: List[ScriptTurn]
    theme_scripts: List[List[ScriptTurn]]
    scripts: List[ScriptTurn]


class ThemeWorkerState(TypedDict, total=False):
    date: str
    nutshell: str
    theme: Theme
    base_scripts: List[ScriptTurn]
    messages: Annotated[Sequence[BaseMessage], add_messages]
    theme_context: Dict[str, Any]
    scripts: List[ScriptTurn]


# ==== ThemeWorkerGraph 노드 ====
def prefetch_news_node(state: ThemeWorkerState) -> ThemeWorkerState:
    """DynamoDB에서 뉴스 메타데이터를 사전 수집한다."""
    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다.")

    try:
        date_obj = datetime.strptime(date_str, "%Y%m%d").date()
    except ValueError:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()

    os.environ.setdefault("BRIEFING_DATE", date_obj.strftime("%Y%m%d"))

    try:
        payload = prefetch.prefetch_news(today=date_obj)
        logger.info("ThemeWorker 프리페치 완료: %d건 (날짜: %s)", payload.get("count", 0), date_str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("프리페치 실패, 그래프는 계속 진행합니다: %s", exc)
        payload = {}

    return {**state, "news_meta": payload}  # type: ignore[typeddict-unknown-key]


def load_context_node(state: ThemeWorkerState) -> ThemeWorkerState:
    """단일 테마 기반 컨텍스트를 구성한다."""
    theme = state.get("theme", {})
    related = theme.get("related_news", []) if isinstance(theme, dict) else []
    context = {
        "headline": theme.get("headline") if isinstance(theme, dict) else None,
        "description": theme.get("description") if isinstance(theme, dict) else None,
        "related_news": related,
        "nutshell": state.get("nutshell"),
    }
    return {**state, "theme_context": context}


def prepare_messages_node(state: ThemeWorkerState) -> ThemeWorkerState:
    """프롬프트를 로드하고 시스템/사용자 메시지를 생성한다."""
    prompt_cfg = _load_prompt()
    theme = state.get("theme", {})
    base_scripts = state.get("base_scripts", [])
    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다.")

    date_korean = _format_date_korean(date_str.replace("-", ""))

    system_prompt = prompt_cfg["worker_system"].replace("{{tools}}", _get_tools_description()).replace(
        "{{date}}", date_korean
    )

    human_prompt = (
        prompt_cfg["worker_user_template"]
        .replace("{{date}}", date_korean)
        .replace("{{nutshell}}", state.get("nutshell") or "")
        .replace("{{theme}}", json.dumps(theme, ensure_ascii=False, indent=2))
        .replace("{{theme_context}}", json.dumps(state.get("theme_context", {}), ensure_ascii=False, indent=2))
        .replace("{{base_scripts}}", json.dumps(base_scripts, ensure_ascii=False, indent=2))
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ]
    return {**state, "messages": messages}


def worker_agent_node(state: ThemeWorkerState) -> ThemeWorkerState:
    """Tool이 바인딩된 LLM을 호출한다."""
    llm = _build_llm()
    llm_with_tools = llm.bind_tools(TOOLS)
    messages = state.get("messages", [])
    logger.info("ThemeWorker Agent 호출: %d개 메시지", len(messages))
    response = llm_with_tools.invoke(messages)
    return {**state, "messages": [response]}


def worker_should_continue(state: ThemeWorkerState) -> str:
    """Tool 호출 여부에 따라 다음 노드를 결정한다."""
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last_message = messages[-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        logger.info("ThemeWorker Tool 호출 감지: %d개", len(last_message.tool_calls))
        return "tools"
    return "end"


def extract_theme_scripts_node(state: ThemeWorkerState) -> ThemeWorkerState:
    """마지막 AI 응답에서 테마별 스크립트를 추출한다."""
    messages = state.get("messages", [])
    raw_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            raw_content = msg.content
            break

    parsed = _parse_json_from_response(raw_content)
    scripts = parsed.get("scripts", [])
    return {**state, "scripts": scripts}


def build_worker_graph():
    """ThemeWorkerGraph를 구성하고 컴파일한다."""
    _load_env()
    graph = StateGraph(ThemeWorkerState)

    graph.add_node("prefetch_news", prefetch_news_node)
    graph.add_node("load_context", load_context_node)
    graph.add_node("prepare_messages", prepare_messages_node)
    graph.add_node("agent", worker_agent_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("extract_scripts", extract_theme_scripts_node)

    graph.add_edge(START, "prefetch_news")
    graph.add_edge("prefetch_news", "load_context")
    graph.add_edge("load_context", "prepare_messages")
    graph.add_edge("prepare_messages", "agent")

    graph.add_conditional_edges(
        "agent",
        worker_should_continue,
        {
            "tools": "tools",
            "end": "extract_scripts",
        },
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("extract_scripts", END)
    graph.set_entry_point("prefetch_news")

    return graph.compile()


# ==== ThemeGraph 노드 ====
def build_theme_graph():
    """ThemeGraph를 구성하고 컴파일한다."""
    _load_env()
    worker_graph = build_worker_graph()
    graph = StateGraph(ThemeState)

    async def _run_workers(inputs: List[Dict[str, Any]]) -> List[Any]:
        tasks = [worker_graph.ainvoke(inp) for inp in inputs]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def run_theme_workers(state: ThemeState) -> ThemeState:
        themes = state.get("themes", []) or []
        if not themes:
            logger.warning("테마 목록이 비어 있습니다.")
            return {**state, "theme_scripts": []}

        inputs = [
            {
                "date": state.get("date"),
                "nutshell": state.get("nutshell"),
                "theme": theme,
                "base_scripts": state.get("base_scripts", []),
            }
            for theme in themes
        ]

        try:
            results = asyncio.run(_run_workers(inputs))
        except RuntimeError:
            # 이미 이벤트 루프가 돌고 있는 환경 대비
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                results = loop.run_until_complete(_run_workers(inputs))
            finally:
                asyncio.set_event_loop(None)
                loop.close()

        theme_scripts: List[List[ScriptTurn]] = []
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                logger.warning("ThemeWorker %d 실패: %s", idx, res)
                theme_scripts.append([])
            else:
                theme_scripts.append(res.get("scripts", []))
        return {**state, "theme_scripts": theme_scripts}

    def merge_scripts(state: ThemeState) -> ThemeState:
        merged: List[ScriptTurn] = []
        opening_scripts = state.get("base_scripts", []) or []
        merged.extend(opening_scripts)

        for theme_sc in state.get("theme_scripts", []) or []:
            merged.extend(theme_sc)

        return {**state, "scripts": merged}

    def refine_transitions(state: ThemeState) -> ThemeState:
        prompt_cfg = _load_prompt()
        llm = _build_llm()

        scripts_json = json.dumps(state.get("scripts", []), ensure_ascii=False, indent=2)
        themes_json = json.dumps(state.get("themes", []), ensure_ascii=False, indent=2)
        date_str = state.get("date") or ""
        date_korean = _format_date_korean(date_str.replace("-", "")) if date_str else ""

        system_prompt = prompt_cfg["refiner_system"].replace("{{date}}", date_korean)
        human_prompt = (
            prompt_cfg["refiner_user_template"]
            .replace("{{scripts}}", scripts_json)
            .replace("{{themes}}", themes_json)
            .replace("{{date}}", date_korean)
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt),
        ]

        logger.info("Refiner 호출")
        response = llm.invoke(messages)
        parsed = _parse_json_from_response(response.content if isinstance(response, AIMessage) else str(response))
        refined_scripts: List[ScriptTurn] | Any
        if isinstance(parsed, list):
            refined_scripts = parsed  # 이미 배열 형태로 응답
        elif isinstance(parsed, dict):
            refined_scripts = parsed.get("scripts", [])
        else:
            refined_scripts = []
        if not refined_scripts:
            logger.warning("Refiner가 유효한 scripts를 반환하지 않아 merge 결과를 그대로 사용합니다.")
            refined_scripts = state.get("scripts", [])

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(refined_scripts, ensure_ascii=False, indent=2), encoding="utf-8")

        return {**state, "scripts": refined_scripts}

    graph.add_node("run_theme_workers", run_theme_workers)
    graph.add_node("merge_scripts", merge_scripts)
    graph.add_node("refine_transitions", refine_transitions)

    graph.add_edge(START, "run_theme_workers")
    graph.add_edge("run_theme_workers", "merge_scripts")
    graph.add_edge("merge_scripts", "refine_transitions")
    graph.add_edge("refine_transitions", END)
    graph.set_entry_point("run_theme_workers")

    return graph.compile()


# ==== 캐시 정리 ====
def cleanup_cache() -> None:
    """ThemeAgent 실행 후 캐시 파일을 정리한다."""
    news_files = [
        BASE_DIR / "data/theme/news_list.json",
        BASE_DIR / "data/theme/titles.txt",
    ]
    bodies_dir = BASE_DIR / "data/theme/bodies"
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


if __name__ == "__main__":
    # 간단한 수동 실행용 진입점 (테스트 목적)
    import argparse

    parser = argparse.ArgumentParser(description="ThemeAgent 실행 (standalone 테스트용)")
    parser.add_argument("date", type=str, help="브리핑 날짜 (YYYYMMDD 또는 YYYY-MM-DD)")
    parser.add_argument(
        "--theme-json",
        type=str,
        default='[]',
        help="테마 리스트 JSON (headline/description/related_news 포함). 생략 시 빈 리스트.",
    )
    parser.add_argument(
        "--opening-scripts",
        type=str,
        default='[]',
        help="오프닝 스크립트 JSON 배열(선택).",
    )
    args = parser.parse_args()

    date_input = args.date
    if "-" in date_input:
        date_input = datetime.strptime(date_input, "%Y-%m-%d").strftime("%Y%m%d")

    try:
        themes = json.loads(args.theme_json)
    except json.JSONDecodeError:
        themes = []

    try:
        base_scripts = json.loads(args.opening_scripts)
    except json.JSONDecodeError:
        base_scripts = []

    os.environ["BRIEFING_DATE"] = date_input

    workflow = build_theme_graph()
    result = workflow.invoke(
        {
            "date": date_input,
            "nutshell": "",
            "themes": themes,
            "base_scripts": base_scripts,
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
