"""Markdown renderer for categorized transactions.

Reads a consolidated IR JSON and a categories JSON, then produces
a markdown report with per-currency transaction tables and summary statistics.

Usage:
    python scripts/render_md.py ir.json categories.json
    python scripts/render_md.py ir.json categories.json -o report.md
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pfa_categorize.ir import parse_ir


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TxnRecord:
    """Unified transaction record with category attached."""

    txn_id: str
    date: str
    description: str
    amount: float  # signed: + for deposit/credit, - for withdrawal/debit
    currency: str
    bank: str
    account: str
    category: str
    account_type: str  # "current", "fixed_deposit", "credit_card", "srs", etc.

    @property
    def cls(self) -> str:
        """Top-level class (Income / Expense / Transfer)."""
        parts = self.category.split(": ", 1)
        if len(parts) == 2:
            return parts[0]
        return ""

    @property
    def sub(self) -> str:
        """Subtype (second level of the category)."""
        parts = self.category.split(": ", 1)
        if len(parts) == 2:
            return parts[1]
        return self.category


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def load_categories(path: Path) -> dict[str, str]:
    """Load ``{txn_id: category}`` from JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------


def _signed_amount(row: dict[str, Any], account_type: str) -> float:
    """Compute signed amount from IR-native ``amount`` field.

    - Bank accounts: amount is already signed (+ inflow, - outflow).
    - Credit cards: charges are positive in IR, so negate for spending.
    """
    amount = float(row.get("amount") or 0.0)
    if account_type == "credit_card":
        return -amount
    return amount


def build_records(
    txns_raw: list[dict[str, Any]],
    categories: dict[str, str],
    account_types: dict[str, str],
) -> list[TxnRecord]:
    """Merge raw transactions with categories and compute signed amounts."""
    records: list[TxnRecord] = []

    for row in txns_raw:
        txn_id = row["txn_id"]
        acc_type = account_types.get(row.get("account", ""), "unknown")
        records.append(
            TxnRecord(
                txn_id=txn_id,
                date=row["date"],
                description=row["description"],
                amount=_signed_amount(row, acc_type),
                currency=row.get("currency", ""),
                bank=row["bank"],
                account=row.get("account", ""),
                category=categories.get(txn_id, "Uncategorized"),
                account_type=acc_type,
            )
        )

    return records


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_amount(amount: float, currency: str) -> str:
    """Format signed amount with currency, e.g. ``+SGD 1,234.56``."""
    sign = "+" if amount >= 0 else "-"
    abs_amt = abs(amount)
    return f"{sign}{currency} {abs_amt:,.2f}"


def _fmt_amt_plain(amount: float) -> str:
    """Plain signed number, e.g. ``+1,234.56``."""
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{abs(amount):,.2f}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_markdown(
    records: list[TxnRecord],
    meta: dict[str, Any],
    *,
    default_category: str = "Uncategorized",
) -> str:
    """Produce the full markdown report string."""
    lines: list[str] = []

    # ---- Header ------------------------------------------------------------
    n_total = len(records)
    n_cat = sum(1 for r in records if r.category != default_category)
    coverage = (n_cat / n_total * 100) if n_total else 0

    lines.append("# Transaction Report")
    lines.append("")
    lines.append(f"- **Period:** {meta['period_from']} → {meta['period_to']}")
    lines.append(f"- **Institutions:** {', '.join(meta['institutions'])}")
    lines.append(f"- **Total Transactions:** {n_total}  |  **Categorized:** {n_cat}  |  **Coverage:** {coverage:.1f}%")
    lines.append("")

    # Collect currency set (sorted descending so SGD, CNY, JPY, ...)
    currencies = sorted(set(r.currency for r in records), reverse=True)

    # Build: category → {currency: (count, total_amount)}
    cat_data: dict[str, dict[str, tuple[int, float]]] = defaultdict(
        lambda: defaultdict(lambda: (0, 0.0))
    )
    for r in records:
        cnt, tot = cat_data[r.category][r.currency]
        cat_data[r.category][r.currency] = (cnt + 1, tot + r.amount)

    # Order categories by class then category name
    def _cat_sort_key(cat: str) -> tuple[str, str]:
        parts = cat.split(": ", 1)
        return (parts[0] if len(parts) == 2 else "", cat)

    sorted_cats = sorted(cat_data.keys(), key=_cat_sort_key)

    # ---- Full Summary by Category (count only, no currency totals) ----------
    lines.append("## Summary by Category")
    lines.append("")

    lines.append("| Class | Category | Count |")
    lines.append("|---|---|:---:|")

    for cat in sorted_cats:
        total_cnt = sum(cat_data[cat][c][0] for c in currencies)
        parts = cat.split(": ", 1)
        cls_disp = parts[0] if len(parts) == 2 else "—"
        sub_disp = parts[1] if len(parts) == 2 else cat
        lines.append(f"| {cls_disp} | {sub_disp} | {total_cnt} |")

    grand_cnt_full = sum(sum(cat_data[cat][c][0] for c in currencies) for cat in sorted_cats)
    lines.append(f"| | **Total** | **{grand_cnt_full}** |")

    lines.append("")

    # ---- Current Account Summary (count + per-currency totals) --------------
    current_records = [r for r in records if r.account_type == "current"]

    # Build: category → {currency: (count, total_amount)} for current accounts only
    cur_cat_data: dict[str, dict[str, tuple[int, float]]] = defaultdict(
        lambda: defaultdict(lambda: (0, 0.0))
    )
    for r in current_records:
        cnt, tot = cur_cat_data[r.category][r.currency]
        cur_cat_data[r.category][r.currency] = (cnt + 1, tot + r.amount)

    # Order by class then category name
    def _cur_cat_sort_key(cat: str) -> tuple[str, str]:
        parts = cat.split(": ", 1)
        return (parts[0] if len(parts) == 2 else "", cat)

    cur_sorted_cats = sorted(cur_cat_data.keys(), key=_cur_cat_sort_key)

    if cur_sorted_cats:
        lines.append("## Current Account Transaction Summary")
        lines.append("")

        header = "| Class | Category | Count |"
        sep = "|---|---|:---:|"
        for cur in currencies:
            header += f" Total ({cur}) |"
            sep += ":---:|"
        lines.append(header)
        lines.append(sep)

        for cat in cur_sorted_cats:
            total_cnt = sum(cur_cat_data[cat][c][0] for c in currencies)
            parts = cat.split(": ", 1)
            cls_disp = parts[0] if len(parts) == 2 else "—"
            sub_disp = parts[1] if len(parts) == 2 else cat
            row = f"| {cls_disp} | {sub_disp} | {total_cnt} |"
            for cur in currencies:
                cnt, tot = cur_cat_data[cat][cur]
                if cnt > 0:
                    row += f" {_fmt_amt_plain(tot)} |"
                else:
                    row += " — |"
            lines.append(row)

        # Total row
        cur_grand_cnt = sum(sum(cur_cat_data[cat][c][0] for c in currencies) for cat in cur_sorted_cats)
        total_row = f"| | **Total** | **{cur_grand_cnt}** |"
        for cur in currencies:
            grand_tot = sum(cur_cat_data[cat][cur][1] for cat in cur_sorted_cats)
            total_row += f" **{_fmt_amt_plain(grand_tot)}** |"
        lines.append(total_row)

        lines.append("")

    # ---- Separate current accounts vs credit cards --------------------------
    bank_records = [r for r in records if r.account_type == "current"]
    cc_records = [r for r in records if r.account_type == "credit_card"]

    # ---- Current account sections (per currency) ----------------------------
    for currency in currencies:
        cur_records = [r for r in bank_records if r.currency == currency]
        if not cur_records:
            continue

        lines.append("---")
        lines.append("")
        lines.append(f"## Current Accounts — {currency}")
        lines.append("")
        _render_table(cur_records, lines)

    # ---- Credit card sections ----------------------------------------------
    if cc_records:
        for currency in currencies:
            cur_records = [r for r in cc_records if r.currency == currency]
            if not cur_records:
                continue

            lines.append("---")
            lines.append("")
            lines.append(f"## Credit Card — {currency}")
            lines.append("")
            _render_table(cur_records, lines)

    return "\n".join(lines) + "\n"


def _render_table(records: list[TxnRecord], lines: list[str]) -> None:
    """Append a sorted markdown table to ``lines``."""
    # Sort by date, then description
    sorted_recs = sorted(records, key=lambda r: (r.date, r.description))

    lines.append(
        "| # | Date | Description | Amount | Bank | Account | Class | Category |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|"
    )

    for idx, r in enumerate(sorted_recs, 1):
        desc = r.description[:70] + ("…" if len(r.description) > 70 else "")
        lines.append(
            f"| {idx} "+
            f"| {r.date} "+
            f"| {desc} "+
            f"| {_fmt_amount(r.amount, r.currency)} "+
            f"| {r.bank} "+
            f"| {r.account} "+
            f"| {r.cls or '—'} "+
            f"| {r.sub} |"
        )

    lines.append("")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description="Render categorized transactions as a markdown report.",
    )
    _ = p.add_argument("ir_json", help="Path to consolidated IR JSON.")
    _ = p.add_argument("categories", help="Path to categories.json output.")
    _ = p.add_argument(
        "-o", "--output",
        default=None,
        help="Write report to file (default: print to stdout).",
    )

    args = p.parse_args(argv)

    rm_path = Path(args.ir_json)
    cat_path = Path(args.categories)

    if not rm_path.exists():
        print(f"ERROR: IR JSON not found: {rm_path}", file=sys.stderr)
        sys.exit(1)
    if not cat_path.exists():
        print(f"ERROR: Categories file not found: {cat_path}", file=sys.stderr)
        sys.exit(1)

    ir_data = parse_ir(rm_path)
    txns_raw = ir_data.txns_raw
    meta = ir_data.meta
    account_types = ir_data.account_types
    categories = load_categories(cat_path)
    records = build_records(txns_raw, categories, account_types)

    md = render_markdown(records, meta)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _ = out_path.write_text(md, encoding="utf-8")
        print(f"Written: {out_path.resolve()}")
    else:
        print(md)


if __name__ == "__main__":
    main()
