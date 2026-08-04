# pfa-ir-consolidator

Consolidate multiple bank-statement IR JSON files (`*.ir.json`) into a single
consolidated IR and render it as a human-readable, cross-bank Markdown summary.

This package takes the per-statement `ParsedStatement` objects produced by
[`pfa-parser`](../pfa-parser), merges them, detects internal transfers between
accounts/banks, applies FX conversion, and renders a consolidated report. It is
the middle stage of the `personal-finance-cli` pipeline (between `pfa-parser`
and the analysis package).

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
- **Category merge** — fold categorizer output (`categories.json`, produced
  by `pfa-analysis`) back into the consolidated IR.
- **Render** — emit a cross-bank Markdown summary (net position, per-account
  tables, FD records, investment holdings) with masking on by default.

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

### 2. Render to Markdown

```bash
python -m pfa_ir_consolidator.render_md consolidated.ir.json -o consolidated.md
```

- Net Position (SGD-equivalent via FX rates, plus per-currency native
  balances).
- Per-bank, per-account transaction tables, FD records, investment holdings.
- Masking on by default; use `--no-mask` to disable.

Options: `-o/--output`, `--no-mask`, `--fx-date YYYY-MM-DD`,
`--fx-cache-dir DIR`, `--fx-provider NAME` (default `frankfurter`),
`--fx-offline`, `--fx-force-refresh`, `--fx-no-embed`.

### FX rates (on-demand, cached)

- FX rates are **not hardcoded**; fetched live at render time.
- Fetches mid-market SGD-per-unit rates for built-in watch-list
  (`USD, JPY, CNY`) + every non-SGD currency in accounts.
- Cached per `(provider, date)` under `--fx-cache-dir` (default `./cache/`,
  git-ignored).
- Default provider: **Frankfurter** (`https://api.frankfurter.dev`, free,
  keyless, historical dates supported).

```bash
python -m pfa_ir_consolidator.render_md consolidated.ir.json -o consolidated.md \
  --fx-date 2026-06-30 --fx-provider frankfurter --fx-cache-dir ./cache
```

- Falls back: network fail → previous cache → hardcoded `DEFAULT_FX_RATES`
  (never crashes).
- Weekend/holiday: steps back up to 5 days to the last trading day.
- FX provenance embedded into `extras.consolidation.fx` (skip with
  `--fx-no-embed`). The Markdown FX table shows live / cached / fallback +
  effective date.

## Programmatic API

```python
from pfa_ir_consolidator import consolidate_statements
from pfa_ir_consolidator.detect_transfers import (
    detect_inter_bank_transfers,
    detect_intra_bank_transfers,
    detect_currency_conversions,
    detect_cc_payments,
)
from pfa_ir_schema import from_json, to_json

# Merge multiple IR JSON strings into one ParsedStatement
irs = [from_json(open(p, encoding="utf-8").read()) for p in ir_paths]
consolidated = consolidate_statements(irs)

# Flag internal transfers (run after consolidation, before verify_txn_links)
detect_inter_bank_transfers(consolidated)
detect_intra_bank_transfers(consolidated)
detect_currency_conversions(consolidated)
detect_cc_payments(consolidated)

out = to_json(consolidated)
```

Helper entry points also available:

- `export_model.export_render_model(ir, ...)` — serialize to the
  `RenderModel` JSON consumed by `pfa-analysis` categorization (no account-holder PII).
- `merge_categories.merge_categories(ir, categories_json, ...)` — fold
  categorizer output back into the consolidated IR.
- `render_model.build_render_model(ir, ...)` — build the in-memory render
  model.

## Repo layout

```
pfa-ir-consolidator/
├── pyproject.toml            # hatchling build; depends on pfa-ir-schema, pfa-parser, pfa-fx, pyyaml
├── tests/
└── src/
    └── pfa_ir_consolidator/
        ├── __init__.py       # consolidate_statements, main
        ├── consolidate.py    # merge + dedup + provenance
        ├── detect_transfers.py  # inter/intra-bank, currency conversion, CC payment detection
        ├── fx_rates.py       # back-compat shim → pfa-fx (live FX fetch + cache + fallback)
        ├── render_md.py      # consolidated IR -> Markdown
        ├── render_model.py   # in-memory render model
        ├── render_model_io.py
        ├── export_model.py   # consolidated IR -> RenderModel JSON (for categorizer)
        └── merge_categories.py  # fold categorizer output back into IR
```

## Public IR contract

The IR schema is defined by [`pfa-ir-schema`](../pfa-ir-schema). Downstream
consumers must require `ir_version >= 2026.4`.

## License

MIT © 2026 Zhou Shaowen
