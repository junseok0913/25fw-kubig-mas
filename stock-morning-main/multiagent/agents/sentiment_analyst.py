from __future__ import annotations

from typing import Any, Dict, List

from .base_agent import BaseAgent
from multiagent.services import AgentToolkit
from multiagent.prompts import SENTIMENT_BLIND_PROMPT, SENTIMENT_REBUTTAL_PROMPT


class SentimentAnalyst(BaseAgent):
    """George Soros 스타일 시장 심리/반사성 이론 전문가"""

    def __init__(self, toolkit: AgentToolkit, name: str = "Market Sentiment Analyst"):
        super().__init__(name=name, role="sentiment")
        self.toolkit = toolkit

    def blind_assessment(self, dataset: Dict[str, Any]) -> str:
        """초기 분석: 뉴스와 공시에서 시장 심리 읽기"""
        ticker = dataset.get("ticker", "")
        context = self._build_full_context(dataset)
        prompt = f"분석 대상 기업: {ticker}\n\n{SENTIMENT_BLIND_PROMPT}"
        return self.toolkit.summarize(context, prompt)

    def rebut(self, ticker: str, opponents_statements: List[str]) -> str:
        """다른 분석가들의 합리적 분석에 시장 비합리성 주입 (데이터 재분석 없이 의견만으로 토론)"""
        opponents_text = "\n\n---\n\n".join(opponents_statements)
        instruction = SENTIMENT_REBUTTAL_PROMPT.format(opponents=opponents_text)
        full_prompt = f"분석 대상 기업: {ticker}\n\n{instruction}"
        return self.toolkit.summarize("", full_prompt)

    def _build_full_context(self, dataset: Dict[str, Any]) -> str:
        """뉴스 중심 + 시장 데이터 + SEC 공시 보조 컨텍스트 생성"""
        lines = []
        
        # 시장 데이터 (주가 위치 파악용)
        market_data_text = dataset.get("market_data_text")
        if market_data_text:
            lines.append(market_data_text)
            lines.append("")
        
        # 뉴스 (헤드라인과 톤 분석 중심)
        news_items = dataset.get("aws_news", [])
        if news_items:
            lines.append("=== 뉴스 데이터 ===")
            for news in news_items[:15]:  # 심리 분석은 뉴스를 더 많이 봄
                title = news.get("title") or news.get("pk") or "제목 없음"
                published = news.get("published_at") or "N/A"
                summary = news.get("summary") or ""
                body = news.get("article_raw") or ""
                snippet = summary or body[:800]
                lines.append(f"[{published}] {title}\n{snippet}")
        else:
            lines.append("=== 뉴스 데이터 ===\n관련 뉴스가 없습니다.")
        
        # SEC 공시 (참고용)
        sec_filings = dataset.get("sec_filings", [])
        if sec_filings:
            lines.append("\n\n=== SEC 공시 데이터 (참고) ===")
            for filing in sec_filings[:5]:  # 심리 분석은 공시는 간략히
                meta = filing.get("metadata", {})
                form = meta.get("form", "N/A")
                filed = meta.get("filed_date") or meta.get("filed") or "N/A"
                entity = meta.get("filing_entity", "")
                text = filing.get("content") or ""
                snippet = text[:1000]
                lines.append(f"[Form {form} | {filed} | {entity}]\n{snippet}")
        else:
            lines.append("\n\n=== SEC 공시 데이터 ===\n관련 공시가 없습니다.")
        
        return "\n\n".join(lines)

