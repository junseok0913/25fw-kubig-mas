"""Gemini multi-speaker TTS 호출 유틸."""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from hashlib import sha256
from typing import Any, Dict

from langsmith.run_helpers import traceable

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL_PATH = "models/gemini-2.5-pro-preview-tts"


def get_model_path() -> str:
    """TTS 모델 경로를 반환한다.

    - `.env` 또는 환경변수 `GEMINI_TTS_MODEL`로 오버라이드 가능
    - `models/` prefix가 없으면 자동으로 붙인다.
    """
    raw = (os.getenv("GEMINI_TTS_MODEL") or DEFAULT_MODEL_PATH).strip()
    if not raw:
        return DEFAULT_MODEL_PATH
    if not raw.startswith("models/"):
        raw = f"models/{raw}"
    return raw


def _is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


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
    model_path = get_model_path()
    url = f"{GEMINI_BASE_URL}/{model_path}:generateContent"
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


def _trace_inputs_tts_call(inputs: dict) -> dict:
    prompt = inputs.get("prompt")
    prompt_chars = len(prompt) if isinstance(prompt, str) else None
    prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()[:16] if isinstance(prompt, str) else None
    return {
        "prompt": prompt if isinstance(prompt, str) else None,
        "chapter": inputs.get("chapter"),
        "start_id": inputs.get("start_id"),
        "end_id": inputs.get("end_id"),
        "turns": inputs.get("turns"),
        "temperature": inputs.get("temperature"),
        "speaker1_voice": inputs.get("speaker1_voice"),
        "speaker2_voice": inputs.get("speaker2_voice"),
        "timeout_s": inputs.get("timeout_s"),
        "prompt_chars": prompt_chars,
        "prompt_sha256_16": prompt_hash,
        "api_key": "(redacted)" if inputs.get("api_key") else "(missing)",
    }


def _trace_outputs_tts_call(output: Any) -> dict:
    if isinstance(output, (bytes, bytearray)):
        b = bytes(output)
        return {"audio_bytes": len(b), "is_wav": _is_wav(b)}
    return {"output_type": type(output).__name__}


@traceable(
    run_type="llm",
    name="gemini_tts.generate",
    tags=["tts", "gemini"],
    process_inputs=_trace_inputs_tts_call,
    process_outputs=_trace_outputs_tts_call,
)
def gemini_generate_tts_traced(
    *,
    chapter: str,
    start_id: int,
    end_id: int,
    turns: int,
    prompt: str,
    api_key: str,
    temperature: float,
    speaker1_voice: str,
    speaker2_voice: str,
    timeout_s: float = 120.0,
) -> bytes:
    return gemini_generate_tts(
        prompt,
        api_key=api_key,
        temperature=temperature,
        speaker1_voice=speaker1_voice,
        speaker2_voice=speaker2_voice,
        timeout_s=timeout_s,
    )
