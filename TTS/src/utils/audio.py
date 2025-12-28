"""오디오(WAV/PCM) 처리 유틸.

TTS 파이프라인에서 사용되는 WAV 포맷 검증, frame 수 계산, PCM 추출/저장을 담당한다.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

SAMPLE_RATE_HZ = 24000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # s16le
BYTES_PER_FRAME = CHANNELS * SAMPLE_WIDTH_BYTES


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
