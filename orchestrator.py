"""
상위 LangGraph 오케스트레이터.

OpeningAgent → ThemeAgent 순서로 에이전트를 실행하는 그래프를 구성한다.
--stage 옵션으로 어느 에이전트까지 실행할지 제어할 수 있다.

Usage:
    python orchestrator.py 20251125                # ThemeAgent까지 실행 (기본값)
    python orchestrator.py 2025-11-25 --stage 0    # OpeningAgent만 실행
    python orchestrator.py 20251125 --stage 1      # ThemeAgent까지 실행
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

# OpeningAgent 모듈 import 경로 설정
ROOT = Path(__file__).parent
OPENING_AGENT_ROOT = ROOT / "OpeningAgent"
if str(OPENING_AGENT_ROOT) not in sys.path:
    sys.path.append(str(OPENING_AGENT_ROOT))

from src import opening_agent  # noqa: E402
from src.utils.tracing import configure_tracing  # noqa: E402
from ThemeAgent.src import theme_agent  # noqa: E402


def parse_date_arg(date_str: str) -> str:
    """날짜 문자열을 YYYYMMDD 형식으로 정규화한다.
    
    Args:
        date_str: 입력 날짜 (YYYYMMDD 또는 YYYY-MM-DD 형식)
    
    Returns:
        YYYYMMDD 형식의 날짜 문자열
    
    Raises:
        ValueError: 잘못된 날짜 형식
    """
    # YYYY-MM-DD 형식 시도
    if "-" in date_str:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%Y%m%d")
        except ValueError:
            pass
    
    # YYYYMMDD 형식 시도
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.strftime("%Y%m%d")
    except ValueError:
        raise ValueError(f"잘못된 날짜 형식입니다: {date_str}. YYYYMMDD 또는 YYYY-MM-DD 형식을 사용하세요.")


def format_date_korean(date_yyyymmdd: str) -> str:
    """YYYYMMDD를 '11월 25일' 형식의 한국어로 변환한다."""
    dt = datetime.strptime(date_yyyymmdd, "%Y%m%d")
    return f"{dt.month}월 {dt.day}일"


class Theme(TypedDict):
    headline: str
    description: str
    related_news: List[Dict[str, Any]]


class ScriptTurn(TypedDict):
    speaker: str  # "진행자" | "해설자"
    text: str
    sources: List[Dict[str, Any]]


class BriefingState(TypedDict, total=False):
    # 날짜 (EST 기준, YYYYMMDD 형식)
    date: str
    
    # User input
    user_tickers: List[str]

    # Agent 1 (Opening) output
    nutshell: str
    themes: List[Theme]

    # Accumulated scripts
    scripts: List[ScriptTurn]

    # Metadata
    current_section: str


def opening_node(state: BriefingState) -> BriefingState:
    """OpeningAgent를 실행해 BriefingState 필드를 채운다."""
    import os
    
    # state에서 날짜를 가져와 OpeningAgent에 전달
    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다. orchestrator 실행 시 날짜를 지정하세요.")
    
    # BRIEFING_DATE 환경변수로 설정 (Tool에서 사용)
    os.environ["BRIEFING_DATE"] = date_str
    
    oa_graph = opening_agent.build_graph()
    # OpeningAgent에 date를 전달
    oa_result = oa_graph.invoke({"date": date_str})
    
    # orchestrator 경유 실행 시 OpeningAgent.main()의 cleanup_cache가 호출되지 않으므로 여기서 정리
    try:
        opening_agent.cleanup_cache()
    except Exception:
        pass

    themes = oa_result.get("themes", [])
    scripts = list(state.get("scripts", []))
    scripts.extend(oa_result.get("scripts", []))

    return {
        **state,
        "nutshell": oa_result.get("nutshell", ""),
        "themes": themes,
        "scripts": scripts,
        "current_section": "theme",
    }


def theme_node(state: BriefingState) -> BriefingState:
    """ThemeAgent를 실행해 테마별 심층 스크립트를 생성한다."""
    import os

    date_str = state.get("date")
    if not date_str:
        raise ValueError("date 필드가 state에 없습니다. orchestrator 실행 시 날짜를 지정하세요.")

    # BRIEFING_DATE 환경변수 설정 (Tool에서 사용)
    os.environ["BRIEFING_DATE"] = date_str

    ta_graph = theme_agent.build_theme_graph()
    result = ta_graph.invoke(
        {
            "date": date_str,
            "nutshell": state.get("nutshell", ""),
            "themes": state.get("themes", []),
            "base_scripts": state.get("scripts", []),
        }
    )

    try:
        theme_agent.cleanup_cache()
    except Exception:
        pass

    return {
        **state,
        "scripts": result.get("scripts", []),
        "current_section": "stock",
    }


def build_orchestrator(stage: int = 1):
    """상위 그래프를 컴파일한다.
    
    Args:
        stage: 실행할 에이전트 단계
            0 - OpeningAgent만 실행
            1 - ThemeAgent까지 실행 (기본값)
    """
    load_dotenv(ROOT / ".env", override=False)
    configure_tracing()
    graph = StateGraph(BriefingState)
    graph.add_node("opening", opening_node)
    graph.set_entry_point("opening")
    
    if stage >= 1:
        graph.add_node("theme", theme_node)
        graph.add_edge("opening", "theme")
        graph.add_edge("theme", END)
    else:
        # stage == 0: OpeningAgent만 실행
        graph.add_edge("opening", END)

    return graph.compile()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="장마감 브리핑 오케스트레이터",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
    python orchestrator.py 20251125
    python orchestrator.py 2025-11-25
    python orchestrator.py 20251125 --stage 0  # OpeningAgent만 실행
    python orchestrator.py 20251125 --stage 1  # ThemeAgent까지 실행
        
날짜는 EST(미국 동부 시간) 기준입니다.
        """
    )
    parser.add_argument(
        "date",
        type=str,
        help="브리핑 날짜 (YYYYMMDD 또는 YYYY-MM-DD 형식, EST 기준)"
    )
    parser.add_argument(
        "--stage",
        type=int,
        default=1,
        choices=[0, 1],
        help="실행할 에이전트 단계 (0: OpeningAgent만, 1: ThemeAgent까지, 기본값: 1)"
    )
    args = parser.parse_args()
    
    # 날짜 파싱 및 검증
    try:
        date_yyyymmdd = parse_date_arg(args.date)
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)
    
    stage_names = {0: "OpeningAgent", 1: "ThemeAgent"}
    date_korean = format_date_korean(date_yyyymmdd)
    print(f"=== {date_korean} 장마감 브리핑 시작 ===")
    print(f"날짜: {date_yyyymmdd} (EST)")
    print(f"실행 단계: {args.stage} ({stage_names.get(args.stage, 'Unknown')}까지)")
    
    app = build_orchestrator(stage=args.stage)
    result = app.invoke({
        "date": date_yyyymmdd,
        "user_tickers": [],
    })
    
    print("\n=== Orchestrator Result ===")
    print("nutshell:", result.get("nutshell"))
    print("themes:", result.get("themes"))
    print("scripts len:", len(result.get("scripts", [])))
    print("current_section:", result.get("current_section"))


if __name__ == "__main__":
    main()
