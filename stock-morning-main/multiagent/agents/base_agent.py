from __future__ import annotations

from typing import Dict, Any


class BaseAgent:
    """멀티 에이전트 분석 공통 인터페이스"""

    def __init__(self, name: str, role: str):
        self.name = name
        self.role = role

    def analyze(self, dataset: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
