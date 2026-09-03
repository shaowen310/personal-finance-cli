"""On-disk cache for fetched FX rates (stdlib only, zero deps)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .defaults import DEFAULT_CACHE_DIR

CACHE_FILENAME = "fx_cache.json"


def _as_dict(obj: object) -> dict[str, object]:
    """Coerce *obj* to a ``dict[str, object]``, returning ``{}`` if not a dict.

    ``json.load`` / ``json.loads`` yield ``Any``; routing through here pins the
    type to ``dict[str, object]`` so downstream ``dict`` navigation stays typed
    and no ``Any`` leaks into the package.
    """
    return obj if isinstance(obj, dict) else {}


def _cache_path() -> Path:
    return Path(os.environ.get("PFA_FX_CACHE_DIR") or DEFAULT_CACHE_DIR) / CACHE_FILENAME


def load_cache() -> dict[str, object]:
    """Load the FX cache from disk, returning an empty dict on any failure."""
    try:
        with _cache_path().open("r", encoding="utf-8") as f:
            data: object = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return _as_dict(data)


def save_cache(cache: dict[str, object]) -> None:
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


def _provider_bucket(cache: dict[str, object], provider: str) -> dict[str, object]:
    """Return the per-provider sub-dict for *provider* (``{}`` if absent)."""
    return _as_dict(_as_dict(cache.get("entries")).get(provider))


def cache_get(provider: str, key: str) -> dict[str, object] | None:
    """Return a cached entry if present, else None.

    Historical FX rates never change, so cached entries are treated as
    permanently valid (no TTL / expiry).
    """
    entry = _provider_bucket(load_cache(), provider).get(key)
    return entry if isinstance(entry, dict) else None


def cache_put(provider: str, key: str, entry: Mapping[str, object]) -> None:
    """Store *entry* under (provider, key) and persist."""
    cache = load_cache()
    entries = _as_dict(cache.get("entries"))
    bucket = _as_dict(entries.get(provider))
    bucket[key] = entry
    entries[provider] = bucket
    cache["entries"] = entries
    save_cache(cache)
