"""
최종 결론 구조화를 위한 Pydantic 스키마
"""

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class KeyTrigger(BaseModel):
    """시나리오별 트리거 조건"""
    condition: str = Field(description="트리거 발동 조건 (예: '매출 성장률 YoY +20% 초과')")
    action: str = Field(description="해당 조건 시 취할 행동")


class Scores(BaseModel):
    """4가지 투자 관점 점수"""
    fundamental: int = Field(ge=0, le=10, description="펀더멘털 점수 (0-10)")
    risk: int = Field(ge=0, le=10, description="리스크 점수 (0-10, 높을수록 위험)")
    growth: int = Field(ge=0, le=10, description="성장 가능성 점수 (0-10)")
    sentiment: int = Field(ge=0, le=10, description="시장 심리 점수 (0-10)")
    overall: float = Field(ge=0, le=10, description="종합 점수 (가중평균)")


class InvestmentConclusion(BaseModel):
    """4명 전문가 토론 최종 결론"""
    ticker: str = Field(description="분석 대상 티커")
    
    # 핵심 지표
    scores: Scores = Field(description="4가지 관점 점수")
    action: Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"] = Field(
        description="최종 투자 액션"
    )
    position_size: int = Field(ge=0, le=20, description="추천 포지션 크기 (포트폴리오의 0-20%)")
    confidence: float = Field(ge=0, le=1, description="4명 전문가 합의도 (0-1)")
    
    # 요약
    executive_summary: str = Field(description="전체 토론 핵심 요약 (2-3문장)")
    
    # 쟁점
    key_debates: List[str] = Field(
        default_factory=list,
        description="전문가 간 주요 의견 충돌 사항"
    )
    
    # 실행 계획
    immediate_action: Optional[str] = Field(
        default=None,
        description="즉시 행동 (1-5일)"
    )
    short_term_strategy: Optional[str] = Field(
        default=None,
        description="단기 전략 (1-3개월)"
    )
    long_term_strategy: Optional[str] = Field(
        default=None,
        description="장기 전략 (6개월-1년)"
    )
    
    # 트리거
    bullish_trigger: Optional[KeyTrigger] = Field(
        default=None,
        description="상승 시나리오 트리거"
    )
    bearish_trigger: Optional[KeyTrigger] = Field(
        default=None,
        description="하락 시나리오 트리거"
    )
    
    # 재검토 항목
    next_review_items: List[str] = Field(
        default_factory=list,
        description="3-6개월 후 재검토 항목"
    )
    
    # 원문
    raw_conclusion: str = Field(description="LLM이 생성한 원문")


class MarketData(BaseModel):
    """실시간 주가 및 재무 지표"""
    current_price: Optional[float] = None
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    forward_pe: Optional[float] = None
    price_to_book: Optional[float] = None
    dividend_yield: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None
    fifty_day_avg: Optional[float] = None
    two_hundred_day_avg: Optional[float] = None
    beta: Optional[float] = None
    volume: Optional[int] = None
    avg_volume: Optional[int] = None
    
    # 재무 지표
    revenue: Optional[float] = None
    revenue_growth: Optional[float] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    profit_margin: Optional[float] = None
    roe: Optional[float] = None  # Return on Equity
    roa: Optional[float] = None  # Return on Assets
    debt_to_equity: Optional[float] = None
    current_ratio: Optional[float] = None
    free_cash_flow: Optional[float] = None


class ConsensusMetrics(BaseModel):
    """전문가 합의도 측정 지표"""
    action_consensus: float = Field(
        ge=0, le=1,
        description="액션 합의도 (4명이 같은 방향이면 1.0)"
    )
    score_variance: float = Field(
        ge=0,
        description="점수 분산 (낮을수록 의견 일치)"
    )
    debate_convergence: float = Field(
        ge=0, le=1,
        description="토론 수렴도 (라운드마다 의견이 수렴하는지)"
    )
    overall_consensus: float = Field(
        ge=0, le=1,
        description="전체 합의도 (위 3개 평균)"
    )

