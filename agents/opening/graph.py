"""Opening agent graph using shared cache."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
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
    get_market_context_path,
    get_temp_opening_path,
    get_titles_path,
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
    list_downloaded_bodies,
)
from shared.types import Theme
from shared.utils.llm import build_llm
from shared.utils.tracing import configure_tracing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent

PROMPT_PATH = BASE_DIR / "prompt/opening_main.yaml"
STOPWORDS_PATH = BASE_DIR / "config/stopwords.txt"

TOOLS = [
    get_news_list,
    get_news_content,
    list_downloaded_bodies,
    count_keyword_frequency,
    get_calendar,
    get_ohlcv,
]


class NewsSource(TypedDict):
    pk: str
    title: str


class ScriptSource(TypedDict):
    type: Literal["article", "chart", "event"]


class ScriptTurn(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    text: str
    sources: List[ScriptSource]


class OpeningState(TypedDict, total=False):
    date: str
    messages: Annotated[Sequence[BaseMessage], add_messages]
    context_json: Dict[str, Any]
    themes: List[Theme]
    nutshell: str
    scripts: List[ScriptTurn]


def _load_env() -> None:
    load_dotenv(ROOT_DIR / ".env", override=False)


def _get_tools_description() -> str:
    descriptions = []
    for tool in TOOLS:
        descriptions.append(f"- {tool.name}: {tool.description or ''}")
    return "\n".join(descriptions)


def _load_stopwords() -> frozenset[str]:
    if not STOPWORDS_PATH.exists():
        logger.warning("불용어 파일이 없습니다: %s", STOPWORDS_PATH)
        return frozenset()

    words = []
    for line in STOPWORDS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.append(line.lower())

    return frozenset(words)


def _format_date_korean(date_yyyymmdd: str) -> str:
    from datetime import datetime

    dt = datetime.strptime(date_yyyymmdd, "%Y%m%d")
    return f"{dt.month}월 {dt.day}일"


def _top_words_from_titles(limit: int = 30) -> list[dict[str, Any]]:
    titles_path = get_titles_path()
    if not titles_path.exists():
        return []
    text = titles_path.read_text(encoding="utf-8")
    tokens = re.findall(r"[A-Za-z0-9$%+\-']+", text.lower())

    stopwords = _load_stopwords()
    filtered_tokens = [
        t
        for t in tokens
        if len(t) > 1 and not t.isdigit() and t not in stopwords
    ]

    counter = Counter(filtered_tokens)
    top = counter.most_common(limit)
    return [{"word": w, "count": c} for w, c in top]


def load_context_node(state: OpeningState) -> OpeningState:
    date_str = state.get("date")
    if date_str:
        set_briefing_date(date_str)

    context_path = get_market_context_path()
    if not context_path.exists():
        raise FileNotFoundError(f"컨텍스트 파일이 없습니다: {context_path}")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    logger.info("Loaded market context from %s", context_path)
    context["title_top_words"] = _top_words_from_titles(limit=50)
    return {**state, "context_json": context}


def load_prompt() -> Dict[str, str]:
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


def _build_llm():
    return build_llm("OPENING", logger=logger)


def _prepare_initial_messages(state: OpeningState) -> OpeningState:
    context = state.get("context_json")
    if not context:
        raise ValueError("context_json이 비어 있습니다. load_context_node를 확인하세요.")

    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다.")

    date_korean = _format_date_korean(date_str)

    prompt_cfg = load_prompt()
    system_prompt = prompt_cfg["system"].replace("{tools}", _get_tools_description()).replace("{date}", date_korean)

    title_top_words = json.dumps(context.get("title_top_words", []), ensure_ascii=False, indent=2)
    calendar_context = _load_calendar_context()
    context_for_prompt = dict(context)
    context_for_prompt.pop("title_top_words", None)

    user_prompt = (
        prompt_cfg["user_template"]
        .replace("{context_json}", json.dumps(context_for_prompt, ensure_ascii=False, indent=2))
        .replace("{title_top_words}", title_top_words)
        .replace("{calendar_context}", calendar_context)
        .replace("{date}", date_korean)
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]

    return {**state, "messages": messages}


def agent_node(state: OpeningState) -> OpeningState:
    llm = _build_llm()
    llm_with_tools = llm.bind_tools(TOOLS)

    messages = state.get("messages", [])
    logger.info("Agent 호출: %d개 메시지", len(messages))

    response = llm_with_tools.invoke(messages)
    return {**state, "messages": [response]}


def should_continue(state: OpeningState) -> str:
    messages = state.get("messages", [])
    if not messages:
        return "end"

    last_message = messages[-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        logger.info("Tool 호출 감지: %d개", len(last_message.tool_calls))
        return "tools"
    return "end"


def extract_script_node(state: OpeningState) -> OpeningState:
    messages = state.get("messages", [])

    raw_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            raw_content = msg.content
            break

    parsed = parse_json_from_response(raw_content)

    themes = parsed.get("themes", [])
    nutshell = parsed.get("nutshell", "")
    scripts = normalize_script_turns(parsed.get("scripts", []))

    result = {"date": state.get("date"), "themes": themes, "nutshell": nutshell, "scripts": scripts}

    ensure_temp_dir()
    output_path = get_temp_opening_path()
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Opening 결과를 저장했습니다: %s", output_path)

    return result


def build_graph():
    _load_env()
    configure_tracing(logger=logger)
    graph = StateGraph(OpeningState)

    graph.add_node("load_context", load_context_node)
    graph.add_node("prepare_messages", _prepare_initial_messages)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("extract_script", extract_script_node)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "prepare_messages")
    graph.add_edge("prepare_messages", "agent")

    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            "end": "extract_script",
        },
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("extract_script", END)

    graph.set_entry_point("load_context")
    return graph.compile()


def main() -> None:
    import argparse
    from datetime import datetime as dt

    parser = argparse.ArgumentParser(description="오프닝 에이전트 실행")
    parser.add_argument("date", type=str, help="브리핑 날짜 (YYYYMMDD 또는 YYYY-MM-DD 형식, EST 기준)")
    args = parser.parse_args()

    date_str = args.date
    if "-" in date_str:
        date_str = dt.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")
    else:
        dt.strptime(date_str, "%Y%m%d")

    _load_env()
    set_briefing_date(date_str)
    date_obj = dt.strptime(date_str, "%Y%m%d").date()
    cache_dir = ensure_cache_dir(date_str)
    prefetch_all(date_obj, cache_dir=cache_dir)

    try:
        app = build_graph()
        result = app.invoke({"date": date_str})

        print("\n=== 오프닝 대본 결과 ===\n")
        print("테마:")
        for i, t in enumerate(result.get("themes", []), 1):
            headline = t.get("headline", "") if isinstance(t, dict) else t
            description = t.get("description", "") if isinstance(t, dict) else ""
            print(f"  {i}. {headline}")
            if description:
                print(f"     - {description}")

        print("\n한 마디:", result.get("nutshell"))
        print("\n대본:")
        for turn in result.get("scripts", []):
            print(f"[{turn.get('id')}] {turn.get('speaker')}: {turn.get('text')}")
    finally:
        cleanup_cache_dir(date_str)


if __name__ == "__main__":
    main()
