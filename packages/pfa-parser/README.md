# pfa-parser

Parse Singapore bank statement PDFs (DBS/POSB, OCBC, UOB, ICBC) into structured
Markdown tables and a machine-readable IR JSON. No OCR, no manual cleanup.

This package contains all PDF extraction logic and IR→Markdown rendering. It
produces `pfa_ir_schema.ParsedStatement` objects (defined in the
[`pfa-ir-schema`](../pfa-ir-schema) package) and is consumed by the
consolidation, categorization, and analysis packages in the monorepo.

The original `sg_bank_pdf_parser` library has been absorbed into this package;
its IR data models now live in `pfa-ir-schema`.

## Supported banks / statement families

Detection is explicit (regex/coordinate rules per family) with no fallback — an
unrecognized PDF is reported as unsupported rather than silently mis-parsed.

- **DBS / POSB** — consolidated (Account Summary + multi-account Transaction
  Details, Fixed Deposits, etc.)
- **OCBC** — consolidated · credit card
- **ICBC** — consolidated (bilingual multi-currency statement)
- **UOB** — single-account (`txn`) · multi-account (`one`) · portfolio summary

## Install

```bash
pip install -e packages/pfa-ir-schema   # dependency, install first
pip install -e packages/pfa-parser
```

Requires Python >= 3.12, plus `pdfplumber` and `pandas` (installed
automatically).

## CLI

```bash
python -m pfa_parser <input.pdf|input.ir.json> [output.md] [--no-mask] [--ir-only]
```

- `input.pdf` — the bank statement PDF.
- `input.ir.json` — an IR JSON previously produced by this tool; skips PDF
  extraction and renders directly.
- `output.md` — optional; defaults to `<input>.md`.
- `--no-mask` — disable masking (account numbers, NRIC/FIN, person names).
  Masking is on by default.
- `--ir-only` — write only the IR JSON (for PDF input) or validate and exit
  (for IR JSON input); no Markdown is produced.

Two files are produced alongside the input:

- `<input>.md` — human-readable Markdown tables (sensitive data masked).
- `<input>.ir.json` — schema-versioned `ParsedStatement` IR
  (`ir_version`, `statement_meta`, `accounts[]`, `warnings[]`) for downstream
  cashflow analysis and multi-bank consolidation.

## Programmatic API

```python
from pfa_parser import SGBankPDFParser, detect_type, ParsedStatement

# Facade: parse a PDF into a list of Transaction objects
parser = SGBankPDFParser()
if parser.supports_format("statement.pdf"):
    transactions = parser.parse("statement.pdf")

# Or run the full pipeline (PDF -> IR -> MD) and get the ParsedStatement
from pfa_parser.convert_statement import run
statement: ParsedStatement = run("statement.pdf")
print(statement.statement_meta.bank, statement.statement_meta.family)
```

`detect_type` returns a `(bank, family)` tuple, e.g.
`("dbs", "consolidated")`, `("ocbc", "card")`, `("uob", "one")`.

## Masking

On by default: account/card/deposit numbers show only the last 4 digits; long
numeric IDs (4+ digits) in descriptions become `[ID-XXXX]`; NRIC/FIN is fully
replaced with `[NRIC]`; person names are masked context-aware (UEN, bank codes,
and reference numbers are preserved). Masking is applied at **render time**, so
the IR JSON always stores unmasked raw data.

## Repo layout

```
pfa-parser/
├── pyproject.toml          # hatchling build; depends on pfa-ir-schema
├── tests/                  # unit tests (test_base.py, test_sg_parser.py)
└── src/
    └── pfa_parser/
        ├── __init__.py         # public API: SGBankPDFParser, detect_type, ...
        ├── base.py             # BankStatementParser / Transaction interfaces
        ├── convert_statement.py# CLI entry + auto-detect/dispatch pipeline
        ├── ir_builder.py       # chainable IRBuilder
        ├── postprocess.py      # balance/FD/txn-link verification & filling
        ├── sg_parser.py        # SGBankPDFParser facade
        ├── extractors/         # IR extraction + (bank, family) registry
        ├── parsers/            # bank-specific pdfplumber logic
        └── renderers/          # per-family Markdown renderers + masking
```

## Development

```bash
pip install -e packages/pfa-parser
pytest packages/pfa-parser/tests
```

## License

MIT © 2026 Zhou Shaowen
