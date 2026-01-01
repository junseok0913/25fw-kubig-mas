from __future__ import annotations

from typing import Any, Dict

from .base_agent import BaseAgent


class PlaceholderAgent(BaseAgent):
    """
    차트/거시 등 추후 구현용 에이전트의 더미 버전
    """

    def __init__(self, name: str, role: str):
        super().__init__(name=name, role=role)

    def analyze(self, dataset: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "agent": self.name,
            "role": self.role,
            "status": "pending",
            "opinion": "이 에이전트는 추후 차트/거시경제 데이터를 받아 분석하도록 확장될 예정입니다.",
        }
