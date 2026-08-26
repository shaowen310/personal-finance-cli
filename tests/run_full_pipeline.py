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
import json
import os
import shutil
import stat
import sys
import traceback
from collections import Counter
from pathlib import Path

from pfa_parser import SGBankPDFParser
from pfa_ir_schema import from_json as ir_from_json

# Make repo root importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pfa_ir_consolidator.consolidate import consolidate_statements  # noqa: E402
from pfa_analysis.categorize import categorize  # noqa: E402
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
    """Consolidate all .ir.json into consolidated.ir.json."""
    ir_paths = sorted(IR_DIR.glob("*.ir.json"))
    if not ir_paths:
        print("  No .ir.json files found — skipping consolidation.")
        return None

    print(f"  Consolidating {len(ir_paths)} .ir.json files …", end=" ", flush=True)
    try:
        stmts_with_paths: list[tuple[str, object]] = []
        for p in ir_paths:
            stmt = ir_from_json(p.read_text(encoding="utf-8"))
            stmts_with_paths.append((str(p), stmt))

        consolidated, total_in, deduped, filtered = consolidate_statements(
            stmts_with_paths, do_dedup=True
        )

        # Transfer detection & postprocessing
        from pfa_ir_consolidator.detect_transfers import (
            detect_cc_payments,
            detect_currency_conversions,
            detect_inter_bank_transfers,
            detect_intra_bank_transfers,
        )
        consolidated = detect_inter_bank_transfers(consolidated)
        consolidated = detect_intra_bank_transfers(consolidated)
        consolidated = detect_currency_conversions(consolidated)
        consolidated = detect_cc_payments(consolidated)

        from pfa_parser.postprocess import verify_txn_links
        consolidated = verify_txn_links(consolidated)

        # Embed FX rates into extras.consolidation.fx so downstream analysis /
        # rendering can convert foreign balances to SGD.
        from pfa_ir_consolidator.consolidate import embed_fx_rates
        consolidated = embed_fx_rates(consolidated)

        out_path = OUTPUT_DIR / "consolidated.ir.json"
        _ = out_path.write_text(consolidated.to_json(indent=2), encoding="utf-8")

        print(
            f"OK  accounts={len(consolidated.accounts)} "
            + f"txns_in={total_in} txns_out={total_in - deduped - filtered} "
            + f"deduped={deduped} filtered={filtered} → {out_path.name}"
        )
        return out_path
    except Exception:
        print("FAILED")
        traceback.print_exc()
        return None


def _step3_categorize(consolidated_path: Path) -> bool:
    """Categorize transactions from consolidated.ir.json."""
    print(f"  Categorizing {consolidated_path.name} …", end=" ", flush=True)
    try:
        result = categorize(
            input_path=consolidated_path,
            rules_path=RULES_PATH,
        )
        out_path = OUTPUT_DIR / "categories.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _ = out_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # Quick summary
        counts = Counter(result.values())
        n_total = len(result)
        n_uncat = counts.get("Other: Other", 0)
        n_cat = n_total - n_uncat
        coverage = n_cat / n_total * 100 if n_total else 0
        print(f"OK  {n_total} txns, {len(counts)} categories, {coverage:.1f}% coverage → {out_path.name}")
        return True
    except Exception:
        print("FAILED")
        traceback.print_exc()
        return False


def _step4_render_report(
    consolidated_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    """Generate markdown reports from consolidated IR + categories."""
    categories_path = OUTPUT_DIR / "categories.json"
    if not categories_path.exists():
        print("  categories.json not found — skipping reports.")
        return

    # ── 4a. Balance Sheet & Cash Flow Report ───────────────────────────────
    print("  4a. Finance report …", end=" ", flush=True)
    try:
        from pfa_analysis.analyze import render_consolidated_report

        md = render_consolidated_report(
            consolidated_path, categories_path,
            start_date=start_date, end_date=end_date,
        )
        out_path = OUTPUT_DIR / "finance_report.md"
        _ = out_path.write_text(md, encoding="utf-8")
        print(f"OK → {out_path.name}")
    except Exception:
        print("FAILED")
        traceback.print_exc()


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
