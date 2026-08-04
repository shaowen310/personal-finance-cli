# pfa-analysis

Transaction categorization, balance-sheet analysis, and cash-flow reporting for
`personal-finance-cli`. Produces a single **finance report** (Markdown) from a
consolidated IR JSON.

This package is the final stage of the pipeline. It consumes the consolidated
IR JSON from [`pfa-ir-consolidator`](../pfa-ir-consolidator) and a rules file
(`categories.yaml`), then renders an SGD-denominated report. The former
`pfa-categorize` and `personal-finance-analysis` packages have been merged into
this package.

## What it does

- **Auto-categorization** — rule-based transaction categorization via
  `categories.yaml` (description/amount/account patterns with regex support).
- **Balance sheet** — assets (cash, time deposits, investments) and liabilities
  (credit-card balances), per currency, with an account-level drill-down that
  reconciles to the totals.
- **Cash-flow statement** — per-currency income, expense, transfer in, and
  transfer out, reconciled to the balance change.
- **Income / Expense / Transfer breakdowns** — per-category drill-down tables
  with individual transaction details.
- **Categorization summary** — Class × Category counts with coverage
  percentage.
- **FX conversion** — converts every currency to SGD-equivalent using
  period-end rates from the Frankfurter API via the shared `pfa-fx` package
  (ECB data, free, keyless), with graceful degradation.
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

Requires Python >= 3.12, plus `pyyaml`.

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

The report contains: **Executive Summary**, **Categorization Summary**,
**Balance Sheet** (per currency, with drill-down), **Cash Flow Statement**
(per currency, reconciled), **Income Breakdown**, **Expense Breakdown**,
**Transfer Breakdown**, **Key Observations**, **FX Rates Reference**, and
**Notes & Caveats**.

### Programmatic API

```python
from pfa_analysis import (
    analyze_statement, compute_metrics, build_assets,
    build_dashboard_json, classify_cash_flow,
    render_report, convert_to_sgd,
)
from pfa_analysis.analyze import (
    load_statement, render_consolidated_report,
    build_income_expense_drilldowns, build_transfer_drilldown,
)

# Analysis
meta, txns = load_statement(path)                 # auto-detect schema
result = analyze_statement(raw, meta, txns, path)
md = render_report(result)                        # -> Markdown string

# Full report with categorization
md = render_consolidated_report(
    consolidated_path, categories_path,
    start_date="2026-06-01", end_date="2026-06-15",
)

# Categorization
from pfa_analysis.categorize import categorize
result = categorize(ir_path, rules_path, output_path)
```

Key functions:

- `load_statement(path, start_date, end_date)` — load + normalize to
  `(meta, list[Txn])`, auto-detecting consolidated / `.ir.json` / default
  schema. Supports date-range filtering.
- `compute_metrics(txns, meta, ccy, ...)` — per-currency income, expense,
  transfers, net operating, savings rate, opening/closing, reconciliation flag.
  Supports `use_txn_balances` mode for date-filtered statements.
- `build_assets(meta, metrics_by_ccy)` — per-currency cash / time deposits /
  investments / liabilities.
- `classify_cash_flow(txn)` — `Income` / `Expense` / `Transfer In` /
  `Transfer Out`.
- `convert_to_sgd(amount, currency, fx_rates)` — SGD conversion.
- `build_dashboard_json(...)` — structured dashboard payload for UIs.
- `categorize(ir_path, rules_path, output_path)` — rule-based transaction
  categorization, outputs `categories.json`.
- `build_income_expense_drilldowns(path, categories_path, ...)` — category-level
  drilldown for income and expense.
- `build_transfer_drilldown(path, categories_path, ...)` — category-level
  drilldown for transfers.

## Categorization rules

Rules are defined in `references/categories.yaml`. Each rule specifies:

```yaml
rules:
  - category: "Expense: Dining"
    description_pattern: "(?i).*mcdonald.*|.*kfc.*"
    min_amount: 0
    max_amount: 500
    account_pattern: ""
```

## FX & caveats

- FX rates come from `https://api.frankfurter.dev/v1` (base SGD), via the
  shared `pfa-fx` package, priced at the statement period-end date. If the
  fetch fails, the report degrades gracefully.
- Reports always carry a **Notes & Caveats** section.

## Repo layout

```
pfa-analysis/
├── pyproject.toml     # hatchling build; depends on pfa-ir-schema, pfa-fx, pyyaml
├── references/
│   └── categories.yaml  # auto-categorization rules
├── tests/
└── src/
    └── pfa_analysis/
        ├── __init__.py       # analyze_statement, compute_metrics, build_assets, ...
        ├── analyze.py        # schema loading, metrics, balance sheet, drilldowns, FX
        ├── categorize.py     # rule-based transaction auto-categorization
        ├── categorize_ir.py  # consolidated IR parser for categorization
        └── render_md.py      # Markdown report renderer (FX_BASE = "SGD")
```

## License

MIT © 2026 Zhou Shaowen
