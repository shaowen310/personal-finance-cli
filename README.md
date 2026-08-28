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
├── pfa-ir-verifier/       # IR verification (internal-transfer reconciliation)
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

## Development Install

All packages are installed in **editable mode** (`pip install -e`), so changes
to the source are picked up without reinstalling. Requires **Python >= 3.12**.

**Option A — setup script (recommended):** installs every package in the
correct dependency order (`pfa-ir-schema` → `pfa-fx` → `pfa-parser` →
`pfa-ir-consolidator` → `pfa-analysis` → `pfa-cli`).

```bash
# Windows
powershell -File scripts/setup_dev.ps1

# macOS / Linux
bash scripts/setup_dev.sh
```

**Option B — manual:** create a virtual environment first (recommended), then
install each package from the repo root.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e packages/pfa-ir-schema
pip install -e packages/pfa-fx
pip install -e packages/pfa-ir-verifier
pip install -e packages/pfa-parser
pip install -e packages/pfa-ir-consolidator
pip install -e packages/pfa-analysis
pip install -e apps/pfa-cli
```

After either option, verify with `pfa --help`.

## Commands

| Command | Description |
|---------|-------------|
| `pfa parse -i <file>` | Parse a bank statement PDF |
| `pfa analyze -m <YYYYMM>` | Run financial analysis for a given month |
| `pfa analyze -s <YYYYMMDD\|YYYYMM> -e <YYYYMMDD\|YYYYMM>` | Run analysis for a date range |
| `pfa run --full` | Execute the full pipeline |

## Pipeline

```bash
# Full pipeline (all PDFs in tests/cache/)
python tests/run_full_pipeline.py

# Date-filtered range (YYYYMMDD or YYYYMM format)
python tests/run_full_pipeline.py -s 20260601 -e 20260615

# Whole month (YYYYMM → -s uses 1st, -e uses last day)
python tests/run_full_pipeline.py -s 202606 -e 202606
```

Outputs in `tests/outputs/`:
- `ir/*.ir.json` — per-bank parsed IR
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
pfa-ir-verifier        (IR verification: internal-transfer reconciliation)
    ↑
pfa-analysis           (categorization, financial reports & visualization)
    ↑
pfa-cli                (CLI entry point)
```

## Tech Stack

Python >= 3.12, Click, pdfplumber, Pandas, Pydantic, Matplotlib, Hatchling
