"""script.json(대본) 파싱/정규화 유틸.

- 날짜 인자(YYYYMMDD/YYYY-MM-DD) 정규화
- speaker → label(speaker1/speaker2) 매핑
- orchestrator가 제공하는 chapter 범위 파싱
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from ..state import ChapterSpec


def parse_date_arg(date_str: str) -> str:
    if "-" in date_str:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%Y%m%d")
    dt = datetime.strptime(date_str, "%Y%m%d")
    return dt.strftime("%Y%m%d")


def _speaker_to_label(speaker: str) -> Literal["speaker1", "speaker2"]:
    if speaker == "진행자":
        return "speaker1"
    if speaker == "해설자":
        return "speaker2"
    raise ValueError(f"지원하지 않는 speaker입니다: {speaker!r} (허용: '진행자', '해설자')")


def _parse_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _extract_chapter_specs(raw_script: Dict[str, Any]) -> List[ChapterSpec]:
    chapter_raw = raw_script.get("chapter")
    if not isinstance(chapter_raw, list):
        return []

    out: List[ChapterSpec] = []
    for item in chapter_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        start_id = _parse_int(item.get("start_id"))
        end_id = _parse_int(item.get("end_id"))
        if start_id is None or end_id is None:
            continue
        start_id = int(start_id)
        end_id = int(end_id)
        if start_id < 0 or end_id < 0 or end_id < start_id:
            continue
        out.append({"name": name.strip(), "start_id": start_id, "end_id": end_id})
    return out
