"""FX providers (data sources).

A provider knows how to fetch rates for a set of currencies "as of" a date and
return them in the **canonical pfa-fx shape**: SGD per 1 unit of foreign
currency (``"SGD": 1.0``).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Protocol, TypedDict, runtime_checkable

from .cache import _as_dict
from .defaults import BASE_CCY

# Latest Frankfurter API base (ECB data, free, no key).
DEFAULT_BASE_URL = os.environ.get("PFA_FX_BASE_URL", "https://api.frankfurter.dev/v1")


class FXFetchResult(TypedDict):
    """Canonical provider result: SGD per 1 unit, plus provenance."""

    rates: dict[str, float]
    date: str
    source: str


@runtime_checkable
class FXProvider(Protocol):
    """Protocol for an FX rate source."""

    name: str

    def fetch(self, currencies: list[str], as_of: str | None = None) -> FXFetchResult | None:
        """Return a Frankfurter-shaped dict (units per 1 SGD) or None.

        The returned ``rates`` mapping is **units per 1 SGD** (because
        Frankfurter is queried with ``base=SGD``). The caller
        (``pfa_fx.rates``) inverts it to SGD-per-unit.
        """
        ...


def _http_get_json(url: str) -> dict[str, object] | None:
    req = urllib.request.Request(
        url, headers={"User-Agent": "personal-finance-cli/1.0"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - https only
        text = resp.read().decode("utf-8")
    parsed: object = json.loads(text)
    return _as_dict(parsed) if isinstance(parsed, dict) else None


class FrankfurterProvider:
    """Fetch rates from the Frankfurter API (ECB reference rates, no key)."""

    name: str = "frankfurter"

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url: str = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def fetch(self, currencies: list[str], as_of: str | None = None) -> FXFetchResult | None:
        syms = [c for c in currencies if c and c.upper() != BASE_CCY]
        if not syms:
            result: FXFetchResult = {
                "rates": {BASE_CCY: 1.0},
                "date": as_of or "",
                "source": self.base_url,
            }
            return result
        symbols_csv = ",".join(syms)
        if as_of:
            url = f"{self.base_url}/{as_of}?from={BASE_CCY}&to={symbols_csv}"
        else:
            url = f"{self.base_url}/latest?from={BASE_CCY}&to={symbols_csv}"
        try:
            data = _http_get_json(url)
        except Exception as e:  # noqa: BLE001 - degrade, never crash the caller
            print(
                f"[WARN] Failed to fetch FX rates from {self.name}: {e}",
                file=sys.stderr,
            )
            return None
        if not isinstance(data, dict):
            return None
        rates = _as_dict(data.get("rates"))
        result: FXFetchResult = {
            "rates": {
                str(k).upper(): float(v) if isinstance(v, (int, float)) else 0.0
                for k, v in rates.items()
            },
            "date": str(data.get("date") or as_of or ""),
            "source": self.base_url,
        }
        return result


# Registry of available providers.
PROVIDERS: dict[str, type[FrankfurterProvider]] = {
    "frankfurter": FrankfurterProvider,
}


def get_provider(key: str | None = None) -> FrankfurterProvider:
    """Return a provider instance.

    Selection order:
      1. explicit ``key`` (must be in :data:`PROVIDERS`)
      2. ``PFA_FX_PROVIDER`` env var
      3. ``frankfurter`` (default)
    """
    selected = (key or os.environ.get("PFA_FX_PROVIDER") or "frankfurter").lower()
    cls = PROVIDERS.get(selected, FrankfurterProvider)
    return cls()
