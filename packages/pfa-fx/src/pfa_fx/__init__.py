"""pfa-fx — FX rate retrieval and SGD conversion (leaf package, stdlib-only)."""

from .defaults import (
    BASE_CCY,
    DEFAULT_CACHE_DIR,
    DEFAULT_FX_RATES,
    DEFAULT_WATCH_SYMBOLS,
)
from .providers import FXProvider, FrankfurterProvider, PROVIDERS, get_provider
from .rates import FXResult, collect_currencies, get_fx_rates
from .convert import convert_to_sgd
from .wrapper import fetch_fx_rates, fx_result_to_wrapper

__all__ = [
    "BASE_CCY",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_FX_RATES",
    "DEFAULT_WATCH_SYMBOLS",
    "FXProvider",
    "FrankfurterProvider",
    "PROVIDERS",
    "get_provider",
    "FXResult",
    "collect_currencies",
    "get_fx_rates",
    "convert_to_sgd",
    "fetch_fx_rates",
    "fx_result_to_wrapper",
]
