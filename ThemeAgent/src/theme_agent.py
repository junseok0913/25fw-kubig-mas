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
    get_calendar,
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
WORKER_PROMPT_PATH = BASE_DIR / "prompt/theme_worker.yaml"
REFINER_PROMPT_PATH = BASE_DIR / "prompt/theme_refine.yaml"
OUTPUT_PATH = BASE_DIR / "data/theme_result.json"
CALENDAR_CSV_PATH = BASE_DIR / "data/theme/calendar.csv"
CALENDAR_JSON_PATH = BASE_DIR / "data/theme/calendar.json"

# LangChain Tool 리스트 (LLM에 바인딩할 도구들)
TOOLS = [
    get_news_list,
    get_news_content,
    list_downloaded_bodies,
    count_keyword_frequency,
    get_ohlcv,
    get_calendar,
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


def _build_llm(profile: str | None = None) -> ChatOpenAI:
    """OpenAI LLM 인스턴스를 생성한다.

    Env override 우선순위:
    - profile 지정 시: THEME_{PROFILE}_OPENAI_* (예: THEME_REFINER_OPENAI_MODEL)
    - 공통: OPENAI_*
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY가 설정되지 않았습니다. .env 또는 환경변수를 확인하세요.")

    prefix = f"THEME_{profile.upper()}" if profile else "THEME"

    def _getenv_nonempty(name: str, default: str) -> str:
        v = os.getenv(name)
        if v is None:
            return default
        v = v.strip()
        return v if v else default

    def cfg(key: str, default_env_key: str, default: str) -> str:
        return _getenv_nonempty(f"{prefix}_{key}", _getenv_nonempty(default_env_key, default))

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


def _looks_like_model_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("model_not_found" in msg) or ("does not exist" in msg) or ("do not have access" in msg)


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

_DATE_YYYY_MM_DD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_valid_date_yyyy_mm_dd(value: Any) -> bool:
    if not _is_nonempty_str(value):
        return False
    s = str(value).strip()
    if not _DATE_YYYY_MM_DD.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False


def _normalize_sources(raw_sources: Any, *, turn_index: int) -> List[Dict[str, Any]] | None:
    """sources 배열을 스키마에 맞게 정규화한다.

    - type이 없고 pk/title만 있으면 legacy article로 간주한다(경고 후 유지).
    - 필수 필드 누락/형식 오류는 warning 로그 후 drop한다.
    """
    if not isinstance(raw_sources, list):
        logger.warning("ScriptTurn[%d] drop: sources가 리스트가 아닙니다.", turn_index)
        return None

    out: List[Dict[str, Any]] = []
    for src_index, src in enumerate(raw_sources):
        if not isinstance(src, dict):
            logger.warning("ScriptTurn[%d].sources[%d] drop: 객체가 아닙니다.", turn_index, src_index)
            continue

        src_type = src.get("type")
        if not _is_nonempty_str(src_type):
            if _is_nonempty_str(src.get("pk")) and _is_nonempty_str(src.get("title")):
                logger.warning(
                    "ScriptTurn[%d].sources[%d] missing type: legacy article로 처리합니다.",
                    turn_index,
                    src_index,
                )
                out.append({"type": "article", "pk": str(src["pk"]).strip(), "title": str(src["title"]).strip()})
            else:
                logger.warning("ScriptTurn[%d].sources[%d] drop: type 누락", turn_index, src_index)
            continue

        st = str(src_type).strip()
        if st == "article":
            if not _is_nonempty_str(src.get("pk")):
                logger.warning("ScriptTurn[%d].sources[%d] drop: article.pk 누락", turn_index, src_index)
                continue
            if not _is_nonempty_str(src.get("title")):
                logger.warning("ScriptTurn[%d].sources[%d] drop: article.title 누락", turn_index, src_index)
                continue
            out.append({"type": "article", "pk": str(src["pk"]).strip(), "title": str(src["title"]).strip()})
            continue

        if st == "chart":
            if not _is_nonempty_str(src.get("ticker")):
                logger.warning("ScriptTurn[%d].sources[%d] drop: chart.ticker 누락", turn_index, src_index)
                continue
            if not _is_valid_date_yyyy_mm_dd(src.get("start_date")):
                logger.warning("ScriptTurn[%d].sources[%d] drop: chart.start_date 누락/형식 오류", turn_index, src_index)
                continue
            if not _is_valid_date_yyyy_mm_dd(src.get("end_date")):
                logger.warning("ScriptTurn[%d].sources[%d] drop: chart.end_date 누락/형식 오류", turn_index, src_index)
                continue
            out.append(
                {
                    "type": "chart",
                    "ticker": str(src["ticker"]).strip(),
                    "start_date": str(src["start_date"]).strip(),
                    "end_date": str(src["end_date"]).strip(),
                }
            )
            continue

        if st == "event":
            if not _is_nonempty_str(src.get("id")):
                logger.warning("ScriptTurn[%d].sources[%d] drop: event.id 누락", turn_index, src_index)
                continue
            if not _is_nonempty_str(src.get("title")):
                logger.warning("ScriptTurn[%d].sources[%d] drop: event.title 누락", turn_index, src_index)
                continue
            if not _is_valid_date_yyyy_mm_dd(src.get("date")):
                logger.warning("ScriptTurn[%d].sources[%d] drop: event.date 누락/형식 오류", turn_index, src_index)
                continue
            out.append(
                {
                    "type": "event",
                    "id": str(src["id"]).strip(),
                    "title": str(src["title"]).strip(),
                    "date": str(src["date"]).strip(),
                }
            )
            continue

        logger.warning("ScriptTurn[%d].sources[%d] drop: 알 수 없는 type=%r", turn_index, src_index, st)

    return out


def _normalize_script_turns(raw_scripts: Any) -> List[Dict[str, Any]]:
    """scripts 배열을 ScriptTurn 스키마에 맞게 정규화한다.

    - speaker/text/sources 필수
    - id는 0부터 순서대로 재부여
    - sources 내부는 type 기반 스키마로 정규화
    - 필드 누락/형식 오류는 warning 로그 후 drop
    """
    if not isinstance(raw_scripts, list):
        logger.warning("scripts drop: 배열이 아닙니다.")
        return []

    out: List[Dict[str, Any]] = []
    for idx, turn in enumerate(raw_scripts):
        if not isinstance(turn, dict):
            logger.warning("ScriptTurn[%d] drop: 객체가 아닙니다.", idx)
            continue

        speaker = turn.get("speaker")
        if speaker not in {"진행자", "해설자"}:
            logger.warning("ScriptTurn[%d] drop: speaker 누락/형식 오류", idx)
            continue

        text = turn.get("text")
        if not _is_nonempty_str(text):
            logger.warning("ScriptTurn[%d] drop: text 누락/형식 오류", idx)
            continue

        if "sources" not in turn:
            logger.warning("ScriptTurn[%d] drop: sources 누락", idx)
            continue

        sources = _normalize_sources(turn.get("sources"), turn_index=idx)
        if sources is None:
            continue

        out.append({"id": len(out), "speaker": speaker, "text": str(text).strip(), "sources": sources})

    return out


def _load_prompt() -> Dict[str, str]:
    """worker/refiner 프롬프트 파일을 로드한다."""
    if not WORKER_PROMPT_PATH.exists():
        raise FileNotFoundError(f"worker 프롬프트 파일이 없습니다: {WORKER_PROMPT_PATH}")
    if not REFINER_PROMPT_PATH.exists():
        raise FileNotFoundError(f"refiner 프롬프트 파일이 없습니다: {REFINER_PROMPT_PATH}")

    with open(WORKER_PROMPT_PATH, "r", encoding="utf-8") as f:
        worker_raw = yaml.safe_load(f) or {}
    with open(REFINER_PROMPT_PATH, "r", encoding="utf-8") as f:
        refiner_raw = yaml.safe_load(f) or {}

    return {
        "worker_system": worker_raw.get("system", ""),
        "worker_user_template": worker_raw.get("user_template", ""),
        "refiner_system": refiner_raw.get("system", ""),
        "refiner_user_template": refiner_raw.get("user_template", ""),
    }


def _load_calendar_context() -> str:
    """calendar.csv를 읽어 worker 프롬프트에 넣을 최소 컨텍스트 문자열을 만든다.

    각 줄: id, est_date(YYYYMMDD), title (TSV)
    """
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


# ==== State 타입 ====
class NewsSource(TypedDict):
    pk: str
    title: str


class ArticleSource(TypedDict):
    type: Literal["article"]
    pk: str
    title: str


class ChartSource(TypedDict):
    type: Literal["chart"]
    ticker: str
    start_date: str  # YYYY-MM-DD (ET)
    end_date: str  # YYYY-MM-DD (ET)


class EventSource(TypedDict):
    type: Literal["event"]
    id: str  # calendar event id
    title: str
    date: str  # YYYY-MM-DD (ET)


ScriptSource = ArticleSource | ChartSource | EventSource


class Theme(TypedDict):
    headline: str
    description: str
    related_news: List[NewsSource]


class ScriptTurn(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    text: str
    sources: List[ScriptSource]


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

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ]
    return {**state, "messages": messages}


def worker_agent_node(state: ThemeWorkerState) -> ThemeWorkerState:
    """Tool이 바인딩된 LLM을 호출한다."""
    llm = _build_llm(profile="worker")
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
    scripts = _normalize_script_turns(parsed.get("scripts", []))
    return {**state, "scripts": scripts}


def build_worker_graph():
    """ThemeWorkerGraph를 구성하고 컴파일한다."""
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
def build_theme_graph():
    """ThemeGraph를 구성하고 컴파일한다."""
    _load_env()
    worker_graph = build_worker_graph()
    graph = StateGraph(ThemeState)

    def prefetch_cache(state: ThemeState) -> ThemeState:
        """ThemeAgent 실행 전 캐시를 한 번만 프리페치한다 (뉴스 + 캘린더)."""
        date_str = state.get("date") or ""
        if not date_str:
            raise ValueError("date 필드가 state에 없습니다.")

        try:
            date_obj = datetime.strptime(date_str.replace("-", ""), "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError(f"잘못된 날짜 형식: {date_str}") from exc

        os.environ.setdefault("BRIEFING_DATE", date_obj.strftime("%Y%m%d"))

        try:
            payload = prefetch.prefetch_news(today=date_obj)
            logger.info("ThemeAgent 뉴스 프리페치 완료: %d건 (날짜: %s)", payload.get("count", 0), date_str)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ThemeAgent 뉴스 프리페치 실패, 그래프는 계속 진행합니다: %s", exc)

        try:
            cal_payload = prefetch.prefetch_calendar(today=date_obj)
            logger.info("ThemeAgent 캘린더 프리페치 완료: %d건 (날짜: %s)", cal_payload.get("count", 0), date_str)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ThemeAgent 캘린더 프리페치 실패, 그래프는 계속 진행합니다: %s", exc)

        return state

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
        # langgraph compiled graph의 batch는 내부적으로 비동기 실행을 처리한다.
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

        return {**state, "scripts": merged}

    def refine_transitions(state: ThemeState) -> ThemeState:
        prompt_cfg = _load_prompt()
        llm = _build_llm(profile="refiner")

        # Refiner 입력은 커질 수 있으므로(Opening+Theme 전체) 최대한 압축해 전송한다.
        scripts_json = json.dumps(state.get("scripts", []), ensure_ascii=False, separators=(",", ":"))
        themes_json = json.dumps(state.get("themes", []), ensure_ascii=False, separators=(",", ":"))
        date_str = state.get("date") or ""
        date_korean = _format_date_korean(date_str.replace("-", "")) if date_str else ""

        system_prompt = prompt_cfg["refiner_system"].replace("{date}", date_korean)
        human_prompt = (
            prompt_cfg["refiner_user_template"]
            .replace("{scripts}", scripts_json)
            .replace("{themes}", themes_json)
            .replace("{date}", date_korean)
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt),
        ]

        logger.info("Refiner 호출")
        timeout = float(
            os.getenv(
                "THEME_REFINER_OPENAI_TIMEOUT",
                os.getenv("OPENAI_REFINER_TIMEOUT", os.getenv("OPENAI_TIMEOUT", "120")),
            )
        )

        parsed: Any
        try:
            response = llm.invoke(messages, config={"timeout": timeout})
            logger.info("Refiner 응답 수신")
            parsed = _parse_json_from_response(response.content if isinstance(response, AIMessage) else str(response))
        except Exception as exc:  # noqa: BLE001
            # 모델 이름 오타/권한 문제는 설정 문제이므로, 자동으로 공통(또는 worker) 모델로 1회 폴백해 시도한다.
            if _looks_like_model_not_found(exc):
                fallback_llm = _build_llm(profile="worker")
                logger.warning(
                    "Refiner 모델 접근 불가로 폴백합니다: %s -> %s",
                    os.getenv("THEME_REFINER_OPENAI_MODEL") or os.getenv("OPENAI_MODEL"),
                    os.getenv("THEME_WORKER_OPENAI_MODEL") or os.getenv("OPENAI_MODEL"),
                )
                try:
                    response = fallback_llm.invoke(messages, config={"timeout": timeout})
                    logger.info("Refiner(폴백) 응답 수신")
                    parsed = _parse_json_from_response(response.content if isinstance(response, AIMessage) else str(response))
                except Exception as exc2:  # noqa: BLE001
                    logger.warning("Refiner(폴백) 호출 실패: %s", exc2)
                    parsed = {}
            else:
                logger.warning("Refiner 호출 실패(타임아웃/네트워크 등): %s", exc)
                parsed = {}
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
        else:
            logger.info("Refiner 결과 수신: %d턴", len(refined_scripts))

        refined_scripts = _normalize_script_turns(refined_scripts)

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(refined_scripts, ensure_ascii=False, indent=2), encoding="utf-8")

        return {**state, "scripts": refined_scripts}

    graph.add_node("prefetch_cache", prefetch_cache)
    graph.add_node("run_theme_workers", run_theme_workers)
    graph.add_node("merge_scripts", merge_scripts)
    graph.add_node("refine_transitions", refine_transitions)

    graph.add_edge(START, "prefetch_cache")
    graph.add_edge("prefetch_cache", "run_theme_workers")
    graph.add_edge("run_theme_workers", "merge_scripts")
    graph.add_edge("merge_scripts", "refine_transitions")
    graph.add_edge("refine_transitions", END)
    graph.set_entry_point("prefetch_cache")

    return graph.compile()


# ==== 캐시 정리 ====
def cleanup_cache() -> None:
    """ThemeAgent 실행 후 캐시 파일을 정리한다."""
    news_files = [
        BASE_DIR / "data/theme/news_list.json",
        BASE_DIR / "data/theme/titles.txt",
        CALENDAR_CSV_PATH,
        CALENDAR_JSON_PATH,
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
