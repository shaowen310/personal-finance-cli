"""Cached FX rate retrieval for the analysis package.

Rates are fetched from the standalone :mod:`pfa_fx` leaf package (canonical
shape: SGD per 1 unit of foreign currency, with ``"SGD": 1.0``) and cached on
disk under the OS temp directory (``%TEMP%/pfa_fx_cache`` on Windows) so they
survive across report runs and even separate months without polluting the IR
files. The IR itself stays free of any build-date FX snapshot.

This module replaces the FX-caching logic that previously lived in
``pfa_analysis.analyze`` so that FX concerns are isolated and reusable by both
``report`` and ``dashboard``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from pfa_fx import fetch_fx_rates as _pfa_fetch_fx_rates

# In-memory cache keyed by date string (YYYY-MM-DD) to avoid re-reading the
# temp cache file within a single process run.
_FX_CACHE: dict[str, dict[str, Any] | None] = {}


def _fx_cache_dir() -> str:
    """FX rate snapshots are cached in the OS temp directory so they survive
    across report runs (and even separate months) without polluting the IR
    files. Falls back to a local ``.fx_cache`` if ``%TEMP%`` is unavailable.
    """
    base = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    d = os.path.join(base, "pfa_fx_cache")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.join(".", ".fx_cache")
        os.makedirs(d, exist_ok=True)
    return d


def _fx_cache_path(date_str: str) -> str:
    return os.path.join(_fx_cache_dir(), f"fx_{date_str}.json")


def _load_fx_cache(date_str: str) -> dict[str, Any] | None:
    """Read a previously cached FX snapshot for ``date_str`` (YYYY-MM-DD).

    Returns the canonical ``{"rates":{...}, "date":..., "source":...}`` shape,
    or ``None`` if absent or corrupt.
    """
    p = _fx_cache_path(date_str)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("rates"):
            return data
    except (OSError, ValueError):
        pass
    return None


def _save_fx_cache(date_str: str, data: dict[str, Any]) -> None:
    """Persist an FX snapshot to the temp cache so future runs (any month)
    reuse it offline instead of re-hitting the network.
    """
    try:
        with open(_fx_cache_path(date_str), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def fetch_fx_rates(date_str: str) -> dict[str, Any] | None:
    """Fetch FX rates as of ``date_str`` (YYYY-MM-DD), base = SGD.

    Resolution order:

    1. In-memory cache for this process run.
    2. On-disk temp cache (offline reuse, shared across months).
    3. Network fetch via :mod:`pfa_fx`, then persisted to the temp cache.

    Returns ``{"rates": {CCY: SGD per 1 unit, ...}, "date": str, "source": str}``
    (canonical SGD-per-unit shape) or ``None`` on any failure. Callers must
    handle ``None`` by degrading gracefully (no FX conversion).
    """
    if date_str in _FX_CACHE:
        return _FX_CACHE[date_str]
    # Reuse a previously fetched snapshot from the temp cache (offline-safe,
    # shared across months) before hitting the network.
    cached = _load_fx_cache(date_str)
    if cached is not None:
        _FX_CACHE[date_str] = cached
        return cached
    try:
        # currencies=None (not []) so pfa_fx falls back to DEFAULT_WATCH_SYMBOLS
        # and actually fetches CNY/JPY/USD/etc. An empty list would request no
        # currencies and return rates = {"SGD": 1.0}, breaking the FX table and
        # leaving multi-currency balances unconverted.
        result = _pfa_fetch_fx_rates(date_str, currencies=None)
    except Exception as e:  # noqa: BLE001 - degrade, never crash the report
        print(f"[WARN] Failed to fetch FX rates for {date_str}: {e}", file=sys.stderr)
        _FX_CACHE[date_str] = None
        return None
    # Persist for offline reuse (covers the no-network / API-down fallback gap).
    _save_fx_cache(date_str, result)
    _FX_CACHE[date_str] = result
    return result
