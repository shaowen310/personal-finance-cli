"""SGD conversion helpers (canonical shape: SGD per 1 unit of currency)."""

from __future__ import annotations


def convert_to_sgd(amount: float, currency: str, fx_rates: dict[str, float]) -> float | None:
    """Convert *amount* in *currency* to SGD using *fx_rates*.

    ``fx_rates[ccy]`` is SGD per 1 unit of *ccy* (SGD = 1.0), so this multiplies.

    Returns ``None`` if the currency is absent from *fx_rates*.
    """
    rate = fx_rates.get(currency)
    if rate is None:
        return None
    return amount * rate


def sgd_equiv(balance: float, currency: str, fx_rates: dict[str, float]) -> float | None:
    """Alias for :func:`convert_to_sgd` (back-compat with pfa-ir-consolidator)."""
    return convert_to_sgd(balance, currency, fx_rates)
