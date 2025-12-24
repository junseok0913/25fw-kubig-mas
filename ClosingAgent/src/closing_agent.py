"""ClosingAgent LangGraph 구현.

목표:
- Opening+Theme까지 누적된 대본(`scripts`)을 입력으로 받아 클로징(마무리) 파트를 생성한다.
- 경제 일정 컨텍스트는 오직 "오늘과 이후의 캘린더 리스트"만 프롬프트로 주입한다.
- 제공 Tool은 get_ohlcv / get_calendar만 사용한다.
"""

from __future__ import annotations

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
from .tools import get_calendar, get_ohlcv
from .utils.tracing import configure_tracing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent  # ClosingAgent/
ROOT_DIR = BASE_DIR.parent
PROMPT_PATH = BASE_DIR / "prompt/closing_main.yaml"
CALENDAR_CSV_PATH = BASE_DIR / "data/closing/calendar.csv"
CALENDAR_JSON_PATH = BASE_DIR / "data/closing/calendar.json"
OUTPUT_PATH = BASE_DIR / "data/closing_result.json"

TOOLS = [get_ohlcv, get_calendar]


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


def _build_llm() -> ChatOpenAI:
    """OpenAI LLM 인스턴스 생성.

    Env override 우선순위:
    - CLOSING_OPENAI_* (예: CLOSING_OPENAI_MODEL)
    - OPENAI_*
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY가 설정되지 않았습니다. .env 또는 환경변수를 확인하세요.")

    def _getenv_nonempty(name: str, default: str) -> str:
        v = os.getenv(name)
        if v is None:
            return default
        v = v.strip()
        return v if v else default

    def cfg(key: str, default_env_key: str, default: str) -> str:
        return _getenv_nonempty(f"CLOSING_{key}", _getenv_nonempty(default_env_key, default))

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


def _parse_json_from_response(content: str) -> Dict[str, Any]:
    json_match = re.search(r"```json\\s*([\\s\\S]*?)\\s*```", content)
    json_str = json_match.group(1) if json_match else content.strip()
    try:
        parsed = json.loads(json_str)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError as exc:  # noqa: BLE001
        logger.error("JSON 파싱 실패: %s", exc)
        return {}


def _assign_script_ids(scripts: Any) -> List[Dict[str, Any]]:
    """scripts 배열의 각 턴에 0부터 증가하는 id를 부여한다.

    LLM이 id를 포함해 반환하더라도 최종적으로는 순서 기반으로 다시 부여한다.
    """
    if not isinstance(scripts, list):
        return []
    out: List[Dict[str, Any]] = []
    for idx, turn in enumerate(scripts):
        if not isinstance(turn, dict):
            continue
        row = dict(turn)
        row["id"] = idx
        out.append(row)
    return out


def _load_prompt() -> Dict[str, str]:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"프롬프트 파일이 없습니다: {PROMPT_PATH}")
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    system = raw.get("system", "")
    user_template = raw.get("user_template", "")
    if not system or not user_template:
        raise ValueError("프롬프트 YAML에 system 또는 user_template가 비어 있습니다.")
    return {"system": system, "user_template": user_template}


def _load_calendar_context() -> str:
    """calendar.csv를 읽어 프롬프트에 넣을 최소 컨텍스트 문자열을 만든다(TSV)."""
    if not CALENDAR_CSV_PATH.exists():
        return ""

    import csv as _csv

    lines: list[str] = ["id\test_date\ttitle"]
    with CALENDAR_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = _csv.DictReader(f)
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


class NewsSource(TypedDict):
    pk: str
    title: str


class ScriptTurn(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    text: str
    sources: List[NewsSource]


class ClosingState(TypedDict, total=False):
    date: str
    scripts: List[ScriptTurn]
    calendar_context: str
    messages: Annotated[Sequence[BaseMessage], add_messages]
    closing_turns: List[ScriptTurn]


def prefetch_node(state: ClosingState) -> ClosingState:
    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다. 날짜를 지정하세요.")

    try:
        date_obj = datetime.strptime(date_str, "%Y%m%d").date()
        payload = prefetch.prefetch_calendar(today=date_obj)
        logger.info("캘린더 프리페치 완료: %d건 (날짜: %s)", payload.get("count", 0), date_str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("캘린더 프리페치 실패, 그래프는 계속 진행합니다: %s", exc)

    return state


def load_context_node(state: ClosingState) -> ClosingState:
    if not CALENDAR_JSON_PATH.exists() or not CALENDAR_CSV_PATH.exists():
        logger.warning("캘린더 캐시 파일이 없습니다: %s, %s", CALENDAR_CSV_PATH, CALENDAR_JSON_PATH)
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

    calendar_context = state.get("calendar_context") or ""
    scripts_json = json.dumps(scripts, ensure_ascii=False, separators=(",", ":"))

    user_prompt = (
        prompt_cfg["user_template"]
        .replace("{date}", date_korean)
        .replace("{scripts_json}", scripts_json)
        .replace("{calendar_context}", calendar_context)
    )

    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    return {**state, "messages": messages}


def agent_node(state: ClosingState) -> ClosingState:
    llm = _build_llm()
    llm_with_tools = llm.bind_tools(TOOLS)
    messages = state.get("messages", [])
    logger.info("Agent 호출: %d개 메시지", len(messages))
    response = llm_with_tools.invoke(messages)
    return {**state, "messages": [response]}


def should_continue(state: ClosingState) -> str:
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "end"


def extract_closing_turns_node(state: ClosingState) -> ClosingState:
    messages = state.get("messages", [])
    raw_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            raw_content = msg.content
            break

    parsed = _parse_json_from_response(raw_content)
    closing_turns = parsed.get("closing_turns", [])
    if not isinstance(closing_turns, list):
        closing_turns = []

    cleaned: List[ScriptTurn] = []
    for t in closing_turns:
        if not isinstance(t, dict):
            continue
        speaker = t.get("speaker")
        text = t.get("text")
        sources = t.get("sources", [])
        if speaker not in {"진행자", "해설자"}:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(sources, list):
            sources = []
        cleaned.append({"speaker": speaker, "text": text.strip(), "sources": sources})  # type: ignore[typeddict-item]

    if not cleaned:
        logger.warning("closing_turns를 추출하지 못했습니다. (모델 응답 확인 필요)")

    return {**state, "closing_turns": _assign_script_ids(cleaned)}


def append_scripts_node(state: ClosingState) -> ClosingState:
    scripts = list(state.get("scripts", []))
    closing_turns = state.get("closing_turns", [])
    scripts.extend(closing_turns)

    scripts = _assign_script_ids(scripts)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(scripts, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("최종 결과를 저장했습니다: %s", OUTPUT_PATH)

    return {**state, "scripts": scripts}


def cleanup_node(state: ClosingState) -> ClosingState:
    """prefetch로 생성된 캐시 파일을 정리한다."""
    try:
        cleanup_cache()
    except Exception as exc:  # noqa: BLE001
        logger.warning("캐시 정리 실패(치명적이지 않음): %s", exc)
    return state


def build_graph():
    _load_env()
    graph = StateGraph(ClosingState)

    graph.add_node("prefetch_calendar", prefetch_node)
    graph.add_node("load_context", load_context_node)
    graph.add_node("prepare_messages", prepare_messages_node)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("extract_closing_turns", extract_closing_turns_node)
    graph.add_node("append_scripts", append_scripts_node)
    graph.add_node("cleanup_cache", cleanup_node)

    graph.add_edge(START, "prefetch_calendar")
    graph.add_edge("prefetch_calendar", "load_context")
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
    graph.add_edge("append_scripts", "cleanup_cache")
    graph.add_edge("cleanup_cache", END)

    graph.set_entry_point("prefetch_calendar")
    return graph.compile()


def cleanup_cache() -> None:
    """ClosingAgent 실행 후 캐시 파일을 정리한다."""
    for file in [CALENDAR_CSV_PATH, CALENDAR_JSON_PATH]:
        if file.exists():
            try:
                file.unlink()
                logger.info("삭제: %s", file)
            except Exception as exc:  # noqa: BLE001
                logger.warning("파일 삭제 실패 %s: %s", file, exc)


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="ClosingAgent 실행 (standalone 테스트용)")
    parser.add_argument("date", type=str, help="브리핑 날짜 (YYYYMMDD)")
    parser.add_argument("--scripts-path", type=str, required=True, help="누적 scripts JSON 파일 경로")
    args = parser.parse_args()

    date_str = args.date.strip()
    datetime.strptime(date_str, "%Y%m%d")  # validate

    scripts_path = Path(args.scripts_path)
    scripts = json.loads(scripts_path.read_text(encoding="utf-8"))
    if not isinstance(scripts, list):
        raise ValueError("scripts JSON은 배열이어야 합니다.")

    os.environ["BRIEFING_DATE"] = date_str
    app = build_graph()
    result = app.invoke({"date": date_str, "scripts": scripts})
    print("scripts len:", len(result.get("scripts", [])))
    print("closing_turns len:", len(result.get("closing_turns", [])))
    print("output:", OUTPUT_PATH)
