"""Fetch and normalise FX rates into the canonical pfa-fx shape.

Canonical shape: **SGD per 1 unit of foreign currency** (``"SGD": 1.0``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Protocol

from .cache import cache_get, cache_put
from .defaults import BASE_CCY, DEFAULT_FX_RATES
from .providers import FXProvider, get_provider

_LOGGER = logging.getLogger(__name__)

# Working copy of the fallback rates; may be augmented by PFA_FX_FALLBACK.
_fallback_rates: dict[str, float] = dict(DEFAULT_FX_RATES)

ALLOW_FETCH = os.environ.get("PFA_FX_FETCH", "1") != "0"
# When PFA_FX_FALLBACK is set, it overrides the hardcoded DEFAULT_FX_RATES.
_ENV_FALLBACK = os.environ.get("PFA_FX_FALLBACK")
if _ENV_FALLBACK:
    try:
        _parsed = {str(k).upper(): float(v) for k, v in json.loads(_ENV_FALLBACK).items()}
        if _parsed:
            _fallback_rates = {**_fallback_rates, **_parsed}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass


@dataclass
class FXResult:
    """Result of :func:`get_fx_rates`."""

    rates: dict[str, float]                # currency → SGD per 1 unit (SGD = 1.0)
    source: str = ""
    provider: str = ""
    as_of: str = ""
    fetched_at: float = 0.0
    missing: list[str] = field(default_factory=list)


class _AccountLike(Protocol):
    """An account exposing a ``currency`` attribute (str or str-convertible)."""

    currency: str


class _StatementLike(Protocol):
    """Statement-like object exposing an ``accounts`` list for FX collection."""

    accounts: list[_AccountLike]


def collect_currencies(stmt: _StatementLike) -> list[str]:
    """Collect distinct non-SGD currency codes from a statement-like object.

    Duck-typed: works with ``ParsedStatement`` (pfa-ir-schema) as well as plain
    dicts/lists, requiring no import of business packages.
    """
    currencies: set[str] = set()

    def add(acc: _AccountLike) -> None:
        cur = getattr(acc, "currency", None)
        if cur and str(cur).upper() != BASE_CCY:
            currencies.add(str(cur).upper())

    try:
        accounts = getattr(stmt, "accounts", None)
        if accounts is None and isinstance(stmt, dict):
            accounts = stmt.get("accounts")
        if accounts is not None:
            for acc in accounts:
                add(acc)
    except (TypeError, AttributeError) as exc:  # never let FX break statement collection
        _LOGGER.warning("collect_currencies: skipped FX currency collection: %s", exc)
    return sorted(currencies)


def _invert_to_sgd_per_unit(raw: dict[str, object], _as_of: str = "") -> dict[str, float]:
    """Convert Frankfurter's units-per-SGD into canonical SGD-per-unit.

    ``raw["rates"][ccy]`` = how many *ccy* per 1 SGD. We want SGD per 1 ccy,
    so invert: ``1.0 / (ccy per SGD)``. SGD itself stays ``1.0``.
    """
    out: dict[str, float] = {BASE_CCY: 1.0}
    rates = raw.get("rates")
    if not isinstance(rates, dict):
        return out
    for k, v in rates.items():
        ccy = str(k).upper()
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        if ccy == BASE_CCY:
            out[ccy] = 1.0
        elif val and val > 0:
            out[ccy] = 1.0 / val
    return out


def get_fx_rates(
    currencies: list[str],
    as_of: str | None = None,
    provider: FXProvider | None = None,
    force_refresh: bool = False,
) -> FXResult:
    """Get FX rates (SGD per 1 unit) for *currencies*, as of *as_of*.

    Resolution order per currency:
      1. cached fetch (permanent; historical rates never change)
      2. live fetch via provider (if ``PFA_FX_FETCH != 0``), unless *force_refresh*
      3. hardcoded :data:`_fallback_rates` fallback

    Cached entries are treated as permanently valid (no TTL). Pass
    *force_refresh* to ignore the cache and re-fetch live rates.

    Returns an :class:`FXResult`. ``missing`` lists currencies that had to fall
    back to the hardcoded default.

    When *currencies* is empty, the provider is asked for **all** currencies
    available for the date (Frankfurter returns everything when no ``to`` list
    is given).
    """
    import time

    prov = provider or get_provider()
    rates: dict[str, float] = {}
    missing: list[str] = []
    source = ""
    fetched_at = 0.0
    prov_name = getattr(prov, "name", "default")

    # Fast path: no specific currencies requested -> fetch everything once.
    if not currencies:
        if ALLOW_FETCH and hasattr(prov, "fetch"):
            fetched = prov.fetch([], as_of=as_of)  # type: ignore[union-attr]
            if fetched and isinstance(fetched.get("rates"), dict):
                norm = _invert_to_sgd_per_unit(fetched)
                source = str(fetched.get("source", prov_name))
                fetched_at = time.time()
                for ccy, val in norm.items():
                    if ccy == BASE_CCY:
                        rates[ccy] = 1.0
                        continue
                    rates[ccy] = val
                    # Persist each fetched currency so a later call that requests
                    # only a subset can serve it from cache without re-fetching.
                    cache_put(
                        prov_name,
                        f"{as_of or 'latest'}:{ccy}",
                        {
                            "rate": val,
                            "source": source,
                            "as_of": str(fetched.get("date") or as_of or ""),
                            "fetched_at": fetched_at,
                        },
                    )
                missing = [c for c in rates if c != BASE_CCY and c not in _fallback_rates]
                return FXResult(
                    rates=rates, source=source or "frankfurter",
                    provider=prov_name, as_of=as_of or "",
                    fetched_at=fetched_at, missing=missing,
                )
        # No fetch or fetch failed -> fall back to defaults only.
        rates = {BASE_CCY: 1.0, **{k: float(v) for k, v in _fallback_rates.items()}}
        return FXResult(
            rates=rates, source="hardcoded fallback", provider=prov_name,
            as_of=as_of or "", fetched_at=0.0,
            missing=sorted(k for k in _fallback_rates if k != BASE_CCY),
        )

    for ccy in sorted(set(currencies)):
        ccy = str(ccy).upper()
        if ccy == BASE_CCY:
            rates[ccy] = 1.0
            continue
        cache_key = f"{as_of or 'latest'}:{ccy}"
        entry = (
            cache_get(prov_name, cache_key)
            if (ALLOW_FETCH and not force_refresh)
            else None
        )
        if entry is None and ALLOW_FETCH:
            fetched = prov.fetch([ccy], as_of=as_of) if hasattr(prov, "fetch") else None  # type: ignore[union-attr]
            if fetched and isinstance(fetched.get("rates"), dict):
                norm = _invert_to_sgd_per_unit(fetched)
                if ccy in norm:
                    rate = norm[ccy]
                    fetched_at = time.time()
                    entry = {
                        "rate": rate,
                        "source": fetched.get("source", prov_name),
                        "as_of": str(fetched.get("date") or as_of or ""),
                        "fetched_at": fetched_at,
                    }
                    cache_put(prov_name, cache_key, entry)
        if entry is not None:
            try:
                rates[ccy] = float(entry["rate"])
                if not source:
                    source = str(entry.get("source", prov_name))
                if not fetched_at:
                    fetched_at = float(entry.get("fetched_at", 0.0))
                continue
            except (TypeError, ValueError, KeyError):
                pass
        # Fallback.
        if ccy in _fallback_rates:
            rates[ccy] = float(_fallback_rates[ccy])
            missing.append(ccy)
        # else: leave absent; caller can handle.

    if not source:
        source = "hardcoded fallback"
    return FXResult(
        rates=rates,
        source=source,
        provider=prov_name,
        as_of=as_of or "",
        fetched_at=fetched_at,
        missing=missing,
    )
