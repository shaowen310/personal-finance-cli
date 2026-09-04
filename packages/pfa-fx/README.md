# pfa-fx

Cross-cutting FX (foreign-exchange) utilities for `personal-finance-cli`.

This is a **leaf package**: it has no internal dependencies (only the Python
standard library) and is meant to be imported by `pfa-ir-consolidator`,
`pfa-analysis`, and any future package that needs currency conversion.

## Why this package exists

FX rate fetching and SGD conversion are pure infrastructure — HTTP + on-disk
caching + hardcoded fallback — with zero `personal-finance-cli` business
semantics. Previously this logic lived inside `pfa-ir-consolidator/fx_rates.py`
and was *also* duplicated inside `pfa-analysis/analyze.py`. That was a design
smell: a "consolidator" should only merge IR JSONs, and the analysis package
should not reach into the consolidator just to get exchange rates.

## Canonical FX shape

All rates are **SGD per 1 unit of foreign currency**:

```python
{"USD": 1.29, "JPY": 0.0079, "CNY": 0.19, "SGD": 1.0}
```

`convert_to_sgd(amount, currency, rates)` therefore multiplies: `amount * rate`.
This matches `pfa-ir-consolidator`'s `DEFAULT_FX_RATES` and `txn_table_model`.
The Frankfurter API returns *units per 1 base currency*, so we invert to the
SGD-per-unit shape on retrieval.

## Usage

```python
from pfa_fx import get_fx_rates, convert_to_sgd, collect_currencies, DEFAULT_FX_RATES

# Discover which currencies appear in a statement (duck-typed / getattr).
currencies = collect_currencies(statement)

# Fetch live + cached + fallback rates (SGD-per-unit).
result = get_fx_rates(currencies, as_of="2026-06-30")

# Convert.
sgd = convert_to_sgd(100.0, "USD", result.rates)
```

## Environment variables

- `PFA_FX_CACHE_DIR` — directory for the on-disk rate cache
  (default: `~/.cache/pfa/fx/`).
- `PFA_FX_PROVIDER` — provider key to use (default: `frankfurter`).
- `PFA_FX_BASE_URL` — override the Frankfurter base URL
  (default: `https://api.frankfurter.dev/v1`).
- `PFA_FX_FALLBACK` — set to a JSON object to override `DEFAULT_FX_RATES`.
- `PFA_FX_FETCH` — set to `0` to disable network fetch (use fallback only).
