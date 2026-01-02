"""Debate output/state type definitions.

These types are used by the Debate agent implementation under `agents/debate/`.
`debate/types.py` remains as a compatibility wrapper to avoid breaking imports.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, TypedDict

from shared.types import Source as SharedSource


DebateAction = Literal["BUY", "HOLD", "SELL"]


class SecFilingSource(TypedDict):
    type: Literal["sec_filing"]
    ticker: str
    form: str
    filed_date: str  # YYYY-MM-DD
    accession_number: str


Source = SharedSource | SecFilingSource


class DebateUtterance(TypedDict):
    text: str
    action: DebateAction
    confidence: float  # 0.0 ~ 1.0
    sources: List[Source]


class DebateRound(TypedDict):
    round: int
    fundamental: DebateUtterance
    risk: DebateUtterance
    growth: DebateUtterance
    sentiment: DebateUtterance


class DebateConclusion(TypedDict):
    text: str
    action: DebateAction
    confidence: float  # 0.0 ~ 1.0


class TickerDebateOutput(TypedDict):
    ticker: str
    date: str  # YYYYMMDD
    rounds: List[DebateRound]
    conclusion: DebateConclusion


class TickerDebateState(TypedDict, total=False):
    """Internal debate state (graph-level)."""

    # output fields
    ticker: str
    date: str  # YYYYMMDD
    rounds: List[DebateRound]
    conclusion: DebateConclusion

    # runtime-only fields
    max_rounds: int
    current_round: int
    should_continue: bool

    news_list_json: str
    sec_list_json: str
    ohlcv_summary: str
    allowed_sources: List[Source]
    guidance_by_role: Dict[str, str]

    # allow future extensions without churn
    extra: Dict[str, Any]
