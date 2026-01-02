"""TTS LangGraph state 및 데이터 스키마(TypedDict) 정의.

노드들이 주고받는 상태(`TTSState`)와 script/turn/timeline 등 주요 타입을 한 곳에 모아
파이프라인 전체의 I/O 형태를 명확하게 유지한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, TypedDict


class ScriptTurn(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    text: str
    sources: List[Dict[str, Any]]


class ChapterSpec(TypedDict):
    name: str
    start_id: int
    end_id: int


class GeminiTTSConfig(TypedDict, total=False):
    instructions: Dict[str, str]  # speaker1/speaker2
    temperature: float
    voices: Dict[str, str]  # speaker1/speaker2
    timeout_seconds: float
    max_parallel_requests: int
    batch_timeout_seconds: float  # cooldown seconds between batches (legacy key name)
    common_gap_seconds: float
    chapter_gap_seconds: float


class Turn(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    label: Literal["speaker1", "speaker2"]
    chapter: str
    text: str


class TurnRequest(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    label: Literal["speaker1", "speaker2"]
    chapter: str
    prompt: str


class TurnAudio(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    label: Literal["speaker1", "speaker2"]
    chapter: str
    wav: str  # out_dir 기준 상대경로 (예: 00.wav)
    frames: int


class TimelineItem(TypedDict):
    id: int
    chapter: str
    speaker: Literal["진행자", "해설자"]
    wav: str
    start_time_ms: int
    end_time_ms: int
    duration_ms: int


class TTSState(TypedDict, total=False):
    date: str
    script_path: Path
    out_dir: Path

    # config
    temperature: float
    speaker1_voice: str
    speaker2_voice: str
    instructions_by_label: Dict[str, str]  # speaker1/speaker2
    timeout_seconds: float
    max_parallel_requests: int
    batch_timeout_seconds: float
    common_gap_seconds: float
    chapter_gap_seconds: float

    # data
    raw_script: Dict[str, Any]
    turns: List[Turn]
    requests: List[TurnRequest]
    turn_audios: List[TurnAudio]
    gaps_after_frames: List[int]
    timeline: List[TimelineItem]
    out_wav: str
