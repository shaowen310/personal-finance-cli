# Personal Finance CLI

A personal finance analysis CLI tool (`pfa`) that connects bank statement
parsing, IR data consolidation, transaction categorization, and financial
analysis into a single workflow.

Supported banks: **DBS/POSB**, **OCBC**, **UOB**, **ICBC** (Singapore).

## Repository Structure

```
packages/
├── pfa-ir-schema/         # Shared IR data models (Account, Transaction, etc.)
├── pfa-parser/            # Bank statement PDF parser (detect → extract → IR)
├── pfa-fx/                # FX rate retrieval & SGD conversion (stdlib-only)
├── pfa-ir-consolidator/   # IR data consolidation & transfer detection
└── pfa-analysis/          # Categorization, financial analysis & reporting
apps/
└── pfa-cli/               # CLI entry point + workflow orchestration
```

## Quick Start

```bash
# Windows
powershell -File scripts/setup_dev.ps1

# macOS / Linux
bash scripts/setup_dev.sh

# Verify installation
pfa --help
```

## Commands

| Command | Description |
|---------|-------------|
| `pfa parse -i <file>` | Parse a bank statement PDF |
| `pfa analyze -m <YYYY-MM>` | Run financial analysis for a given month |
| `pfa run --full` | Execute the full pipeline |

## Pipeline

```bash
# Full pipeline (all PDFs in tests/cache/)
python tests/run_full_pipeline.py

# Date-filtered range
python tests/run_full_pipeline.py --start 2026-06-01 --end 2026-06-15
```

Outputs in `tests/outputs/`:
- `*.ir.json` — per-bank parsed IR
- `consolidated.ir.json` — merged & deduplicated IR
- `categories.json` — transaction → category mapping
- `finance_report.md` — balance sheet, cash flow, income/expense/transfer breakdowns

## Dependency Graph

```
pfa-ir-schema          (shared data models)
    ↑
pfa-parser             (PDF extraction, all SG bank support)
    ↑
pfa-ir-consolidator    (merge & deduplicate IR, detect transfers)
    ↑
pfa-analysis           (categorization, financial reports & visualization)
    ↑
pfa-cli                (CLI entry point)
```

## Tech Stack

Python >= 3.12, Click, pdfplumber, Pandas, Pydantic, Matplotlib, Hatchling
