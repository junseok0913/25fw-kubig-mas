"""Shared TypedDict definitions."""

from __future__ import annotations

from typing import List, Literal, TypedDict, Union


class NewsSource(TypedDict):
    pk: str
    title: str


class ArticleSource(TypedDict):
    type: Literal["article"]
    pk: str
    title: str


class ChartSource(TypedDict):
    type: Literal["chart"]
    ticker: str
    start_date: str  # YYYY-MM-DD (ET)
    end_date: str


class EventSource(TypedDict):
    type: Literal["event"]
    id: str
    title: str
    date: str  # YYYY-MM-DD (ET)

class SecFilingSource(TypedDict):
    type: Literal["sec_filing"]
    ticker: str
    form: str
    filed_date: str  # YYYY-MM-DD (ET)
    accession_number: str


Source = Union[ArticleSource, ChartSource, EventSource, SecFilingSource]


class Theme(TypedDict):
    headline: str
    description: str
    related_news: List[NewsSource]


class ScriptTurn(TypedDict):
    id: int
    speaker: Literal["진행자", "해설자"]
    text: str
    sources: List[Source]
