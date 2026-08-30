"""Batch pipeline: parse PDFs → consolidate IR → categorize → report.

Steps:
  1. Parse each PDF into ParsedStatement IR (.ir.json)
  2. Consolidate all .ir.json into a single consolidated.ir.json
  3. Categorize transactions from consolidated.ir.json → categories.json
  4. Generate markdown reports from consolidated.ir.json + categories.json

Usage:
    python tests/run_full_pipeline.py [-s YYYYMMDD|YYYYMM] [-e YYYYMMDD|YYYYMM]
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import traceback
from pathlib import Path

import subprocess

from pfa_parser import SGBankPDFParser

# Make repo root importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pfa_cli.dates import parse_start_date, parse_end_date  # noqa: E402


CACHE_DIR = REPO_ROOT / "tests" / "cache"
OUTPUT_DIR = REPO_ROOT / "tests" / "outputs"
IR_DIR = OUTPUT_DIR / "ir"
RULES_PATH = REPO_ROOT / "packages" / "pfa-analysis" / "references" / "categories.yaml"


def _clear_readonly(path: Path) -> None:
    """Remove the read-only flag from *path* (file or dir).

    On Windows / OneDrive, ``tests/outputs`` can end up with the ``ReadOnly``
    attribute set, which makes ``shutil.rmtree`` and ``Path.mkdir`` raise
    ``PermissionError: Access is denied``. Clearing the flag first avoids that.
    """
    try:
        if path.is_dir():
            for entry in path.iterdir():
                _clear_readonly(entry)
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except (OSError, PermissionError):
        pass


def _reset_output_dir() -> None:
    """Remove and recreate OUTPUT_DIR, tolerating read-only / OneDrive flags."""
    if OUTPUT_DIR.exists():
        _clear_readonly(OUTPUT_DIR)
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    IR_DIR.mkdir(parents=True, exist_ok=True)


def _step1_parse_pdfs(pdf_paths: list[Path]) -> bool:
    """Parse all PDFs into ParsedStatement IR (.ir.json)."""
    ok = True
    parser = SGBankPDFParser()

    for pdf_path in pdf_paths:
        print(f"  Parsing: {pdf_path.name} …", end=" ", flush=True)
        try:
            ir_path = IR_DIR / f"{pdf_path.stem}.ir.json"
            ir_stmt = parser.parse(str(pdf_path))
            n_txns = sum(len(a.transactions) for a in ir_stmt.accounts)
            _ = ir_path.write_text(ir_stmt.to_json(indent=2), encoding="utf-8")

            print(f"OK  ({n_txns} txns) → {ir_path.name}")
        except Exception:
            ok = False
            print("FAILED")
            traceback.print_exc()

    return ok


def _step2_consolidate() -> Path | None:
    """Consolidate all .ir.json into consolidated.ir.json via the CLI.

    Delegating to ``consolidate.py`` keeps this pipeline in lock-step with the
    canonical consolidation path (all transfer detectors, link verification,
    optional FX embed) so detector changes there apply here automatically.
    """
    ir_paths = sorted(IR_DIR.glob("*.ir.json"))
    if not ir_paths:
        print("  No .ir.json files found — skipping consolidation.")
        return None

    out_path = OUTPUT_DIR / "consolidated.ir.json"
    consolidate_cli = (
        REPO_ROOT / "packages" / "pfa-ir-consolidator" / "src"
        / "pfa_ir_consolidator" / "consolidate.py"
    )
    cmd = [
        sys.executable, str(consolidate_cli),
        *[str(p) for p in ir_paths],
        "-o", str(out_path),
        "--embed-fx",
    ]

    print(f"  Consolidating {len(ir_paths)} .ir.json files via CLI …", end=" ", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception:
        print("FAILED to launch CLI")
        traceback.print_exc()
        return None

    if result.returncode != 0:
        print("FAILED")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return None

    print("OK → " + out_path.name)
    for line in result.stdout.splitlines():
        print("  " + line)
    return out_path


def _step3_categorize(consolidated_path: Path) -> bool:
    """Categorize transactions via the ``categorize`` CLI → categories.json.

    Delegating to the CLI keeps the pipeline in lock-step with the canonical
    categorizer. The CLI exits non-zero on coverage issues but still writes
    categories.json, so a coverage failure is treated as a warning, not a hard
    error (matching the old in-process behaviour).
    """
    out_path = OUTPUT_DIR / "categories.json"
    categorize_cli = (
        REPO_ROOT / "packages" / "pfa-analysis" / "src"
        / "pfa_analysis" / "categorize.py"
    )
    cmd = [
        sys.executable, str(categorize_cli),
        str(consolidated_path),
        "-o", str(out_path),
        "--rules", str(RULES_PATH),
    ]

    print(f"  Categorizing {consolidated_path.name} via CLI …", end=" ", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception:
        print("FAILED to launch CLI")
        traceback.print_exc()
        return False

    if result.returncode != 0 and not out_path.exists():
        print("FAILED")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return False

    if result.returncode != 0:
        print("OK (with coverage warnings)")
    else:
        print("OK → " + out_path.name)
    for line in result.stdout.splitlines():
        print("  " + line)
    return True


def _step4_render_report(
    consolidated_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    """Generate the markdown finance report via the ``report`` CLI.

    The report CLI consumes categories.json (default: alongside the input) and
    writes ``<input_stem>_Finance_Report.md``; we rename that to ``finance_report.md``
    to preserve the pipeline's historical output filename.
    """
    categories_path = OUTPUT_DIR / "categories.json"
    if not categories_path.exists():
        print("  categories.json not found — skipping reports.")
        return

    report_cli = (
        REPO_ROOT / "packages" / "pfa-analysis" / "src"
        / "pfa_analysis" / "report.py"
    )
    cmd = [
        sys.executable, str(report_cli),
        str(consolidated_path), str(OUTPUT_DIR),
        "--categories", str(categories_path),
    ]
    if start_date:
        cmd += ["--start-date", start_date]
    if end_date:
        cmd += ["--end-date", end_date]

    print("  4a. Finance report via CLI …", end=" ", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception:
        print("FAILED to launch CLI")
        traceback.print_exc()
        return

    if result.returncode != 0:
        print("FAILED")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return

    # Preserve the historical output filename (report CLI writes
    # "<stem>_Finance_Report.md" = "consolidated_Finance_Report.md").
    cli_md = OUTPUT_DIR / (consolidated_path.stem + "_Finance_Report.md")
    final_md = OUTPUT_DIR / "finance_report.md"
    if cli_md.exists() and cli_md != final_md:
        cli_md.replace(final_md)
    print("OK → " + final_md.name)
    for line in result.stdout.splitlines():
        print("  " + line)


def main(start_date: str | None = None, end_date: str | None = None) -> None:
    pdf_paths = sorted(CACHE_DIR.glob("*.pdf"))
    if not pdf_paths:
        print("No PDF files found in", CACHE_DIR)
        return

    # Clean up previous outputs
    _reset_output_dir()
    print(f"Cleaned: {OUTPUT_DIR}\n")

    # ── Step 1: Parse all PDFs ─────────────────────────────────────────────
    print("── Step 1: Parse PDFs ──")
    if not _step1_parse_pdfs(pdf_paths):
        print("\nStep 1 had errors.  Continuing with available outputs …\n")

    # ── Step 2: Consolidate IR ─────────────────────────────────────────────
    print("\n── Step 2: Consolidate IR ──")
    consolidated_path = _step2_consolidate()
    if consolidated_path is None:
        print("ERROR: Consolidation failed — cannot proceed.")
        return

    # ── Step 3: Categorize ─────────────────────────────────────────────────
    print("\n── Step 3: Categorize ──")
    _ = _step3_categorize(consolidated_path)

    # ── Step 4: Render Markdown Reports ────────────────────────────────────
    print("\n── Step 4: Render Reports ──")
    _step4_render_report(consolidated_path, start_date, end_date)

    print(f"\nDone — outputs in {OUTPUT_DIR}")


def _parse_start_date(raw: str | None) -> str | None:
    """argparse type for --start; delegates to shared pfa_cli.dates."""
    try:
        return parse_start_date(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _parse_end_date(raw: str | None) -> str | None:
    """argparse type for --end; delegates to shared pfa_cli.dates."""
    try:
        return parse_end_date(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full PFA pipeline: parse → consolidate → categorize → report.",
    )
    _ = parser.add_argument(
        "-s", "--start", dest="start_date", default=None, type=_parse_start_date,
        help="Start date (YYYYMMDD or YYYYMM). "
             +"YYYYMM uses the 1st of the month. "
             +"Transactions before this date are excluded.",
    )
    _ = parser.add_argument(
        "-e", "--end", dest="end_date", default=None, type=_parse_end_date,
        help="End date (YYYYMMDD or YYYYMM). "
             +"YYYYMM uses the last day of the month. "
             +"Transactions after this date are excluded.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(start_date=args.start_date, end_date=args.end_date)
