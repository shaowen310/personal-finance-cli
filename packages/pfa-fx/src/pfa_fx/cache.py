"""On-disk cache for fetched FX rates (stdlib only, zero deps)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .defaults import DEFAULT_CACHE_DIR

CACHE_FILENAME = "fx_cache.json"
CACHE_TTL_SECONDS = 24 * 3600  # 1 day


def _cache_path() -> Path:
    return Path(os.environ.get("PFA_FX_CACHE_DIR") or DEFAULT_CACHE_DIR) / CACHE_FILENAME


def load_cache() -> dict[str, Any]:
    """Load the FX cache from disk, returning an empty dict on any failure."""
    try:
        with _cache_path().open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict[str, Any]) -> None:
    """Persist *cache* to disk. Best-effort; ignores write errors."""
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
        _ = tmp.replace(path)
    except OSError:
        pass


def cache_get(provider: str, key: str) -> dict[str, Any] | None:
    """Return a cached entry if present and not expired, else None."""
    cache = load_cache()
    bucket = cache.get("entries", {}).get(provider, {})
    entry = bucket.get(key)
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return entry


def cache_put(provider: str, key: str, entry: dict[str, Any]) -> None:
    """Store *entry* under (provider, key) and persist."""
    cache = load_cache()
    cache.setdefault("entries", {}).setdefault(provider, {})[key] = entry
    save_cache(cache)
