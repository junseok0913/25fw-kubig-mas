from __future__ import annotations

import argparse
import base64
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


logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MODEL_PATH = "models/gemini-2.5-pro-preview-tts"  # fixed as requested

SAMPLE_RATE_HZ = 24000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # s16le

INSTRUCTIONS = (
    "Podcast-style Korean U.S. stock market close briefing: brisk and professional; "
    "Host sounds warm and conversational, Analyst sounds confident and data-driven;"
)


class ScriptTurn(TypedDict):
    id: int
    speaker: str
    text: str
    sources: List[Dict[str, Any]]


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


def build_conversation_prompt(scripts: List[ScriptTurn]) -> str:
    lines: List[str] = [
        INSTRUCTIONS,
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


def gemini_generate_tts(prompt: str, *, api_key: str, timeout_s: float = 120.0) -> bytes:
    url = f"{GEMINI_BASE_URL}/{MODEL_PATH}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "temperature": 1,
            "speechConfig": {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {
                            "speaker": "speaker1",
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": "Zephyr"}
                            },
                        },
                        {
                            "speaker": "speaker2",
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": "Charon"}
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

    script_path = ROOT / "Podcast" / date_yyyymmdd / "script.json"
    out_dir = ROOT / "Podcast" / date_yyyymmdd / "tts"
    out_wav = out_dir / "final.wav"

    if out_dir.exists():
        logger.error("출력 디렉터리가 이미 존재합니다(덮어쓰기 금지): %s", out_dir)
        return 1

    try:
        data = load_script_json(script_path)
        scripts: List[ScriptTurn] = data["scripts"]
        prompt = build_conversation_prompt(scripts)
    except Exception as e:
        logger.error("입력 준비 실패: %s", e)
        return 1

    try:
        audio_bytes = gemini_generate_tts(prompt, api_key=api_key, timeout_s=120.0)
    except Exception:
        logger.error("TTS 생성 실패. 길이 제한/네트워크/권한 등을 확인하세요.")
        return 1

    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    if _is_wav(audio_bytes):
        out_wav.write_bytes(audio_bytes)
    else:
        _write_wav(out_wav, audio_bytes)

    logger.info("Saved: %s", out_wav)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
