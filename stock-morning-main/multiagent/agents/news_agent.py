from __future__ import annotations

from typing import Any, Dict, List

from .base_agent import BaseAgent
from multiagent.services import AgentToolkit
from multiagent.prompts import NEWS_BLIND_PROMPT, NEWS_REBUTTAL_PROMPT


class NewsAgent(BaseAgent):
    """뉴스 기반 모멘텀 에이전트"""

    def __init__(self, toolkit: AgentToolkit, name: str = "News Analyst"):
        super().__init__(name=name, role="news")
        self.toolkit = toolkit

    def blind_assessment(self, dataset: Dict[str, Any]) -> str:
        context = self._build_news_context(dataset.get("aws_news", []))
        return self.toolkit.summarize(context, NEWS_BLIND_PROMPT)

    def rebut(self, dataset: Dict[str, Any], opponent_statement: str) -> str:
        context = self._build_news_context(dataset.get("aws_news", []))
        instruction = NEWS_REBUTTAL_PROMPT.format(opponent=opponent_statement)
        return self.toolkit.summarize(context, instruction)

    def _build_news_context(self, news_items: List[Dict[str, Any]]) -> str:
        if not news_items:
            return "관련 뉴스 데이터가 없습니다."
        lines = []
        for news in news_items[:10]:
            title = news.get("title") or news.get("pk") or "제목 없음"
            published = news.get("published_at") or "N/A"
            summary = news.get("summary") or ""
            body = news.get("article_raw") or ""
            snippet = summary or body[:1000]
            lines.append(f"[{published}] {title}\n{snippet}")
        return "\n\n".join(lines)
