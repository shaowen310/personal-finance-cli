"""Default constants for pfa-fx.

Rates are canonical: **SGD per 1 unit of foreign currency** (SGD = 1.0).
This matches ``pfa-ir-consolidator``'s ``DEFAULT_FX_RATES`` usage.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_CCY = "SGD"

# Default hardcoded fallback rates: SGD per 1 unit of foreign currency,
# mid-market as of period_to. Source: ValutaFX historical rates
# (1 SGD = 0.7745 USD, 125.82 JPY, 5.2473 CNY). Used only when the
# network fetch fails or is disabled.
DEFAULT_FX_RATES: dict[str, float] = {
    "SGD": 1.0,
    "USD": 1.29,   # 1 USD ≈ 1.29 SGD
    "JPY": 0.0079,  # 1 JPY ≈ 0.0079 SGD
    "CNY": 0.19,   # 1 CNY ≈ 0.19 SGD
}

# Currencies to watch / fetch by default when none are explicitly requested.
DEFAULT_WATCH_SYMBOLS: list[str] = ["SGD", "USD", "JPY", "CNY"]

# On-disk cache location. Defaults to an XDG-style user cache dir so that an
# editable install does not pollute the source tree and a site-packages install
# does not require write access to the package directory.
DEFAULT_CACHE_DIR: Path = Path(
    os.environ.get("PFA_FX_CACHE_DIR")
    or (Path.home() / ".cache" / "pfa" / "fx")
)
