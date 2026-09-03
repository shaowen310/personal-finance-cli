"""Convenience wrappers producing the dict shape used by renderers.

Some callers (``pfa-analysis`` renderers) expect an FX rate object shaped as::

    {"rates": {CCY: <SGD per 1 unit>}, "date": "...", "source": "..."}

This module adapts :func:`pfa_fx.rates.get_fx_rates` to that shape so the
analysis package needs almost no structural change. The resolved wrapper dict
is cached on disk (via :mod:`pfa_fx.cache`) keyed by date so it is reused
offline across report runs and months.
"""

from __future__ import annotations

from typing import TypedDict, cast

from .cache import cache_get, cache_put
from .defaults import DEFAULT_WATCH_SYMBOLS
from .rates import FXResult, get_fx_rates


class FxWrapper(TypedDict):
    """Canonical renderer FX wrapper: SGD-per-unit rates plus provenance."""

    rates: dict[str, float]
    date: str
    source: str


# In-memory cache keyed by date string (YYYY-MM-DD) to avoid re-reading the
# on-disk cache within a single process run.
_WRAPPER_CACHE: dict[str, FxWrapper] = {}


def _wrapper_cache_get(date_str: str) -> FxWrapper | None:
    """Return a previously cached wrapper dict for *date_str*, or None.

    Wrapper entries are stored without an expiry so they survive across months
    for offline reuse (distinct from the 1-day TTL on raw provider fetches in
    :mod:`pfa_fx.cache`).
    """
    entry = cache_get("wrapper", date_str)
    return cast(FxWrapper, entry) if isinstance(entry, dict) else None


def _wrapper_cache_put(date_str: str, wrapper: FxWrapper) -> None:
    """Persist *wrapper* under the ``wrapper`` bucket for offline reuse."""
    cache_put("wrapper", date_str, wrapper)


def fx_result_to_wrapper(result: FXResult) -> FxWrapper:
    """Convert an :class:`FXResult` to the renderer wrapper dict."""
    return {
        "rates": dict(result.rates),
        "date": result.as_of,
        "source": result.source,
    }


def fetch_fx_rates(
    date_str: str | None = None,
    currencies: list[str] | None = None,
) -> FxWrapper | None:
    """Fetch FX rates as a renderer wrapper dict (SGD-per-unit rates).

    With an empty *currencies* list, all available currencies are fetched.

    The resolved wrapper dict is cached in memory and on disk (via
    :mod:`pfa_fx.cache`) keyed by *date_str* so it is reused offline across
    report runs and months. The ``None`` ("latest") case is fetched but not
    cached, since it has no stable key. Returns the canonical
    ``{"rates": {CCY: SGD per 1 unit}, "date": str, "source": str}`` shape
    (degrading to hardcoded fallback rates on failure).
    """
    if date_str is not None:
        cached = _WRAPPER_CACHE.get(date_str)
        if cached is None:
            cached = _wrapper_cache_get(date_str)
        if cached is not None:
            _WRAPPER_CACHE[date_str] = cached
            return cached
    # Network fetch via pfa_fx (raw rates are themselves cached in pfa_fx.cache,
    # degrading to hardcoded fallback internally).
    requested = currencies if currencies is not None else DEFAULT_WATCH_SYMBOLS
    result = get_fx_rates(requested, as_of=date_str)
    wrapper = fx_result_to_wrapper(result)
    if date_str is not None:
        # Persist for offline reuse (covers the no-network / API-down gap).
        _wrapper_cache_put(date_str, wrapper)
        _WRAPPER_CACHE[date_str] = wrapper
    return wrapper
