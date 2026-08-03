# pfa-categorize

Categorize consolidated bank-statement transactions into spending/income
categories, using a **rule-first** classifier (YAML keyword rules) with an
optional **LLM fallback** for anything uncategorized. It also detects external
transfers so they are not double-counted as spend or income.

This package consumes the consolidated IR JSON produced by
[`pfa-ir-consolidator`](../pfa-ir-consolidator) and emits a `categories.json`
that the consolidator can merge back into the IR. The original `txn-categorize`
skill has been absorbed into this package.

## What it does

- **Rule-first classification** — matches each transaction's description against
  a YAML rules file (case- and punctuation-insensitive). First match wins.
- **Two-level categories** — categories use a `"Class: Subtype"` form
  (e.g. `Income: Salary`, `Expense: Groceries`, `Transfer: External`); flat
  category names are still supported for backward compatibility.
- **External transfer detection** — finds withdrawals/deposits that reference
  another institution's account number (or contain transfer keywords), and
  overrides the rule category with `Transfer: External`. Pairs incoming deposits
  to known outgoing transfers by amount + destination + within 3 days.
- **Parser hints** — reuses category hints the parser already attached
  (`salary`, `dividend`, `interest`, `fd_interest`, `investment`, `groceries`).
- **LLM fallback** — optionally classifies only the still-uncategorized
  transactions via an OpenAI-compatible chat API, constrained to the existing
  category list.

## Inputs

- A consolidated IR JSON (`consolidated.ir.json`) from `pfa-ir-consolidator`.
- A rules YAML file (defaults to `references/categories.yaml` shipped with the
  package). Example shape:

  ```yaml
  categories: [Income, Transfer, Groceries, Dining, Transport]
  rules:
    - category: Income: Salary
      match: [SALARY, PAYROLL, GIRO PAY]
    - category: Expense: Groceries
      match: [NTUC, FAIRPRICE, COLD STORAGE]
  ```

## Install

```bash
pip install -e packages/pfa-ir-schema   # dependency
pip install -e packages/pfa-parser       # dependency (for IR schema)
pip install -e packages/pfa-ir-consolidator
pip install -e packages/pfa-categorize
```

Requires Python >= 3.12, plus `pfa-ir-schema` and `pyyaml` (installed
automatically). The LLM fallback additionally needs `requests` and an
`OPENAI_API_KEY`.

## Usage

### CLI

```bash
python -m pfa_categorize.categorize consolidated.ir.json -o categories.json
# with a custom rules file:
python -m pfa_categorize.categorize consolidated.ir.json -o categories.json --rules my_rules.yaml
# with LLM fallback for uncategorized items:
python -m pfa_categorize.categorize consolidated.ir.json -o categories.json --llm --model gpt-4o-mini
```

Options:

- `input` — consolidated IR JSON from `pfa-ir-consolidator`.
- `-o/--output` — output `categories.json` (required).
- `--rules PATH` — YAML rules file (default: `references/categories.yaml`).
- `--llm` — use an OpenAI-compatible LLM for uncategorized transactions.
- `--model NAME` — LLM model (default `gpt-4o-mini`).
- `--api-key KEY` — API key (default `$OPENAI_API_KEY`).
- `--base-url URL` — OpenAI-compatible base URL (default `$OPENAI_BASE_URL` or
  `https://api.openai.com/v1`).

### Programmatic API

```python
from pfa_categorize import categorize, parse_input, classify_by_rules
from pfa_categorize.ir import parse_ir

# Full pipeline: write categories.json
categorize(input_path, rules_path, use_llm=False, model="gpt-4o-mini")

# Or lower-level:
txns, accounts_raw, meta = parse_input(input_path)   # -> list[TxnRow], ...
category = classify_by_rules(txn, rules)             # -> "Expense: Groceries" | None
```

`parse_ir` (from `pfa_categorize.ir`) normalizes a consolidated IR JSON into a
`TxnIR` dataclass aligned with `pfa_ir_schema.ir_schema`, so the categorizer
never depends on parser internals.

## Repo layout

```
pfa-categorize/
├── pyproject.toml        # hatchling build; depends on pfa-ir-schema, pyyaml
├── references/
│   └── categories.yaml   # default categorization rules
├── tests/
└── src/
    └── pfa_categorize/
        ├── __init__.py   # categorize, parse_input, classify_by_rules
        ├── ir.py         # consolidated IR JSON parser (TxnIR)
        ├── categorize.py # rule engine, transfer detection, LLM fallback, CLI
        └── render_md.py  # category breakdown Markdown
```

## Output format (`categories.json`)

A mapping of `txn_id -> category` (plus any metadata), suitable for
`pfa-ir-consolidator`'s `merge_categories` to fold back into the consolidated IR.

## License

MIT © 2026 Zhou Shaowen
