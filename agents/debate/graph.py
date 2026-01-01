"""LangGraph-based ticker debate pipeline.

This pipeline is designed to be used as a research/debate step.
It outputs a compact JSON artifact per ticker:
  - ticker, date(YYYYMMDD)
  - rounds[] (per round, each role utterance has text + action + confidence + sources[])
  - conclusion (moderator, with action + confidence)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Mapping, Sequence, TypedDict

import yaml
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from shared.config import cleanup_cache_dir, ensure_cache_dir, normalize_date, set_briefing_date
from shared.fetchers import prefetch_all
from shared.normalization import parse_json_from_response
from shared.tools import get_news_content, get_news_list, get_ohlcv, get_sec_filing_content, get_sec_filing_list
from shared.utils.llm import build_llm
from shared.utils.tracing import configure_tracing
from shared.yaml_config import load_env_from_yaml

from .types import DebateAction, DebateConclusion, DebateRound, DebateUtterance, Source, TickerDebateOutput, TickerDebateState

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent
PROMPT_PATH = BASE_DIR / "prompt" / "debate_main.yaml"

TOOLS = [get_news_content, get_ohlcv, get_sec_filing_list, get_sec_filing_content]

RoleName = Literal["fundamental", "risk", "growth", "sentiment"]
ROLES: tuple[RoleName, ...] = ("fundamental", "risk", "growth", "sentiment")


def _expect_mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{PROMPT_PATH}의 {name}는 mapping이어야 합니다.")
    return value


def _expect_str(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{PROMPT_PATH}의 {name}는 비어있지 않은 문자열이어야 합니다.")
    return value


def _load_prompts() -> dict[str, Any]:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"Debate prompt YAML이 없습니다: {PROMPT_PATH}")
    raw = yaml.safe_load(PROMPT_PATH.read_text(encoding="utf-8")) or {}
    root = _expect_mapping(raw, name="root")

    role_display_name = dict(_expect_mapping(root.get("role_display_name"), name="role_display_name"))  # type: ignore[arg-type]

    expert = _expect_mapping(root.get("expert"), name="expert")
    expert_available_tools = _expect_str(expert.get("available_tools"), name="expert.available_tools")
    expert_output_rules = _expect_str(expert.get("output_rules"), name="expert.output_rules")
    expert_role_descriptions = dict(
        _expect_mapping(expert.get("role_descriptions"), name="expert.role_descriptions")  # type: ignore[arg-type]
    )
    expert_user_templates = _expect_mapping(expert.get("user_templates"), name="expert.user_templates")
    expert_user_template_round1 = _expect_str(expert_user_templates.get("round1"), name="expert.user_templates.round1")
    expert_user_template_debate = _expect_str(expert_user_templates.get("debate"), name="expert.user_templates.debate")
    expert_user_template_debate_sentiment = _expect_str(
        expert_user_templates.get("debate_sentiment"), name="expert.user_templates.debate_sentiment"
    )

    moderator = _expect_mapping(root.get("moderator"), name="moderator")
    moderator_system = _expect_str(moderator.get("system"), name="moderator.system")
    moderator_user_template = _expect_str(moderator.get("user_template"), name="moderator.user_template")

    return {
        "role_display_name": role_display_name,
        "expert_available_tools": expert_available_tools,
        "expert_output_rules": expert_output_rules,
        "expert_role_descriptions": expert_role_descriptions,
        "expert_user_template_round1": expert_user_template_round1,
        "expert_user_template_debate": expert_user_template_debate,
        "expert_user_template_debate_sentiment": expert_user_template_debate_sentiment,
        "moderator_system": moderator_system,
        "moderator_user_template": moderator_user_template,
    }


_PROMPTS = _load_prompts()

ROLE_DISPLAY_NAME: dict[str, str] = _PROMPTS["role_display_name"]
EXPERT_AVAILABLE_TOOLS: str = _PROMPTS["expert_available_tools"]
EXPERT_OUTPUT_RULES: str = _PROMPTS["expert_output_rules"]
EXPERT_ROLE_DESCRIPTIONS: dict[str, str] = _PROMPTS["expert_role_descriptions"]
EXPERT_USER_TEMPLATE_ROUND1: str = _PROMPTS["expert_user_template_round1"]
EXPERT_USER_TEMPLATE_DEBATE: str = _PROMPTS["expert_user_template_debate"]
EXPERT_USER_TEMPLATE_DEBATE_SENTIMENT: str = _PROMPTS["expert_user_template_debate_sentiment"]
MODERATOR_SYSTEM: str = _PROMPTS["moderator_system"]
MODERATOR_USER_TEMPLATE: str = _PROMPTS["moderator_user_template"]


class ExpertState(TypedDict, total=False):
    date: str
    ticker: str
    role: RoleName
    round_number: int

    news_list_json: str
    sec_list_json: str
    ohlcv_summary: str
    allowed_sources: List[Source]

    guidance: str
    opponents: str

    messages: Annotated[Sequence[BaseMessage], add_messages]
    utterance: DebateUtterance


def _build_llm_for_role(role: str):
    return build_llm(f"DEBATE_{role.upper()}", logger=logger)


def _canonical_source(src: Dict[str, Any]) -> str:
    t = (src.get("type") or "").strip()
    if t == "article":
        return f"article:{src.get('pk','')}:{src.get('title','')}"
    if t == "chart":
        return f"chart:{src.get('ticker','')}:{src.get('start_date','')}:{src.get('end_date','')}"
    if t == "event":
        return f"event:{src.get('id','')}:{src.get('title','')}:{src.get('date','')}"
    if t == "sec_filing":
        return f"sec_filing:{src.get('ticker','')}:{src.get('form','')}:{src.get('filed_date','')}:{src.get('accession_number','')}"
    return f"unknown:{t}"


def _normalize_sources(raw_sources: Any, *, allowed_sources: List[Source]) -> List[Source]:
    if not isinstance(raw_sources, list):
        return []

    allowed = {_canonical_source(s): s for s in allowed_sources if isinstance(s, dict)}
    out: List[Source] = []
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        key = _canonical_source(item)
        picked = allowed.get(key)
        if picked:
            out.append(picked)

    if out:
        return out

    # fallback: at least one allowed source if possible
    return allowed_sources[:1] if allowed_sources else []


def _extract_utterance(content: str, *, allowed_sources: List[Source]) -> DebateUtterance:
    parsed = parse_json_from_response(content or "")
    text = parsed.get("text")
    if not isinstance(text, str) or not text.strip():
        text = ""
    action = _normalize_action(parsed.get("action"))
    confidence = _normalize_confidence(parsed.get("confidence"))
    sources = _normalize_sources(parsed.get("sources"), allowed_sources=allowed_sources)
    return {"text": text.strip(), "action": action, "confidence": confidence, "sources": sources}


def _format_opponents(prev_round: DebateRound | None, *, role: RoleName) -> str:
    if not prev_round:
        return ""
    parts: list[str] = []
    for other in ("fundamental", "risk", "growth", "sentiment"):
        if other == role:
            continue
        utter = prev_round.get(other)  # type: ignore[index]
        if isinstance(utter, dict):
            text = str(utter.get("text") or "")
            if text:
                parts.append(f"[{other}] {text[:800]}")
    return "\n\n".join(parts)


def expert_prepare_messages_node(state: ExpertState) -> ExpertState:
    role = state.get("role")
    if role not in EXPERT_ROLE_DESCRIPTIONS:
        raise ValueError(f"지원하지 않는 role입니다: {role!r}")

    round_number = int(state.get("round_number") or 1)
    allowed_sources = state.get("allowed_sources", []) or []
    allowed_sources_json = json.dumps(allowed_sources, ensure_ascii=False, indent=2)

    system_prompt = "\n".join(
        [
            EXPERT_ROLE_DESCRIPTIONS[role],
            "",
            EXPERT_AVAILABLE_TOOLS,
            "",
            EXPERT_OUTPUT_RULES,
        ]
    ).strip()

    if round_number <= 1:
        user_prompt = (
            EXPERT_USER_TEMPLATE_ROUND1.format(
                ticker=state.get("ticker", ""),
                date=state.get("date", ""),
                round_number=round_number,
                ohlcv_summary=state.get("ohlcv_summary", "") or "N/A",
                news_list_json=state.get("news_list_json", "") or "[]",
                sec_list_json=state.get("sec_list_json", "") or "[]",
                allowed_sources_json=allowed_sources_json,
            )
            .strip()
        )
    else:
        template = EXPERT_USER_TEMPLATE_DEBATE_SENTIMENT if role == "sentiment" else EXPERT_USER_TEMPLATE_DEBATE
        user_prompt = (
            template.format(
                role_display=ROLE_DISPLAY_NAME.get(role, str(role)),
                ticker=state.get("ticker", ""),
                date=state.get("date", ""),
                round_number=round_number,
                ohlcv_summary=state.get("ohlcv_summary", "") or "N/A",
                news_list_json=state.get("news_list_json", "") or "[]",
                sec_list_json=state.get("sec_list_json", "") or "[]",
                allowed_sources_json=allowed_sources_json,
                guidance=(state.get("guidance") or "").strip() or "(없음)",
                opponents=(state.get("opponents") or "").strip() or "(없음)",
            )
            .strip()
        )

    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    return {**state, "messages": messages}


def expert_agent_node(state: ExpertState) -> ExpertState:
    role = state.get("role") or "fundamental"
    llm = _build_llm_for_role(str(role))
    llm_with_tools = llm.bind_tools(TOOLS)
    messages = state.get("messages", [])
    resp = llm_with_tools.invoke(list(messages))
    return {**state, "messages": [resp]}


def expert_should_continue(state: ExpertState) -> str:
    messages = state.get("messages") or []
    if not messages:
        return "end"
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "end"


def expert_extract_node(state: ExpertState) -> ExpertState:
    messages = state.get("messages") or []
    raw = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            raw = msg.content or ""
            break
    utterance = _extract_utterance(raw, allowed_sources=state.get("allowed_sources", []) or [])
    return {**state, "utterance": utterance}


def build_expert_graph():
    graph = StateGraph(ExpertState)
    graph.add_node("prepare_messages", expert_prepare_messages_node)
    graph.add_node("agent", expert_agent_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("extract", expert_extract_node)

    graph.add_edge(START, "prepare_messages")
    graph.add_edge("prepare_messages", "agent")
    graph.add_conditional_edges(
        "agent",
        expert_should_continue,
        {
            "tools": "tools",
            "end": "extract",
        },
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("extract", END)
    return graph.compile()


def _summarize_ohlcv(rows: List[Dict[str, Any]], *, start_date: str, end_date: str) -> str:
    if not rows:
        return f"OHLCV({start_date}~{end_date}): 데이터 없음"
    closes: list[float] = []
    for r in rows:
        c = r.get("close")
        if isinstance(c, (int, float)):
            closes.append(float(c))
    if len(closes) < 2:
        return f"OHLCV({start_date}~{end_date}): close 데이터 부족 (rows={len(rows)})"

    last = closes[-1]
    prev = closes[-2]
    change_1d = (last - prev) / prev * 100 if prev else 0.0
    return f"OHLCV({start_date}~{end_date}): last_close={last:.2f}, 1d_change={change_1d:+.2f}% (rows={len(rows)})"


def _summarize_intraday_5m(rows: List[Dict[str, Any]], *, date: str) -> str:
    if not rows:
        return f"INTRADAY_5M({date}): 데이터 없음(또는 yfinance 제한/휴장)"

    first = rows[0]
    last = rows[-1]

    def _as_float(v: Any) -> float | None:
        try:
            return float(v)
        except Exception:
            return None

    open_first = _as_float(first.get("open"))
    close_last = _as_float(last.get("close"))
    highs = [_as_float(r.get("high")) for r in rows]
    lows = [_as_float(r.get("low")) for r in rows]
    highs_f = [h for h in highs if h is not None]
    lows_f = [l for l in lows if l is not None]

    high_max = max(highs_f) if highs_f else None
    low_min = min(lows_f) if lows_f else None

    change_pct = None
    if open_first not in (None, 0) and close_last is not None:
        change_pct = (close_last / open_first - 1.0) * 100.0

    ts_start = str(first.get("ts") or "")
    ts_end = str(last.get("ts") or "")

    parts = [f"INTRADAY_5M({date}): bars={len(rows)}"]
    if ts_start and ts_end:
        parts.append(f"range={ts_start}~{ts_end}")
    if open_first is not None and close_last is not None:
        parts.append(f"open={open_first:.2f}, close={close_last:.2f}")
    if change_pct is not None:
        parts.append(f"chg={change_pct:+.2f}%")
    if high_max is not None and low_min is not None:
        parts.append(f"high={high_max:.2f}, low={low_min:.2f}")
    return ", ".join(parts)


def debate_load_context_node(state: TickerDebateState) -> TickerDebateState:
    date = state.get("date") or ""
    ticker = (state.get("ticker") or "").upper()
    if not date or not ticker:
        raise ValueError("date/ticker가 필요합니다.")

    set_briefing_date(date)

    # 뉴스 후보 (pk/title)
    news = get_news_list.invoke({"tickers": [ticker]})
    raw_articles = news.get("articles", []) if isinstance(news, dict) else []
    articles_min: list[dict[str, str]] = []
    allowed_sources: list[Source] = []

    for art in raw_articles[:10]:
        if not isinstance(art, dict):
            continue
        pk = art.get("pk")
        title = art.get("title")
        if isinstance(pk, str) and pk.strip() and isinstance(title, str) and title.strip():
            articles_min.append({"pk": pk.strip(), "title": title.strip()})
            allowed_sources.append({"type": "article", "pk": pk.strip(), "title": title.strip()})

    # SEC 공시 후보(최신 10-K/10-Q 각 1개씩)
    sec_min: list[dict[str, str]] = []
    try:
        sec_list = get_sec_filing_list.invoke({"ticker": ticker, "forms": ["10-K", "10-Q"], "limit": 10})
        raw_filings = sec_list.get("filings", []) if isinstance(sec_list, dict) else []
        seen: set[str] = set()
        for f in raw_filings:
            if not isinstance(f, dict):
                continue
            form = str(f.get("form") or "").strip().upper()
            filed_date = str(f.get("filed_date") or "").strip()
            accession_number = str(f.get("accession_number") or "").strip()
            if not form or not filed_date or not accession_number:
                continue
            if form not in {"10-K", "10-Q"}:
                continue
            if form in seen:
                continue
            seen.add(form)
            sec_min.append({"form": form, "filed_date": filed_date, "accession_number": accession_number})
            allowed_sources.append(
                {
                    "type": "sec_filing",
                    "ticker": ticker,
                    "form": form,
                    "filed_date": filed_date,
                    "accession_number": accession_number,
                }
            )
            if len(seen) >= 2:
                break
    except Exception as exc:
        logger.warning("SEC filing list 실패: %s", exc)

    # 차트 source 2개
    # - 30d daily 요약용
    # - 당일 5m intraday 요약용
    end_dt = datetime.strptime(date, "%Y%m%d").date()
    start_dt_30d = end_dt - timedelta(days=30)

    chart_source_30d: Source = {
        "type": "chart",
        "ticker": ticker,
        "start_date": start_dt_30d.isoformat(),
        "end_date": end_dt.isoformat(),
    }
    allowed_sources.append(chart_source_30d)

    chart_source_intraday: Source = {
        "type": "chart",
        "ticker": ticker,
        "start_date": end_dt.isoformat(),
        "end_date": end_dt.isoformat(),
    }
    allowed_sources.append(chart_source_intraday)

    # 가격 요약
    # - 30일 일봉(1d)
    ohlcv = get_ohlcv.invoke(
        {
            "ticker": ticker,
            "start_date": start_dt_30d.isoformat(),
            "end_date": end_dt.isoformat(),
            "interval": "1d",
        }
    )
    rows = ohlcv.get("rows", []) if isinstance(ohlcv, dict) else []
    ohlcv_1d_summary = _summarize_ohlcv(
        rows if isinstance(rows, list) else [],
        start_date=str(ohlcv.get("start_date")),
        end_date=str(ohlcv.get("end_date")),
    )

    # - 당일 5분봉(5m) (yfinance 제한으로 과거 날짜는 비어 있을 수 있음)
    intraday = get_ohlcv.invoke(
        {
            "ticker": ticker,
            "start_date": end_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "interval": "5m",
        }
    )
    intraday_rows = intraday.get("rows", []) if isinstance(intraday, dict) else []
    intraday_5m_summary = _summarize_intraday_5m(intraday_rows if isinstance(intraday_rows, list) else [], date=end_dt.isoformat())

    ohlcv_summary = "\n".join([ohlcv_1d_summary, intraday_5m_summary]).strip()

    return {
        **state,
        "ticker": ticker,
        "date": date,
        "allowed_sources": allowed_sources,
        "news_list_json": json.dumps(articles_min, ensure_ascii=False, indent=2),
        "sec_list_json": json.dumps(sec_min, ensure_ascii=False, indent=2),
        "ohlcv_summary": ohlcv_summary,
    }


def debate_run_round_node(state: TickerDebateState) -> TickerDebateState:
    roles = list(ROLES)
    expert_graph = build_expert_graph()

    round_number = int(state.get("current_round") or 1)
    rounds: List[DebateRound] = list(state.get("rounds") or [])
    prev_round: DebateRound | None = rounds[-1] if rounds else None

    guidance_by_role = state.get("guidance_by_role") if isinstance(state.get("guidance_by_role"), dict) else {}
    allowed_sources = state.get("allowed_sources", []) or []

    inputs: list[ExpertState] = []
    for role in roles:
        inputs.append(
            {
                "date": state.get("date", ""),
                "ticker": state.get("ticker", ""),
                "role": role,
                "round_number": round_number,
                "news_list_json": str(state.get("news_list_json") or "[]"),
                "sec_list_json": str(state.get("sec_list_json") or "[]"),
                "ohlcv_summary": str(state.get("ohlcv_summary") or ""),
                "allowed_sources": allowed_sources,
                "guidance": str((guidance_by_role or {}).get(role) or ""),
                "opponents": _format_opponents(prev_round, role=role),
            }
        )

    results = expert_graph.batch(inputs, return_exceptions=True)  # type: ignore[attr-defined]

    round_obj: Dict[str, Any] = {"round": round_number}
    for role, res in zip(roles, results):
        if isinstance(res, Exception):
            logger.warning("Expert %s 실패: %s", role, res)
            round_obj[role] = {
                "text": "",
                "action": "HOLD",
                "confidence": 0.5,
                "sources": allowed_sources[:1] if allowed_sources else [],
            }
            continue
        utter = res.get("utterance")
        if not isinstance(utter, dict):
            utter = {
                "text": "",
                "action": "HOLD",
                "confidence": 0.5,
                "sources": allowed_sources[:1] if allowed_sources else [],
            }
        round_obj[role] = {
            "text": str(utter.get("text") or "").strip(),
            "action": _normalize_action(utter.get("action")),
            "confidence": _normalize_confidence(utter.get("confidence")),
            "sources": utter.get("sources") if isinstance(utter.get("sources"), list) else [],
        }

    rounds.append(round_obj)  # type: ignore[arg-type]
    return {**state, "rounds": rounds}


class ModeratorResult(TypedDict, total=False):
    needs_more_debate: bool
    guidance: Dict[str, str]
    conclusion: Dict[str, Any]


def _parse_moderator(content: str) -> ModeratorResult:
    parsed = parse_json_from_response(content or "")
    out: ModeratorResult = {}
    if isinstance(parsed.get("needs_more_debate"), bool):
        out["needs_more_debate"] = bool(parsed["needs_more_debate"])
    guidance = parsed.get("guidance")
    if isinstance(guidance, dict):
        out["guidance"] = {str(k): str(v) for k, v in guidance.items() if isinstance(k, str) and isinstance(v, str)}
    conclusion = parsed.get("conclusion")
    if isinstance(conclusion, dict):
        out["conclusion"] = conclusion
    return out


def _normalize_action(value: Any) -> DebateAction:
    raw = str(value or "").strip().upper()
    if raw in {"BUY", "HOLD", "SELL"}:
        return raw  # type: ignore[return-value]
    if "BUY" in raw:
        return "BUY"
    if "SELL" in raw:
        return "SELL"
    return "HOLD"


def _normalize_confidence(value: Any) -> float:
    try:
        f = float(value)
    except Exception:
        return 0.5
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return f


def _get_min_rounds() -> int:
    raw = os.getenv("DEBATE_MIN_ROUNDS", "2")
    try:
        v = int(raw)
    except Exception:
        v = 2
    return max(2, v)


def _get_consensus_confidence_threshold() -> float:
    raw = os.getenv("DEBATE_CONSENSUS_CONFIDENCE", "0.7")
    try:
        v = float(raw)
    except Exception:
        v = 0.7
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


def _summarize_expert_positions(round_obj: Any) -> str:
    if not isinstance(round_obj, dict):
        return ""
    parts: list[str] = []
    for role in ROLES:
        utter = round_obj.get(role)
        if not isinstance(utter, dict):
            utter = {}
        action = _normalize_action(utter.get("action"))
        confidence = _normalize_confidence(utter.get("confidence"))
        parts.append(f"{role}={action}({confidence:.2f})")
    return ", ".join(parts)


def _round_meets_consensus(round_obj: Any, *, confidence_threshold: float) -> bool:
    if not isinstance(round_obj, dict):
        return False
    actions: list[DebateAction] = []
    confidences: list[float] = []
    for role in ROLES:
        utter = round_obj.get(role)
        if not isinstance(utter, dict):
            return False
        actions.append(_normalize_action(utter.get("action")))
        confidences.append(_normalize_confidence(utter.get("confidence")))
    if len(set(actions)) != 1:
        return False
    return all(c >= confidence_threshold for c in confidences)


def debate_moderator_node(state: TickerDebateState) -> TickerDebateState:
    llm = build_llm("DEBATE_MODERATOR", logger=logger)

    ticker = state.get("ticker", "")
    date = state.get("date", "")
    rounds = state.get("rounds", []) or []
    round_number = int(state.get("current_round") or 1)
    max_rounds = int(state.get("max_rounds") or 2)
    min_rounds = _get_min_rounds()
    confidence_threshold = _get_consensus_confidence_threshold()

    last_round = rounds[-1] if rounds else None
    consensus_reached = _round_meets_consensus(last_round, confidence_threshold=confidence_threshold)
    last_round_positions = _summarize_expert_positions(last_round)

    forced_next_step = (
        "continue" if (round_number < min_rounds or (not consensus_reached and round_number < max_rounds)) else "end"
    )

    rounds_json = json.dumps(rounds, ensure_ascii=False, indent=2)
    user_prompt = MODERATOR_USER_TEMPLATE.format(
        ticker=ticker,
        date=date,
        round_number=round_number,
        max_rounds=max_rounds,
        min_rounds=min_rounds,
        confidence_threshold=confidence_threshold,
        last_round_positions=last_round_positions or "(없음)",
        consensus_reached=str(consensus_reached).lower(),
        forced_next_step=forced_next_step,
        rounds_json=rounds_json,
    ).strip()

    messages = [SystemMessage(content=MODERATOR_SYSTEM), HumanMessage(content=user_prompt)]
    resp = llm.invoke(messages)
    parsed = _parse_moderator(resp.content or "")

    should_continue = forced_next_step == "continue"
    if should_continue:
        guidance = parsed.get("guidance") or {}
        guidance_by_role = {
            "fundamental": str(guidance.get("fundamental") or "").strip(),
            "risk": str(guidance.get("risk") or "").strip(),
            "growth": str(guidance.get("growth") or "").strip(),
            "sentiment": str(guidance.get("sentiment") or "").strip(),
        }
        if not any(guidance_by_role.values()):
            fallback = (
                "다른 전문가들의 action/confidence 및 근거를 직접 반응(동의/반박)하고, "
                "필요 시 자신의 action/confidence를 조정하세요. 가능한 한 숫자/날짜 근거를 sources로 남기세요."
            )
            guidance_by_role = {k: fallback for k in guidance_by_role}
        return {**state, "guidance_by_role": guidance_by_role, "current_round": round_number + 1, "should_continue": True}

    # finalize
    concl = parsed.get("conclusion") or {}
    conclusion: DebateConclusion = {
        "text": str(concl.get("text") or "").strip(),
        "action": _normalize_action(concl.get("action")),
        "confidence": _normalize_confidence(concl.get("confidence")),
    }
    return {**state, "conclusion": conclusion, "should_continue": False}


def debate_should_continue(state: TickerDebateState) -> str:
    if state.get("should_continue"):
        return "continue"
    return "end"


def debate_init_node(state: TickerDebateState) -> TickerDebateState:
    date = normalize_date(state.get("date") or "")
    ticker = str(state.get("ticker") or "").upper().strip()
    max_rounds_raw = state.get("max_rounds")
    try:
        max_rounds = int(max_rounds_raw) if max_rounds_raw is not None else int(os.getenv("DEBATE_MAX_ROUNDS", "2"))
    except Exception:
        max_rounds = 2
    min_rounds = _get_min_rounds()
    if max_rounds < min_rounds:
        max_rounds = min_rounds
    return {
        **state,
        "date": date,
        "ticker": ticker,
        "rounds": [],
        "current_round": 1,
        "max_rounds": max_rounds,
    }


def build_graph():
    load_env_from_yaml(logger=logger)
    load_dotenv(ROOT_DIR / ".env", override=False)
    configure_tracing(logger=logger)

    graph = StateGraph(TickerDebateState)
    graph.add_node("init", debate_init_node)
    graph.add_node("load_context", debate_load_context_node)
    graph.add_node("run_round", debate_run_round_node)
    graph.add_node("moderator", debate_moderator_node)

    graph.add_edge(START, "init")
    graph.add_edge("init", "load_context")
    graph.add_edge("load_context", "run_round")
    graph.add_edge("run_round", "moderator")

    graph.add_conditional_edges(
        "moderator",
        debate_should_continue,
        {
            "continue": "run_round",
            "end": END,
        },
    )

    return graph.compile()


def _ensure_prefetch(date_yyyymmdd: str) -> None:
    cache_dir = ensure_cache_dir(date_yyyymmdd)
    if (cache_dir / "news_list.json").exists():
        return
    date_obj = datetime.strptime(date_yyyymmdd, "%Y%m%d").date()
    prefetch_all(date_obj, cache_dir=cache_dir)


def run_debate(*, date: str, ticker: str, max_rounds: int = 2, prefetch: bool = True, cleanup: bool = False) -> TickerDebateOutput:
    date_norm = normalize_date(date)
    ticker_norm = str(ticker).upper().strip()
    set_briefing_date(date_norm)

    if prefetch:
        _ensure_prefetch(date_norm)

    app = build_graph()
    try:
        result = app.invoke({"date": date_norm, "ticker": ticker_norm, "max_rounds": max_rounds})
    finally:
        if cleanup:
            cleanup_cache_dir(date_norm)

    # Persisted output shape
    return {
        "ticker": str(result.get("ticker") or ticker_norm),
        "date": str(result.get("date") or date_norm),
        "rounds": result.get("rounds") or [],
        "conclusion": result.get("conclusion") or {"text": "", "action": "HOLD", "confidence": 0.5},
    }


def main(argv: List[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Ticker debate (multi-agent, outputs compact JSON)")
    parser.add_argument("date", type=str, help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("ticker", type=str, help="Ticker symbol (e.g., GOOG)")
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="Max debate rounds (overrides DEBATE_MAX_ROUNDS env when provided).",
    )
    parser.add_argument("--no-prefetch", action="store_true", help="Do not run prefetch_all; require existing cache/{date}.")
    parser.add_argument("--cleanup", action="store_true", help="Cleanup cache/{date} after run (temp outputs unaffected).")
    args = parser.parse_args(argv)

    try:
        max_rounds = int(args.max_rounds) if args.max_rounds is not None else int(os.getenv("DEBATE_MAX_ROUNDS", "2"))
    except Exception:
        max_rounds = 2

    try:
        out = run_debate(
            date=args.date,
            ticker=args.ticker,
            max_rounds=max_rounds,
            prefetch=not args.no_prefetch,
            cleanup=args.cleanup,
        )
    except Exception as e:
        logger.error("Debate failed: %s", e)
        return 1

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
