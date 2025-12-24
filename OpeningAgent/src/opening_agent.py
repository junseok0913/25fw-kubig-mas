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
from typing import Annotated, Any, Dict, List, Literal, Sequence, TypedDict

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
    get_calendar,
    get_news_content,
    get_news_list,
    get_ohlcv,
    list_downloaded_bodies,
)

# 로깅 설정: CLI 실행 시에도 깔끔하게 남도록 INFO 기본값 사용
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent  # OpeningAgent/
ROOT_DIR = BASE_DIR.parent  # repo root

# 컨텍스트 파일 경로 (yfinance 수집 결과)
CONTEXT_PATH = BASE_DIR / "data/market_context.json"
# 프롬프트 YAML 경로
PROMPT_PATH = BASE_DIR / "prompt/opening_main.yaml"
TITLES_PATH = BASE_DIR / "data/opening/titles.txt"
CALENDAR_CSV_PATH = BASE_DIR / "data/opening/calendar.csv"
CALENDAR_JSON_PATH = BASE_DIR / "data/opening/calendar.json"
# 불용어 파일 경로
STOPWORDS_PATH = BASE_DIR / "config/stopwords.txt"
# 최종 결과 JSON 저장 경로
OUTPUT_PATH = BASE_DIR / "data/opening_result.json"


def _load_env() -> None:
    """리포지토리 루트(.env)에서 환경변수 로드."""
    load_dotenv(ROOT_DIR / ".env", override=False)

# LangChain Tool 리스트 (LLM에 바인딩할 도구들)
TOOLS = [
    get_news_list,
    get_news_content,
    list_downloaded_bodies,
    count_keyword_frequency,
    get_calendar,
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


def _load_stopwords() -> frozenset[str]:
    """config/stopwords.txt에서 불용어를 로드한다.
    
    파일이 없으면 빈 frozenset을 반환한다.
    '#'으로 시작하는 줄은 주석으로 무시된다.
    """
    if not STOPWORDS_PATH.exists():
        logger.warning("불용어 파일이 없습니다: %s", STOPWORDS_PATH)
        return frozenset()
    
    words = []
    for line in STOPWORDS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 빈 줄이나 주석(#)은 무시
        if not line or line.startswith("#"):
            continue
        words.append(line.lower())
    
    return frozenset(words)


def _format_date_korean(date_yyyymmdd: str) -> str:
    """YYYYMMDD를 '11월 25일' 형식의 한국어로 변환한다."""
    from datetime import datetime
    dt = datetime.strptime(date_yyyymmdd, "%Y%m%d")
    return f"{dt.month}월 {dt.day}일"


def _top_words_from_titles(limit: int = 30) -> list[dict[str, Any]]:
    """titles.txt에서 단어 빈도 상위 N개를 계산한다.
    
    불용어(stopwords), 한글자 단어, 순수 숫자는 제외된다.
    """
    if not TITLES_PATH.exists():
        return []
    text = TITLES_PATH.read_text(encoding="utf-8")
    tokens = re.findall(r"[A-Za-z0-9$%+\-']+", text.lower())
    
    # 불용어 로드
    stopwords = _load_stopwords()
    
    # 필터링: 한글자 단어, 순수 숫자, 불용어 제외
    filtered_tokens = [
        t for t in tokens
        if len(t) > 1  # 한글자 제외
        and not t.isdigit()  # 순수 숫자 제외
        and t not in stopwords  # 불용어 제외
    ]
    
    counter = Counter(filtered_tokens)
    top = counter.most_common(limit)
    return [{"word": w, "count": c} for w, c in top]


class NewsSource(TypedDict):
    """뉴스 출처 정보."""
    pk: str
    title: str


class Theme(TypedDict):
    """핵심 테마 정보."""
    headline: str  # 짧은 테마 제목 (10~20자)
    description: str  # 테마 상세 설명 (1~3문장)
    related_news: List[NewsSource]  # 근거 뉴스 목록


class ScriptTurn(TypedDict):
    """진행자/해설자 한 턴의 발언."""
    id: int
    speaker: Literal["진행자", "해설자"]
    text: str
    sources: List[NewsSource]


class OpeningState(TypedDict, total=False):
    """그래프 상태 정의: 메시지 기반 ReAct 패턴.
    
    공통 필드:
        date: 브리핑 날짜 (YYYYMMDD 형식, EST 기준)
    
    중간 처리용 필드:
        messages: ReAct 메시지 히스토리
        context_json: yfinance 시장 데이터
        news_meta: DynamoDB에서 프리페치한 뉴스 메타
    
    최종 출력용 필드 (Agent 완료 후):
        themes: 핵심 테마 1~3개 (headline, description, related_news 포함)
        nutshell: 오늘 장 한마디
        scripts: 진행자/해설자 대화
    """
    # 공통: 날짜 (YYYYMMDD 형식, EST 기준)
    date: str
    
    # 중간 처리용
    messages: Annotated[Sequence[BaseMessage], add_messages]
    context_json: Dict[str, Any]
    news_meta: Dict[str, Any]
    
    # 최종 출력용
    themes: List[Theme]
    nutshell: str
    scripts: List[ScriptTurn]


def prefetch_node(state: OpeningState) -> OpeningState:
    """DynamoDB에서 뉴스 메타데이터를 사전 수집한다."""
    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다. 날짜를 지정하세요.")
    
    try:
        # date를 prefetch_news에 전달
        from datetime import datetime
        date_obj = datetime.strptime(date_str, "%Y%m%d").date()
        payload = prefetch.prefetch_news(today=date_obj)
        logger.info("프리페치 완료: %d건 (날짜: %s)", payload.get("count", 0), date_str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("프리페치 실패, 그래프는 계속 진행합니다: %s", exc)
        payload = {}

    try:
        cal_payload = prefetch.prefetch_calendar(today=date_obj)
        logger.info("캘린더 프리페치 완료: %d건 (날짜: %s)", cal_payload.get("count", 0), date_str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("캘린더 프리페치 실패, 그래프는 계속 진행합니다: %s", exc)
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
    # 제목 상위 50개 단어 빈도 추가
    context["title_top_words"] = _top_words_from_titles(limit=50)
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


def _load_calendar_context() -> str:
    """calendar.csv를 읽어 프롬프트에 넣을 최소 컨텍스트 문자열을 만든다.

    각 줄: id, est_date(YYYYMMDD), title (TSV)
    """
    if not CALENDAR_CSV_PATH.exists():
        return ""

    import csv

    lines: list[str] = ["id\test_date\ttitle"]
    with CALENDAR_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
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


def _build_llm() -> ChatOpenAI:
    """
    OpenAI LLM 인스턴스 생성 (context-7/5.1 스타일 설정).

    Env override 우선순위:
    - OPENING_OPENAI_* (예: OPENING_OPENAI_MODEL)
    - OPENAI_*
    - LangGraph에서 요구하는 ChatModel 인터페이스를 준수
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
        return _getenv_nonempty(f"OPENING_{key}", _getenv_nonempty(default_env_key, default))

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


def _prepare_initial_messages(state: OpeningState) -> OpeningState:
    """초기 시스템/사용자 메시지를 준비한다."""
    context = state.get("context_json")
    if not context:
        raise ValueError("context_json이 비어 있습니다. load_context_node를 확인하세요.")
    
    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다.")
    
    # 한국어 날짜 형식 (예: "11월 25일")
    date_korean = _format_date_korean(date_str)

    prompt_cfg = load_prompt()

    # {tools} 플레이스홀더를 실제 도구 설명으로 대체
    # {date} 플레이스홀더를 한국어 날짜로 대체
    system_prompt = prompt_cfg["system"].replace(
        "{tools}", _get_tools_description()
    ).replace(
        "{date}", date_korean
    )

    # {context_json} 플레이스홀더를 컨텍스트로 대체
    # {date} 플레이스홀더를 한국어 날짜로 대체
    title_top_words = json.dumps(context.get("title_top_words", []), ensure_ascii=False, indent=2)
    calendar_context = _load_calendar_context()
    context_for_prompt = dict(context)
    context_for_prompt.pop("title_top_words", None)
    user_prompt = prompt_cfg["user_template"].replace(
        "{context_json}", json.dumps(context_for_prompt, ensure_ascii=False, indent=2)
    ).replace(
        "{title_top_words}", title_top_words
    ).replace(
        "{calendar_context}", calendar_context
    ).replace(
        "{date}", date_korean
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


def _parse_json_from_response(content: str) -> Dict[str, Any]:
    """AI 응답에서 JSON을 추출하고 파싱한다."""
    # ```json ... ``` 블록에서 JSON 추출
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", content)
    if json_match:
        json_str = json_match.group(1)
    else:
        # 블록이 없으면 전체 내용을 JSON으로 시도
        json_str = content.strip()
    
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error("JSON 파싱 실패: %s", e)
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


def extract_script_node(state: OpeningState) -> OpeningState:
    """최종 메시지에서 구조화된 대본을 추출하고 JSON 파일로 저장한다.
    
    중간 처리용 필드(messages, context_json, news_meta)를 제거하고
    최종 출력용 필드(themes, nutshell, scripts)만 반환한다.
    """
    messages = state.get("messages", [])
    
    # AI의 마지막 응답에서 JSON 추출
    raw_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            raw_content = msg.content
            break
    
    # JSON 파싱
    parsed = _parse_json_from_response(raw_content)
    
    themes = parsed.get("themes", [])
    nutshell = parsed.get("nutshell", "")
    scripts = _assign_script_ids(parsed.get("scripts", []))
    
    # 결과 구성
    result = {"themes": themes, "nutshell": nutshell, "scripts": scripts}
    
    # JSON 파일로 저장
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.info("최종 결과를 저장했습니다: %s", OUTPUT_PATH)
    
    # 최종 State: date 유지, 중간 처리용 필드 제거, 최종 출력용 필드만 반환
    return {
        "date": state.get("date"),
        "themes": themes,
        "nutshell": nutshell,
        "scripts": scripts,
    }


def build_graph():
    """LangGraph 그래프를 구성하고 반환.

    ReAct 패턴:
    START → prefetch_news → load_context → prepare_messages
          → agent ⇄ tools (반복)
          → extract_script → END
    """
    _load_env()
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
        BASE_DIR / "data/opening/news_list.json",
        BASE_DIR / "data/opening/titles.txt",
        BASE_DIR / "data/market_context.json",
        CALENDAR_CSV_PATH,
        CALENDAR_JSON_PATH,
    ]
    bodies_dir = BASE_DIR / "data/opening/bodies"
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
    tmp_dir = BASE_DIR / "data/_tmp_csv"
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
    """그래프 실행 진입점.
    
    Usage:
        python -m src.opening_agent 20251125
        python -m src.opening_agent 2025-11-25
    """
    import argparse
    from datetime import datetime as dt
    
    parser = argparse.ArgumentParser(description="오프닝 에이전트 실행")
    parser.add_argument(
        "date",
        type=str,
        help="브리핑 날짜 (YYYYMMDD 또는 YYYY-MM-DD 형식, EST 기준)"
    )
    args = parser.parse_args()
    
    # 날짜 파싱
    date_str = args.date
    if "-" in date_str:
        try:
            parsed = dt.strptime(date_str, "%Y-%m-%d")
            date_str = parsed.strftime("%Y%m%d")
        except ValueError:
            raise ValueError(f"잘못된 날짜 형식: {date_str}")
    else:
        try:
            dt.strptime(date_str, "%Y%m%d")
        except ValueError:
            raise ValueError(f"잘못된 날짜 형식: {date_str}")
    
    _load_env()
    
    # BRIEFING_DATE 환경변수로 설정 (Tool에서 사용)
    os.environ["BRIEFING_DATE"] = date_str

    workflow = build_graph()
    # date를 state에 전달하여 실행
    result = workflow.invoke({"date": date_str})

    # 최종 State 출력
    print("\n=== 오프닝 대본 결과 ===\n")
    
    themes = result.get("themes", [])
    nutshell = result.get("nutshell", "")
    scripts = result.get("scripts", [])
    
    print("테마:")
    for i, t in enumerate(themes, 1):
        headline = t.get("headline", "") if isinstance(t, dict) else t
        description = t.get("description", "") if isinstance(t, dict) else ""
        related_news = t.get("related_news", []) if isinstance(t, dict) else []
        
        print(f"  {i}. {headline}")
        if description:
            print(f"     {description}")
        if related_news:
            news_titles = [n.get("title", "") for n in related_news]
            print(f"근거: {', '.join(news_titles[:3])}")  # 최대 3개만 표시
    
    print(f"\n한마디: {nutshell}")
    print("\n--- 대본 ---\n")
    
    for turn in scripts:
        speaker = turn.get("speaker", "")
        text = turn.get("text", "")
        sources = turn.get("sources", [])
        
        print(f"[{speaker}] {text}")
        if sources:
            source_titles = [s.get("title", "") for s in sources]
            print(f"출처: {', '.join(source_titles)}")
        print()
    
    print(f"\n결과가 저장되었습니다: {OUTPUT_PATH}")

    # 실행이 끝나면 캐시된 파일 정리
    try:
        cleanup_cache()
    except Exception as exc:  # noqa: BLE001
        logger.warning("캐시 삭제 중 오류: %s", exc)


if __name__ == "__main__":
    main()
