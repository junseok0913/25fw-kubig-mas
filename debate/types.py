"""Compatibility wrapper for debate types.

Type definitions moved to `agents/debate/types.py` during the agent-style
refactor. This module is kept so existing imports like `debate.ticker_script`
continue to work.
"""

from __future__ import annotations

from agents.debate.types import *  # noqa: F403

