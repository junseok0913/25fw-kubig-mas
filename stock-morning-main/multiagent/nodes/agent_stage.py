"""
멀티에이전트 초기 라운드: 뉴스/SEC 담당 에이전트 실행
"""

from __future__ import annotations

from typing import Any, Dict, List

from multiagent.agents.news_agent import NewsAgent
from multiagent.agents.sec_agent import SECAgent


def run_initial_agents(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    준비된 데이터셋을 입력받아 News/SEC 에이전트의 1차 의견을 반환합니다.
    (향후 Debate 로직, 합의 로직 등은 여기서 확장 예정)
    """
    news_agent = NewsAgent()
    sec_agent = SECAgent()

    news_opinion = news_agent.analyze(dataset)
    sec_opinion = sec_agent.analyze(dataset)

    return [news_opinion, sec_opinion]
