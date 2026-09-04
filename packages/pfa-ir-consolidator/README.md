# pfa-ir-consolidator

Consolidate multiple bank-statement IR JSON files (`*.ir.json`) into a single
consolidated IR ready for analysis and reporting.

This package takes the per-statement `ParsedStatement` objects produced by
[`pfa-parser`](../pfa-parser), merges them, detects internal transfers between
accounts/banks, and applies FX conversion. It is
the middle stage of the `personal-finance-cli` pipeline (between `pfa-parser`
and the analysis package; report rendering — including the balance sheet and
net position — lives in [`pfa-analysis`](../pfa-analysis)).

The original `bank-ir-consolidate` skill has been absorbed into this package.

## What it does

- **Consolidate** — merge N `*.ir.json` files into one `ParsedStatement`,
  grouping accounts by `(institution, account_no, name)` and de-duplicating
  transactions by `txn_id` (handles overlapping statement periods).
- **Transfer detection** — flag internal transfers (inter-bank FAST,
  intra-bank CA→SA, currency conversions, credit-card payments) so net-worth
  math doesn't double-count.
- **FX** — convert non-SGD balances to SGD-equivalent via live mid-market
  rates (with cache + fallback), never crashes on network failure.

Categorization (rule-first with LLM fallback) and report rendering live in
[`pfa-analysis`](../pfa-analysis), which consumes the consolidated IR directly.

## Supported inputs

- Multiple IR JSON files produced by `pfa-parser` (matching `*.ir.json`).
- Banks supported upstream: DBS, OCBC, UOB, ICBC.
- IR schema must satisfy `ir_version >= 2026.4` (older versions are refused by
  the `from_json` gate).

## Install

```bash
pip install -e packages/pfa-ir-schema   # dependency
pip install -e packages/pfa-parser       # dependency
pip install -e packages/pfa-ir-consolidator
```

Requires Python >= 3.12, plus `pfa-ir-schema`, `pfa-parser`, and `pyyaml`.

## Usage

### 1. Consolidate

```bash
python -m pfa_ir_consolidator.consolidate a.ir.json b.ir.json c.ir.json -o consolidated.ir.json
```

- De-duplicates transactions by `txn_id` within each
  `(institution, account_no, name)` group.
- Carries forward the **minimum** `ir_version` and refuses IR older than
  `2026.4`.
- Stores provenance in `extras.consolidation.sources` (per-source file, parser,
  parsed_at, ir_version, institution, account/txn counts) plus dedup count.

Options: `-o/--output`, `--min-ir-version VER`, `--no-dedup`, `--indent N`.

### FX rates (on-demand, cached)

- FX rates are **not hardcoded**; `consolidate` fetches them via
  [`pfa-fx`](../pfa-fx) and embeds them into the consolidated IR.
- Fetches mid-market SGD-per-unit rates for every non-SGD currency in the
  accounts, cached per `(provider, date)`.
- Default provider: **Frankfurter** (`https://api.frankfurter.dev`, free,
  keyless, historical dates supported).
- Falls back: network fail → previous cache → hardcoded defaults (never
  crashes). Weekend/holiday: steps back up to 5 days to the last trading day.
- FX provenance is stored in `extras.consolidation.fx` so downstream rendering
  and the categorizer never need to hit the network again.

## Programmatic API

```python
from pfa_ir_consolidator import consolidate_statements
from pfa_ir_schema import from_json, to_json

# Merge multiple IR JSON files into one fully-linked, reconciled ParsedStatement.
# consolidate_statements runs the link_* + verify/demote pipeline internally, so
# callers only invoke this single function. Each input is a (path, statement) tuple.
stmts = [
    (p, from_json(open(p, encoding="utf-8").read()))
    for p in ir_paths
]
consolidated = consolidate_statements(stmts)

out = to_json(consolidated)
```

## Repo layout

```
pfa-ir-consolidator/
├── pyproject.toml            # hatchling build; depends on pfa-ir-schema, pfa-parser, pfa-fx, pyyaml
├── tests/
└── src/
    └── pfa_ir_consolidator/
        ├── __init__.py       # consolidate_statements, main
        ├── consolidate.py    # merge + dedup + provenance
        ├── link_transfers.py  # inter/intra-bank, currency conversion, CC payment linking
        └── fx_rates.py       # back-compat shim → pfa-fx (live FX fetch + cache + fallback)
```

## Public IR contract

The IR schema is defined by [`pfa-ir-schema`](../pfa-ir-schema). Downstream
consumers must require `ir_version >= 2026.4`.

## License

MIT © 2026 Zhou Shaowen
