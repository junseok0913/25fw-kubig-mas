"""Ticker debate module (prototype).

This package provides a LangGraph-based, per-ticker multi-agent debate pipeline
that outputs a compact debate JSON artifact (rounds + moderator conclusion).

Note: Imports are kept lazy to avoid side effects/warnings when running
`python -m debate.graph`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .graph import build_graph as build_graph
    from .graph import run_debate as run_debate
    from .ticker_script import run_ticker_script_pipeline as run_ticker_script_pipeline


def build_graph():  # type: ignore[no-redef]
    from .graph import build_graph as _build_graph

    return _build_graph()


def run_debate(*args, **kwargs):  # type: ignore[no-redef]
    from .graph import run_debate as _run_debate

    return _run_debate(*args, **kwargs)

def run_ticker_script_pipeline(*args, **kwargs):  # type: ignore[no-redef]
    from .ticker_script import run_ticker_script_pipeline as _run

    return _run(*args, **kwargs)


__all__ = ["build_graph", "run_debate", "run_ticker_script_pipeline"]
