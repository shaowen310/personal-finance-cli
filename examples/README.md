# Examples

A sandbox for trying `pfa` with your own bank statements, driven by two
PowerShell scripts that wrap the `pfa` CLI (installed in a conda environment).

## Prerequisites

- `pfa` must be installed in a conda environment. Both scripts default to the
  environment named **`doc`**; override with `-CondaEnv <name>` (or pass
  `-CondaExe` if `conda` is not on your `PATH`).
- Run the scripts from PowerShell. Paths are resolved relative to the script's
  own folder, so you can run them from anywhere.

## Layout

```
examples/
├── pdf/
│   └── <bank>/                 # one folder per bank (DBS, OCBC, UOB, ICBC …)
│       └── estatements/        # ← drop that bank's statement PDFs here
├── ir/
│   └── <bank>/                 # IR JSON produced by convert_estatements_to_ir.ps1
├── report/
│   └── monthly/
│       └── <YYYYMM>/           # consolidated IR + report (build_monthly_report.ps1)
├── convert_estatements_to_ir.ps1
├── build_monthly_report.ps1
└── README.md
```

`ir/` and `report/` are git-ignored; only the folder skeletons and your PDFs
(also ignored) stay out of version control.

## Step 1 — Organize your PDFs

Place each bank's statements under `pdf/<bank>/estatements/`, e.g.:

```
examples/pdf/DBS/estatements/DBS_202607_cons.pdf
examples/pdf/OCBC/estatements/OCBC_202607_cc.pdf
examples/pdf/UOB/estatements/UOB_202607_sa.pdf
```

Filename convention: `<BANK>_<YYYYMM>_<type>.pdf`, where `<type>` is something
like `cons` (current/consolidated account), `cc` (credit card), `sa` (savings),
or `one` (OCBC ONE / UOB ONE account). If you have more than one card or account
of the same type at a bank, append a short, **non-personal** suffix of your
choosing (e.g. `cc_amex` or `sa_joint`) to tell them apart. The parser reads the
bank and account from the PDF *content*, not the filename, so the suffix is only
for your own organization — avoid embedding real account/card numbers.

## Step 2 — Convert PDFs → IR

`convert_estatements_to_ir.ps1` runs `pfa parse` on every PDF under each
`estatements/` folder and writes `ir/<bank>/<stem>.ir.json`.

```powershell
# From the examples/ folder:
.\convert_estatements_to_ir.ps1
# or with overrides:
.\convert_estatements_to_ir.ps1 -PdfRoot D:\stmts -IrRoot D:\ir -CondaEnv doc
```

Notes:
- **Incremental**: a PDF is skipped if its IR output already exists and is
  newer than the PDF. Use `-Force` to reprocess everything.
- **OneDrive-safe**: because `pfa` mishandles OneDrive paths, each PDF is copied
  to a temp directory, parsed there, and the resulting IR is moved back.

## Step 3 — Build the monthly report

`build_monthly_report.ps1` gathers the `*.ir.json` files for a month, consolidates
them with `pfa consolidate`, then generates the finance report with
`pfa analyze -m <YYYYMM>`.

```powershell
# Prompts for year/month (defaults: this year / last month):
.\build_monthly_report.ps1
# or explicit:
.\build_monthly_report.ps1 -Year 2026 -Month 7
# exclude the following month's credit-card statements:
.\build_monthly_report.ps1 -Year 2026 -Month 7 -ExcludeNextMonthCC
```

Notes:
- By default it also pulls the **next month's `*_cc_*` statements** into the
  consolidation, because credit cards bill on a cycle that lags the spend month
  (some of month M's spending only appears on the next statement). The report
  itself still scopes to month M. Use `-ExcludeNextMonthCC` to skip this.
- Output lands in `report/monthly/<YYYYMM>/`:
  - `<YYYYMM>.ir.json` — consolidated IR
  - `<YYYYMM>_Finance_Report.md` — balance sheet, cash flow, income/expense/
    transfer breakdowns
- Categorization runs inside `pfa analyze` (reusing/generating `categories.json`
  next to the consolidated IR). Pass `-Llm` to use the LLM fallback for
  uncategorized transactions (`-LlmModel`, `-LlmApiKey`, `-LlmBaseUrl`); use
  `-SkipCategorize` to skip it.

## Outputs

### `ir/<bank>/` (intermediate)
- `<stem>.ir.json`   per-statement parsed IR

### `report/monthly/<YYYYMM>/` (final)
- `<YYYYMM>.ir.json`            consolidated & deduplicated IR
- `<YYYYMM>_Finance_Report.md`  finance report

## Single-command alternative

If you don't need incremental parsing, the per-bank layout, or next-month credit-
card handling, the CLI can do everything in one step:

```bash
pfa run -m 202608 -i examples/pdf -o examples/ir
```
(That writes the consolidated IR, `categories.json`, and `finance_report.md`
into `examples/ir/`.)
