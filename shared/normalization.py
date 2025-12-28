"""Shared normalization helpers for LLM outputs."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_DATE_YYYY_MM_DD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_json_from_response(content: str) -> Dict[str, Any]:
    """Extract JSON from LLM response content."""
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", content)
    json_str = json_match.group(1) if json_match else content.strip()
    try:
        parsed = json.loads(json_str)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError as exc:
        logger.error("JSON 파싱 실패: %s", exc)
        return {}


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
    """Normalize sources list to the expected schema."""
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


def normalize_script_turns(raw_scripts: Any) -> List[Dict[str, Any]]:
    """Normalize scripts list to ScriptTurn schema."""
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
