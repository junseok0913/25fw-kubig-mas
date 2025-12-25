"""TTS LangGraph 파이프라인.

목표:
- 입력 `Podcast/{date}/script.json`의 `scripts[]`를 기반으로, **turn(1개) 단위**로 TTS를 생성한다.
- turn 오디오 길이(샘플/프레임 기반)를 이용해 `start_time_ms`/`end_time_ms` 타임라인을 계산한다.
- 최종 `{date}.wav`를 만들고, `tts/*.wav`(턴별)와 `timeline.json`을 함께 저장한다.
"""

from __future__ import annotations

import concurrent.futures
import argparse
import io
import json
import logging
import os
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

import yaml
from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import traceable
from langsmith.utils import ContextThreadPoolExecutor

from .utils.gemini_tts import gemini_generate_tts_traced, get_model_path
from .utils.tracing import configure_tracing

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT_DIR / "TTS" / "config" / "gemini_tts.yaml"

SAMPLE_RATE_HZ = 24000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # s16le
BYTES_PER_FRAME = CHANNELS * SAMPLE_WIDTH_BYTES

KNOWN_CHAPTERS: set[str] = {"opening", "theme", "closing"}


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


def parse_date_arg(date_str: str) -> str:
    if "-" in date_str:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%Y%m%d")
    dt = datetime.strptime(date_str, "%Y%m%d")
    return dt.strftime("%Y%m%d")


def _is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(SAMPLE_RATE_HZ)
        wf.writeframes(pcm)


def _read_wav_frames(path: Path) -> int:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        fr = wf.getframerate()
        frames = wf.getnframes()
    if channels != CHANNELS or sampwidth != SAMPLE_WIDTH_BYTES or fr != SAMPLE_RATE_HZ:
        raise ValueError(
            "예상치 못한 WAV 포맷입니다: "
            f"path={path}, channels={channels}, sampwidth={sampwidth}, fr={fr} "
            f"(expected channels={CHANNELS}, sampwidth={SAMPLE_WIDTH_BYTES}, fr={SAMPLE_RATE_HZ})"
        )
    return int(frames)


def _extract_pcm(audio_bytes: bytes) -> bytes:
    if _is_wav(audio_bytes):
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            fr = wf.getframerate()
            if channels != CHANNELS or sampwidth != SAMPLE_WIDTH_BYTES or fr != SAMPLE_RATE_HZ:
                raise ValueError(
                    "예상치 못한 WAV 포맷입니다: "
                    f"channels={channels}, sampwidth={sampwidth}, fr={fr} "
                    f"(expected channels={CHANNELS}, sampwidth={SAMPLE_WIDTH_BYTES}, fr={SAMPLE_RATE_HZ})"
                )
            return wf.readframes(wf.getnframes())
    return audio_bytes


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


def _load_gemini_tts_config(path: Path) -> GeminiTTSConfig:
    if not path.exists():
        raise FileNotFoundError(f"TTS config가 없습니다: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"TTS config YAML이 객체가 아닙니다: {path}")

    raw_instructions = raw.get("instructions")
    if not isinstance(raw_instructions, dict):
        raise ValueError("TTS config instructions는 객체여야 합니다. (speaker1/speaker2)")
    speaker1_inst = raw_instructions.get("speaker1")
    speaker2_inst = raw_instructions.get("speaker2")
    if not isinstance(speaker1_inst, str) or not speaker1_inst.strip():
        raise ValueError("TTS config instructions.speaker1가 비어있습니다.")
    if not isinstance(speaker2_inst, str) or not speaker2_inst.strip():
        raise ValueError("TTS config instructions.speaker2가 비어있습니다.")

    temperature_raw = raw.get("temperature", 1.0)
    try:
        temperature = float(temperature_raw)
    except Exception as e:
        raise ValueError(f"TTS config temperature 파싱 실패: {temperature_raw!r} ({e})")

    raw_voices = raw.get("voices")
    if not isinstance(raw_voices, dict):
        raise ValueError("TTS config voices는 객체여야 합니다. (speaker1/speaker2)")
    speaker1_voice = raw_voices.get("speaker1")
    speaker2_voice = raw_voices.get("speaker2")
    if not isinstance(speaker1_voice, str) or not speaker1_voice.strip():
        raise ValueError("TTS config voices.speaker1가 비어있습니다.")
    if not isinstance(speaker2_voice, str) or not speaker2_voice.strip():
        raise ValueError("TTS config voices.speaker2가 비어있습니다.")

    timeout_raw = raw.get("timeout_seconds", 240)
    try:
        timeout_seconds = float(timeout_raw)
    except Exception as e:
        raise ValueError(f"TTS config timeout_seconds 파싱 실패: {timeout_raw!r} ({e})")
    if timeout_seconds <= 0:
        raise ValueError("TTS config timeout_seconds는 0보다 커야 합니다.")

    parallel_raw = raw.get("max_parallel_requests", 4)
    try:
        max_parallel = int(parallel_raw)
    except Exception as e:
        raise ValueError(f"TTS config max_parallel_requests 파싱 실패: {parallel_raw!r} ({e})")
    if max_parallel <= 0:
        raise ValueError("TTS config max_parallel_requests는 1 이상이어야 합니다.")

    batch_timeout_raw = raw.get("batch_timeout_seconds", 60)
    try:
        batch_timeout_seconds = float(batch_timeout_raw)
    except Exception as e:
        raise ValueError(f"TTS config batch_timeout_seconds 파싱 실패: {batch_timeout_raw!r} ({e})")
    if batch_timeout_seconds < 0:
        raise ValueError("TTS config batch_timeout_seconds는 0 이상이어야 합니다.")

    common_gap_raw = raw.get("common_gap_seconds", 0.25)
    chapter_gap_raw = raw.get("chapter_gap_seconds", 0.25)
    try:
        common_gap_seconds = float(common_gap_raw)
        chapter_gap_seconds = float(chapter_gap_raw)
    except Exception as e:
        raise ValueError(f"TTS config gap_seconds 파싱 실패: {e}")
    if common_gap_seconds < 0 or chapter_gap_seconds < 0:
        raise ValueError("TTS config gap_seconds는 0 이상이어야 합니다.")

    return {
        "instructions": {"speaker1": speaker1_inst.strip(), "speaker2": speaker2_inst.strip()},
        "temperature": temperature,
        "voices": {"speaker1": speaker1_voice.strip(), "speaker2": speaker2_voice.strip()},
        "timeout_seconds": timeout_seconds,
        "max_parallel_requests": max_parallel,
        "batch_timeout_seconds": batch_timeout_seconds,
        "common_gap_seconds": common_gap_seconds,
        "chapter_gap_seconds": chapter_gap_seconds,
    }


def _load_env() -> None:
    load_dotenv(ROOT_DIR / ".env", override=False)


def load_config_node(state: TTSState) -> TTSState:
    cfg = _load_gemini_tts_config(DEFAULT_CONFIG_PATH)
    instructions = cfg.get("instructions") or {}
    voices = cfg.get("voices") or {}
    return {
        **state,
        "temperature": float(cfg.get("temperature") or 1.0),
        "speaker1_voice": str(voices.get("speaker1")).strip(),
        "speaker2_voice": str(voices.get("speaker2")).strip(),
        "instructions_by_label": {
            "speaker1": str(instructions.get("speaker1")).strip(),
            "speaker2": str(instructions.get("speaker2")).strip(),
        },
        "timeout_seconds": float(cfg.get("timeout_seconds") or 240),
        "max_parallel_requests": int(cfg.get("max_parallel_requests") or 4),
        "batch_timeout_seconds": float(cfg.get("batch_timeout_seconds") or 60),
        "common_gap_seconds": float(cfg.get("common_gap_seconds") or 0.25),
        "chapter_gap_seconds": float(cfg.get("chapter_gap_seconds") or 0.25),
    }


def validate_paths_node(state: TTSState) -> TTSState:
    script_path = state.get("script_path")
    out_dir = state.get("out_dir")
    if script_path is None or out_dir is None:
        raise ValueError("script_path/out_dir가 state에 없습니다.")
    if not script_path.exists():
        raise FileNotFoundError(f"입력 파일이 없습니다: {script_path}")
    return state


def load_script_node(state: TTSState) -> TTSState:
    script_path = state.get("script_path")
    if script_path is None:
        raise ValueError("script_path가 state에 없습니다.")
    data = json.loads(script_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("script.json 최상위는 객체여야 합니다.")
    scripts = data.get("scripts")
    if not isinstance(scripts, list):
        raise ValueError("script.json에 'scripts' 배열이 없습니다.")
    return {**state, "raw_script": data}


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


def map_turns_with_chapter_node(state: TTSState) -> TTSState:
    raw_script = state.get("raw_script")
    if not isinstance(raw_script, dict):
        raise ValueError("raw_script가 비어 있습니다.")

    scripts = raw_script.get("scripts")
    if not isinstance(scripts, list):
        raise ValueError("raw_script.scripts가 리스트가 아닙니다.")

    chapter_specs = _extract_chapter_specs(raw_script)

    turns: List[Turn] = []
    for idx, raw_turn in enumerate(scripts):
        if not isinstance(raw_turn, dict):
            raise ValueError(f"scripts[{idx}]가 객체가 아닙니다.")
        tid = _parse_int(raw_turn.get("id"))
        if tid is None:
            raise ValueError(f"scripts[{idx}].id가 없습니다.")
        speaker = raw_turn.get("speaker")
        if speaker not in {"진행자", "해설자"}:
            raise ValueError(f"scripts[{idx}].speaker가 유효하지 않습니다: {speaker!r}")
        text = raw_turn.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"scripts[{idx}].text가 비어 있습니다.")

        label = _speaker_to_label(str(speaker))

        chapter_name = "all"
        for spec in chapter_specs:
            if spec["start_id"] <= int(tid) <= spec["end_id"]:
                chapter_name = spec["name"]
                break

        turns.append(
            {
                "id": int(tid),
                "speaker": speaker,
                "label": label,
                "chapter": chapter_name,
                "text": text.strip(),
            }
        )

    turns.sort(key=lambda t: int(t["id"]))
    return {**state, "turns": turns}


def build_turn_requests_node(state: TTSState) -> TTSState:
    turns = state.get("turns") or []
    if not turns:
        raise ValueError("turns가 비어 있습니다.")

    instructions_by_label = state.get("instructions_by_label") or {}
    speaker1_inst = instructions_by_label.get("speaker1")
    speaker2_inst = instructions_by_label.get("speaker2")
    if not speaker1_inst or not speaker2_inst:
        raise ValueError("instructions_by_label이 비어 있습니다. load_config_node를 확인하세요.")

    requests: List[TurnRequest] = []
    for t in turns:
        label = t["label"]
        inst = speaker1_inst if label == "speaker1" else speaker2_inst
        text = t["text"].replace("\n", " ").strip()
        prompt = f"{inst}\n\n{label}: {text}\n"
        requests.append(
            {
                "id": t["id"],
                "speaker": t["speaker"],
                "label": t["label"],
                "chapter": t["chapter"],
                "prompt": prompt,
            }
        )

    return {**state, "requests": requests}


def generate_turn_audio_parallel_node(state: TTSState) -> TTSState:
    requests = state.get("requests") or []
    if not requests:
        raise ValueError("requests가 비어 있습니다.")

    out_dir = state.get("out_dir")
    if out_dir is None:
        raise ValueError("out_dir가 state에 없습니다.")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY가 설정되지 않았습니다. (.env 또는 환경변수)")

    temperature = float(state.get("temperature") or 1.0)
    speaker1_voice = str(state.get("speaker1_voice") or "").strip()
    speaker2_voice = str(state.get("speaker2_voice") or "").strip()
    timeout_seconds = float(state.get("timeout_seconds") or 240.0)
    batch_cooldown_seconds = float(state.get("batch_timeout_seconds") or 60.0)
    max_parallel = int(state.get("max_parallel_requests") or 4)
    if not speaker1_voice or not speaker2_voice:
        raise ValueError("speaker1_voice/speaker2_voice가 비어 있습니다. load_config_node를 확인하세요.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds는 0보다 커야 합니다.")
    if batch_cooldown_seconds < 0:
        raise ValueError("batch_timeout_seconds는 0 이상이어야 합니다.")

    request_timeout_seconds = timeout_seconds
    batch_wait_timeout_seconds = max(1.0, request_timeout_seconds)

    logger.info(
        "Gemini TTS 요청 시작: turns=%d, batch_size=%d, request_timeout_s=%.1f, batch_wait_timeout_s=%.1f, batch_cooldown_s=%.1f",
        len(requests),
        max_parallel,
        request_timeout_seconds,
        batch_wait_timeout_seconds,
        batch_cooldown_seconds,
    )

    # turn 오디오를 응답 즉시 저장하기 위해, generate 단계에서 출력 디렉터리를 먼저 생성한다.
    # - 이전 실행에서 생성된 파일이 존재하면(중간 실패 등) 그대로 재사용하며, 존재하는 turn wav는 skip한다.
    if out_dir.exists() and not out_dir.is_dir():
        raise NotADirectoryError(f"out_dir가 디렉터리가 아닙니다: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 레거시 호환: 예전에는 `tts/turns/*.wav`로 저장했으므로, 존재하면 `tts/*.wav`로 이동한다.
    legacy_turns_dir = out_dir / "turns"
    if legacy_turns_dir.exists():
        if not legacy_turns_dir.is_dir():
            raise NotADirectoryError(f"legacy_turns_dir가 디렉터리가 아닙니다: {legacy_turns_dir}")
        moved = 0
        for legacy_wav in sorted(legacy_turns_dir.glob("*.wav")):
            target = out_dir / legacy_wav.name
            if target.exists():
                continue
            legacy_wav.replace(target)
            moved += 1
        if moved:
            logger.info("레거시 turns 폴더 마이그레이션: moved=%d, from=%s, to=%s", moved, legacy_turns_dir, out_dir)
        try:
            legacy_turns_dir.rmdir()
        except OSError:
            pass

    max_id = max(int(r["id"]) for r in requests)
    width = max(2, len(str(max_id)))

    remaining_missing_ids: set[int] = set()
    for r in requests:
        tid = int(r["id"])
        wav_path = out_dir / f"{str(tid).zfill(width)}.wav"
        if not wav_path.exists():
            remaining_missing_ids.add(tid)

    def _generate_one(r: TurnRequest) -> TurnAudio:
        tid = int(r["id"])
        chapter = str(r.get("chapter") or "all")
        speaker = str(r.get("speaker") or "")
        wav_path = out_dir / f"{str(tid).zfill(width)}.wav"

        if wav_path.exists():
            t0 = time.monotonic()
            logger.info("Gemini TTS 스킵(기존 파일): id=%s, chapter=%s, speaker=%s", tid, chapter, speaker)
            frames = _read_wav_frames(wav_path)
            elapsed_ms = int(round((time.monotonic() - t0) * 1000))
            logger.info(
                "Gemini TTS 로드 완료: id=%s, chapter=%s, frames=%d, elapsed_ms=%d, wav=%s",
                tid,
                chapter,
                frames,
                elapsed_ms,
                wav_path,
            )
            return {
                "id": tid,
                "speaker": r["speaker"],
                "label": r["label"],
                "chapter": r["chapter"],
                "wav": wav_path.name,
                "frames": frames,
            }

        t0 = time.monotonic()
        logger.info("Gemini TTS 요청(실행): id=%s, chapter=%s, speaker=%s", tid, chapter, speaker)
        try:
            audio_bytes = gemini_generate_tts_traced(
                chapter=chapter,
                start_id=tid,
                end_id=tid,
                turns=1,
                prompt=str(r["prompt"]),
                api_key=api_key,
                temperature=temperature,
                speaker1_voice=speaker1_voice,
                speaker2_voice=speaker2_voice,
                timeout_s=request_timeout_seconds,
            )
        except Exception:
            elapsed_ms = int(round((time.monotonic() - t0) * 1000))
            logger.error("Gemini TTS 실패: id=%s, chapter=%s", tid, r.get("chapter"))
            logger.error("Gemini TTS 실패(소요): id=%s, elapsed_ms=%d", tid, elapsed_ms)
            raise

        pcm = _extract_pcm(audio_bytes)
        if len(pcm) % BYTES_PER_FRAME != 0:
            raise ValueError(f"PCM 바이트 길이가 frame 단위로 나누어지지 않습니다: id={tid}, bytes={len(pcm)}")
        frames = len(pcm) // BYTES_PER_FRAME

        _write_wav(wav_path, pcm)

        elapsed_ms = int(round((time.monotonic() - t0) * 1000))
        logger.info(
            "Gemini TTS 완료: id=%s, chapter=%s, frames=%d, elapsed_ms=%d, saved=%s",
            tid,
            chapter,
            frames,
            elapsed_ms,
            wav_path,
        )
        return {
            "id": tid,
            "speaker": r["speaker"],
            "label": r["label"],
            "chapter": r["chapter"],
            "wav": wav_path.name,
            "frames": frames,
        }

    batch_size = max(1, int(max_parallel))
    total = len(requests)
    batches: List[List[TurnRequest]] = [requests[i : i + batch_size] for i in range(0, total, batch_size)]

    turn_audios: List[TurnAudio] = []
    for batch_idx, batch in enumerate(batches, start=1):
        ids = [int(r["id"]) for r in batch]
        batch_missing_ids = [tid for tid in ids if tid in remaining_missing_ids]
        logger.info(
            "Gemini TTS 배치 시작: batch=%d/%d, size=%d, ids=%s, missing=%s, wait_timeout_s=%.1f",
            batch_idx,
            len(batches),
            len(batch),
            ids,
            batch_missing_ids,
            batch_wait_timeout_seconds,
        )
        t_batch0 = time.monotonic()

        max_workers = max(1, len(batch))
        with ContextThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_req = {executor.submit(_generate_one, r): r for r in batch}
            done, not_done = concurrent.futures.wait(
                future_to_req.keys(),
                timeout=batch_wait_timeout_seconds,
                return_when=concurrent.futures.ALL_COMPLETED,
            )

            if not_done:
                pending_ids = sorted(int(future_to_req[f]["id"]) for f in not_done)
                logger.error(
                    "Gemini TTS 배치 완료 대기 타임아웃: batch=%d/%d, pending_ids=%s, timeout_s=%.1f",
                    batch_idx,
                    len(batches),
                    pending_ids,
                    batch_wait_timeout_seconds,
                )
                raise TimeoutError(
                    f"Gemini TTS batch wait timeout: batch={batch_idx}/{len(batches)} pending={pending_ids}"
                )

            for fut in done:
                turn_audios.append(fut.result())

        elapsed_ms = int(round((time.monotonic() - t_batch0) * 1000))
        logger.info(
            "Gemini TTS 배치 완료: batch=%d/%d, size=%d, elapsed_ms=%d",
            batch_idx,
            len(batches),
            len(batch),
            elapsed_ms,
        )

        if batch_missing_ids:
            remaining_missing_ids.difference_update(batch_missing_ids)

        if remaining_missing_ids and batch_missing_ids and batch_cooldown_seconds > 0:
            logger.info("Gemini TTS 배치 쿨다운 시작: sleep_s=%.1f", batch_cooldown_seconds)
            time.sleep(batch_cooldown_seconds)
            logger.info("Gemini TTS 배치 쿨다운 완료")

    turn_audios.sort(key=lambda a: int(a["id"]))
    return {**state, "turn_audios": turn_audios}


def compute_timeline_node(state: TTSState) -> TTSState:
    turn_audios = state.get("turn_audios") or []
    if not turn_audios:
        raise ValueError("turn_audios가 비어 있습니다.")

    out_dir = state.get("out_dir")
    if out_dir is None:
        raise ValueError("out_dir가 state에 없습니다.")

    common_gap_seconds = float(state.get("common_gap_seconds") or 0.25)
    chapter_gap_seconds = float(state.get("chapter_gap_seconds") or 0.25)
    common_gap_frames = int(round(common_gap_seconds * SAMPLE_RATE_HZ))
    chapter_gap_frames = int(round(chapter_gap_seconds * SAMPLE_RATE_HZ))

    max_id = max(int(a["id"]) for a in turn_audios)
    width = max(2, len(str(max_id)))

    gaps_after_frames: List[int] = []
    for idx, cur in enumerate(turn_audios):
        if idx == len(turn_audios) - 1:
            gaps_after_frames.append(0)
            continue
        nxt = turn_audios[idx + 1]
        cur_ch = str(cur.get("chapter") or "all")
        nxt_ch = str(nxt.get("chapter") or "all")
        if cur_ch != nxt_ch and cur_ch in KNOWN_CHAPTERS and nxt_ch in KNOWN_CHAPTERS:
            gaps_after_frames.append(chapter_gap_frames)
        else:
            gaps_after_frames.append(common_gap_frames)

    timeline: List[TimelineItem] = []
    cursor_frames = 0
    for idx, a in enumerate(turn_audios):
        tid = int(a["id"])
        frames = int(a["frames"])
        start_frames = cursor_frames
        end_frames = start_frames + frames

        start_ms = int(round(start_frames * 1000 / SAMPLE_RATE_HZ))
        end_ms = int(round(end_frames * 1000 / SAMPLE_RATE_HZ))
        duration_ms = max(0, end_ms - start_ms)

        wav_rel = str(a.get("wav") or f"{str(tid).zfill(width)}.wav")
        timeline.append(
            {
                "id": tid,
                "chapter": str(a.get("chapter") or "all"),
                "speaker": a["speaker"],
                "wav": wav_rel,
                "start_time_ms": start_ms,
                "end_time_ms": end_ms,
                "duration_ms": duration_ms,
            }
        )

        cursor_frames = end_frames + int(gaps_after_frames[idx])

    return {**state, "timeline": timeline, "gaps_after_frames": gaps_after_frames}


def merge_audio_node(state: TTSState) -> TTSState:
    turn_audios = state.get("turn_audios") or []
    gaps_after_frames = state.get("gaps_after_frames") or []
    if not turn_audios:
        raise ValueError("turn_audios가 비어 있습니다.")
    if len(gaps_after_frames) != len(turn_audios):
        raise ValueError("gaps_after_frames 길이가 turn_audios와 일치하지 않습니다.")
    out_dir = state.get("out_dir")
    if out_dir is None:
        raise ValueError("out_dir가 state에 없습니다.")
    date = str(state.get("date") or "").strip()
    if not date:
        raise ValueError("date가 state에 없습니다.")

    out_dir.mkdir(parents=True, exist_ok=True)
    base_dir = out_dir.parent
    base_dir.mkdir(parents=True, exist_ok=True)
    out_wav = base_dir / f"{date}.wav"

    silence_cache: Dict[int, bytes] = {0: b""}
    with wave.open(str(out_wav), "wb") as wf_out:
        wf_out.setnchannels(CHANNELS)
        wf_out.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf_out.setframerate(SAMPLE_RATE_HZ)

        for idx, a in enumerate(turn_audios):
            wav_rel = str(a.get("wav") or "").strip()
            if not wav_rel:
                raise ValueError(f"turn_audios[{idx}].wav가 비어 있습니다.")
            in_path = out_dir / wav_rel
            if not in_path.exists():
                raise FileNotFoundError(f"턴 WAV 파일이 없습니다: {in_path}")

            with wave.open(str(in_path), "rb") as wf_in:
                channels = wf_in.getnchannels()
                sampwidth = wf_in.getsampwidth()
                fr = wf_in.getframerate()
                if channels != CHANNELS or sampwidth != SAMPLE_WIDTH_BYTES or fr != SAMPLE_RATE_HZ:
                    raise ValueError(
                        "예상치 못한 WAV 포맷입니다: "
                        f"path={in_path}, channels={channels}, sampwidth={sampwidth}, fr={fr} "
                        f"(expected channels={CHANNELS}, sampwidth={SAMPLE_WIDTH_BYTES}, fr={SAMPLE_RATE_HZ})"
                    )

                while True:
                    chunk = wf_in.readframes(8192)
                    if not chunk:
                        break
                    wf_out.writeframes(chunk)

            gap_frames = int(gaps_after_frames[idx])
            if gap_frames:
                if gap_frames not in silence_cache:
                    silence_cache[gap_frames] = b"\x00" * (gap_frames * BYTES_PER_FRAME)
                wf_out.writeframes(silence_cache[gap_frames])

    return {**state, "out_wav": str(out_wav)}


def write_outputs_node(state: TTSState) -> TTSState:
    out_dir = state.get("out_dir")
    turn_audios = state.get("turn_audios") or []
    timeline = state.get("timeline") or []
    script_path = state.get("script_path")
    out_wav_raw = state.get("out_wav")
    if out_dir is None:
        raise ValueError("out_dir가 state에 없습니다.")
    if not turn_audios:
        raise ValueError("turn_audios가 비어 있습니다.")
    if not timeline:
        raise ValueError("timeline이 비어 있습니다.")
    if script_path is None:
        raise ValueError("script_path가 state에 없습니다.")
    if not isinstance(out_wav_raw, str) or not out_wav_raw.strip():
        raise ValueError("out_wav가 비어 있습니다. merge_audio_node를 확인하세요.")

    # generate 단계에서 out_dir에 턴 오디오(wav)를 응답 즉시 저장하고,
    # 여기서는 최종 산출물(timeline + 날짜 JSON)을 저장한다.
    out_dir.mkdir(parents=True, exist_ok=True)

    # turn wav 존재 확인
    for idx, a in enumerate(turn_audios):
        wav_rel = str(a.get("wav") or "").strip()
        if not wav_rel:
            raise ValueError(f"turn_audios[{idx}].wav가 비어 있습니다.")
        wav_path = out_dir / wav_rel
        if not wav_path.exists():
            raise FileNotFoundError(f"턴 WAV 파일이 없습니다: {wav_path}")

    # timeline.json 저장
    date = state.get("date") or ""
    final_wav_name = f"../{date}.wav"
    payload = {
        "date": date,
        "audio": {
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "channels": CHANNELS,
            "sample_width_bytes": SAMPLE_WIDTH_BYTES,
        },
        "gaps": {
            "common_gap_ms": int(round(float(state.get("common_gap_seconds") or 0.0) * 1000)),
            "chapter_gap_ms": int(round(float(state.get("chapter_gap_seconds") or 0.0) * 1000)),
        },
        "turns": timeline,
        "final_wav": final_wav_name,
    }
    (out_dir / "timeline.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    out_wav = Path(out_wav_raw)
    if not out_wav.exists():
        raise FileNotFoundError(f"최종 WAV 파일이 없습니다: {out_wav}")

    # 날짜 파일 저장: 기존 script.json을 복사하고 scripts[] 각 항목에 time=[start_ms,end_ms]를 주입한다.
    time_by_id: Dict[int, List[int]] = {}
    for item in timeline:
        tid = int(item["id"])
        time_by_id[tid] = [int(item["start_time_ms"]), int(item["end_time_ms"])]

    script_obj = json.loads(script_path.read_text(encoding="utf-8"))
    if not isinstance(script_obj, dict):
        raise ValueError("script.json 최상위는 객체여야 합니다.")
    scripts = script_obj.get("scripts")
    if not isinstance(scripts, list):
        raise ValueError("script.json에 'scripts' 배열이 없습니다.")

    for idx, turn in enumerate(scripts):
        if not isinstance(turn, dict):
            raise ValueError(f"scripts[{idx}]가 객체가 아닙니다.")
        tid = _parse_int(turn.get("id"))
        if tid is None:
            raise ValueError(f"scripts[{idx}].id가 없습니다.")
        tid_int = int(tid)
        if tid_int not in time_by_id:
            raise ValueError(f"scripts[{idx}].id={tid_int}에 대한 timeline 항목이 없습니다.")
        turn["time"] = time_by_id[tid_int]

    root_out = script_path.parent / f"{date}.json"
    if root_out.exists():
        logger.warning("날짜 JSON 파일이 이미 존재합니다. 덮어씁니다: %s", root_out)
    root_out.write_text(json.dumps(script_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"date": date, "out_wav": str(out_wav)}


def build_graph():
    _load_env()
    graph = StateGraph(TTSState)
    graph.add_node("load_config", load_config_node)
    graph.add_node("validate_paths", validate_paths_node)
    graph.add_node("load_script", load_script_node)
    graph.add_node("map_turns_with_chapter", map_turns_with_chapter_node)
    graph.add_node("build_turn_requests", build_turn_requests_node)
    graph.add_node("generate_turn_audio_parallel", generate_turn_audio_parallel_node)
    graph.add_node("compute_timeline", compute_timeline_node)
    graph.add_node("merge_audio", merge_audio_node)
    graph.add_node("write_outputs", write_outputs_node)

    graph.add_edge(START, "load_config")
    graph.add_edge("load_config", "validate_paths")
    graph.add_edge("validate_paths", "load_script")
    graph.add_edge("load_script", "map_turns_with_chapter")
    graph.add_edge("map_turns_with_chapter", "build_turn_requests")
    graph.add_edge("build_turn_requests", "generate_turn_audio_parallel")
    graph.add_edge("generate_turn_audio_parallel", "compute_timeline")
    graph.add_edge("compute_timeline", "merge_audio")
    graph.add_edge("merge_audio", "write_outputs")
    graph.add_edge("write_outputs", END)

    graph.set_entry_point("load_config")
    return graph.compile()


def _trace_inputs_pipeline(inputs: dict) -> dict:
    def _p(v: Any) -> str | None:
        if v is None:
            return None
        return str(v)

    return {
        "date": inputs.get("date"),
        "script_path": _p(inputs.get("script_path")),
        "out_dir": _p(inputs.get("out_dir")),
        "model_path": get_model_path(),
        "api_key_present": bool(os.getenv("GEMINI_API_KEY")),
    }


@traceable(
    run_type="chain",
    name="gemini_tts.pipeline",
    tags=["tts", "gemini"],
    process_inputs=_trace_inputs_pipeline,
)
def run_tts(*, date: str, script_path: Path, out_dir: Path) -> Dict[str, Any]:
    app = build_graph()
    return app.invoke({"date": date, "script_path": script_path, "out_dir": out_dir})


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Gemini TTS (turn-level, LangGraph)")
    parser.add_argument("date", type=str, help="브리핑 날짜 (YYYYMMDD 또는 YYYY-MM-DD)")
    args = parser.parse_args(argv)

    try:
        date_yyyymmdd = parse_date_arg(args.date)
    except ValueError as e:
        logger.error("날짜 파싱 실패: %s", e)
        return 2

    load_dotenv(ROOT_DIR / ".env", override=False)
    configure_tracing(logger=logger)

    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY가 설정되지 않았습니다. (.env 또는 환경변수)")
        return 2

    script_path = ROOT_DIR / "Podcast" / date_yyyymmdd / "script.json"
    out_dir = ROOT_DIR / "Podcast" / date_yyyymmdd / "tts"

    try:
        result = run_tts(date=date_yyyymmdd, script_path=script_path, out_dir=out_dir)
    except Exception as e:
        if isinstance(e, FileExistsError):
            logger.error("%s", e)
        else:
            logger.error("TTS 생성 실패: %s", e)
        return 1

    logger.info("Saved: %s", result.get("out_wav"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
