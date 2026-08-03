# Personal Finance CLI

A personal finance analysis CLI tool (`pfa`) that connects bank statement parsing, IR data consolidation, transaction categorization, and financial analysis into a single workflow.

## Repository Structure

```
packages/
├── pfa-parser/           # Bank statement parser (wraps sg-bank-pdf-parser)
├── pfa-ir-consolidator/  # IR data consolidation
├── pfa-categorize/       # Transaction auto-categorization
└── pfa-analysis/         # Financial analysis & reporting engine
apps/
└── pfa-cli/              # CLI entry point + workflow orchestration
```

## Quick Start

```bash
# Install all packages in development mode
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
sg-bank-pdf-parser (external)
    ↑
pfa-parser
    ↑
pfa-ir-consolidator
    ↑
pfa-categorize
    ↑
pfa-analysis
    ↑
pfa-cli
```

## Tech Stack

Python >= 3.12, Click, Pandas, Pydantic, Matplotlib, Hatchling
