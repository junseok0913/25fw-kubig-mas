"""Ticker script generation pipeline (debate 기반, tool-less).

Fan-out (per ticker worker) -> fan-in merge -> refiner(transition polish).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Sequence, TypedDict

import yaml
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.messages.base import BaseMessage
from langgraph.graph import END, START, StateGraph

from shared.config import normalize_date, set_briefing_date
from shared.normalization import parse_json_from_response
from shared.tools import get_ohlcv
from shared.utils.llm import build_llm

from .types import Source as DebateSource

logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[1]

WORKER_PROMPT_PATH = Path(__file__).resolve().parent / "prompt" / "ticker_script_worker.yaml"
REFINER_PROMPT_PATH = Path(__file__).resolve().parent / "prompt" / "ticker_script_refine.yaml"


class TickerScriptWorkerOutput(TypedDict):
    ticker: str
    scripts: List["TickerScriptTurn"]


class TickerScriptTurn(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    text: str
    sources: List[DebateSource]


class TickerSection(TypedDict):
    ticker: str
    start_id: int
    end_id: int


class TickerScriptPipelineOutput(TypedDict):
    date: str
    user_tickers: List[str]
    ticker_scripts: List[TickerScriptWorkerOutput]
    ticker_sections: List[TickerSection]
    refiner_edits: List[Dict[str, Any]]
    scripts: List[TickerScriptTurn]


class _WorkerPromptCfg(TypedDict):
    system: str
    user_template: str


class _RefinerPromptCfg(TypedDict):
    system: str
    user_template: str


class TickerScriptWorkerState(TypedDict, total=False):
    date: str
    ticker: str
    ticker_index: int
    tickers_total: int
    all_tickers: List[str]

    base_scripts: List[TickerScriptTurn]
    debate_json: Dict[str, Any]
    allowed_sources: List[DebateSource]

    intraday_ohlcv_5m_summary: str
    intraday_ohlcv_5m_json: str
    intraday_ohlcv_source_json: str

    messages: Sequence[BaseMessage]
    scripts: List[TickerScriptTurn]


def _load_worker_prompt() -> _WorkerPromptCfg:
    if not WORKER_PROMPT_PATH.exists():
        raise FileNotFoundError(f"worker 프롬프트 파일이 없습니다: {WORKER_PROMPT_PATH}")
    with WORKER_PROMPT_PATH.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    system = str(raw.get("system") or "")
    user_template = str(raw.get("user_template") or "")
    if not system or not user_template:
        raise ValueError("ticker_script_worker.yaml의 system/user_template가 비어 있습니다.")
    return {"system": system, "user_template": user_template}


def _load_refiner_prompt() -> _RefinerPromptCfg:
    if not REFINER_PROMPT_PATH.exists():
        raise FileNotFoundError(f"refiner 프롬프트 파일이 없습니다: {REFINER_PROMPT_PATH}")
    with REFINER_PROMPT_PATH.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    system = str(raw.get("system") or "")
    user_template = str(raw.get("user_template") or "")
    if not system or not user_template:
        raise ValueError("ticker_script_refine.yaml의 system/user_template가 비어 있습니다.")
    return {"system": system, "user_template": user_template}


def _one_line(text: str) -> str:
    s = str(text or "").replace("\r", " ").replace("\n", " ")
    s = " ".join(s.split())
    return s.strip()


def _canonical_source(src: Dict[str, Any]) -> str:
    t = str(src.get("type") or "").strip()
    if t == "article":
        return f"article:{src.get('pk','')}:{src.get('title','')}"
    if t == "chart":
        ticker = str(src.get("ticker") or "").strip().upper()
        return f"chart:{ticker}:{src.get('start_date','')}:{src.get('end_date','')}"
    if t == "event":
        return f"event:{src.get('id','')}:{src.get('title','')}:{src.get('date','')}"
    if t == "sec_filing":
        ticker = str(src.get("ticker") or "").strip().upper()
        form = str(src.get("form") or "").strip().upper()
        filed_date = str(src.get("filed_date") or "").strip()
        acc = str(src.get("accession_number") or "").strip()
        return (
            f"sec_filing:{ticker}:{form}:"
            f"{filed_date}:{acc}"
        )
    return f"unknown:{t}"


def _collect_allowed_sources(*, base_scripts: List[TickerScriptTurn], debate_json: Dict[str, Any]) -> List[DebateSource]:
    allowed: List[DebateSource] = []
    seen: set[str] = set()

    def add(src: Any) -> None:
        if not isinstance(src, dict):
            return
        if str(src.get("type") or "").strip() not in {"article", "chart", "event", "sec_filing"}:
            return
        key = _canonical_source(src)
        if key in seen:
            return
        seen.add(key)
        allowed.append(src)  # type: ignore[arg-type]

    for turn in base_scripts or []:
        if not isinstance(turn, dict):
            continue
        for src in turn.get("sources", []) or []:
            add(src)

    rounds = debate_json.get("rounds", []) if isinstance(debate_json, dict) else []
    if isinstance(rounds, list):
        for rnd in rounds:
            if not isinstance(rnd, dict):
                continue
            for role in ("fundamental", "risk", "growth", "sentiment"):
                utter = rnd.get(role)
                if not isinstance(utter, dict):
                    continue
                for src in utter.get("sources", []) or []:
                    add(src)

    return allowed


def _filter_sources_to_allowed(*, scripts: List[TickerScriptTurn], allowed_sources: List[DebateSource]) -> List[TickerScriptTurn]:
    allowed_map: dict[str, DebateSource] = {}
    for s in allowed_sources or []:
        if not isinstance(s, dict):
            continue
        key = _canonical_source(s)
        if key.startswith("unknown:"):
            continue
        allowed_map[key] = s

    out: List[TickerScriptTurn] = []
    for turn in scripts or []:
        if not isinstance(turn, dict):
            continue
        sources = turn.get("sources", [])
        filtered: list[DebateSource] = []
        if isinstance(sources, list):
            for src in sources:
                if not isinstance(src, dict):
                    continue
                key = _canonical_source(src)
                picked = allowed_map.get(key)
                if picked:
                    filtered.append(picked)
        out.append(
            {
                "id": int(turn.get("id", len(out))),
                "speaker": turn.get("speaker"),  # type: ignore[typeddict-item]
                "text": _one_line(str(turn.get("text") or "")),
                "sources": filtered,
            }
        )

    # reassign ids to 0..N-1
    return _normalize_ticker_script_turns(out)


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


def _fetch_intraday_ohlcv_5m(*, ticker: str, date_yyyymmdd: str) -> Dict[str, Any]:
    day = datetime.strptime(date_yyyymmdd, "%Y%m%d").date().isoformat()
    try:
        payload = get_ohlcv.invoke({"ticker": ticker, "start_date": day, "end_date": day, "interval": "5m"})
        return payload if isinstance(payload, dict) else {"ticker": ticker, "start_date": day, "end_date": day, "interval": "5m", "rows": []}
    except Exception as exc:
        logger.warning("intraday 5m OHLCV 조회 실패: %s (%s)", ticker, exc)
        return {"ticker": ticker, "start_date": day, "end_date": day, "interval": "5m", "rows": [], "error": str(exc)}


def _summarize_intraday_5m(rows: List[Dict[str, Any]], *, date_iso: str) -> str:
    if not rows:
        return f"INTRADAY_5M({date_iso}): 데이터 없음(또는 yfinance 제한/휴장)"

    def _as_float(v: Any) -> float | None:
        try:
            return float(v)
        except Exception:
            return None

    first = rows[0]
    last = rows[-1]
    open_first = _as_float(first.get("open"))
    close_last = _as_float(last.get("close"))

    highs_f = [h for h in (_as_float(r.get("high")) for r in rows) if h is not None]
    lows_f = [l for l in (_as_float(r.get("low")) for r in rows) if l is not None]
    high_max = max(highs_f) if highs_f else None
    low_min = min(lows_f) if lows_f else None

    change_pct = None
    if open_first not in (None, 0) and close_last is not None:
        change_pct = (close_last / open_first - 1.0) * 100.0

    ts_start = str(first.get("ts") or "")
    ts_end = str(last.get("ts") or "")

    parts = [f"INTRADAY_5M({date_iso}): bars={len(rows)}"]
    if ts_start and ts_end:
        parts.append(f"range={ts_start}~{ts_end}")
    if open_first is not None and close_last is not None:
        parts.append(f"open={open_first:.2f}, close={close_last:.2f}")
    if change_pct is not None:
        parts.append(f"chg={change_pct:+.2f}%")
    if high_max is not None and low_min is not None:
        parts.append(f"high={high_max:.2f}, low={low_min:.2f}")
    return ", ".join(parts)


def _normalize_sources_for_script_turn(raw_sources: Any, *, turn_index: int) -> List[Dict[str, Any]] | None:
    if not isinstance(raw_sources, list):
        logger.warning("TickerScriptTurn[%d] drop: sources가 리스트가 아닙니다.", turn_index)
        return None

    out: List[Dict[str, Any]] = []
    for src_index, src in enumerate(raw_sources):
        if not isinstance(src, dict):
            logger.warning("TickerScriptTurn[%d].sources[%d] drop: 객체가 아닙니다.", turn_index, src_index)
            continue

        src_type = src.get("type")
        if not _is_nonempty_str(src_type):
            if _is_nonempty_str(src.get("pk")) and _is_nonempty_str(src.get("title")):
                out.append({"type": "article", "pk": str(src["pk"]).strip(), "title": str(src["title"]).strip()})
            else:
                logger.warning("TickerScriptTurn[%d].sources[%d] drop: type 누락", turn_index, src_index)
            continue

        st = str(src_type).strip()
        if st == "article":
            if not _is_nonempty_str(src.get("pk")) or not _is_nonempty_str(src.get("title")):
                logger.warning("TickerScriptTurn[%d].sources[%d] drop: article 필드 누락", turn_index, src_index)
                continue
            out.append({"type": "article", "pk": str(src["pk"]).strip(), "title": str(src["title"]).strip()})
            continue

        if st == "chart":
            if (
                not _is_nonempty_str(src.get("ticker"))
                or not _is_valid_date_yyyy_mm_dd(src.get("start_date"))
                or not _is_valid_date_yyyy_mm_dd(src.get("end_date"))
            ):
                logger.warning("TickerScriptTurn[%d].sources[%d] drop: chart 필드 누락/형식 오류", turn_index, src_index)
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
            if (
                not _is_nonempty_str(src.get("id"))
                or not _is_nonempty_str(src.get("title"))
                or not _is_valid_date_yyyy_mm_dd(src.get("date"))
            ):
                logger.warning("TickerScriptTurn[%d].sources[%d] drop: event 필드 누락/형식 오류", turn_index, src_index)
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

        if st == "sec_filing":
            if (
                not _is_nonempty_str(src.get("ticker"))
                or not _is_nonempty_str(src.get("form"))
                or not _is_valid_date_yyyy_mm_dd(src.get("filed_date"))
                or not _is_nonempty_str(src.get("accession_number"))
            ):
                logger.warning(
                    "TickerScriptTurn[%d].sources[%d] drop: sec_filing 필드 누락/형식 오류",
                    turn_index,
                    src_index,
                )
                continue
            out.append(
                {
                    "type": "sec_filing",
                    "ticker": str(src["ticker"]).strip().upper(),
                    "form": str(src["form"]).strip().upper(),
                    "filed_date": str(src["filed_date"]).strip(),
                    "accession_number": str(src["accession_number"]).strip(),
                }
            )
            continue

        logger.warning("TickerScriptTurn[%d].sources[%d] drop: 알 수 없는 type=%r", turn_index, src_index, st)

    return out


def _normalize_ticker_script_turns(raw_scripts: Any) -> List[TickerScriptTurn]:
    if not isinstance(raw_scripts, list):
        logger.warning("ticker scripts drop: 배열이 아닙니다.")
        return []

    out: List[TickerScriptTurn] = []
    for idx, turn in enumerate(raw_scripts):
        if not isinstance(turn, dict):
            logger.warning("TickerScriptTurn[%d] drop: 객체가 아닙니다.", idx)
            continue

        speaker = turn.get("speaker")
        if speaker not in {"진행자", "해설자"}:
            logger.warning("TickerScriptTurn[%d] drop: speaker 누락/형식 오류", idx)
            continue

        text = turn.get("text")
        if not _is_nonempty_str(text):
            logger.warning("TickerScriptTurn[%d] drop: text 누락/형식 오류", idx)
            continue

        if "sources" not in turn:
            logger.warning("TickerScriptTurn[%d] drop: sources 누락", idx)
            continue

        sources = _normalize_sources_for_script_turn(turn.get("sources"), turn_index=idx)
        if sources is None:
            continue

        out.append({"id": len(out), "speaker": speaker, "text": _one_line(str(text)), "sources": sources})  # type: ignore[typeddict-item]

    return out


def worker_prepare_messages_node(state: TickerScriptWorkerState) -> TickerScriptWorkerState:
    prompt_cfg = _load_worker_prompt()

    date = state.get("date", "")
    ticker = str(state.get("ticker") or "").upper().strip()
    ticker_index = int(state.get("ticker_index") or 1)
    tickers_total = int(state.get("tickers_total") or 1)
    all_tickers = state.get("all_tickers") or []

    base_scripts = state.get("base_scripts") or []
    debate_json = state.get("debate_json") or {}
    intraday_summary = str(state.get("intraday_ohlcv_5m_summary") or "").strip()
    intraday_json = str(state.get("intraday_ohlcv_5m_json") or "").strip()
    intraday_source_json = str(state.get("intraday_ohlcv_source_json") or "").strip()

    system = prompt_cfg["system"].replace("{ticker}", ticker)
    user_prompt = (
        prompt_cfg["user_template"]
        .replace("{date}", str(date))
        .replace("{ticker}", ticker)
        .replace("{ticker_index}", str(ticker_index))
        .replace("{tickers_total}", str(tickers_total))
        .replace("{all_tickers_json}", json.dumps(all_tickers, ensure_ascii=False, separators=(",", ":")))
        .replace("{base_scripts_json}", json.dumps(base_scripts, ensure_ascii=False, indent=2))
        .replace("{debate_json}", json.dumps(debate_json, ensure_ascii=False, indent=2))
        .replace("{intraday_ohlcv_5m_summary}", intraday_summary)
        .replace("{intraday_ohlcv_5m_json}", intraday_json)
        .replace("{intraday_ohlcv_source_json}", intraday_source_json)
    )

    messages = [SystemMessage(content=system), HumanMessage(content=user_prompt)]
    return {**state, "messages": messages}


def worker_agent_node(state: TickerScriptWorkerState) -> TickerScriptWorkerState:
    llm = build_llm("TICKER_SCRIPT_WORKER", logger=logger)
    messages = state.get("messages", [])
    resp = llm.invoke(list(messages))
    return {**state, "messages": [resp]}


def worker_extract_scripts_node(state: TickerScriptWorkerState) -> TickerScriptWorkerState:
    messages = state.get("messages", [])
    raw = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            raw = msg.content or ""
            break

    parsed = parse_json_from_response(raw)
    scripts = _normalize_ticker_script_turns(parsed.get("scripts", []))
    scripts = _filter_sources_to_allowed(scripts=scripts, allowed_sources=state.get("allowed_sources", []) or [])

    # defensive: force one-line texts
    scripts_clean: List[TickerScriptTurn] = []
    for t in scripts:
        if not isinstance(t, dict):
            continue
        scripts_clean.append(
            {
                "id": int(t.get("id", len(scripts_clean))),
                "speaker": t.get("speaker"),  # type: ignore[typeddict-item]
                "text": _one_line(str(t.get("text") or "")),
                "sources": t.get("sources", []) if isinstance(t.get("sources", []), list) else [],
            }
        )
    scripts_clean = _normalize_ticker_script_turns(scripts_clean)

    return {**state, "scripts": scripts_clean}


def build_worker_graph():
    graph = StateGraph(TickerScriptWorkerState)
    graph.add_node("prepare_messages", worker_prepare_messages_node)
    graph.add_node("agent", worker_agent_node)
    graph.add_node("extract", worker_extract_scripts_node)

    graph.add_edge(START, "prepare_messages")
    graph.add_edge("prepare_messages", "agent")
    graph.add_edge("agent", "extract")
    graph.add_edge("extract", END)
    return graph.compile()


def _build_ticker_sections(
    *,
    base_len: int,
    tickers: List[str],
    ticker_scripts: List[TickerScriptWorkerOutput],
) -> List[TickerSection]:
    sections: List[TickerSection] = []
    cursor = base_len
    for ticker, item in zip(tickers, ticker_scripts):
        scripts = item.get("scripts", []) if isinstance(item, dict) else []
        if not isinstance(scripts, list) or not scripts:
            sections.append({"ticker": ticker, "start_id": -1, "end_id": -1})
            continue
        start = cursor
        end = cursor + len(scripts) - 1
        sections.append({"ticker": ticker, "start_id": start, "end_id": end})
        cursor = end + 1
    return sections


def _apply_refiner_edits(
    *, scripts: List[TickerScriptTurn], edits: Any
) -> tuple[List[TickerScriptTurn], List[Dict[str, Any]]]:
    if not isinstance(edits, list):
        return scripts, []

    id_to_index: dict[int, int] = {}
    for idx, turn in enumerate(scripts):
        if not isinstance(turn, dict):
            continue
        try:
            tid = int(turn.get("id"))
        except Exception:
            continue
        id_to_index[tid] = idx

    applied: List[Dict[str, Any]] = []
    for e in edits:
        if not isinstance(e, dict):
            continue
        try:
            tid = int(e.get("id"))
        except Exception:
            continue
        idx = id_to_index.get(tid)
        if idx is None:
            continue
        speaker = e.get("speaker")
        if speaker not in {"진행자", "해설자"}:
            continue
        text = _one_line(str(e.get("text") or ""))
        if not text:
            continue
        scripts[idx] = {**scripts[idx], "speaker": speaker, "text": text}
        applied.append({"id": tid, "speaker": speaker, "text": text})

    return scripts, applied


def run_ticker_script_pipeline(
    *,
    date: str,
    user_tickers: List[str],
    base_scripts: List[TickerScriptTurn],
    debate_outputs: List[Dict[str, Any]],
) -> TickerScriptPipelineOutput:
    load_dotenv(ROOT_DIR / ".env", override=False)

    date_norm = normalize_date(date)
    set_briefing_date(date_norm)
    tickers = [str(t).upper().strip() for t in user_tickers if str(t).strip()]
    if not tickers:
        raise ValueError("user_tickers가 비어 있습니다.")
    if len(debate_outputs) != len(tickers):
        raise ValueError("debate_outputs 길이는 user_tickers와 같아야 합니다.")

    base_scripts_norm = _normalize_ticker_script_turns(base_scripts or [])
    worker_graph = build_worker_graph()

    inputs: List[TickerScriptWorkerState] = []
    for idx, (ticker, debate_json) in enumerate(zip(tickers, debate_outputs), start=1):
        allowed_sources = _collect_allowed_sources(base_scripts=base_scripts_norm, debate_json=debate_json or {})

        # Intraday 5m OHLCV (tool-collected, injected as context)
        intraday_ohlcv = _fetch_intraday_ohlcv_5m(ticker=ticker, date_yyyymmdd=date_norm)
        rows = intraday_ohlcv.get("rows", []) if isinstance(intraday_ohlcv, dict) else []
        rows_list = rows if isinstance(rows, list) else []
        day_iso = datetime.strptime(date_norm, "%Y%m%d").date().isoformat()
        intraday_summary = _summarize_intraday_5m(rows_list, date_iso=day_iso)

        intraday_chart_source: DebateSource = {
            "type": "chart",
            "ticker": ticker,
            "start_date": day_iso,
            "end_date": day_iso,
        }
        # Ensure the intraday chart source is selectable by the worker.
        intraday_key = _canonical_source(intraday_chart_source)
        if intraday_key and not any(_canonical_source(s) == intraday_key for s in allowed_sources if isinstance(s, dict)):
            allowed_sources.append(intraday_chart_source)

        inputs.append(
            {
                "date": date_norm,
                "ticker": ticker,
                "ticker_index": idx,
                "tickers_total": len(tickers),
                "all_tickers": tickers,
                "base_scripts": base_scripts_norm,
                "debate_json": debate_json or {},
                "allowed_sources": allowed_sources,
                "intraday_ohlcv_5m_summary": intraday_summary,
                "intraday_ohlcv_5m_json": json.dumps(intraday_ohlcv, ensure_ascii=False, indent=2),
                "intraday_ohlcv_source_json": json.dumps(intraday_chart_source, ensure_ascii=False, indent=2),
            }
        )

    results = worker_graph.batch(inputs, return_exceptions=True)  # type: ignore[attr-defined]
    ticker_scripts: List[TickerScriptWorkerOutput] = []
    for ticker, res in zip(tickers, results):
        if isinstance(res, Exception):
            logger.warning("TickerScript worker 실패: %s (%s)", ticker, res)
            ticker_scripts.append({"ticker": ticker, "scripts": []})
            continue
        scripts = res.get("scripts", []) if isinstance(res, dict) else []
        ticker_scripts.append({"ticker": ticker, "scripts": scripts if isinstance(scripts, list) else []})

    merged_raw: List[Dict[str, Any]] = []
    merged_raw.extend(base_scripts_norm)
    for item in ticker_scripts:
        merged_raw.extend(item.get("scripts", []) or [])

    merged_scripts = _normalize_ticker_script_turns(merged_raw)
    ticker_sections = _build_ticker_sections(base_len=len(base_scripts_norm), tickers=tickers, ticker_scripts=ticker_scripts)

    # refiner (tool-less)
    prompt_cfg = _load_refiner_prompt()
    llm = build_llm("TICKER_SCRIPT_REFINER", logger=logger)

    scripts_minimal = [{"id": t.get("id"), "speaker": t.get("speaker"), "text": t.get("text")} for t in merged_scripts]
    user_prompt = (
        prompt_cfg["user_template"]
        .replace("{scripts_minimal}", json.dumps(scripts_minimal, ensure_ascii=False, separators=(",", ":")))
        .replace("{tickers_json}", json.dumps(tickers, ensure_ascii=False, separators=(",", ":")))
        .replace("{ticker_sections_json}", json.dumps(ticker_sections, ensure_ascii=False, separators=(",", ":")))
    )
    messages = [SystemMessage(content=prompt_cfg["system"]), HumanMessage(content=user_prompt)]
    resp = llm.invoke(messages)
    parsed = parse_json_from_response(str(resp.content or ""))
    merged_scripts, applied_edits = _apply_refiner_edits(scripts=merged_scripts, edits=parsed.get("edits"))

    return {
        "date": date_norm,
        "user_tickers": tickers,
        "ticker_scripts": ticker_scripts,
        "ticker_sections": ticker_sections,
        "refiner_edits": applied_edits,
        "scripts": merged_scripts,
    }


def _read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_base_scripts(path: Path) -> List[TickerScriptTurn]:
    data = _read_json_file(path)
    if isinstance(data, list):
        return data  # type: ignore[return-value]
    if isinstance(data, dict):
        scripts = data.get("scripts")
        if isinstance(scripts, list):
            return scripts  # type: ignore[return-value]
    raise ValueError(f"base scripts JSON 형식을 인식할 수 없습니다: {path}")


def main(argv: List[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="UserTicker script worker/refiner (debate-based, tool-less)")
    parser.add_argument("date", type=str, help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("tickers", nargs="+", help="Tickers (space-separated, order preserved)")
    parser.add_argument(
        "--base-scripts",
        type=str,
        default=str(ROOT_DIR / "temp" / "theme.json"),
        help="Base scripts JSON (list or {scripts:[...]}). Default: temp/theme.json",
    )
    parser.add_argument(
        "--debate-json",
        nargs="*",
        default=None,
        help="Debate output JSON file paths (same order as tickers). If omitted, run debate internally.",
    )
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--no-prefetch", action="store_true", help="If running debate, do not run prefetch_all.")
    parser.add_argument("--output", type=str, default=None, help="Write full pipeline output JSON to this path.")
    args = parser.parse_args(argv)

    load_dotenv(ROOT_DIR / ".env", override=False)

    date_norm = normalize_date(args.date)
    set_briefing_date(date_norm)

    base_scripts = _load_base_scripts(Path(args.base_scripts))

    tickers = [str(t).upper().strip() for t in args.tickers if str(t).strip()]
    if not tickers:
        raise ValueError("tickers가 비어 있습니다.")

    debate_outputs: List[Dict[str, Any]] = []
    if args.debate_json:
        if len(args.debate_json) != len(tickers):
            raise ValueError("--debate-json 개수는 tickers 개수와 같아야 합니다.")
        for p in args.debate_json:
            payload = _read_json_file(Path(p))
            if not isinstance(payload, dict):
                raise ValueError(f"debate json이 객체가 아닙니다: {p}")
            debate_outputs.append(payload)
    else:
        from .graph import run_debate  # lazy import

        for t in tickers:
            debate_outputs.append(
                run_debate(
                    date=date_norm,
                    ticker=t,
                    max_rounds=int(args.max_rounds),
                    prefetch=not args.no_prefetch,
                    cleanup=False,
                )
            )

    out = run_ticker_script_pipeline(
        date=date_norm,
        user_tickers=tickers,
        base_scripts=base_scripts,
        debate_outputs=debate_outputs,
    )

    rendered = json.dumps(out, ensure_ascii=False, indent=2)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        logger.info("Wrote ticker script pipeline output to %s", out_path)
    else:
        print(rendered)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
