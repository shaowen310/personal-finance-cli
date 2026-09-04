"""Standalone CLI for the pfa_analysis package (argparse).

The primary end-user CLI lives in ``apps/pfa-cli`` (the ``pfa`` command). This
module exists so the analysis package can also be invoked directly during
development, e.g.::

    python -m pfa_analysis analyze consolidated.ir.json
    python -m pfa_analysis categorize consolidated.ir.json -o categories.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import cast

from pfa_analysis.categorize import (
    DEFAULT_RULES_PATH,
    categorize,
    parse_input,
    print_summary,
    validate_coverage,
    write_categories,
)
from pfa_analysis.dashboard import DashboardData, build_dashboard_json
from pfa_analysis.report import demo_ir, render_consolidated_report


def _add_analyze_args(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument("input", nargs="?", help="Input IR JSON file (single statement or consolidated)")
    _ = p.add_argument("output", nargs="?", help="Output dir (default: alongside input)")
    _ = p.add_argument("--demo", action="store_true", help="Run with embedded synthetic data")
    _ = p.add_argument(
        "--dashboard-json",
        action="store_true",
        help="Output dashboard_data.json instead of Markdown",
    )
    _ = p.add_argument("--categories", help="Path to categories.json from txn-categorize")
    _ = p.add_argument(
        "--cost-basis",
        help="Path to cost_basis.json for unrealized P&L (for --dashboard-json)",
    )
    _ = p.add_argument(
        "--start-date",
        metavar="YYYY-MM-DD",
        help="Only include transactions on or after this date (inclusive)",
    )
    _ = p.add_argument(
        "--end-date",
        metavar="YYYY-MM-DD",
        help="Only include transactions on or before this date (inclusive)",
    )


def _run_analyze(args: argparse.Namespace) -> int:
    if args.demo:
        ir = demo_ir()
        tmp = Path(tempfile.gettempdir()) / "demo_statements.ir.json"
        _ = tmp.write_text(
            json.dumps(ir, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        text = render_consolidated_report(tmp)
        out_path = Path("demo_Finance_Report.md")
        _ = out_path.write_text(text, encoding="utf-8")
        print(f"[DEMO] wrote {out_path}")
        return 0

    if not args.input:
        print("error: provide <input.json> or --demo", file=sys.stderr)
        return 2

    in_path = Path(args.input)
    out_dir = Path(args.output) if args.output else in_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Dashboard JSON mode: produce dashboard_data.json from this single IR file.
    if args.dashboard_json:
        cat_path = Path(args.categories) if args.categories else None
        cb_path = Path(args.cost_basis) if args.cost_basis else None
        dashboard = cast(DashboardData, build_dashboard_json([in_path], cat_path, cb_path))
        out_path = out_dir / "dashboard_data.json"
        _ = out_path.write_text(
            json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Written dashboard data: {out_path}")
        print(f"  period: {dashboard.get('period', {})}")
        print(
            "  total_sgd_equivalent: "
            f"{dashboard.get('asset_composition', {}).get('total_sgd_equivalent', {})}"
        )
        cats_loaded = "yes" if cat_path and cat_path.exists() else "no"
        cost_loaded = "yes" if cb_path and cb_path.exists() else "no"
        print(f"  categories: {cats_loaded}  cost_basis: {cost_loaded}")
        return 0

    # Standard Markdown mode (single file, which may be a consolidated IR).
    text = render_consolidated_report(
        in_path,
        categories_path=args.categories,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    out_path = out_dir / (in_path.stem + "_Finance_Report.md")
    _ = out_path.write_text(text, encoding="utf-8")
    print(f"Written: {out_path}")
    return 0


def _add_categorize_args(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument("input", help="Input IR JSON file (from bank-ir-consolidate export).")
    _ = p.add_argument("-o", "--output", required=True, help="Output categories.json file.")
    _ = p.add_argument(
        "--rules",
        help=(
            "Path to rules YAML file "
            + "(default: references/categories.yaml alongside this script)."
        ),
    )
    _ = p.add_argument(
        "--llm",
        action="store_true",
        help=(
            "Use LLM (OpenAI-compatible API) to classify transactions "
            + "that rules could not categorize."
        ),
    )
    _ = p.add_argument("--model", default="gpt-4o-mini", help="LLM model name (default: gpt-4o-mini).")
    _ = p.add_argument("--api-key", help="OpenAI API key (default: $OPENAI_API_KEY).")
    _ = p.add_argument(
        "--base-url",
        help=(
            "OpenAI-compatible API base URL "
            + "(default: $OPENAI_BASE_URL or https://api.openai.com/v1)."
        ),
    )


def _run_categorize(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        return 1

    rules_path = Path(args.rules) if args.rules else DEFAULT_RULES_PATH
    if not rules_path.exists():
        print(f"ERROR: Rules file not found: {rules_path}", file=sys.stderr)
        return 1

    # ---- run pipeline ------------------------------------------------------
    try:
        txns, _, _ = parse_input(input_path)
    except Exception as exc:  # noqa: BLE001 -- report the parse failure and exit non-zero
        print(f"ERROR parsing input: {exc}", file=sys.stderr)
        return 1

    try:
        result = categorize(
            input_path=input_path,
            rules_path=rules_path,
            use_llm=args.llm,
            model=args.model,
            api_key=args.api_key,
            base_url=args.base_url,
        )
    except Exception as exc:  # noqa: BLE001 -- report the categorization failure and exit non-zero
        print(f"ERROR during categorization: {exc}", file=sys.stderr)
        return 1

    # ---- validate ----------------------------------------------------------
    coverage_issues = validate_coverage(txns, result)

    # ---- write output ------------------------------------------------------
    output_path = Path(args.output)
    write_categories(result, output_path)
    print(f"Written: {output_path.resolve()}")

    # ---- print summary -----------------------------------------------------
    print_summary(txns, result)

    # ---- print validation issues -------------------------------------------
    if coverage_issues:
        print("COVERAGE ISSUES:")
        for issue in coverage_issues:
            print(f"  - {issue}")
        print()

    # Exit non-zero on coverage failure
    if coverage_issues:
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pfa_analysis",
        description="Standalone CLI for the pfa_analysis package.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    analyze_p = sub.add_parser(
        "analyze",
        help="Generate the analysis report (Markdown or dashboard JSON).",
    )
    _add_analyze_args(analyze_p)
    categorize_p = sub.add_parser(
        "categorize",
        help="Categorize transactions from a consolidated IR JSON export.",
    )
    _add_categorize_args(categorize_p)

    args = parser.parse_args(argv)
    if args.command == "analyze":
        return _run_analyze(args)
    # argparse guarantees ``command`` is set (required=True); only "categorize" remains.
    return _run_categorize(args)
