from __future__ import annotations

from typing import Any, Dict, List

from .base_agent import BaseAgent
from multiagent.services import AgentToolkit
from multiagent.prompts import RISK_BLIND_PROMPT, RISK_REBUTTAL_PROMPT


class RiskManager(BaseAgent):
    """Ray Dalio 스타일 리스크 관리 전문가"""

    def __init__(self, toolkit: AgentToolkit, name: str = "Risk Manager"):
        super().__init__(name=name, role="risk")
        self.toolkit = toolkit

    def blind_assessment(self, dataset: Dict[str, Any]) -> str:
        """초기 분석: SEC 공시와 뉴스에서 리스크 요인 추출"""
        ticker = dataset.get("ticker", "")
        context = self._build_full_context(dataset)
        prompt = f"분석 대상 기업: {ticker}\n\n{RISK_BLIND_PROMPT}"
        return self.toolkit.summarize(context, prompt)

    def rebut(self, ticker: str, opponents_statements: List[str]) -> str:
        """다른 분석가들의 낙관론 견제 (데이터 재분석 없이 의견만으로 토론)"""
        opponents_text = "\n\n---\n\n".join(opponents_statements)
        instruction = RISK_REBUTTAL_PROMPT.format(opponents=opponents_text)
        full_prompt = f"분석 대상 기업: {ticker}\n\n{instruction}"
        return self.toolkit.summarize("", full_prompt)

    def _build_full_context(self, dataset: Dict[str, Any]) -> str:
        """SEC 공시 + 뉴스 + 시장 데이터 통합 컨텍스트 생성"""
        lines = []
        
        # 시장 데이터
        market_data_text = dataset.get("market_data_text")
        if market_data_text:
            lines.append(market_data_text)
            lines.append("")
        
        # SEC 공시 (Risk Factors 섹션 우선)
        sec_filings = dataset.get("sec_filings", [])
        if sec_filings:
            lines.append("=== SEC 공시 데이터 ===")
            for filing in sec_filings[:10]:
                meta = filing.get("metadata", {})
                form = meta.get("form", "N/A")
                filed = meta.get("filed_date") or meta.get("filed") or "N/A"
                entity = meta.get("filing_entity", "")
                text = filing.get("content") or ""
                # Risk Factors 섹션이 있으면 우선 추출
                if "risk factors" in text.lower():
                    risk_section_start = text.lower().find("risk factors")
                    snippet = text[risk_section_start:risk_section_start+3000]
                else:
                    snippet = text[:2000]
                lines.append(f"[Form {form} | {filed} | {entity}]\n{snippet}")
        else:
            lines.append("=== SEC 공시 데이터 ===\n관련 공시가 없습니다.")
        
        # 뉴스 (부정적 키워드 우선)
        news_items = dataset.get("aws_news", [])
        if news_items:
            lines.append("\n\n=== 뉴스 데이터 ===")
            for news in news_items[:10]:
                title = news.get("title") or news.get("pk") or "제목 없음"
                published = news.get("published_at") or "N/A"
                summary = news.get("summary") or ""
                body = news.get("article_raw") or ""
                snippet = summary or body[:800]
                lines.append(f"[{published}] {title}\n{snippet}")
        else:
            lines.append("\n\n=== 뉴스 데이터 ===\n관련 뉴스가 없습니다.")
        
        return "\n\n".join(lines)

