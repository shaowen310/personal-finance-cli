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

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from pfa_fx import BASE_CCY, convert_to_sgd as pfa_convert_to_sgd

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
    return pfa_convert_to_sgd(amount, currency, fx_rates["rates"])


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def fmt(v: float | None) -> str:
    """Format a float with two decimals or return an em-dash for None."""
    return f"{v:,.2f}" if isinstance(v, (int, float)) else "\u2014"


def _it_display_amount(txn: dict[str, Any]) -> float:
    """Cash-flow sign for an internal-transfer transaction in this report.

    Money OUT of an account is shown negative, money INTO an account positive.
    Liability accounts (``credit_card``) store a payment as a negative amount
    (it reduces the liability owed), which is the opposite of the physical flow
    of funds, so flip their sign to reflect the real direction. Other account
    types already carry cash-flow sign (debit negative, credit positive). The
    IR itself is never mutated — this is display-only.
    """
    amt = float(txn.get("amount", 0.0))
    if str(txn.get("account_type", "")) == "credit_card":
        return -amt
    return amt


def _render_txn_detail_table(out: list[str], entries: list[dict[str, Any]], key: str,
                             use_fx: bool,
                             fx_rates: dict[str, Any] | None,
                             signed_total: bool = False) -> dict[str, float]:
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
    amount, signed so refund rows display negative and net correctly) columns,
    plus an ``SGD Eq.`` column when ``use_fx`` is set.

    Returns a mapping of entry heading -> "Total SGD Eq." value. By default the
    total nets the signed amounts per currency, converts each net, then takes the
    absolute value — correct for single-signed sections (Income/Expense) so it
    matches the per-category summary. When ``signed_total`` is set (used for the
    Internal Transfers "Transactions" table, which mixes inflow and outflow legs
    in one entry), the sign is preserved so the total is a plain arithmetic sum
    that agrees with the section's signed summary total.
    """
    totals: dict[str, float] = {}
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
        ccy_net: dict[str, float] = {}
        for t in txns:
            desc = t["description"].strip().replace("|", "\\|")
            ccy = t.get("currency", "")
            amt = float(t["amount"])
            oc = fmt(amt)
            if use_fx:
                ccy_net[ccy] = ccy_net.get(ccy, 0.0) + amt
                sgd = convert_to_sgd(amt, ccy, fx_rates)
                out.append(
                    f"| {t['date']} | {t.get('bank', '')} | {t.get('account', '')} | "+
                    f"{t.get('account_type', '')} | {desc} | {ccy} | {oc} | {fmt(sgd)} |"
                )
            else:
                out.append(
                    f"| {t['date']} | {t.get('bank', '')} | {t.get('account', '')} | "+
                    f"{t.get('account_type', '')} | {desc} | {ccy} | {oc} |"
                )
        if use_fx:
            if signed_total:
                total = sum(
                    (convert_to_sgd(net, c, fx_rates) or 0.0)
                    for c, net in ccy_net.items()
                )
            else:
                total = sum(
                    abs(convert_to_sgd(net, c, fx_rates) or 0.0)
                    for c, net in ccy_net.items()
                )
            totals[heading] = total
            out.append(
                f"| | | | | **Total SGD Eq.** | | | **{fmt(total)}** |"
            )
        out.append("\n")
    return totals


def _assert_sgd_total(section: str, label: str, summary_sgd: float,
                      detail_total: float) -> None:
    """Assert the summary table's SGD Eq. for one entry equals the drill-down
    detail table's "Total SGD Eq." for the same entry."""
    if not math.isclose(summary_sgd, detail_total, rel_tol=1e-9, abs_tol=0.005):
        raise AssertionError(
            f"{section}: SGD Eq. total mismatch for {label!r}: "
            f"summary={summary_sgd:.2f} vs drill-down={detail_total:.2f}"
        )


def render_report(result: dict[str, Any], consolidated: bool = False,
                  fx_rates: dict[str, Any] | None = None,
                  drilldown: list[dict[str, Any]] | None = None,
                  income_drilldown: list[dict[str, Any]] | None = None,
                  expense_drilldown: list[dict[str, Any]] | None = None,
                  transfer_drilldown: list[dict[str, Any]] | None = None,
                  fx_gain_loss: dict[str, Any] | None = None,
                  cat_summary: list[dict[str, Any]] | None = None,
                  cat_coverage: float | None = None,
                  warnings: list[str] | None = None) -> str:
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
    # Warnings surfaced in the top "## 0. Warnings" section. Reconciliation
    # warnings come from the analysis result; the section-specific warnings
    # (internal-transfer imbalance, credit-card caveats) are appended below as
    # they are detected, so they appear both here and in their detailed section.
    report_warnings: list[str] = list(warnings or [])

    # ---- Title ---------------------------------------------------------------
    title = "Consolidated Balance Sheet & Funds Flow" if consolidated else "Personal Balance Sheet & Funds Flow"
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

    # ---- 1. Summary ----------------------------------------------------------
    out.append("## 1. Summary\n")
    net_pos = (sum(assets["cash"].values()) + sum(assets["time_deposits"].values())
               + sum(assets["investments"].values()))
    liabilities_bs = assets.get("liabilities", {})
    has_balances = (bool(mbc) or any(assets["cash"].values())
                    or any(assets["time_deposits"].values())
                    or any(assets["investments"].values()) or any(liabilities_bs.values()))
    if has_balances:
        # Mirror the Balance Sheet currency set (account balances), so currencies
        # with a balance but no transactions in the window (e.g. a dormant USD
        # account) still appear here. Transaction-derived metrics default to zeros.
        def _default_metrics() -> dict[str, Any]:
            return {"income": 0.0, "expense": 0.0, "transfer_in_external": 0.0,
                    "transfer_out_external": 0.0, "net_change_cash": 0.0,
                    "net_operating": 0.0, "savings_rate": 0.0, "closing": None}
        exec_ccies = sorted(
            c for c in (set(mbc) | set(assets["cash"]) | set(assets["time_deposits"])
                        | set(assets["investments"]) | set(liabilities_bs))
            if (assets["cash"].get(c, 0.0) + assets["time_deposits"].get(c, 0.0)
                + assets["investments"].get(c, 0.0) + liabilities_bs.get(c, 0.0)) != 0.0
        )
        # Closing mirrors the Balance Sheet "Net Position" row (Cash + Time
        # Deposits + Investments - Liabilities), so the headline figures agree.
        def _net_pos(ccy: str) -> float:
            return (assets["cash"].get(ccy, 0.0) + assets["time_deposits"].get(ccy, 0.0)
                    + assets["investments"].get(ccy, 0.0) - liabilities_bs.get(ccy, 0.0))
        # Table 1: per native currency
        out.append("| Currency | Closing | Income | Expense | Net |")
        out.append("|---|---:|---:|---:|---:|")
        for ccy in exec_ccies:
            m = mbc.get(ccy) or _default_metrics()
            # Income / Net mirror the Funds Flow Statement (\u00a73): external
            # transfers are part of operating, internal transfers are excluded.
            income_ff = m["income"] + m["transfer_in_external"]
            net_ff = (m["income"] + m["transfer_in_external"]
                      - m["expense"] - m["transfer_out_external"])
            out.append(
                f"| {ccy} | {fmt(_net_pos(ccy))} | "
                + f"{fmt(income_ff)} | {fmt(m['expense'] + m['transfer_out_external'])} | {fmt(net_ff)} |"
            )
        if use_fx:
            assert fx_rates is not None  # guaranteed by use_fx
            fx_date = fx_rates.get("date", "")
            # Table 2: SGD-equivalent
            out.append("")
            out.append(f"**SGD Equivalent (as of {fx_date}):**\n")
            out.append("| Currency | Closing (SGD) | Income (SGD) | Expense (SGD) | Net (SGD) |")
            out.append("|---|---:|---:|---:|---:|")
            total_closing = total_income = total_expense = total_net = 0.0
            for ccy in exec_ccies:
                m = mbc.get(ccy) or _default_metrics()
                closing_sgd = convert_to_sgd(_net_pos(ccy), ccy, fx_rates)
                income_sgd = convert_to_sgd(m["income"] + m["transfer_in_external"], ccy, fx_rates)
                expense_sgd = convert_to_sgd(m["expense"] + m["transfer_out_external"], ccy, fx_rates)
                net_sgd = convert_to_sgd((m["income"] + m["transfer_in_external"]
                                         - m["expense"] - m["transfer_out_external"]), ccy, fx_rates)
                out.append(
                    f"| {ccy} | {fmt(closing_sgd)} | {fmt(income_sgd)} | "
                    + f"{fmt(expense_sgd)} | {fmt(net_sgd)} |"
                )
                total_closing += closing_sgd or 0.0
                total_income += income_sgd or 0.0
                total_expense += expense_sgd or 0.0
                total_net += net_sgd or 0.0
            out.append(
                f"| **Total** | {fmt(total_closing)} | {fmt(total_income)} | "
                + f"{fmt(total_expense)} | {fmt(total_net)} |"
            )
            total_savings = (f"{total_net / total_income * 100:.1f}%"
                             if total_income > 0
                             else "n/a")
            out.append("")
            out.append(f"- **Total Savings Rate:** {total_savings}")
        else:
            out.append(
                f"\n- **Net Position (cash + deposits + investments):** {fmt(net_pos)} "
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
        out.append("_No transactions \u2014 funds-flow statement below is not applicable._\n")

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

    # ---- 3. Funds Flow Statement ---------------------------------------------
    # Transposed layout (mirrors the Balance Sheet): cash-flow line items as
    # rows, one column per currency, final column = SGD Equivalent. Outflows
    # (Expense, Transfer Out) and the derived net figures are shown with their
    # natural sign (outflows negative).
    out.append("## 3. Funds Flow Statement\n")
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
        # Rows mirror the breakdowns: operating income/expense now encompass
        # external transfers (Transfer In/Out (External)); internal transfers are
        # excluded from net cash flow (see §Internal Transfers). Currency conversions
        # are surfaced as a single "Currency Conversion" line below Net Operating
        # Cash Flow (matching the "Currency Conversions" section).
        cf_rows: list[tuple[str, dict[str, float], bool]] = [
            ("Income", {c: mbc[c]["income"] + mbc[c]["transfer_in_external"]
                        for c in cf_ccies}, False),
            ("Expense", {c: -(mbc[c]["expense"] + mbc[c]["transfer_out_external"])
                         for c in cf_ccies}, False),
            ("Net Operating Funds Flow",
             {c: (mbc[c]["income"] + mbc[c]["transfer_in_external"]
                  - mbc[c]["expense"] - mbc[c]["transfer_out_external"])
              for c in cf_ccies}, True),
            ("Currency Conversion",
             {c: mbc[c]["fx_conversion_in"] - mbc[c]["fx_conversion_out"]
              for c in cf_ccies}, False),
            ("Net Change in Cash", _col("net_change_cash"), True),
        ]

        header_parts = ["| Funds Flow"]
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
        out.append(
            "_Internal transfers between your own accounts net to zero and are "
            "excluded from the funds flow; see §Internal Transfers. Currency "
            "Conversion is shown at face value per currency — its SGD-equivalent "
            "difference is the realized FX gain/loss in §Currency Conversions._\n"
        )
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
        income_summary_sgd: dict[str, float] = {}
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
                income_summary_sgd[source] = row_sgd
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

        income_detail_totals = _render_txn_detail_table(
            out, income_drilldown, "source", use_fx, fx_rates)
        for src, sgd in income_summary_sgd.items():
            _assert_sgd_total("Income Breakdown", src, sgd,
                              income_detail_totals.get(src, 0.0))

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
        expense_summary_sgd: dict[str, float] = {}
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
                expense_summary_sgd[cat] = row_sgd
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

        expense_detail_totals = _render_txn_detail_table(
            out, expense_drilldown, "category", use_fx, fx_rates)
        for cat, sgd in expense_summary_sgd.items():
            _assert_sgd_total("Expense Breakdown", cat, sgd,
                              expense_detail_totals.get(cat, 0.0))

    # ---- Internal Transfers (own-account moves) -----------------------------
    # Own-account moves must net to zero per currency: every transfer has an
    # equal and opposite leg. Compute the residual here so a warning can be
    # surfaced both in this section and in §Notes & Caveats.
    internal_imbalance: dict[str, float] = {}
    if transfer_drilldown:
        _net_by_ccy: dict[str, float] = defaultdict(float)
        for entry in transfer_drilldown:
            for t in entry.get("transactions", []):
                _net_by_ccy[t["currency"]] += _it_display_amount(t)
        internal_imbalance = {c: v for c, v in _net_by_ccy.items() if abs(v) > 0.005}

    if transfer_drilldown:
        out.append("## Internal Transfers\n")
        out.append(
            "_Own-account moves between the holder's accounts. The summary shows the "
            "signed value per currency, split into inflow and outflow legs (In positive, "
            "Out negative); each balanced pair nets to ~0 and is excluded from income/"
            "expense and the funds flow. Individual transactions follow below (same layout "
            "as the income/expense breakdowns)._\n"
        )
        int_ccies: list[str] = sorted(set(
            c for entry in transfer_drilldown for c in entry["by_currency"]
        ))
        header_parts = ["| Category"]
        for c in int_ccies:
            header_parts.append(c)
        if use_fx:
            header_parts.append("SGD Eq.")
        out.append(" | ".join(header_parts) + " |")
        sep_parts = ["|---"]
        for _ in int_ccies:
            sep_parts.append("---:")
        if use_fx:
            sep_parts.append("---:")
        out.append(" | ".join(sep_parts) + " |")

        # Split each entry into inflow and outflow legs so the reader sees the
        # direction of each own-account move. Signs follow cash flow (money OUT of
        # an account = negative, INTO an account = positive); a balanced pair nets
        # to ~0 in the funds flow. Credit-card legs flip to positive because the
        # card is the account that receives the payment.
        transfer_rows: list[dict[str, Any]] = []
        for entry in transfer_drilldown:
            cat = entry["category"]
            txns = entry.get("transactions", [])
            for label, sign in ((f"{cat} In", 1), (f"{cat} Out", -1)):
                leg = [t for t in txns if sign * _it_display_amount(t) > 0]
                if not leg:
                    continue
                by_ccy: dict[str, float] = defaultdict(float)
                for t in leg:
                    by_ccy[t["currency"]] += _it_display_amount(t)
                transfer_rows.append({
                    "category": label,
                    "by_currency": dict(by_ccy),
                    "transactions": leg,
                })

        totals_sgd = 0.0
        for entry in transfer_rows:
            cat = entry["category"]
            gross = entry["by_currency"]
            int_cells: list[str] = [cat]
            row_sgd = 0.0
            for c in int_ccies:
                amt = gross.get(c, 0.0)
                int_cells.append(fmt(amt))
                if use_fx:
                    conv = convert_to_sgd(amt, c, fx_rates) or 0.0
                    row_sgd += conv
                else:
                    totals_sgd += amt
            if use_fx:
                totals_sgd += row_sgd
                int_cells.append(fmt(row_sgd))
            out.append(" | ".join(int_cells) + " |")
        if totals_sgd:
            total_parts = ["| **Total Internal Transfers**"]
            for _ in int_ccies:
                total_parts.append("")
            total_parts.append(f"**{fmt(totals_sgd)}**")
            out.append(" | ".join(total_parts) + " |")
        out.append("")

        if internal_imbalance:
            out.append(
                "> **⚠ WARNING: Internal transfers do not net to zero.** Each own-account "
                "move must have an equal and opposite leg; the residual below means a leg "
                "is missing or unpaired (e.g. an investment transfer whose counterparty leg "
                "is absent from the IR, or a transfer mis-flagged as internal)."
            )
            for c in sorted(internal_imbalance):
                out.append(f"> - {c}: net {fmt(internal_imbalance[c])}")
            out.append("")

        # Combine all inflow/outflow legs into a single drill-down table titled
        # "Transactions" (the summary above still shows them split by direction).
        # Render each leg with its cash-flow display sign (credit-card payments flip
        # to positive, since the card is the account that receives the money) via a
        # shallow copy, leaving the source IR data untouched.
        combined_txns: list[dict[str, Any]] = []
        for entry in transfer_drilldown:
            for t in entry.get("transactions", []):
                d = dict(t)
                d["amount"] = _it_display_amount(t)
                combined_txns.append(d)
        if combined_txns:
            _render_txn_detail_table(
                out,
                [{"category": "Transactions", "by_currency": {},
                  "transactions": combined_txns}],
                "category", use_fx, fx_rates, signed_total=True,
            )

    # ---- Realized FX Gain/Loss ----------------------------------------------
    if fx_gain_loss:
        pairs = fx_gain_loss.get("pairs") or []
        base_ccy = fx_gain_loss.get("base_currency", "SGD")
        total = fx_gain_loss.get("total_sgd", 0.0)
        out.append("## Currency Conversions & Realized FX Gain/Loss\n")
        if not pairs:
            out.append("- No currency-conversion transactions detected in this period.\n")
        else:
            out.append(
                f"_Realized on currency-conversion pairs only; valued as of "
                f"**{fx_gain_loss.get('as_of') or 'n/a'}** using rates from "
                f"**{fx_gain_loss.get('source') or 'n/a'}** (base = {base_ccy})._\n"
            )
            out.append(
                "| Date | Given | Received | Implied Rate | "
                f"FX Gain/Loss ({base_ccy}) |"
            )
            out.append("|---|---|---|---|---:|")
            for p in pairs:
                g = p["given"]
                r = p["received"]
                gl = p["fx_gl_sgd"]
                sign = "+" if gl >= 0 else "-"
                out.append(
                    f"| {p.get('date', '')} "
                    f"| {fmt(g['amount'])} {g['currency']} "
                    f"| {fmt(r['amount'])} {r['currency']} "
                    f"| {p.get('implied_rate', 0):.4f} "
                    f"| {sign}{fmt(abs(gl))} |"
                )
            out.append(
                f"| | | | **Total** | "
                f"{'+' if total >= 0 else '-'}{fmt(abs(total))} |"
            )
            out.append("")
            by_recv = fx_gain_loss.get("by_received_currency") or {}
            if by_recv:
                detail = ", ".join(
                    f"{c}: {'+' if v >= 0 else '-'}{fmt(abs(v))}"
                    for c, v in by_recv.items()
                )
                out.append(f"_By received currency — {detail}_\n")

    # ---- 4. Key Observations -------------------------------------------------
    out.append("## 4. Key Observations\n")
    if not mbc:
        out.append("- Balance-sheet only statement: review asset allocation (cash vs investments).\n")
    for ccy in sorted(mbc):
        m = mbc[ccy]
        if m["net_operating"] <= 0:
            out.append(f"- **{ccy}:** operating funds flow non-positive \u2014 income did not cover spending.\n")
        elif m["savings_rate"] >= 0.30:
            out.append(f"- **{ccy}:** strong retained funds flow (\u226530%).\n")
        if m["reconciliation_ok"] is False:
            out.append(
                f"- **{ccy}:** balance/funds-flow gap \u2014 money likely moved to/from an "
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
        "- Balance sheet + funds flow only; merchant-level spending categorization is out of scope.\n"
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
    if internal_imbalance:
        report_warnings.append(
            f"Internal transfers did not net to zero "
            f"({', '.join(f'{c} {fmt(v)}' for c, v in sorted(internal_imbalance.items()))}). "
            "A missing/unpaired leg was detected \u2014 see \u00a7Internal Transfers."
        )
    # Carried-forward credit-card liabilities: a card whose statement period
    # falls outside the report window is shown at its last known closing balance,
    # which is not the true period-end position (e.g. a bill payment to the card
    # inside the window is recorded on the paying account only).
    cf_liab_rows: list[dict[str, Any]] = []
    for r in (drilldown or []):
        if r.get("carried_forward"):
            cf_liab_rows.append(r)
    if cf_liab_rows:
        detail = "; ".join(
            f"{r['institution']} {r['account_no']} "
            f"(stmt ends {r.get('period_to') or 'n/a'}, {r['currency']} {fmt(r['native_value'])})"
            for r in cf_liab_rows
        )
        report_warnings.append(
            "Credit-card balance carried forward: " + detail + " \u2014 statement period ends "
            "before the report-window end, so the reported balance is not the position as of "
            "the window end. Verify against the latest statement."
        )

    me_liab_rows: list[dict[str, Any]] = []
    for r in (drilldown or []):
        if r.get("missing_early"):
            me_liab_rows.append(r)
    if me_liab_rows:
        detail = "; ".join(
            f"{r['institution']} {r['account_no']} "
            f"(earliest txn {r.get('earliest_covered') or 'n/a'}, "
            f"{r['currency']} {fmt(r['native_value'])})"
            for r in me_liab_rows
        )
        report_warnings.append(
            "Credit-card transactions may be missing: " + detail + " \u2014 earliest covered "
            "transaction is after the report-window start; activity before it is not in this "
            "report. Verify against the full statement."
        )

    # ---- 0. Warnings (prominent summary near the top) -----------------------
    if report_warnings:
        _warn_block = ["## ⚠ Warnings\n"]
        for w in report_warnings:
            _warn_block.append(f"- **\u26a0 {w}**\n")
        _warn_block.append("\n")
        # Insert right after the top divider so all warnings are seen first.
        out.insert(out.index("---\n") + 1, "".join(_warn_block))
    return "\n".join(out)
