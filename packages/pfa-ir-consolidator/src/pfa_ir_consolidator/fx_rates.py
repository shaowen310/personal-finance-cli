"""Back-compat shim.

FX logic has moved to the standalone ``pfa-fx`` leaf package. This module
re-exports the symbols previously defined here so existing imports keep working
for one release. New code should import from :mod:`pfa_fx` directly.

The old implementation also carried a ``sys.path`` hack and a reverse import of
``DEFAULT_FX_RATES`` from ``render_model``; both are gone now that the data
lives in ``pfa_fx.defaults``.
"""

from __future__ import annotations

from pfa_fx import (  # noqa: F401
    DEFAULT_FX_RATES,
    DEFAULT_WATCH_SYMBOLS,
    FXResult,
    collect_currencies,
    convert_to_sgd,
    get_fx_rates,
    get_provider,
)

__all__ = [
    "DEFAULT_FX_RATES",
    "DEFAULT_WATCH_SYMBOLS",
    "FXResult",
    "collect_currencies",
    "convert_to_sgd",
    "get_fx_rates",
    "get_provider",
]
