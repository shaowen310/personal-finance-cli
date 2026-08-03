# Personal Finance CLI

A personal finance analysis CLI tool (`pfa`) that connects bank statement parsing, IR data consolidation, transaction categorization, and financial analysis into a single workflow.

Supported banks: **DBS/POSB**, **OCBC**, **UOB**, **ICBC** (Singapore).

## Repository Structure

```
packages/
├── pfa-ir-schema/         # Shared IR data models (Account, Transaction, etc.)
├── pfa-parser/            # Bank statement PDF parser (detect → extract → IR)
├── pfa-fx/                # FX rate retrieval & SGD conversion (leaf, stdlib-only)
├── pfa-ir-consolidator/   # IR data consolidation & transfer detection
├── pfa-categorize/        # Transaction auto-categorization
└── pfa-analysis/          # Financial analysis & reporting engine
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

## Dependency Graph

```
pfa-ir-schema          (shared data models, zero heavy deps)
    ↑
pfa-parser             (PDF extraction, all SG bank support)
    ↑
pfa-ir-consolidator    (merge & deduplicate IR, detect transfers)
    ↑
pfa-categorize         (auto-categorize transactions)
    ↑
pfa-analysis           (financial reports & visualization)
    ↑
pfa-cli                (CLI entry point)
```

## Tech Stack

Python >= 3.12, Click, pdfplumber, Pandas, Pydantic, Matplotlib, Hatchling
