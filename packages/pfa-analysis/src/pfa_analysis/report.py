"""CLI + consolidated report rendering.

Extracted from ``analyze.py`` as part of a structural split. Contains:
  * ``demo_ir``  — synthetic consolidated IR for ``--demo`` runs
  * ``render_consolidated_report`` — the canonical Markdown-report builder
  * ``main`` — the ``argparse`` CLI entry point

All analysis helpers it depends on are imported from ``analyze``; no behaviour
or signatures were changed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from pfa_analysis.analyze import (
    _analyze_file,
    _internal_transfer_ids,
    build_fx_drilldown,
    build_income_expense_drilldowns,
    build_transfer_drilldown,
    parse_date_to_iso,
)
from pfa_analysis.fx_cache import fetch_fx_rates
from pfa_analysis.dashboard import build_dashboard_json
from pfa_analysis.render_md import render_report


def demo_ir() -> dict[str, Any]:
    """Synthetic consolidated IR (``accounts[]`` shape) used by ``--demo``.

    Mirrors the real parser/consolidator output so the demo exercises the same
    loading + analysis pipeline as a production ``.ir.json``.
    """
    opening = 1000.0
    rows = [
        ("2026-04-01", "SALARY", 5000.00),
        ("2026-04-05", "GIANT", -120.40),
        ("2026-04-12", "GRAB", -33.10),
        ("2026-04-20", "TRANSFER TO DBS", -500.00),
        ("2026-05-01", "SALARY", 5200.00),
        ("2026-05-04", "NTUC", -150.20),
        ("2026-05-11", "BUS/MRT", -16.23),
        ("2026-05-15", "TRANSFER TO DBS", -600.00),
        ("2026-05-22", "TRANSFER FROM UOB", 300.00),
    ]
    balance = opening
    txns: list[dict[str, Any]] = []
    for i, (d, desc, amt) in enumerate(rows):
        balance += amt
        txns.append({
            "txn_id": f"demo-{i:02d}",
            "posted_date": d,
            "amount": amt,
            "currency": "SGD",
            "description": desc,
            "tags": [],
            "link_labels": [],
            "is_reversal": False,
            "is_internal_transfer": False,
            "linked_txn_ids": [],
            "balance_after": round(balance, 2),
        })
    return {
        "ir_version": "2026.5",
        "parser": {"name": "demo_consolidated", "version": "1.0"},
        "source_file": "demo_statements.pdf",
        "statement_meta": {
            "institution": "OCBC_SG",
            "period_from": "2026-04-01",
            "period_to": "2026-05-31",
            "functional_currency": "SGD",
        },
        "accounts": [
            {
                "name": "DEMO SAVINGS",
                "account_no": "xxx",
                "account_type": "current",
                "currency": "SGD",
                "opening_balance": opening,
                "closing_balance": round(balance, 2),
                "transactions": txns,
            }
        ],
    }


def render_consolidated_report(
    consolidated_path: Path,
    categories_path: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Analyse a consolidated IR and return the Markdown report string.

    This is the canonical entry point for producing a ``_Finance_Report.md``
    from a consolidated ``.ir.json`` file. It wraps the full pipeline:
    ``_analyze_file`` → FX extraction → income/expense drilldowns →
    ``render_report``.

    When *start_date* and/or *end_date* are provided (ISO 8601 ``YYYY-MM-DD``),
    only transactions whose ``posted_date`` falls within the inclusive range
    are analysed.

    Callers (the CLI ``main``, batch test drivers) should use this instead of
    re-implementing the assembly logic.
    """
    # Load the transaction-category map (if present) so cash-flow metrics can
    # honour "Transfer:*" categories (e.g. a PayNow transfer to a person) produced
    # by txn-categorize, instead of relying on description keywords alone.
    _cat_path = Path(categories_path) if categories_path else consolidated_path.with_name("categories.json")
    _txn_categories: dict[str, str] = {}
    if _cat_path.exists():
        _txn_categories = json.loads(_cat_path.read_text(encoding="utf-8"))
    result = _analyze_file(consolidated_path, start_date, end_date,
                           txn_categories=_txn_categories or None)
    # FX rate selection:
    #  - When a cut-off window is supplied, value FX as of the cut-off end date
    #    ("as of 0731" uses the 0731 rate, not the IR's build-date snapshot).
    #    The rate is fetched and cached in %TEMP% by ``fetch_fx_rates``.
    #  - If that is unavailable (API down / non-trading day / no network), fall
    #    back to the statement period-end rate so SGD conversion still works.
    if end_date:
        fx_cutoff = parse_date_to_iso(end_date)
        fx_rates = fetch_fx_rates(fx_cutoff) if fx_cutoff else None
    else:
        fx_rates = None
    if fx_rates is None and result["meta"].get("_consolidated"):
        period_end = parse_date_to_iso(result["meta"].get("period_end"))
        if period_end:
            fx_rates = fetch_fx_rates(period_end)
    if fx_rates is None:
        print(
            "[WARN] FX rates unavailable; multi-currency balances will not be "
            "converted to SGD.",
            file=sys.stderr,
        )
    elif fx_rates:
        print(f"FX rates: {fx_rates['date']} from {fx_rates['source']}")

    cat_path = Path(categories_path) if categories_path else consolidated_path.with_name("categories.json")
    income_drill, expense_drill = build_income_expense_drilldowns(
        consolidated_path, cat_path, start_date, end_date,
    )
    transfer_drill = build_transfer_drilldown(
        consolidated_path, cat_path, start_date, end_date,
    )
    # Realized FX gain/loss from currency-conversion pairs (base currency SGD).
    fx_gain_loss = build_fx_drilldown(
        consolidated_path,
        as_of=(fx_rates.get("date") if fx_rates else None),
        fx_rates=(fx_rates.get("rates") if fx_rates else None),
        start_date=start_date, end_date=end_date,
    )

    # Build categorization summary from drilldown data.
    cat_summary: list[dict[str, Any]] = []
    total_cat = 0
    for entry in income_drill:
        cnt = len(entry.get("transactions", []))
        total_cat += cnt
        cat_summary.append({"class": "Income", "category": entry["source"], "count": cnt})
    for entry in expense_drill:
        cnt = len(entry.get("transactions", []))
        total_cat += cnt
        cat_summary.append({"class": "Expense", "category": entry["category"], "count": cnt})
    for entry in transfer_drill:
        cnt = len(entry.get("transactions", []))
        total_cat += cnt
        cat_summary.append({"class": "Transfer", "category": entry["category"], "count": cnt})
    # Count internal transfers (excluded from drilldowns but present in IR).
    total_txns = total_cat + len(_internal_transfer_ids(consolidated_path, start_date, end_date))
    cat_coverage = (total_cat / total_txns * 100) if total_txns else 0.0

    return render_report(
        result,
        consolidated=result["meta"].get("_consolidated", False),
        fx_rates=fx_rates,
        drilldown=result.get("drilldown"),
        income_drilldown=income_drill if income_drill else None,
        expense_drilldown=expense_drill if expense_drill else None,
        transfer_drilldown=transfer_drill if transfer_drill else None,
        fx_gain_loss=fx_gain_loss,
        cat_summary=cat_summary if cat_summary else None,
        cat_coverage=cat_coverage,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Analyse personal balance sheet & cash flow from bank-statement JSON.")
    _ = p.add_argument("input", nargs="?", help="Input IR JSON file (single statement or consolidated)")
    _ = p.add_argument("output", nargs="?", help="Output dir (default: alongside input)")
    _ = p.add_argument("--demo", action="store_true", help="Run with embedded synthetic data")
    _ = p.add_argument("--dashboard-json", action="store_true",
                       help="Output dashboard_data.json instead of Markdown")
    _ = p.add_argument("--categories", help="Path to categories.json from txn-categorize")
    _ = p.add_argument("--cost-basis", help="Path to cost_basis.json for unrealized P&L (for --dashboard-json)")
    _ = p.add_argument("--start-date", metavar="YYYY-MM-DD",
                       help="Only include transactions on or after this date (inclusive)")
    _ = p.add_argument("--end-date", metavar="YYYY-MM-DD",
                       help="Only include transactions on or before this date (inclusive)")
    args = p.parse_args(argv)

    if args.demo:
        ir = demo_ir()
        tmp = Path(tempfile.gettempdir()) / "demo_statements.ir.json"
        _ = tmp.write_text(json.dumps(ir, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        text = render_consolidated_report(tmp)
        out_path = Path("demo_Finance_Report.md")
        _ = out_path.write_text(text, encoding="utf-8")
        print(f"[DEMO] wrote {out_path}")
        return 0

    if not args.input:
        p.error("provide <input.json> or --demo")

    in_path = Path(args.input)
    out_dir = Path(args.output) if args.output else in_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Dashboard JSON mode: produce dashboard_data.json from this single IR file.
    if args.dashboard_json:
        cat_path = Path(args.categories) if args.categories else None
        cb_path = Path(args.cost_basis) if args.cost_basis else None
        dashboard = build_dashboard_json([in_path], cat_path, cb_path)
        out_path = out_dir / "dashboard_data.json"
        _ = out_path.write_text(
            json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Written dashboard data: {out_path}")
        print(f"  period: {dashboard.get('period', {})}")
        print(f"  total_sgd_equivalent: {dashboard.get('asset_composition', {}).get('total_sgd_equivalent', {})}")
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
