"""
전문가 합의도 계산 모듈
"""

from __future__ import annotations

import re
from typing import Dict, List
from multiagent.schemas import ConsensusMetrics


class ConsensusAnalyzer:
    """4명 전문가의 합의도를 계산"""
    
    def __init__(self):
        self.action_keywords = {
            "STRONG_BUY": ["강력 매수", "적극 매수", "strong buy", "강력히 매수"],
            "BUY": ["매수", "buy", "긍정적", "상승"],
            "HOLD": ["보유", "hold", "관망", "중립"],
            "SELL": ["매도", "sell", "부정적", "하락"],
            "STRONG_SELL": ["강력 매도", "적극 매도", "strong sell", "강력히 매도"],
        }
    
    def calculate_consensus(
        self,
        fundamental_statement: str,
        risk_statement: str,
        growth_statement: str,
        sentiment_statement: str
    ) -> ConsensusMetrics:
        """
        4명 전문가의 의견에서 합의도 계산
        
        Args:
            각 전문가의 statement
        
        Returns:
            ConsensusMetrics 객체
        """
        statements = [
            fundamental_statement,
            risk_statement,
            growth_statement,
            sentiment_statement
        ]
        
        # 1. 액션 합의도 계산
        actions = [self._extract_action(stmt) for stmt in statements]
        action_consensus = self._calculate_action_consensus(actions)
        
        # 2. 점수 분산 계산 (숫자로 표현된 의견 일치도)
        scores = [self._extract_score(stmt) for stmt in statements]
        score_variance = self._calculate_score_variance(scores)
        
        # 3. 토론 수렴도 (의견이 비슷한지)
        convergence = self._calculate_convergence(statements)
        
        # 4. 전체 합의도
        overall = (action_consensus * 0.5 + 
                  (1 - min(score_variance / 10, 1)) * 0.3 + 
                  convergence * 0.2)
        
        return ConsensusMetrics(
            action_consensus=action_consensus,
            score_variance=score_variance,
            debate_convergence=convergence,
            overall_consensus=overall
        )
    
    def _extract_action(self, statement: str) -> str:
        """텍스트에서 투자 액션 추출"""
        statement_lower = statement.lower()
        
        for action, keywords in self.action_keywords.items():
            for keyword in keywords:
                if keyword in statement_lower:
                    return action
        
        # 기본값
        return "HOLD"
    
    def _calculate_action_consensus(self, actions: List[str]) -> float:
        """
        액션 합의도 계산
        - 4명 모두 같은 방향 (BUY/HOLD/SELL): 1.0
        - 3명 같은 방향: 0.75
        - 2명씩 갈림: 0.5
        - 완전 분산: 0.25
        """
        action_counts = {}
        for action in actions:
            # BUY계열과 SELL계열로 단순화
            if "BUY" in action:
                simplified = "BUY"
            elif "SELL" in action:
                simplified = "SELL"
            else:
                simplified = "HOLD"
            
            action_counts[simplified] = action_counts.get(simplified, 0) + 1
        
        max_count = max(action_counts.values())
        return max_count / len(actions)
    
    def _extract_score(self, statement: str) -> float:
        """
        텍스트에서 점수 추출 (X/10 형식)
        없으면 긍정/부정 비율로 추정
        """
        # "8/10", "7 out of 10" 같은 패턴 찾기
        score_pattern = r'(\d+)\s*[/out of]*\s*10'
        matches = re.findall(score_pattern, statement)
        
        if matches:
            return float(matches[0])
        
        # 점수가 없으면 긍정/부정 키워드 비율로 추정
        positive_keywords = ["긍정", "상승", "좋", "강력", "성장", "기회"]
        negative_keywords = ["부정", "하락", "나쁨", "약", "리스크", "위험"]
        
        pos_count = sum(1 for kw in positive_keywords if kw in statement)
        neg_count = sum(1 for kw in negative_keywords if kw in statement)
        
        if pos_count + neg_count == 0:
            return 5.0  # 중립
        
        return (pos_count / (pos_count + neg_count)) * 10
    
    def _calculate_score_variance(self, scores: List[float]) -> float:
        """점수 분산 계산 (낮을수록 의견 일치)"""
        if not scores:
            return 0.0
        
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        return variance ** 0.5  # 표준편차
    
    def _calculate_convergence(self, statements: List[str]) -> float:
        """
        의견 수렴도 계산 (비슷한 단어/표현이 많을수록 높음)
        간단한 휴리스틱: 공통 키워드 비율
        """
        # 각 statement를 단어로 분리
        words_sets = []
        for stmt in statements:
            words = set(re.findall(r'\w+', stmt.lower()))
            words_sets.append(words)
        
        if not words_sets:
            return 0.5
        
        # 4개 중 최소 2개 이상에서 등장하는 단어 비율
        all_words = set()
        for ws in words_sets:
            all_words.update(ws)
        
        common_words = []
        for word in all_words:
            count = sum(1 for ws in words_sets if word in ws)
            if count >= 2:  # 2명 이상이 사용
                common_words.append(word)
        
        if not all_words:
            return 0.5
        
        return len(common_words) / len(all_words)

