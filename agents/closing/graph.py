"""Closing agent graph using shared cache."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Sequence, TypedDict

import yaml
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from shared.config import (
    cleanup_cache_dir,
    ensure_cache_dir,
    ensure_temp_dir,
    get_calendar_csv_path,
    get_temp_closing_path,
    get_temp_theme_path,
    set_briefing_date,
)
from shared.fetchers import prefetch_all
from shared.normalization import normalize_script_turns, parse_json_from_response
from shared.tools import get_calendar, get_ohlcv
from shared.types import ScriptTurn
from shared.utils.llm import build_llm
from shared.utils.tracing import configure_tracing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent
PROMPT_PATH = BASE_DIR / "prompt/closing_main.yaml"

TOOLS = [get_ohlcv, get_calendar]


class ArticleSource(TypedDict):
    type: Literal["article"]
    pk: str
    title: str


class ChartSource(TypedDict):
    type: Literal["chart"]
    ticker: str
    start_date: str
    end_date: str


class EventSource(TypedDict):
    type: Literal["event"]
    id: str
    title: str
    date: str


ScriptSource = ArticleSource | ChartSource | EventSource


class ClosingState(TypedDict, total=False):
    date: str
    scripts: List[ScriptTurn]
    calendar_context: str
    messages: Annotated[Sequence[BaseMessage], add_messages]
    closing_turns: List[ScriptTurn]


def _load_env() -> None:
    load_dotenv(ROOT_DIR / ".env", override=False)


def _get_tools_description() -> str:
    descriptions = []
    for tool in TOOLS:
        descriptions.append(f"- {tool.name}: {tool.description or ''}")
    return "\n".join(descriptions)


def _format_date_korean(date_yyyymmdd: str) -> str:
    dt = datetime.strptime(date_yyyymmdd, "%Y%m%d")
    return f"{dt.month}월 {dt.day}일"


def _build_llm():
    return build_llm("CLOSING", logger=logger)


def _load_prompt() -> Dict[str, str]:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"프롬프트 파일이 없습니다: {PROMPT_PATH}")
    with PROMPT_PATH.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    system = raw.get("system", "")
    user_template = raw.get("user_template", "")
    if not system or not user_template:
        raise ValueError("프롬프트 YAML에 system 또는 user_template가 비어 있습니다.")
    return {"system": system, "user_template": user_template}


def _load_calendar_context() -> str:
    calendar_csv_path = get_calendar_csv_path()
    if not calendar_csv_path.exists():
        return ""

    import csv

    lines: list[str] = ["id\test_date\ttitle"]
    with calendar_csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lines.append(
                "\t".join(
                    [
                        str(row.get("id") or "").strip(),
                        str(row.get("est_date") or "").strip(),
                        str(row.get("title") or "").strip(),
                    ]
                ).rstrip()
            )
    return "\n".join(lines).strip()


def load_scripts_from_temp(state: ClosingState) -> ClosingState:
    scripts = state.get("scripts")
    if scripts is not None:
        if state.get("date"):
            set_briefing_date(state["date"])
        return state

    temp_path = get_temp_theme_path()
    if not temp_path.exists():
        raise FileNotFoundError(f"Theme 결과가 없습니다: {temp_path}")

    payload = json.loads(temp_path.read_text(encoding="utf-8"))
    temp_date = payload.get("date")
    state_date = state.get("date")
    if state_date and temp_date and state_date != temp_date:
        logger.warning("date mismatch: state=%s, temp=%s", state_date, temp_date)

    date_str = state_date or temp_date
    if not date_str:
        raise ValueError("date 정보가 없습니다. CLI 인자 또는 temp/theme.json을 확인하세요.")

    set_briefing_date(date_str)

    return {**state, "date": date_str, "scripts": payload.get("scripts", [])}


def load_context_node(state: ClosingState) -> ClosingState:
    return {**state, "calendar_context": _load_calendar_context()}


def prepare_messages_node(state: ClosingState) -> ClosingState:
    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다.")

    scripts = state.get("scripts", [])
    prompt_cfg = _load_prompt()
    date_korean = _format_date_korean(date_str)

    system_prompt = (
        prompt_cfg["system"]
        .replace("{tools}", _get_tools_description())
        .replace("{date}", date_korean)
    )

    user_prompt = (
        prompt_cfg["user_template"]
        .replace("{date}", date_korean)
        .replace("{calendar_context}", state.get("calendar_context", ""))
        .replace("{scripts}", json.dumps(scripts, ensure_ascii=False, indent=2))
    )

    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    return {**state, "messages": messages}


def agent_node(state: ClosingState) -> ClosingState:
    llm = _build_llm()
    llm_with_tools = llm.bind_tools(TOOLS)
    messages = state.get("messages", [])
    logger.info("Closing Agent 호출: %d개 메시지", len(messages))
    response = llm_with_tools.invoke(messages)
    return {**state, "messages": [response]}


def should_continue(state: ClosingState) -> str:
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last_message = messages[-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        logger.info("Closing Tool 호출 감지: %d개", len(last_message.tool_calls))
        return "tools"
    return "end"


def extract_closing_turns_node(state: ClosingState) -> ClosingState:
    messages = state.get("messages", [])
    raw_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            raw_content = msg.content
            break

    parsed = parse_json_from_response(raw_content)
    closing_turns = normalize_script_turns(parsed.get("closing_turns", []))
    return {**state, "closing_turns": closing_turns}


def append_scripts_node(state: ClosingState) -> ClosingState:
    scripts = list(state.get("scripts", []))
    closing_turns = state.get("closing_turns", [])
    scripts.extend(closing_turns)

    scripts = normalize_script_turns(scripts)

    ensure_temp_dir()
    output_path = get_temp_closing_path()
    output_path.write_text(
        json.dumps({"date": state.get("date"), "scripts": scripts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Closing 결과를 저장했습니다: %s", output_path)

    return {**state, "scripts": scripts}


def build_graph():
    _load_env()
    configure_tracing(logger=logger)
    graph = StateGraph(ClosingState)

    graph.add_node("load_scripts", load_scripts_from_temp)
    graph.add_node("load_context", load_context_node)
    graph.add_node("prepare_messages", prepare_messages_node)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("extract_closing_turns", extract_closing_turns_node)
    graph.add_node("append_scripts", append_scripts_node)

    graph.add_edge(START, "load_scripts")
    graph.add_edge("load_scripts", "load_context")
    graph.add_edge("load_context", "prepare_messages")
    graph.add_edge("prepare_messages", "agent")

    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            "end": "extract_closing_turns",
        },
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("extract_closing_turns", "append_scripts")
    graph.add_edge("append_scripts", END)

    graph.set_entry_point("load_scripts")
    return graph.compile()


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="ClosingAgent 실행 (standalone 테스트용)")
    parser.add_argument("date", type=str, help="브리핑 날짜 (YYYYMMDD)")
    parser.add_argument("--scripts-path", type=str, required=False, help="누적 scripts JSON 파일 경로")
    args = parser.parse_args()

    date_str = args.date.strip()
    if "-" in date_str:
        date_str = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")
    else:
        datetime.strptime(date_str, "%Y%m%d")

    set_briefing_date(date_str)
    date_obj = datetime.strptime(date_str, "%Y%m%d").date()
    cache_dir = ensure_cache_dir(date_str)
    prefetch_all(date_obj, cache_dir=cache_dir)

    try:
        scripts = None
        if args.scripts_path:
            scripts_path = Path(args.scripts_path)
            scripts = json.loads(scripts_path.read_text(encoding="utf-8"))
            if not isinstance(scripts, list):
                raise ValueError("scripts JSON은 배열이어야 합니다.")

        app = build_graph()
        result = app.invoke({"date": date_str, "scripts": scripts})
        print("scripts len:", len(result.get("scripts", [])))
        print("closing_turns len:", len(result.get("closing_turns", [])))
        print("output:", get_temp_closing_path())
    finally:
        cleanup_cache_dir(date_str)


if __name__ == "__main__":
    main()
