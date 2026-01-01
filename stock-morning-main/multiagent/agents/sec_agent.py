from __future__ import annotations

from typing import Any, Dict, List

from .base_agent import BaseAgent
from multiagent.services import AgentToolkit
from multiagent.prompts import SEC_BLIND_PROMPT, SEC_REBUTTAL_PROMPT


class SECAgent(BaseAgent):
    """SEC 공시 기반 보수적 에이전트"""

    def __init__(self, toolkit: AgentToolkit, name: str = "SEC Analyst"):
        super().__init__(name=name, role="sec")
        self.toolkit = toolkit

    def blind_assessment(self, dataset: Dict[str, Any]) -> str:
        context = self._build_sec_context(dataset.get("sec_filings", []))
        return self.toolkit.summarize(context, SEC_BLIND_PROMPT)

    def rebut(self, dataset: Dict[str, Any], opponent_statement: str) -> str:
        context = self._build_sec_context(dataset.get("sec_filings", []))
        instruction = SEC_REBUTTAL_PROMPT.format(opponent=opponent_statement)
        return self.toolkit.summarize(context, instruction)

    def _build_sec_context(self, filings: List[Dict[str, Any]]) -> str:
        if not filings:
            return "관련 SEC 공시 데이터가 없습니다."
        chunks = []
        for filing in filings[:10]:
            meta = filing.get("metadata", {})
            form = meta.get("form", "N/A")
            filed = meta.get("filed_date") or meta.get("filed") or "N/A"
            entity = meta.get("filing_entity", "")
            text = filing.get("content") or ""
            snippet = text[:1500]
            chunks.append(f"[Form {form} | {filed} | {entity}]\n{snippet}")
        return "\n\n".join(chunks)
