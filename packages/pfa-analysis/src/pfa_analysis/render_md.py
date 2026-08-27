"""Markdown report renderer for personal-finance-analysis.

Produces a Markdown report from the structured analysis result dict returned by
``analyze.analyze_file()``.

Also houses shared FX utilities (``convert_to_sgd``, ``FX_BASE``) used by both
the renderer and the main analyzer script.

The renderer is fully self-contained (no dependency on the external
``personal-finance-analysis`` skill). FX rates are expected in the canonical
SGD-per-unit shape ``{"rates": {CCY: SGD per 1 unit}, "date": ..., "source": ...}``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from pfa_fx import BASE_CCY, convert_to_sgd as _pfa_convert_to_sgd

# ---------------------------------------------------------------------------
# Shared FX utilities (used by both renderer & analyze.py)
# ---------------------------------------------------------------------------

FX_BASE = BASE_CCY  # canonical base currency (SGD)


def convert_to_sgd(amount: float, currency: str, fx_rates: dict[str, Any] | None) -> float | None:
    """Convert ``amount`` in ``currency`` to SGD using ``fx_rates``.

    ``fx_rates`` is the renderer wrapper dict ``{"rates": {CCY: SGD per 1 unit}}``.
    SGD returns unchanged. Returns ``None`` if currency is unknown or
    ``fx_rates`` is unavailable. Rates are in the canonical SGD-per-unit shape,
    so foreign -> SGD multiplies.
    """
    if fx_rates is None:
        return None
    return _pfa_convert_to_sgd(amount, currency, fx_rates["rates"])


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def fmt(v: float | None) -> str:
    """Format a float with two decimals or return an em-dash for None."""
    return f"{v:,.2f}" if isinstance(v, (int, float)) else "\u2014"


def _render_txn_detail_table(out: list[str], entries: list[dict[str, Any]], key: str,
                             use_fx: bool,
                             fx_rates: dict[str, Any] | None) -> None:
    """Append a per-transaction detail table (used by the income, expense and
    transfer drill-downs) to ``out``.

    The caller is responsible for writing the section's ``## ...`` heading; this
    helper emits, per entry, a ``### <entry>`` sub-heading followed by its own
    complete table (header + rows). The header is repeated per entry because a
    ``###`` sub-heading between the section header and the rows would otherwise
    break the Markdown table (rows after the heading would lose their header).
    ``entries`` is a list of drill-down entries; each entry's display heading
    comes from ``entry[key]`` and its rows from ``entry["transactions"]``. The
    table shows one row per transaction with ``CCY`` / ``OC`` (original-currency
    amount) columns, plus an ``SGD Eq.`` column when ``use_fx`` is set.
    """
    for entry in entries:
        heading = entry.get(key, "")
        txns = entry.get("transactions", [])
        if not txns:
            continue
        out.append(f"### {heading}\n")
        if use_fx:
            out.append("| Date | Institution | Account | Account Type | Description | CCY | OC | SGD Eq. |")
            out.append("|---|---|---|---|---|---:|---:|---:|")
        else:
            out.append("| Date | Institution | Account | Account Type | Description | CCY | OC |")
            out.append("|---|---|---|---|---|---:|---:|")
        for t in txns:
            desc = t["description"].strip().replace("|", "\\|")
            ccy = t.get("currency", "")
            oc = fmt(t["amount"])
            if use_fx:
                sgd = convert_to_sgd(t["amount"], ccy, fx_rates)
                out.append(
                    f"| {t['date']} | {t.get('bank', '')} | {t.get('account', '')} | "+
                    f"{t.get('account_type', '')} | {desc} | {ccy} | {oc} | {fmt(sgd)} |"
                )
            else:
                out.append(
                    f"| {t['date']} | {t.get('bank', '')} | {t.get('account', '')} | "+
                    f"{t.get('account_type', '')} | {desc} | {ccy} | {oc} |"
                )
        out.append("\n")


def render_report(result: dict[str, Any], consolidated: bool = False,
                  fx_rates: dict[str, Any] | None = None,
                  drilldown: list[dict[str, Any]] | None = None,
                  income_drilldown: list[dict[str, Any]] | None = None,
                  expense_drilldown: list[dict[str, Any]] | None = None,
                  transfer_drilldown: list[dict[str, Any]] | None = None,
                  cat_summary: list[dict[str, Any]] | None = None,
                  cat_coverage: float | None = None) -> str:
    """Render a balance-sheet + cash-flow Markdown report.

    Args:
        result: Dict from ``analyze.analyze_file()`` with keys
            ``meta``, ``metrics_by_ccy``, ``assets``, ``source``.
        consolidated: If True, use consolidated report title & layout.
        fx_rates: FX rate dict in the canonical SGD-per-unit shape
            ``{"rates": {CCY: SGD per 1 unit}, "date": ..., "source": ...}``.
    """
    meta = result["meta"]
    mbc = result["metrics_by_ccy"]
    assets = result["assets"]
    use_fx = consolidated and fx_rates is not None
    out: list[str] = []

    # ---- Title ---------------------------------------------------------------
    title = "Consolidated Balance Sheet & Cash Flow" if consolidated else "Personal Balance Sheet & Cash Flow"
    out.append(f"# {title} \u2014 {result['source']}\n")
    out.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n")
    if not consolidated:
        out.append(f"**Institution:** {meta.get('bank', '')}  ")
        out.append(f"**Account:** {meta.get('account_no', '')}  ")
        out.append(f"**Period:** {meta.get('period_start', '?')} \u2192 {meta.get('period_end', '?')}  ")
        if meta.get("source_file"):
            out.append(f"**Source file:** `{meta['source_file']}`  ")
        out.append(f"**Currencies:** {', '.join(sorted(mbc)) if mbc else '\u2014'}\n")
    else:
        out.append("**Scope:** consolidated balances across all input accounts, per currency.\n")
    out.append("---\n")

    # ---- 1. Executive Summary ------------------------------------------------
    out.append("## 1. Executive Summary\n")
    net_pos = (sum(assets["cash"].values()) + sum(assets["time_deposits"].values())
               + sum(assets["investments"].values()))
    if mbc:
        for ccy in sorted(mbc):
            m = mbc[ccy]
            if m["income"] <= 0 or abs(m["savings_rate"]) > 10:
                savings_disp = "n/a"
            else:
                savings_disp = f"{m['savings_rate'] * 100:.1f}%"
            out.append(
                f"- **{ccy}** \u2014 Closing: {fmt(m['closing'])} | "
                + f"Net Cash Flow: {fmt(m['net_change_cash'])} | "
                + f"Net Operating: {fmt(m['net_operating'])} | "
                + f"Savings: {savings_disp}"
            )
        if use_fx:
            assert fx_rates is not None  # guaranteed by use_fx
            sgd_pos = 0.0
            for kind in ("cash", "time_deposits", "investments"):
                for c, v in assets[kind].items():
                    conv = convert_to_sgd(v, c, fx_rates)
                    if conv is not None:
                        sgd_pos += conv
            out.append(
                f"- **Net Position (SGD equivalent, as of {fx_rates['date']}):** {fmt(sgd_pos)} SGD\n"
            )
        else:
            out.append(
                f"- **Net Position (cash + deposits + investments):** {fmt(net_pos)} "
                + "across the currencies above.\n"
            )
    else:
        out.append("- No transactions in this statement (balance-sheet snapshot only).\n")
        out.append(f"- **Net Position:** {fmt(net_pos)}\n")

    # ---- 1.1 Categorization Summary ------------------------------------------
    if cat_summary:
        out.append("## 1.1 Categorization Summary\n")
        if cat_coverage is not None:
            out.append(f"_Coverage: {cat_coverage:.1f}% of transactions categorized._\n")
        out.append("| Class | Category | Count |")
        out.append("|---|---:|---:|")
        for entry in cat_summary:
            cls_disp = entry["class"] or "\u2014"
            out.append(f"| {cls_disp} | {entry['category']} | {entry['count']} |")
        total_cnt = sum(e["count"] for e in cat_summary)
        out.append(f"| **Total** | | **{total_cnt}** |")
        out.append("")

    # ---- 2. Balance Sheet ----------------------------------------------------
    liabilities = assets.get("liabilities", {})
    out.append("## 2. Balance Sheet\n")
    ccies = sorted(
        c for c in (set(assets["cash"]) | set(assets["time_deposits"]) | set(assets["investments"]) | set(liabilities))
        if (assets["cash"].get(c, 0.0) + assets["time_deposits"].get(c, 0.0)
            + assets["investments"].get(c, 0.0) + liabilities.get(c, 0.0)) != 0.0
    )

    # Transposed layout: asset/liability types as rows, currencies as columns,
    # final column = SGD Equivalent (native-currency sum × rate per row).
    rows: list[tuple[str, dict[str, float], float | None, bool]] = [
        # (label, {ccy: native_value}, multiplier_for_liability)
        ("Cash", assets["cash"], None, False),
        ("Time Deposits", assets["time_deposits"], None, False),
        ("Investments", assets["investments"], None, False),
        ("Liabilities", liabilities, None, True),
        ("Net Position", {}, None, False),
    ]

    # Build the "Net Position" row: Cash + TD + Inv - Liab per currency.
    net_by_ccy: dict[str, float] = {}
    for c in ccies:
        net_by_ccy[c] = (assets["cash"].get(c, 0.0) + assets["time_deposits"].get(c, 0.0)
                         + assets["investments"].get(c, 0.0) - liabilities.get(c, 0.0))

    # Build the header.
    header_parts = ["| Asset / Liability"]
    for c in ccies:
        header_parts.append(c)
    if use_fx:
        assert fx_rates is not None
        header_parts.append("SGD Eq.")
    out.append(" | ".join(header_parts) + " |")
    sep_parts = ["|---"]
    for _ in ccies:
        sep_parts.append("---:")
    if use_fx:
        sep_parts.append("---:")
    out.append(" | ".join(sep_parts) + " |")

    # Data rows.
    tot_inv = 0.0
    for label, by_ccy, _mult, is_lia in rows:
        vals: list[str] = [label]
        row_sgd = 0.0
        row_native = 0.0
        for c in ccies:
            if label == "Net Position":
                v = net_by_ccy.get(c, 0.0)
            else:
                v = by_ccy.get(c, 0.0)
            # Liabilities reduce net worth — display as negative.
            if is_lia:
                v = -v
            vals.append(fmt(v))
            row_native += v
            if use_fx and not is_lia:
                s = convert_to_sgd(v, c, fx_rates) or 0.0
                row_sgd += s
            elif use_fx and is_lia:
                s = convert_to_sgd(v, c, fx_rates) or 0.0
                row_sgd += s
        if label == "Investments":
            tot_inv = row_native
        if use_fx:
            vals.append(fmt(row_sgd))
        out.append(" | ".join(vals) + " |")

    if use_fx:
        out.append("")  # blank line after table
    if not mbc:
        out.append("_No transactions \u2014 cash-flow statement below is not applicable._\n")

    # ---- 2.1 Balance Sheet Drill-Down (by bucket -> currency) ---------------
    if drilldown:
        out.append("## 2.1 Balance Sheet Drill-Down\n")
        out.append("Account-level derivation of every figure in the Balance Sheet above, grouped by asset category (Cash / Time Deposit / Investment / Liability). Within each category all accounts are listed in one table, with one subtotal row per currency. Accounts that contribute nothing are listed under Dropped as data-quality flags. SGD Equivalent uses the same FX rate as section 2.\n")
        agg: dict[tuple[str, str, str, str, str], tuple[float, str]] = {}
        for r in drilldown:
            key = (r["currency"], r["bucket"], r.get("institution", ""),
                   r["account_no"], r["account_type"])
            cur, deriv = agg.get(key, (0.0, r["derivation"]))
            agg[key] = (cur + (r.get("native_value") or 0.0), deriv)

        BUCKET_ORDER = ["Cash", "Time Deposit", "Investment", "Liability", "Dropped"]
        for bucket in BUCKET_ORDER:
            bvals = [(c, i, a, t, d, v) for (c, b, i, a, t), (v, d) in agg.items()
                     if b == bucket and (v != 0.0 or bucket == "Dropped")]
            if not bvals:
                continue
            out.append(f"### {bucket}\n")
            out.append("| Institution | Account | Type | Derivation | CCY | OC | SGD Eq. |")
            out.append("|---|---|---|---|---|---:|---:|")
            # Order currencies by descending SGD-equivalent so the largest
            # currency appears first (matching the example layout).
            ccy_sgd: dict[str, float] = defaultdict(float)
            for c, _i, _a, _t, _d, v in bvals:
                se = convert_to_sgd(v, c, fx_rates)
                ccy_sgd[c] += se if se is not None else 0.0
            csum_all = 0.0
            csum_sgd_all = 0.0
            for ccy in sorted(ccy_sgd, key=lambda x: -ccy_sgd[x]):
                # Each currency's rows, then its subtotal row immediately after.
                cs = 0.0
                cs_sgd = 0.0
                for c, i, a, t, d, v in sorted(
                    [x for x in bvals if x[0] == ccy], key=lambda x: -x[5]
                ):
                    se = convert_to_sgd(v, ccy, fx_rates)
                    out.append(f"| {i} | {a} | {t} | {d} | {ccy} | {fmt(v)} | {fmt(se)} |")
                    cs += v
                    cs_sgd += se if se is not None else 0.0
                out.append(
                    f"| **{ccy} subtotal** | | | | | **{fmt(cs)}** | **{fmt(cs_sgd)}** |"
                )
                csum_all += cs
                csum_sgd_all += cs_sgd
            # The bucket-level OC total sums across currencies, which is
            # meaningless (you cannot add SGD + JPY). Show only the SGD
            # equivalent total; leave the OC cell as a non-applicable dash.
            n_ccy = len(ccy_sgd)
            oc_total_cell = "—" if n_ccy > 1 else fmt(csum_all)
            out.append(
                f"| **{bucket} total** | | | | | **{oc_total_cell}** | **{fmt(csum_sgd_all)}** |"
            )
            out.append("")

        out.append("")

    # ---- 3. Cash Flow Statement ----------------------------------------------
    # Transposed layout (mirrors the Balance Sheet): cash-flow line items as
    # rows, one column per currency, final column = SGD Equivalent. Outflows
    # (Expense, Transfer Out) and the derived net figures are shown with their
    # natural sign (outflows negative).
    out.append("## 3. Cash Flow Statement\n")
    if mbc:
        cf_ccies = sorted(mbc)
        # Helper: pull a per-currency field across all currencies.
        def _col(field: str) -> dict[str, float]:
            return {c: mbc[c][field] for c in cf_ccies}

        # One row per cash-flow line item, each spanning all currency columns.
        # Internal transfers = moves between the user's own accounts (not real
        # cash flow); external transfers = flows to/from third parties. Outflows
        # (expense, transfer out) are shown with their natural negative sign.
        # (label, {ccy: signed_value}, bold?)
        cf_rows: list[tuple[str, dict[str, float], bool]] = [
            ("Income", _col("income"), False),
            ("Transfer In (External)", _col("transfer_in_external"), False),
            ("Transfer In (Internal)", _col("transfer_in_internal"), False),
            ("Expense", {c: -mbc[c]["expense"] for c in cf_ccies}, False),
            ("Transfer Out (External)", {c: -mbc[c]["transfer_out_external"] for c in cf_ccies}, False),
            ("Transfer Out (Internal)", {c: -mbc[c]["transfer_out_internal"] for c in cf_ccies}, False),
            ("Net Operating", _col("net_operating"), True),
            ("Net Change in Cash", _col("net_change_cash"), True),
        ]

        header_parts = ["| Cash Flow"]
        for c in cf_ccies:
            header_parts.append(c)
        if use_fx:
            header_parts.append("SGD Eq.")
        out.append(" | ".join(header_parts) + " |")
        sep_parts = ["|---"]
        for _ in cf_ccies:
            sep_parts.append("---:")
        if use_fx:
            sep_parts.append("---:")
        out.append(" | ".join(sep_parts) + " |")

        for label, by_ccy, bold in cf_rows:
            cf_vals: list[str] = [f"**{label}**" if bold else label]
            row_sgd = 0.0
            for c in cf_ccies:
                v = by_ccy.get(c, 0.0)
                cf_vals.append(fmt(v))
                if use_fx:
                    s = convert_to_sgd(v, c, fx_rates) or 0.0
                    row_sgd += s
            if use_fx:
                cf_vals.append(fmt(row_sgd))
            out.append(" | ".join(cf_vals) + " |")
        out.append("")
    else:
        out.append("_Not applicable \u2014 statement has no transactions._\n")

    # ---- Income Breakdown ----------------------------------------------------
    if income_drilldown:
        out.append("## Income Breakdown\n")
        # Collect all currencies across all sources.
        income_ccies: list[str] = sorted(set(
            c for entry in income_drilldown for c in entry["by_currency"]
        ))
        header_parts = ["| Source"]
        for c in income_ccies:
            header_parts.append(c)
        if use_fx:
            header_parts.append("SGD Eq.")
        out.append(" | ".join(header_parts) + " |")
        sep_parts = ["|---"]
        for _ in income_ccies:
            sep_parts.append("---:")
        if use_fx:
            sep_parts.append("---:")
        out.append(" | ".join(sep_parts) + " |")

        tot_sgd = 0.0
        for entry in income_drilldown:
            source = entry["source"]
            by_ccy = entry["by_currency"]
            income_cells: list[str] = [source]
            row_sgd = 0.0
            for c in income_ccies:
                amt = by_ccy.get(c, 0.0)
                income_cells.append(fmt(amt))
                if use_fx:
                    conv = convert_to_sgd(amt, c, fx_rates) or 0.0
                    row_sgd += conv
                else:
                    tot_sgd += amt
            if use_fx:
                tot_sgd += row_sgd
                income_cells.append(fmt(row_sgd))
            out.append(" | ".join(income_cells) + " |")
        if use_fx and tot_sgd:
            total_parts = ["| **Total Income**"]
            for _ in income_ccies:
                total_parts.append("")
            total_parts.append(f"**{fmt(tot_sgd)}**")
            out.append(" | ".join(total_parts) + " |")
        elif not use_fx:
            out.append(f"| **Total Income (per currency above)** | | |")
        out.append("")

        _render_txn_detail_table(out, income_drilldown, "source", use_fx, fx_rates)

    # ---- Expense Breakdown ---------------------------------------------------
    if expense_drilldown:
        out.append("## Expense Breakdown\n")
        expense_ccies: list[str] = sorted(set(
            c for entry in expense_drilldown for c in entry["by_currency"]
        ))
        header_parts = ["| Category"]
        for c in expense_ccies:
            header_parts.append(c)
        if use_fx:
            header_parts.append("SGD Eq.")
        out.append(" | ".join(header_parts) + " |")
        sep_parts = ["|---"]
        for _ in expense_ccies:
            sep_parts.append("---:")
        if use_fx:
            sep_parts.append("---:")
        out.append(" | ".join(sep_parts) + " |")

        totals_sgd = 0.0
        for entry in expense_drilldown:
            cat = entry["category"]
            by_ccy = entry["by_currency"]
            expense_cells: list[str] = [cat]
            row_sgd = 0.0
            for c in expense_ccies:
                amt = by_ccy.get(c, 0.0)
                expense_cells.append(fmt(abs(amt)))
                if use_fx:
                    conv = convert_to_sgd(amt, c, fx_rates) or 0.0
                    row_sgd += abs(conv)
                else:
                    totals_sgd += abs(amt)
            if use_fx:
                totals_sgd += row_sgd
                expense_cells.append(fmt(row_sgd))
            out.append(" | ".join(expense_cells) + " |")
        if use_fx and totals_sgd:
            total_parts = ["| **Total Expense**"]
            for _ in expense_ccies:
                total_parts.append("")
            total_parts.append(f"**{fmt(totals_sgd)}**")
            out.append(" | ".join(total_parts) + " |")
        elif not use_fx:
            out.append(f"| **Total Expense (per currency above)** | | |")
        out.append("")

        _render_txn_detail_table(out, expense_drilldown, "category", use_fx, fx_rates)

    # ---- Transfer Breakdown --------------------------------------------------
    if transfer_drilldown:
        out.append("## Transfer Breakdown\n")
        transfer_ccies: list[str] = sorted(set(
            c for entry in transfer_drilldown for c in entry["by_currency"]
        ))
        header_parts = ["| Category"]
        for c in transfer_ccies:
            header_parts.append(c)
        if use_fx:
            header_parts.append("SGD Eq.")
        out.append(" | ".join(header_parts) + " |")
        sep_parts = ["|---"]
        for _ in transfer_ccies:
            sep_parts.append("---:")
        if use_fx:
            sep_parts.append("---:")
        out.append(" | ".join(sep_parts) + " |")

        totals_sgd = 0.0
        for entry in transfer_drilldown:
            cat = entry["category"]
            by_ccy = entry["by_currency"]
            cells: list[str] = [cat]
            row_sgd = 0.0
            for c in transfer_ccies:
                amt = by_ccy.get(c, 0.0)
                cells.append(fmt(amt))
                if use_fx:
                    conv = convert_to_sgd(amt, c, fx_rates) or 0.0
                    row_sgd += abs(conv)
                else:
                    totals_sgd += abs(amt)
            if use_fx:
                totals_sgd += row_sgd
                cells.append(fmt(row_sgd))
            out.append(" | ".join(cells) + " |")
        if use_fx and totals_sgd:
            total_parts = ["| **Total Transfers**"]
            for _ in transfer_ccies:
                total_parts.append("")
            total_parts.append(f"**{fmt(totals_sgd)}**")
            out.append(" | ".join(total_parts) + " |")
        elif not use_fx:
            out.append(f"| **Total Transfers (per currency above)** | | |")
        out.append("")

        _render_txn_detail_table(out, transfer_drilldown, "category", use_fx, fx_rates)

    # ---- 4. Key Observations -------------------------------------------------
    out.append("## 4. Key Observations\n")
    if not mbc:
        out.append("- Balance-sheet only statement: review asset allocation (cash vs investments).\n")
    for ccy in sorted(mbc):
        m = mbc[ccy]
        if m["net_operating"] <= 0:
            out.append(f"- **{ccy}:** operating cash flow non-positive \u2014 income did not cover spending.\n")
        elif m["savings_rate"] >= 0.30:
            out.append(f"- **{ccy}:** strong retained cash flow (\u226530%).\n")
        if m["reconciliation_ok"] is False:
            out.append(
                f"- **{ccy}:** balance/cash-flow gap \u2014 money likely moved to/from an "
                + "account not in this statement (external transfer).\n"
            )
        if m["transfer_out"] > 0:
            out.append(f"- **{ccy}:** transfers out {fmt(m['transfer_out'])} (excluded from operating CF).\n")
    if tot_inv > 0 and (use_fx or len(ccies) <= 1):
        out.append(f"- Investments total {fmt(tot_inv)} \u2014 part of net worth, not cash-flow liquid.\n")
    out.append("")

    # ---- 5. FX Rates Reference -----------------------------------------------
    if use_fx:
        assert fx_rates is not None  # guaranteed by use_fx
        out.append("## 5. FX Rates Reference\n")
        out.append(
            f"All non-SGD balances above are converted at the statement-period-end rate "
            + f"(**{fx_rates['date']}**) sourced from **{fx_rates['source']}** "
            + "(ECB reference rates via Frankfurter API). Rates shown are SGD per 1 unit of "
            + "foreign currency, limited to the currencies present in this report.\n"
        )
        used_ccies = sorted((set(ccies) | set(mbc)) - {FX_BASE})
        out.append("| Currency | Rate (1 CCY = SGD) | Date | Source |")
        out.append("|---|---:|---|---|")
        for ccy in used_ccies:
            rate = fx_rates["rates"].get(ccy)
            if rate is None:
                continue
            out.append(
                f"| {ccy} | {rate:.4f} | {fx_rates['date']} | {fx_rates['source']} |"
            )
        out.append("")

    # ---- 6. Notes & Caveats --------------------------------------------------
    out.append("## " + ("6" if use_fx else "5") + ". Notes & Caveats\n")
    out.append(
        "- Balance sheet + cash flow only; merchant-level spending categorization is out of scope.\n"
    )
    out.append(
        "- Transfers (own-account / fixed-deposit moves) are separated from Income/Expense. "
        + "Detection uses `is_internal_transfer` when present, else `TRANSFER_KEYWORDS`.\n"
    )
    if use_fx:
        assert fx_rates is not None  # guaranteed by use_fx
        out.append(
            f"- Multi-currency amounts are converted to SGD at the statement-period-end rate "
            + f"({fx_rates['date']}) from {fx_rates['source']}; the SGD Equivalent column and the "
            + "SGD net position reflect real value. Source rate table is in section 5 above.\n"
        )
    else:
        out.append(
            "- Multi-currency statements are reported per currency. FX conversion was NOT applied "
            + "(FX rates unavailable or not a consolidated report). The Total row sums per currency "
            + "and is not a real net value.\n"
        )
    out.append(
        "- This is an initial automated pass. Transfer vs expense classification and asset "
        + "coverage (e.g. ICBC fixed-deposit rollovers) may need tuning \u2014 refine from here.\n"
    )
    return "\n".join(out)
