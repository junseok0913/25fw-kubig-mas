from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import sys
import urllib.error
import urllib.request
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

from dotenv import load_dotenv
import yaml


logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MODEL_PATH = "models/gemini-2.5-pro-preview-tts"  # fixed as requested

SAMPLE_RATE_HZ = 24000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # s16le

DEFAULT_INSTRUCTIONS = (
    "Podcast-style Korean U.S. stock market close briefing: brisk and professional; "
    "Host sounds warm and conversational, Analyst sounds confident and data-driven;"
)

DEFAULT_CONFIG_PATH = ROOT / "TTS" / "config" / "gemini_tts.yaml"


class ScriptTurn(TypedDict):
    id: int
    speaker: str
    text: str
    sources: List[Dict[str, Any]]

class ChapterRange(TypedDict):
    name: str
    start_id: int
    end_id: int


class GeminiTTSConfig(TypedDict, total=False):
    instructions: str
    temperature: float
    voices: Dict[str, str]
    gap_seconds: float


def parse_date_arg(date_str: str) -> str:
    """Normalize date to YYYYMMDD."""
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


def _speaker_to_label(speaker: str) -> Literal["speaker1", "speaker2"]:
    if speaker == "진행자":
        return "speaker1"
    if speaker == "해설자":
        return "speaker2"
    raise ValueError(f"지원하지 않는 speaker입니다: {speaker!r} (허용: '진행자', '해설자')")


def build_conversation_prompt(scripts: List[ScriptTurn], *, instructions: str) -> str:
    lines: List[str] = [
        instructions,
        "",
        "TTS the following conversation between speaker1 and speaker2:",
        "",
    ]
    for turn in scripts:
        label = _speaker_to_label(turn.get("speaker", ""))
        text = str(turn.get("text", "")).replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"{label}: {text}")
    return "\n".join(lines).strip() + "\n"


def _getenv_nonempty(name: str) -> str | None:
    v = os.getenv(name)
    if v is None:
        return None
    v = v.strip()
    return v if v else None


def _load_gemini_tts_config(path: Path) -> GeminiTTSConfig:
    """Gemini TTS 설정을 YAML로 로드한다.

    파일이 없으면 기본값을 반환한다(에러 아님).
    """
    if not path.exists():
        logger.info("TTS config가 없습니다. 기본값 사용: %s", path)
        return {
            "instructions": DEFAULT_INSTRUCTIONS,
            "temperature": 1.0,
            "voices": {"speaker1": "Zephyr", "speaker2": "Charon"},
            "gap_seconds": 0.0,
        }

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"TTS config YAML이 객체가 아닙니다: {path}")

    instructions = raw.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        instructions = DEFAULT_INSTRUCTIONS

    temperature_raw = raw.get("temperature", 1.0)
    try:
        temperature = float(temperature_raw)
    except Exception as e:
        raise ValueError(f"TTS config temperature 파싱 실패: {temperature_raw!r} ({e})")

    voices = raw.get("voices") or {}
    if not isinstance(voices, dict):
        raise ValueError("TTS config voices는 객체여야 합니다.")
    speaker1_voice = voices.get("speaker1", "Zephyr")
    speaker2_voice = voices.get("speaker2", "Charon")
    if not isinstance(speaker1_voice, str) or not speaker1_voice.strip():
        raise ValueError("TTS config voices.speaker1가 비어있습니다.")
    if not isinstance(speaker2_voice, str) or not speaker2_voice.strip():
        raise ValueError("TTS config voices.speaker2가 비어있습니다.")

    gap_raw = raw.get("gap_seconds", 0.0)
    try:
        gap_seconds = float(gap_raw)
    except Exception as e:
        raise ValueError(f"TTS config gap_seconds 파싱 실패: {gap_raw!r} ({e})")
    if gap_seconds < 0:
        raise ValueError("TTS config gap_seconds는 0 이상이어야 합니다.")

    env_gap = _getenv_nonempty("TTS_GAP_SECONDS")
    if env_gap is not None:
        try:
            gap_seconds = float(env_gap)
        except Exception as e:
            raise ValueError(f"TTS_GAP_SECONDS 파싱 실패: {env_gap!r} ({e})")
        if gap_seconds < 0:
            raise ValueError("TTS_GAP_SECONDS는 0 이상이어야 합니다.")

    return {
        "instructions": instructions.strip(),
        "temperature": temperature,
        "voices": {"speaker1": speaker1_voice.strip(), "speaker2": speaker2_voice.strip()},
        "gap_seconds": gap_seconds,
    }

def _extract_pcm(audio_bytes: bytes) -> bytes:
    """Gemini 응답을 PCM(s16le)로 정규화한다.

    - WAV면 헤더를 제거하고 frames만 반환
    - 아니면 이미 PCM이라고 가정
    """
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


def _split_scripts_by_chapter(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """script.json을 chapter 기준으로 분할한다.

    반환: [{"name": str, "start_id": int, "end_id": int, "scripts": List[ScriptTurn]}]
    """
    scripts = data.get("scripts")
    if not isinstance(scripts, list):
        raise ValueError("script.json에 'scripts' 배열이 없습니다.")

    chapter = data.get("chapter")
    if not isinstance(chapter, list):
        # 하위 호환: chapter가 없으면 전체를 1개 chunk로 처리
        return [{"name": "all", "start_id": 0, "end_id": len(scripts) - 1, "scripts": scripts}]

    ranges: Dict[str, ChapterRange] = {}
    for item in chapter:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        start_id = _parse_int(item.get("start_id"))
        end_id = _parse_int(item.get("end_id"))
        if start_id is None or end_id is None:
            continue
        ranges[name.strip()] = {"name": name.strip(), "start_id": start_id, "end_id": end_id}

    chunks: List[Dict[str, Any]] = []
    for name in ("opening", "theme", "closing"):
        r = ranges.get(name)
        if not r:
            continue
        start_id = int(r["start_id"])
        end_id = int(r["end_id"])
        if start_id < 0 or end_id < 0 or end_id < start_id:
            logger.info("Skip chapter %s: empty range (%d-%d)", name, start_id, end_id)
            continue
        selected: List[ScriptTurn] = []
        for turn in scripts:
            if not isinstance(turn, dict):
                continue
            tid = _parse_int(turn.get("id"))
            if tid is None:
                continue
            if start_id <= tid <= end_id:
                selected.append(turn)  # type: ignore[arg-type]
        selected.sort(key=lambda t: int(t.get("id", 0)))  # type: ignore[arg-type]
        if not selected:
            logger.warning("Chapter %s 범위에 해당하는 turns가 없습니다: %d-%d", name, start_id, end_id)
            continue
        chunks.append({"name": name, "start_id": start_id, "end_id": end_id, "scripts": selected})

    if chunks:
        return chunks

    # chapter는 있었지만 유효한 chunk를 만들지 못한 경우: 전체로 폴백
    logger.warning("chapter 기반 분할 실패: 전체 scripts로 폴백합니다.")
    return [{"name": "all", "start_id": 0, "end_id": len(scripts) - 1, "scripts": scripts}]


def load_script_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"입력 파일이 없습니다: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 파싱 실패: {path} ({e})")

    scripts = data.get("scripts")
    if not isinstance(scripts, list):
        raise ValueError("script.json에 'scripts' 배열이 없습니다.")
    return data


def extract_inline_audio_b64(resp: Dict[str, Any]) -> str:
    candidates = resp.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini 응답에 candidates가 없습니다.")
    content = candidates[0].get("content") or {}
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("Gemini 응답에 content.parts가 없습니다.")

    part0 = parts[0]
    inline_data = part0.get("inlineData") or part0.get("inline_data")
    if not isinstance(inline_data, dict):
        raise ValueError("Gemini 응답에 inlineData/inline_data가 없습니다.")
    data_b64 = inline_data.get("data")
    if not isinstance(data_b64, str) or not data_b64:
        raise ValueError("Gemini 응답 inlineData.data가 비어있습니다.")
    return data_b64


def gemini_generate_tts(
    prompt: str,
    *,
    api_key: str,
    temperature: float,
    speaker1_voice: str,
    speaker2_voice: str,
    timeout_s: float = 120.0,
) -> bytes:
    url = f"{GEMINI_BASE_URL}/{MODEL_PATH}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "temperature": temperature,
            "speechConfig": {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {
                            "speaker": "speaker1",
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": speaker1_voice}
                            },
                        },
                        {
                            "speaker": "speaker2",
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": speaker2_voice}
                            },
                        },
                    ]
                }
            },
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as f:
            raw = f.read()
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        logger.error("Gemini TTS HTTPError: %s %s", e.code, e.reason)
        if err_body:
            logger.error("Gemini error body: %s", err_body[:2000])
        raise
    except urllib.error.URLError as e:
        logger.error("Gemini TTS URLError: %s", e)
        raise

    resp = json.loads(raw.decode("utf-8"))
    audio_b64 = extract_inline_audio_b64(resp)
    audio_bytes = base64.b64decode(audio_b64)
    return audio_bytes


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Gemini multi-speaker TTS -> single final.wav")
    parser.add_argument("date", type=str, help="브리핑 날짜 (YYYYMMDD 또는 YYYY-MM-DD)")
    args = parser.parse_args(argv)

    try:
        date_yyyymmdd = parse_date_arg(args.date)
    except ValueError as e:
        logger.error("날짜 파싱 실패: %s", e)
        return 2

    load_dotenv(ROOT / ".env", override=False)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY가 설정되지 않았습니다. (.env 또는 환경변수)")
        return 2

    try:
        tts_cfg = _load_gemini_tts_config(DEFAULT_CONFIG_PATH)
    except Exception as e:
        logger.error("TTS config 로드 실패: %s", e)
        return 2

    instructions = str(tts_cfg.get("instructions") or DEFAULT_INSTRUCTIONS).strip()
    temperature = float(tts_cfg.get("temperature") or 1.0)
    voices = tts_cfg.get("voices") or {"speaker1": "Zephyr", "speaker2": "Charon"}
    speaker1_voice = str(voices.get("speaker1") or "Zephyr").strip()
    speaker2_voice = str(voices.get("speaker2") or "Charon").strip()
    gap_seconds = float(tts_cfg.get("gap_seconds") or 0.0)

    script_path = ROOT / "Podcast" / date_yyyymmdd / "script.json"
    out_dir = ROOT / "Podcast" / date_yyyymmdd / "tts"
    out_wav = out_dir / "final.wav"

    if out_dir.exists():
        logger.error("출력 디렉터리가 이미 존재합니다(덮어쓰기 금지): %s", out_dir)
        return 1

    try:
        data = load_script_json(script_path)
        chunks = _split_scripts_by_chapter(data)
    except Exception as e:
        logger.error("입력 준비 실패: %s", e)
        return 1

    prompts: List[Dict[str, Any]] = []
    for chunk in chunks:
        chunk_scripts: List[ScriptTurn] = chunk["scripts"]
        prompt = build_conversation_prompt(chunk_scripts, instructions=instructions)
        prompts.append({**chunk, "prompt": prompt})

    try:
        audio_segments: List[Dict[str, Any]] = []
        for p in prompts:
            name = p["name"]
            start_id = p["start_id"]
            end_id = p["end_id"]
            logger.info("Gemini TTS 요청: chapter=%s, id=%s-%s, turns=%d", name, start_id, end_id, len(p["scripts"]))
            try:
                audio_bytes = gemini_generate_tts(
                    p["prompt"],
                    api_key=api_key,
                    temperature=temperature,
                    speaker1_voice=speaker1_voice,
                    speaker2_voice=speaker2_voice,
                    timeout_s=120.0,
                )
            except Exception:
                logger.error("Gemini TTS 실패: chapter=%s, id=%s-%s", name, start_id, end_id)
                raise
            audio_segments.append({**p, "audio_bytes": audio_bytes})
    except Exception:
        logger.error("TTS 생성 실패. 길이 제한/네트워크/권한 등을 확인하세요.")
        return 1

    # WAV/PCM 포맷 검증 및 결합 PCM 준비 (디렉터리 생성 전 선검증)
    gap_frames = int(gap_seconds * SAMPLE_RATE_HZ)
    gap_pcm = b"\x00" * (gap_frames * CHANNELS * SAMPLE_WIDTH_BYTES) if gap_frames > 0 else b""

    combined_pcm_parts: List[bytes] = []
    try:
        for idx, seg in enumerate(audio_segments):
            combined_pcm_parts.append(_extract_pcm(seg["audio_bytes"]))
            if gap_pcm and idx < len(audio_segments) - 1:
                combined_pcm_parts.append(gap_pcm)
    except Exception as e:
        logger.error("오디오 포맷 처리 실패: %s", e)
        return 1

    out_dir.mkdir(parents=True, exist_ok=False)
    prompt_index: List[Dict[str, Any]] = []
    for seg in audio_segments:
        name = str(seg["name"])
        (out_dir / f"prompt_{name}.txt").write_text(str(seg["prompt"]), encoding="utf-8")

        # 디버깅용: 챕터별 wav도 저장
        chapter_wav = out_dir / f"{name}.wav"
        if _is_wav(seg["audio_bytes"]):
            chapter_wav.write_bytes(seg["audio_bytes"])
        else:
            _write_wav(chapter_wav, seg["audio_bytes"])

        prompt_index.append(
            {
                "name": name,
                "start_id": seg.get("start_id"),
                "end_id": seg.get("end_id"),
                "turns": len(seg.get("scripts", [])),
            }
        )

    (out_dir / "segments.json").write_text(json.dumps(prompt_index, ensure_ascii=False, indent=2), encoding="utf-8")

    combined_pcm = b"".join(combined_pcm_parts)
    _write_wav(out_wav, combined_pcm)

    logger.info("Saved: %s", out_wav)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
