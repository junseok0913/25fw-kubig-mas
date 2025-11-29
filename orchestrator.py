"""
상위 LangGraph 오케스트레이터 예제.

OpeningAgent를 하나의 노드로 취급하고, 출력 State를 BriefingState 스키마에 맞춰
더미 ThemeAgent 노드로 전달하는 최소 그래프를 구성한다.
"""

from __future__ import annotations

import sys
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


class Theme(TypedDict):
    headline: str
    description: str
    related_news: List[Dict[str, Any]]


class ScriptTurn(TypedDict):
    speaker: str  # "진행자" | "해설자"
    text: str
    sources: List[Dict[str, Any]]


class BriefingState(TypedDict, total=False):
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
    oa_graph = opening_agent.build_graph()
    oa_result = oa_graph.invoke({})
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
    """더미 ThemeAgent: 테마마다 간단한 멘트를 scripts에 append."""
    themes = state.get("themes", [])
    scripts = list(state.get("scripts", []))

    for idx, theme in enumerate(themes, 1):
        headline = theme.get("headline", f"테마 {idx}")
        scripts.append(
            {
                "speaker": "해설자",
                "text": f"[테마#{idx}] {headline} 요약 스텁입니다. (ThemeAgent placeholder)",
                "sources": theme.get("related_news", []),
            }
        )

    return {
        **state,
        "scripts": scripts,
        "current_section": "stock",
    }


def build_orchestrator():
    """Opening→Theme(dummy) 두 노드로 구성된 상위 그래프를 컴파일한다."""
    load_dotenv(ROOT / ".env", override=False)
    configure_tracing()
    graph = StateGraph(BriefingState)
    graph.add_node("opening", opening_node)
    graph.add_node("theme", theme_node)

    graph.add_edge("opening", "theme")
    graph.add_edge("theme", END)
    graph.set_entry_point("opening")

    return graph.compile()


def main() -> None:
    app = build_orchestrator()
    # user_tickers는 예시 입력; 필요 시 CLI 인자로 치환 가능
    result = app.invoke({"user_tickers": []})
    print("=== Orchestrator Result ===")
    print("nutshell:", result.get("nutshell"))
    print("themes:", result.get("themes"))
    print("scripts len:", len(result.get("scripts", [])))
    print("current_section:", result.get("current_section"))


if __name__ == "__main__":
    main()
