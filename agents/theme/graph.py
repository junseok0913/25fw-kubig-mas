"""Theme agent graph using shared cache."""

from __future__ import annotations

import json
import logging
import os
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
    get_temp_opening_path,
    get_temp_theme_path,
    set_briefing_date,
)
from shared.fetchers import prefetch_all
from shared.normalization import normalize_script_turns, parse_json_from_response
from shared.tools import (
    count_keyword_frequency,
    get_calendar,
    get_news_content,
    get_news_list,
    get_ohlcv,
)
from shared.types import ScriptTurn, Theme
from shared.utils.llm import build_llm
from shared.utils.tracing import configure_tracing
from shared.yaml_config import load_env_from_yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent
WORKER_PROMPT_PATH = BASE_DIR / "prompt/theme_worker.yaml"
REFINER_PROMPT_PATH = BASE_DIR / "prompt/theme_refine.yaml"

TOOLS = [
    get_news_list,
    get_news_content,
    count_keyword_frequency,
    get_ohlcv,
    get_calendar,
]


class ThemeWorkerState(TypedDict, total=False):
    date: str
    nutshell: str
    theme: Theme
    theme_context: Dict[str, Any]
    base_scripts: List[ScriptTurn]
    messages: Annotated[Sequence[BaseMessage], add_messages]
    scripts: List[ScriptTurn]


class ThemeState(TypedDict, total=False):
    date: str
    nutshell: str
    themes: List[Theme]
    base_scripts: List[ScriptTurn]
    theme_scripts: List[List[ScriptTurn]]
    scripts: List[ScriptTurn]


# ==== 공통 유틸 ====

def _load_env() -> None:
    load_env_from_yaml()
    load_dotenv(ROOT_DIR / ".env", override=False)


def _get_tools_description() -> str:
    descriptions = []
    for tool in TOOLS:
        name = tool.name
        desc = tool.description or ""
        descriptions.append(f"- {name}: {desc}")
    return "\n".join(descriptions)


def _format_date_korean(date_yyyymmdd: str) -> str:
    dt = datetime.strptime(date_yyyymmdd, "%Y%m%d")
    return f"{dt.month}월 {dt.day}일"


def _build_llm(profile: str | None = None):
    prefix = f"THEME_{profile.upper()}" if profile else "THEME"
    return build_llm(prefix, logger=logger)


def _looks_like_model_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("model_not_found" in msg) or ("does not exist" in msg) or ("do not have access" in msg)


def _get_refiner_max_retries() -> int:
    def _getenv_nonempty(name: str) -> str | None:
        v = os.getenv(name)
        if v is None:
            return None
        v = v.strip()
        return v if v else None

    raw = _getenv_nonempty("THEME_REFINER_OPENAI_MAX_RETRIES") or _getenv_nonempty("OPENAI_MAX_RETRIES")
    if raw is None:
        return 2
    try:
        return int(raw)
    except ValueError:
        logger.warning("잘못된 재시도 값입니다: %r (기본값 2 사용)", raw)
        return 2


def _load_prompt() -> Dict[str, str]:
    if not WORKER_PROMPT_PATH.exists():
        raise FileNotFoundError(f"worker 프롬프트 파일이 없습니다: {WORKER_PROMPT_PATH}")
    if not REFINER_PROMPT_PATH.exists():
        raise FileNotFoundError(f"refiner 프롬프트 파일이 없습니다: {REFINER_PROMPT_PATH}")

    with WORKER_PROMPT_PATH.open("r", encoding="utf-8") as f:
        worker_raw = yaml.safe_load(f) or {}
    with REFINER_PROMPT_PATH.open("r", encoding="utf-8") as f:
        refiner_raw = yaml.safe_load(f) or {}

    return {
        "worker_system": worker_raw.get("system", ""),
        "worker_user_template": worker_raw.get("user_template", ""),
        "refiner_system": refiner_raw.get("system", ""),
        "refiner_user_template": refiner_raw.get("user_template", ""),
    }


def _load_calendar_context() -> str:
    calendar_csv_path = get_calendar_csv_path()
    if not calendar_csv_path.exists():
        return ""

    import csv as _csv

    lines: list[str] = ["id\test_date\ttitle"]
    with calendar_csv_path.open("r", encoding="utf-8", newline="") as f:
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


# ==== ThemeWorkerGraph 노드 ====

def load_context_node(state: ThemeWorkerState) -> ThemeWorkerState:
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
    prompt_cfg = _load_prompt()
    theme = state.get("theme", {})
    base_scripts = state.get("base_scripts", [])
    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다.")

    date_korean = _format_date_korean(date_str.replace("-", ""))

    system_prompt = prompt_cfg["worker_system"].replace("{tools}", _get_tools_description()).replace("{date}", date_korean)

    calendar_context = _load_calendar_context()

    human_prompt = (
        prompt_cfg["worker_user_template"]
        .replace("{date}", date_korean)
        .replace("{nutshell}", state.get("nutshell") or "")
        .replace("{theme}", json.dumps(theme, ensure_ascii=False, indent=2))
        .replace("{theme_context}", json.dumps(state.get("theme_context", {}), ensure_ascii=False, indent=2))
        .replace("{base_scripts}", json.dumps(base_scripts, ensure_ascii=False, indent=2))
        .replace("{calendar_context}", calendar_context)
    )

    messages = [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
    return {**state, "messages": messages}


def worker_agent_node(state: ThemeWorkerState) -> ThemeWorkerState:
    llm = _build_llm(profile="worker")
    llm_with_tools = llm.bind_tools(TOOLS)
    messages = state.get("messages", [])
    logger.info("ThemeWorker Agent 호출: %d개 메시지", len(messages))
    response = llm_with_tools.invoke(messages)
    return {**state, "messages": [response]}


def worker_should_continue(state: ThemeWorkerState) -> str:
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last_message = messages[-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        logger.info("ThemeWorker Tool 호출 감지: %d개", len(last_message.tool_calls))
        return "tools"
    return "end"


def extract_theme_scripts_node(state: ThemeWorkerState) -> ThemeWorkerState:
    messages = state.get("messages", [])
    raw_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            raw_content = msg.content
            break

    parsed = parse_json_from_response(raw_content)
    scripts = normalize_script_turns(parsed.get("scripts", []))
    return {**state, "scripts": scripts}


def build_worker_graph():
    _load_env()
    graph = StateGraph(ThemeWorkerState)

    graph.add_node("load_context", load_context_node)
    graph.add_node("prepare_messages", prepare_messages_node)
    graph.add_node("agent", worker_agent_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("extract_scripts", extract_theme_scripts_node)

    graph.add_edge(START, "load_context")
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
    graph.set_entry_point("load_context")

    return graph.compile()


# ==== ThemeGraph 노드 ====

def load_base_from_temp(state: ThemeState) -> ThemeState:
    themes = state.get("themes")
    base_scripts = state.get("base_scripts")
    needs_base = themes is None or base_scripts is None
    if not needs_base:
        if state.get("date"):
            set_briefing_date(state["date"])
        return state

    temp_path = get_temp_opening_path()
    if not temp_path.exists():
        raise FileNotFoundError(f"Opening 결과가 없습니다: {temp_path}")

    payload = json.loads(temp_path.read_text(encoding="utf-8"))
    temp_date = payload.get("date")
    state_date = state.get("date")
    if state_date and temp_date and state_date != temp_date:
        logger.warning("date mismatch: state=%s, temp=%s", state_date, temp_date)

    date_str = state_date or temp_date
    if not date_str:
        raise ValueError("date 정보가 없습니다. CLI 인자 또는 temp/opening.json을 확인하세요.")

    set_briefing_date(date_str)

    return {
        **state,
        "date": date_str,
        "nutshell": payload.get("nutshell", state.get("nutshell", "")),
        "themes": payload.get("themes", themes if themes is not None else []),
        "base_scripts": payload.get("scripts", base_scripts if base_scripts is not None else []),
    }


def build_theme_graph():
    _load_env()
    configure_tracing(logger=logger)
    worker_graph = build_worker_graph()
    graph = StateGraph(ThemeState)

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

        logger.info("ThemeWorker 병렬 실행 시작: %d개", len(inputs))
        results = worker_graph.batch(inputs, return_exceptions=True)  # type: ignore[attr-defined]
        logger.info("ThemeWorker 병렬 실행 완료")

        theme_scripts: List[List[ScriptTurn]] = []
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                logger.warning("ThemeWorker %d 실패: %s", idx, res)
                theme_scripts.append([])
            else:
                turns = res.get("scripts", [])
                theme_scripts.append(turns)
                logger.info("ThemeWorker %d 완료: %d턴", idx, len(turns))
        return {**state, "theme_scripts": theme_scripts}

    def merge_scripts(state: ThemeState) -> ThemeState:
        merged: List[ScriptTurn] = []
        opening_scripts = state.get("base_scripts", []) or []
        merged.extend(opening_scripts)

        for theme_sc in state.get("theme_scripts", []) or []:
            merged.extend(theme_sc)

        normalized = normalize_script_turns(merged)
        return {**state, "scripts": normalized}

    def refine_transitions(state: ThemeState) -> ThemeState:
        prompt_cfg = _load_prompt()
        llm = _build_llm(profile="refiner")

        scripts = state.get("scripts", []) or []
        scripts_minimal = [
            {"id": t.get("id"), "speaker": t.get("speaker"), "text": t.get("text")}
            for t in scripts
            if isinstance(t, dict)
        ]

        scripts_minimal_json = json.dumps(scripts_minimal, ensure_ascii=False, separators=(",", ":"))
        themes_json = json.dumps(state.get("themes", []), ensure_ascii=False, separators=(",", ":"))
        date_str = state.get("date") or ""
        date_korean = _format_date_korean(date_str.replace("-", "")) if date_str else ""

        system_prompt = prompt_cfg["refiner_system"].replace("{date}", date_korean)
        human_prompt = (
            prompt_cfg["refiner_user_template"]
            .replace("{scripts_minimal}", scripts_minimal_json)
            .replace("{themes}", themes_json)
            .replace("{date}", date_korean)
        )

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]

        logger.info("Refiner 호출")
        timeout = float(
            os.getenv(
                "THEME_REFINER_OPENAI_TIMEOUT",
                os.getenv("OPENAI_REFINER_TIMEOUT", os.getenv("OPENAI_TIMEOUT", "120")),
            )
        )

        max_retries = _get_refiner_max_retries()
        max_attempts = 1 + max_retries

        def _parse_edits(parsed: Any) -> List[Dict[str, Any]] | None:
            if not isinstance(parsed, dict):
                return None
            edits = parsed.get("edits")
            if not isinstance(edits, list):
                return None
            out: List[Dict[str, Any]] = []
            for idx, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    logger.error("Refiner edits[%d] 스키마 불일치: %r", idx, edit)
                    return None
                turn_id = edit.get("id")
                if not isinstance(turn_id, int):
                    logger.error("Refiner edits[%d].id 스키마 불일치: %r", idx, turn_id)
                    return None
                speaker = edit.get("speaker")
                if speaker not in {"진행자", "해설자"}:
                    logger.error("Refiner edits[%d].speaker 스키마 불일치: %r", idx, speaker)
                    return None
                text = edit.get("text")
                if not isinstance(text, str) or not text.strip():
                    logger.error("Refiner edits[%d].text 스키마 불일치(비어있음)", idx)
                    return None
                cleaned_text = str(text).replace("\n", " ").strip()
                if not cleaned_text:
                    logger.error("Refiner edits[%d].text 스키마 불일치(정리 후 비어있음)", idx)
                    return None
                out.append({"id": turn_id, "speaker": speaker, "text": cleaned_text})
            return out

        def _validate_edits(edits: List[Dict[str, Any]], scripts_len: int) -> List[Dict[str, Any]] | None:
            if edits is None:
                return None
            out: List[Dict[str, Any]] = []
            for idx, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    logger.error("Refiner edits[%d] 스키마 불일치: %r", idx, edit)
                    return None
                turn_id = edit.get("id")
                if not isinstance(turn_id, int):
                    logger.error("Refiner edits[%d].id 스키마 불일치: %r", idx, turn_id)
                    return None
                if turn_id < 0 or turn_id >= scripts_len:
                    logger.error("Refiner edits[%d].id 범위 오류: %r", idx, turn_id)
                    return None
                speaker = edit.get("speaker")
                if speaker not in {"진행자", "해설자"}:
                    logger.error("Refiner edits[%d].speaker 스키마 불일치: %r", idx, speaker)
                    return None
                text = edit.get("text")
                if not isinstance(text, str) or not text.strip():
                    logger.error("Refiner edits[%d].text 스키마 불일치(비어있음)", idx)
                    return None
                cleaned_text = str(text).replace("\n", " ").strip()
                if not cleaned_text:
                    logger.error("Refiner edits[%d].text 스키마 불일치(정리 후 비어있음)", idx)
                    return None
                out.append({"id": turn_id, "speaker": speaker, "text": cleaned_text})
            return out

        applied_scripts: List[ScriptTurn] | None = None
        last_error: str | None = None

        for attempt in range(1, max_attempts + 1):
            parsed: Any = None
            try:
                response = llm.invoke(messages, config={"timeout": timeout})
                logger.info("Refiner 응답 수신")
                parsed = parse_json_from_response(response.content if isinstance(response, AIMessage) else str(response))
            except Exception as exc:
                if _looks_like_model_not_found(exc):
                    llm = _build_llm(profile="worker")
                    logger.warning(
                        "Refiner 모델 접근 불가로 폴백합니다: %s -> %s",
                        os.getenv("THEME_REFINER_OPENAI_MODEL") or os.getenv("OPENAI_MODEL"),
                        os.getenv("THEME_WORKER_OPENAI_MODEL") or os.getenv("OPENAI_MODEL"),
                    )
                    last_error = f"refiner_model_not_found: {exc}"
                    continue
                logger.warning("Refiner 호출 실패(타임아웃/네트워크 등): %s", exc)
                last_error = f"refiner_invoke_failed: {exc}"
                break

            edits = _parse_edits(parsed)
            if edits is None:
                last_error = "refiner_parse_failed: missing_or_invalid_edits"
                logger.error("Refiner 출력 파싱/스키마 불일치(edits). attempt=%d/%d", attempt, max_attempts)
                if attempt < max_attempts:
                    continue
                break

            validated = _validate_edits(edits, scripts_len=len(scripts))
            if validated is None:
                last_error = "refiner_parse_failed: edits_schema_invalid"
                logger.error("Refiner 출력 파싱/스키마 불일치(edits[*]). attempt=%d/%d", attempt, max_attempts)
                if attempt < max_attempts:
                    continue
                break

            if not validated:
                applied_scripts = list(scripts)
                logger.info("Refiner edits 없음: 변경 없이 유지합니다.")
            else:
                id_to_index: Dict[int, int] = {}
                for i, turn in enumerate(scripts):
                    if isinstance(turn, dict) and isinstance(turn.get("id"), int):
                        id_to_index[int(turn["id"])] = i

                patched: List[Dict[str, Any]] = [dict(t) for t in scripts]
                apply_failed = False
                for edit in validated:
                    turn_id = int(edit["id"])
                    target_idx = id_to_index.get(turn_id)
                    if target_idx is None:
                        last_error = f"refiner_apply_failed: missing_turn_id={turn_id}"
                        logger.error("Refiner edits 적용 실패: scripts에 없는 id=%d", turn_id)
                        apply_failed = True
                        break
                    patched_turn = dict(patched[target_idx])
                    patched_turn["speaker"] = edit["speaker"]
                    patched_turn["text"] = edit["text"]
                    patched[target_idx] = patched_turn
                if apply_failed:
                    break

                applied_scripts = normalize_script_turns(patched)
                if not applied_scripts:
                    last_error = "refiner_apply_failed: normalized_empty"
                    logger.error("Refiner edits 적용 후 scripts가 비었습니다.")
                    applied_scripts = None
                    break

            break

        if applied_scripts is None:
            if last_error:
                logger.warning("Refiner 실패로 기존 scripts 유지: %s", last_error)
            applied_scripts = list(scripts)

        ensure_temp_dir()
        output_path = get_temp_theme_path()
        output_path.write_text(
            json.dumps({"date": state.get("date"), "scripts": applied_scripts}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Theme 결과를 저장했습니다: %s", output_path)

        return {**state, "scripts": applied_scripts}

    graph.add_node("load_base", load_base_from_temp)
    graph.add_node("run_theme_workers", run_theme_workers)
    graph.add_node("merge_scripts", merge_scripts)
    graph.add_node("refine_transitions", refine_transitions)

    graph.add_edge(START, "load_base")
    graph.add_edge("load_base", "run_theme_workers")
    graph.add_edge("run_theme_workers", "merge_scripts")
    graph.add_edge("merge_scripts", "refine_transitions")
    graph.add_edge("refine_transitions", END)
    graph.set_entry_point("load_base")

    return graph.compile()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="ThemeAgent 실행 (standalone 테스트용)")
    parser.add_argument("date", type=str, help="브리핑 날짜 (YYYYMMDD 또는 YYYY-MM-DD)")
    parser.add_argument(
        "--theme-json",
        type=str,
        default=None,
        help="테마 리스트 JSON (headline/description/related_news 포함). 생략 시 temp/opening.json 사용.",
    )
    parser.add_argument(
        "--opening-scripts",
        type=str,
        default=None,
        help="오프닝 스크립트 JSON 배열(선택). 생략 시 temp/opening.json 사용.",
    )
    args = parser.parse_args()

    date_input = args.date
    if "-" in date_input:
        date_input = datetime.strptime(date_input, "%Y-%m-%d").strftime("%Y%m%d")

    set_briefing_date(date_input)
    date_obj = datetime.strptime(date_input, "%Y%m%d").date()
    cache_dir = ensure_cache_dir(date_input)
    prefetch_all(date_obj, cache_dir=cache_dir)

    try:
        themes: List[Theme] | None = None
        base_scripts: List[ScriptTurn] | None = None

        if args.theme_json:
            try:
                themes = json.loads(args.theme_json)
            except json.JSONDecodeError:
                themes = []
        if args.opening_scripts:
            try:
                base_scripts = json.loads(args.opening_scripts)
            except json.JSONDecodeError:
                base_scripts = []

        app = build_theme_graph()
        result = app.invoke(
            {
                "date": date_input,
                "nutshell": "",
                "themes": themes,
                "base_scripts": base_scripts,
            }
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        cleanup_cache_dir(date_input)


if __name__ == "__main__":
    main()
