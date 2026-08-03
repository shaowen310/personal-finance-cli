# pfa-analysis

Analyse personal-finance **balance sheet** and **cash flow** from
bank-statement JSON. Produces an executive summary, balance sheet (cash + time
deposits + investments, per currency), a cash-flow statement reconciled to the
balance change, key observations, and caveats.

This package is the final stage of the `personal-finance-cli` pipeline. It
consumes the consolidated IR JSON from
[`pfa-ir-consolidator`](../pfa-ir-consolidator) (or a single `.ir.json` / a
simple default schema) and renders an SGD-denominated Markdown report. The
original `personal-finance-analysis` skill has been absorbed into this package.

## What it does

- **Balance sheet** — assets (cash, time deposits, investments) and liabilities
  (credit-card balances), per currency, with an account-level drill-down that
  reconciles to the totals.
- **Cash-flow statement** — classified only as **Income / Expense / Transfer In
  / Transfer Out** (the minimum needed for an honest cash-flow statement; this
  package does *not* do merchant-level categorization — that's
  [`pfa-categorize`](../pfa-categorize)).
- **Reconciliation** — verifies `balance_change == net_change_cash` to within
  $0.005 and reports whether it ties out.
- **FX conversion** — converts every currency to SGD-equivalent using
  period-end rates from the Frankfurter API via the shared `pfa-fx` package
  (ECB data, free, keyless), with
  graceful degradation if the network is unavailable.
- **Sign conventions** — handles credit-card accounts where a *positive* amount
  is a charge (debit), and normal accounts where negative is a debit.

## Inputs

Auto-detected:

1. **Consolidated `.ir.json`** (from `pfa-ir-consolidator`) — tagged with a
   `consolidate` parser name; can contain multiple accounts/currencies.
2. **Single-statement `.ir.json`** (from `pfa-parser`) — has `statement_meta`.
3. **Default/simple schema** — `{account, period, balances, transactions}`.

## Install

```bash
pip install -e packages/pfa-ir-schema
pip install -e packages/pfa-parser
pip install -e packages/pfa-ir-consolidator
pip install -e packages/pfa-analysis
```

Requires Python >= 3.12, plus `pfa-ir-schema`. FX uses the standard library
(`urllib`) — no extra dependency.

## Usage

### CLI

```bash
# Single statement
python -m pfa_analysis.analyze statement.ir.json report.md

# Consolidated (multi-account, multi-currency) SGD report
python -m pfa_analysis.analyze consolidated.ir.json output_dir/

# Embedded synthetic demo data
python -m pfa_analysis.analyze --demo
```

The report contains: **Executive Summary**, **Balance Sheet** (per currency,
with drill-down), **Cash Flow Statement** (per currency, reconciled to balance),
**Key Observations**, and **Notes & Caveats**.

### Programmatic API

```python
from pfa_analysis import (
    analyze_statement, compute_metrics, build_assets,
    build_dashboard_json, classify_cash_flow,
    render_report, convert_to_sgd,
)
from pfa_analysis.analyze import load_statement

meta, txns = load_statement(path)            # auto-detect schema
result = analyze_statement(raw, meta, txns, path)
md = render_report(result)                    # -> Markdown string
```

Key functions:

- `load_statement(path)` — load + normalize to `(meta, list[Txn])`, auto-detecting
  consolidated / `.ir.json` / default schema.
- `compute_metrics(txns, meta, ccy, opening_override=None, closing_override=None)`
  — per-currency income, expense, transfers, net operating, savings rate,
  opening/closing, reconciliation flag.
- `build_assets(meta, metrics_by_ccy)` — per-currency cash / time deposits /
  investments / liabilities.
- `classify_cash_flow(txn)` — `Income` / `Expense` / `Transfer In` / `Transfer Out`.
- `convert_to_sgd(amount, currency, fx_rates)` — SGD conversion (returns `None`
  if FX unavailable).
- `build_dashboard_json(...)` — structured dashboard payload for UIs.

## FX & caveats

- FX rates come from `https://api.frankfurter.dev/v1` (base SGD), via the
  shared `pfa-fx` package, priced at the
  statement period-end date (supports historical dates). If the fetch fails, the
  report degrades gracefully and notes the limitation — it never crashes.
- Reports always carry a **Notes & Caveats** section (e.g. missing balances,
  unknown currencies, FX-unavailable) so figures are interpretable.

## Repo layout

```
pfa-analysis/
├── pyproject.toml    # hatchling build; depends on pfa-ir-schema, pfa-fx
├── tests/
└── src/
    └── pfa_analysis/
        ├── __init__.py   # analyze_statement, compute_metrics, build_assets,
        │                 #   build_dashboard_json, classify_cash_flow,
        │                 #   render_report, convert_to_sgd, fmt
        ├── analyze.py    # schema loading, metrics, balance sheet, FX fetch
        └── render_md.py  # Markdown report renderer (FX_BASE = "SGD")
```

## License

MIT © 2026 Zhou Shaowen
