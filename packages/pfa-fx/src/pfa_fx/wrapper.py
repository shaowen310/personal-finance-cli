"""Convenience wrappers producing the dict shape used by renderers.

Some callers (``pfa-analysis`` renderers) expect an FX rate object shaped as::

    {"rates": {CCY: <SGD per 1 unit>}, "date": "...", "source": "..."}

This module adapts :func:`pfa_fx.rates.get_fx_rates` to that shape so the
analysis package needs almost no structural change.
"""

from __future__ import annotations

from typing import Any

from .defaults import DEFAULT_WATCH_SYMBOLS
from .rates import FXResult, get_fx_rates


def fx_result_to_wrapper(result: FXResult) -> dict[str, Any]:
    """Convert an :class:`FXResult` to the renderer wrapper dict."""
    return {
        "rates": dict(result.rates),
        "date": result.as_of,
        "source": result.source,
    }


def fetch_fx_rates(
    date_str: str | None = None,
    currencies: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch FX rates as a renderer wrapper dict (SGD-per-unit rates).

    With an empty *currencies* list, all available currencies are fetched.
    """
    requested = currencies if currencies is not None else DEFAULT_WATCH_SYMBOLS
    result = get_fx_rates(requested, as_of=date_str)
    return fx_result_to_wrapper(result)
