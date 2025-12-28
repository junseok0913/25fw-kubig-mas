"""TTS LangGraph 파이프라인.

목표:
- 입력 `Podcast/{date}/script.json`의 `scripts[]`를 기반으로, **turn(1개) 단위**로 TTS를 생성한다.
- turn 오디오 길이(샘플/프레임 기반)를 이용해 `start_time_ms`/`end_time_ms` 타임라인을 계산한다.
- 최종 `{date}.wav`를 만들고, `tts/*.wav`(턴별)와 `timeline.json`을 함께 저장한다.

역할:
- CLI 엔트리포인트: `python -m TTS.src.tts {YYYYMMDD}`
- LangGraph 그래프 조립/실행(노드 구현은 `nodes.py`에 위치)
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import traceable

from .nodes import (
    build_turn_requests_node,
    compute_timeline_node,
    generate_turn_audio_parallel_node,
    load_config_node,
    load_script_node,
    map_turns_with_chapter_node,
    merge_audio_node,
    validate_paths_node,
    write_outputs_node,
)
from .state import TTSState
from .utils.script import parse_date_arg
from .utils.gemini_tts import get_model_path
from .utils.tracing import configure_tracing

logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[2]


def build_graph():
    load_dotenv(ROOT_DIR / ".env", override=False)
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
