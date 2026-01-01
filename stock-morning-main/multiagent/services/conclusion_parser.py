"""
LLM 최종 결론 텍스트를 JSON으로 파싱
"""

from __future__ import annotations

import re
import json
from typing import Optional
from multiagent.schemas import InvestmentConclusion, Scores, KeyTrigger


class ConclusionParser:
    """LLM이 생성한 텍스트를 InvestmentConclusion 객체로 파싱"""
    
    def parse(self, ticker: str, raw_text: str, confidence: float) -> InvestmentConclusion:
        """
        최종 결론 텍스트를 구조화된 객체로 파싱
        
        Args:
            ticker: 티커 심볼
            raw_text: LLM이 생성한 원문
            confidence: 전문가 합의도 (0-1)
        
        Returns:
            InvestmentConclusion 객체
        """
        try:
            # 1. JSON 블록 추출 시도 (우선)
            json_data = self._extract_json_block(raw_text)
            
            if json_data:
                # JSON 파싱 성공
                scores_data = json_data.get("scores", {})
                scores = Scores(
                    fundamental=min(10, max(0, int(scores_data.get("fundamental", 5)))),
                    risk=min(10, max(0, int(scores_data.get("risk", 5)))),
                    growth=min(10, max(0, int(scores_data.get("growth", 5)))),
                    sentiment=min(10, max(0, int(scores_data.get("sentiment", 5)))),
                    overall=self._calculate_overall(scores_data)
                )
                
                # 트리거 파싱
                bullish = None
                bearish = None
                if json_data.get("bullish_trigger"):
                    bullish = KeyTrigger(
                        condition=json_data["bullish_trigger"],
                        action="포지션 확대"
                    )
                if json_data.get("bearish_trigger"):
                    bearish = KeyTrigger(
                        condition=json_data["bearish_trigger"],
                        action="손절 검토"
                    )
                
                # 점수 이유 추출
                score_reasons = []
                if scores_data.get("fundamental_reason"):
                    score_reasons.append(f"펀더멘털: {scores_data['fundamental_reason']}")
                if scores_data.get("risk_reason"):
                    score_reasons.append(f"리스크: {scores_data['risk_reason']}")
                if scores_data.get("growth_reason"):
                    score_reasons.append(f"성장: {scores_data['growth_reason']}")
                if scores_data.get("sentiment_reason"):
                    score_reasons.append(f"심리: {scores_data['sentiment_reason']}")
                
                return InvestmentConclusion(
                    ticker=ticker,
                    scores=scores,
                    action=self._normalize_action(json_data.get("action", "HOLD")),
                    position_size=min(20, max(0, int(json_data.get("position_size", 5)))),
                    confidence=confidence,
                    executive_summary=json_data.get("executive_summary", ""),
                    key_debates=score_reasons,  # 점수 이유를 key_debates에 저장
                    immediate_action=json_data.get("immediate_action"),
                    short_term_strategy=json_data.get("short_term_strategy"),
                    long_term_strategy=json_data.get("long_term_strategy"),
                    bullish_trigger=bullish,
                    bearish_trigger=bearish,
                    raw_conclusion=raw_text
                )
            
            # 2. JSON 파싱 실패 시 기존 정규식 방식 사용 (fallback)
            print("⚠️  JSON 블록을 찾지 못해 정규식 파싱 시도...")
            scores = self._extract_scores(raw_text)
            action = self._extract_action(raw_text)
            position_size = self._extract_position_size(raw_text)
            executive_summary = self._extract_executive_summary(raw_text)
            key_debates = self._extract_key_debates(raw_text)
            immediate, short_term, long_term = self._extract_strategies(raw_text)
            bullish, bearish = self._extract_triggers(raw_text)
            review_items = self._extract_review_items(raw_text)
            
            return InvestmentConclusion(
                ticker=ticker,
                scores=scores,
                action=action,
                position_size=position_size,
                confidence=confidence,
                executive_summary=executive_summary,
                key_debates=key_debates,
                immediate_action=immediate,
                short_term_strategy=short_term,
                long_term_strategy=long_term,
                bullish_trigger=bullish,
                bearish_trigger=bearish,
                next_review_items=review_items,
                raw_conclusion=raw_text
            )
        
        except Exception as exc:
            print(f"⚠️  결론 파싱 중 오류, 기본값 사용: {exc}")
            # 파싱 실패 시 안전한 기본값
            return InvestmentConclusion(
                ticker=ticker,
                scores=Scores(fundamental=5, risk=5, growth=5, sentiment=5, overall=5.0),
                action="HOLD",
                position_size=5,
                confidence=confidence,
                executive_summary="파싱 실패",
                raw_conclusion=raw_text
            )
    
    def _extract_json_block(self, text: str) -> Optional[dict]:
        """텍스트에서 JSON 블록 추출"""
        # ```json ... ``` 블록 찾기
        json_pattern = r'```json\s*(\{.*?\})\s*```'
        match = re.search(json_pattern, text, re.DOTALL)
        
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        
        # { ... } 형태로 직접 찾기 (마지막 JSON 객체)
        brace_pattern = r'\{[^{}]*"action"[^{}]*"scores"[^{}]*\{[^{}]*\}[^{}]*\}'
        matches = re.findall(brace_pattern, text, re.DOTALL)
        
        for m in reversed(matches):
            try:
                return json.loads(m)
            except json.JSONDecodeError:
                continue
        
        return None
    
    def _normalize_action(self, action: str) -> str:
        """액션 문자열 정규화"""
        action_upper = action.upper().replace(" ", "_")
        valid_actions = ["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]
        if action_upper in valid_actions:
            return action_upper
        if "BUY" in action_upper:
            return "BUY"
        if "SELL" in action_upper:
            return "SELL"
        return "HOLD"
    
    def _calculate_overall(self, scores_data: dict) -> float:
        """종합 점수 계산"""
        f = scores_data.get("fundamental", 5)
        r = scores_data.get("risk", 5)
        g = scores_data.get("growth", 5)
        s = scores_data.get("sentiment", 5)
        # 가중평균: Fundamental 30%, Risk 역방향 20%, Growth 30%, Sentiment 20%
        return round(f * 0.3 + (10 - r) * 0.2 + g * 0.3 + s * 0.2, 1)
    
    def _extract_scores(self, text: str) -> Scores:
        """점수 추출 (Fundamental, Risk, Growth, Sentiment)"""
        patterns = {
            "fundamental": r'Fundamental Score[:\s]*(\d+)',
            "risk": r'Risk Score[:\s]*(\d+)',
            "growth": r'Growth Score[:\s]*(\d+)',
            "sentiment": r'Sentiment Score[:\s]*(\d+)',
        }
        
        scores_dict = {}
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                scores_dict[key] = int(match.group(1))
            else:
                scores_dict[key] = 5  # 기본값
        
        # 종합 점수 추출 또는 계산
        overall_match = re.search(r'종합 점수[:\s]*(\d+(?:\.\d+)?)', text)
        if overall_match:
            overall = float(overall_match.group(1))
        else:
            # 가중평균: Fundamental 30%, Risk -20%, Growth 30%, Sentiment 20%
            overall = (scores_dict["fundamental"] * 0.3 + 
                      (10 - scores_dict["risk"]) * 0.2 +  # Risk는 역방향
                      scores_dict["growth"] * 0.3 + 
                      scores_dict["sentiment"] * 0.2)
        
        return Scores(**scores_dict, overall=overall)
    
    def _extract_action(self, text: str) -> str:
        """액션 추출 (STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL)"""
        action_map = {
            "STRONG BUY": "STRONG_BUY",
            "STRONG_BUY": "STRONG_BUY",
            "🟢 STRONG BUY": "STRONG_BUY",
            "BUY": "BUY",
            "🔵 BUY": "BUY",
            "HOLD": "HOLD",
            "⚪ HOLD": "HOLD",
            "SELL": "SELL",
            "🟠 SELL": "SELL",
            "STRONG SELL": "STRONG_SELL",
            "STRONG_SELL": "STRONG_SELL",
            "🔴 STRONG SELL": "STRONG_SELL",
        }
        
        for pattern, action in action_map.items():
            if pattern in text.upper():
                return action
        
        return "HOLD"  # 기본값
    
    def _extract_position_size(self, text: str) -> int:
        """포지션 크기 추출 (0-20%)"""
        match = re.search(r'포트폴리오의?\s*(\d+)\s*%', text)
        if match:
            return min(int(match.group(1)), 20)
        
        match = re.search(r'(\d+)%\s*비중', text)
        if match:
            return min(int(match.group(1)), 20)
        
        return 10  # 기본값
    
    def _extract_executive_summary(self, text: str) -> str:
        """Executive Summary 추출"""
        match = re.search(r'##\s*📊\s*Executive Summary\s*\n(.+?)(?=\n##|\Z)', text, re.DOTALL)
        if match:
            return match.group(1).strip()[:500]  # 최대 500자
        
        # 첫 2-3문장 추출
        sentences = re.split(r'[.!?]\s+', text[:1000])
        return '. '.join(sentences[:3]) + '.'
    
    def _extract_key_debates(self, text: str) -> list:
        """주요 토론 쟁점 추출"""
        debates = []
        
        # "쟁점 1:", "쟁점 2:" 패턴 찾기
        debate_pattern = r'\*\*쟁점\s*\d+\*\*[:\s]*(.+?)(?=\*\*쟁점|\n##|\Z)'
        matches = re.findall(debate_pattern, text, re.DOTALL)
        
        for match in matches[:3]:  # 최대 3개
            debate_text = match.strip()[:300]  # 최대 300자
            debates.append(debate_text)
        
        return debates
    
    def _extract_strategies(self, text: str) -> tuple:
        """실행 계획 추출 (즉시/단기/장기)"""
        immediate = None
        short_term = None
        long_term = None
        
        # 즉시 행동
        match = re.search(r'###\s*즉시 행동.*?\n-\s*(.+?)(?=\n###|\n##|\Z)', text, re.DOTALL)
        if match:
            immediate = match.group(1).strip()[:200]
        
        # 단기 전략
        match = re.search(r'###\s*단기 전략.*?\n-\s*(.+?)(?=\n###|\n##|\Z)', text, re.DOTALL)
        if match:
            short_term = match.group(1).strip()[:200]
        
        # 장기 전략
        match = re.search(r'###\s*장기 전략.*?\n-\s*(.+?)(?=\n###|\n##|\Z)', text, re.DOTALL)
        if match:
            long_term = match.group(1).strip()[:200]
        
        return immediate, short_term, long_term
    
    def _extract_triggers(self, text: str) -> tuple:
        """트리거 추출 (상승/하락 시나리오)"""
        bullish = None
        bearish = None
        
        # 상승 시나리오
        bull_match = re.search(
            r'###\s*상승 시나리오.*?조건[:\s]*(.+?)액션[:\s]*(.+?)(?=\n###|\n##|\Z)',
            text,
            re.DOTALL
        )
        if bull_match:
            bullish = KeyTrigger(
                condition=bull_match.group(1).strip()[:200],
                action=bull_match.group(2).strip()[:200]
            )
        
        # 하락 시나리오
        bear_match = re.search(
            r'###\s*하락 시나리오.*?조건[:\s]*(.+?)액션[:\s]*(.+?)(?=\n###|\n##|\Z)',
            text,
            re.DOTALL
        )
        if bear_match:
            bearish = KeyTrigger(
                condition=bear_match.group(1).strip()[:200],
                action=bear_match.group(2).strip()[:200]
            )
        
        return bullish, bearish
    
    def _extract_review_items(self, text: str) -> list:
        """재검토 항목 추출"""
        items = []
        
        # "1. ...", "2. ...", "3. ..." 패턴
        pattern = r'##\s*🔮.*?재검토 항목.*?\n(.+?)(?=\n---|\n##|\Z)'
        match = re.search(pattern, text, re.DOTALL)
        
        if match:
            content = match.group(1)
            item_pattern = r'\d+\.\s*(.+)'
            for m in re.finditer(item_pattern, content):
                items.append(m.group(1).strip()[:200])
        
        return items[:3]  # 최대 3개

