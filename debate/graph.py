"""Compatibility wrapper for the Debate agent.

The real implementation lives under `agents/debate/` (to match other agents like
`agents/theme/`). This module keeps the existing entrypoint working:
- `python -m debate.graph ...`
- `from debate.graph import run_debate`
"""

from __future__ import annotations

from agents.debate.graph import build_graph, run_debate


def main() -> None:
    from agents.debate.graph import main as _main

    _main()


if __name__ == "__main__":  # pragma: no cover
    main()

