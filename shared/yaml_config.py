"""YAML-based runtime configuration loader.

This project historically used `.env` for *all* configuration.
To reduce env clutter, we support loading non-secret settings from a YAML file
and exporting them into `os.environ` so existing code paths keep working.

Precedence recommendation:
  1) real environment variables (exported in shell)
  2) YAML config (non-secret defaults)
  3) `.env` (secrets only; loaded with override=False)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "app.yaml"

_SECRET_KEYS = {
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "LANGSMITH_API_KEY",
}


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    if upper in _SECRET_KEYS:
        return True
    if upper.endswith("_API_KEY"):
        return True
    return False


def _coerce_env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def _extract_env_map(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}

    # Preferred shape:
    #   secrets: { ... }
    #   env: { ... }
    env_map: dict[str, Any] = {}

    secrets = raw.get("secrets")
    if isinstance(secrets, Mapping):
        env_map.update({str(k): v for k, v in secrets.items()})

    env = raw.get("env")
    if isinstance(env, Mapping):
        env_map.update({str(k): v for k, v in env.items()})
        return env_map

    # Fallback: allow a flat mapping of env vars.
    return {str(k): v for k, v in raw.items()}


def load_env_from_yaml(
    path: str | Path | None = None,
    *,
    override: bool = False,
    logger: logging.Logger | None = None,
) -> bool:
    """Load YAML config and export values to `os.environ`.

    - Secret keys (e.g., *_API_KEY) are ignored even if present in YAML.
    - `null` values are ignored (keeps env unset).
    - By default, existing env vars are not overridden.
    """

    log = logger or logging.getLogger(__name__)

    env_path = os.getenv("APP_CONFIG_PATH")
    config_path = Path(path) if path is not None else Path(env_path) if env_path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = (ROOT_DIR / config_path).resolve()

    if not config_path.exists():
        return False

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        log.warning("YAML config load failed: %s (%s)", config_path, exc)
        return False

    env_map = _extract_env_map(raw)
    if not env_map:
        return False

    applied = 0
    skipped = 0

    for key, value in env_map.items():
        if not key:
            continue
        if _is_secret_key(key):
            skipped += 1
            continue
        if value is None:
            skipped += 1
            continue
        if not override and key in os.environ:
            skipped += 1
            continue
        os.environ[key] = _coerce_env_value(value)
        applied += 1

    log.info("Loaded YAML config: %s (applied=%d, skipped=%d)", config_path, applied, skipped)
    return True

