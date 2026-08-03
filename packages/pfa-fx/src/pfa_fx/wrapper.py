"""Convenience wrappers producing the dict shape used by renderers.

Some callers (``pfa-analysis`` renderers) expect an FX rate object shaped as::

    {"rates": {CCY: <SGD per 1 unit>}, "date": "...", "source": "..."}

This module adapts :func:`pfa_fx.rates.get_fx_rates` to that shape so the
analysis package needs almost no structural change.
"""

from __future__ import annotations

from typing import Any

from .defaults import BASE_CCY
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
    result = get_fx_rates(currencies or [], as_of=date_str)
    return fx_result_to_wrapper(result)


def extract_embedded_fx(rates_sgd_per_unit: dict[str, float], as_of: str = "") -> dict[str, Any]:
    """Wrap already-SGD-per-unit embedded rates (e.g. from a consolidated IR)."""
    merged = {BASE_CCY: 1.0, **{k.upper(): float(v) for k, v in rates_sgd_per_unit.items()}}
    merged[BASE_CCY] = 1.0
    return {"rates": merged, "date": as_of, "source": "embedded in consolidated IR"}
