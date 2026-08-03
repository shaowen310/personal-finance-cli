"""Markdown report renderer for personal-finance-analysis.

Produces a six-section Markdown report from the structured analysis result
dict returned by ``analyze.analyze_file()``.

Also houses shared utilities (``convert_to_sgd``, ``FX_BASE``) used by both
the renderer and the main analyzer script.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from collections import defaultdict

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


def render_report(result: dict[str, Any], consolidated: bool = False,
                  fx_rates: dict[str, Any] | None = None,
                  drilldown: list[dict[str, Any]] | None = None,
                  income_drilldown: list[dict[str, Any]] | None = None,
                  expense_drilldown: list[dict[str, Any]] | None = None) -> str:
    """Render a balance-sheet + cash-flow Markdown report.

    Args:
        result: Dict from ``analyze.analyze_file()`` with keys
            ``meta``, ``metrics_by_ccy``, ``assets``, ``source``.
        consolidated: If True, use consolidated report title & layout.
        fx_rates: FX rate dict from ``analyze.fetch_fx_rates()``, or None.
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

    # ---- 2. Balance Sheet ----------------------------------------------------
    liabilities = assets.get("liabilities", {})
    out.append("## 2. Balance Sheet\n")
    if use_fx:
        out.append("| Currency | Cash | Time Deposits | Investments | Liabilities | Net Position | SGD Equivalent |")
        out.append("|---|---:|---:|---:|---:|---:|---:|")
    else:
        out.append("| Currency | Cash | Time Deposits | Investments | Liabilities | Net Position |")
        out.append("|---|---:|---:|---:|---:|---:|")
    ccies = sorted(
        c for c in (set(assets["cash"]) | set(assets["time_deposits"]) | set(assets["investments"]) | set(liabilities))
        if (assets["cash"].get(c, 0.0) + assets["time_deposits"].get(c, 0.0)
            + assets["investments"].get(c, 0.0) + liabilities.get(c, 0.0)) != 0.0
    )
    tot_cash = tot_td = tot_inv = tot_lia = tot_np = 0.0
    tot_sgd = tot_cash_sgd = tot_td_sgd = tot_inv_sgd = tot_lia_sgd = tot_np_sgd = 0.0
    for c in ccies:
        csh = assets["cash"].get(c, 0.0)
        td = assets["time_deposits"].get(c, 0.0)
        iv = assets["investments"].get(c, 0.0)
        lia = liabilities.get(c, 0.0)
        np_ = csh + td + iv - lia
        tot_cash += csh; tot_td += td; tot_inv += iv; tot_lia += lia; tot_np += np_  # noqa: E702
        if use_fx:
            sgd_csh = convert_to_sgd(csh, c, fx_rates) or 0.0
            sgd_td = convert_to_sgd(td, c, fx_rates) or 0.0
            sgd_iv = convert_to_sgd(iv, c, fx_rates) or 0.0
            sgd_lia = convert_to_sgd(lia, c, fx_rates) or 0.0
            sgd_np = sgd_csh + sgd_td + sgd_iv - sgd_lia
            tot_cash_sgd += sgd_csh; tot_td_sgd += sgd_td  # noqa: E702
            tot_inv_sgd += sgd_iv; tot_lia_sgd += sgd_lia  # noqa: E702
            tot_np_sgd += sgd_np  # noqa: E702
            tot_sgd += sgd_np
            out.append(f"| {c} | {fmt(csh)} | {fmt(td)} | {fmt(iv)} | {fmt(lia)} | {fmt(np_)} | {fmt(sgd_np)} |")
        else:
            out.append(f"| {c} | {fmt(csh)} | {fmt(td)} | {fmt(iv)} | {fmt(lia)} | {fmt(np_)} |")
    if use_fx:
        out.append(
            f"| **Total (SGD)** | **{fmt(tot_cash_sgd)}** | **{fmt(tot_td_sgd)}** | "
            + f"**{fmt(tot_inv_sgd)}** | **{fmt(tot_lia_sgd)}** | **{fmt(tot_np_sgd)}** | **{fmt(tot_sgd)}** |\n"
        )
    elif len(ccies) > 1:
        out.append(
            "| **Total** | *n/a* | *n/a* | *n/a* | *n/a* | *n/a* | "
            + "(cross-currency sum not meaningful without FX rates)\n"
        )
    else:
        out.append(f"| **Total** | **{fmt(tot_cash)}** | **{fmt(tot_td)}** | **{fmt(tot_inv)}** | **{fmt(tot_lia)}** | **{fmt(tot_np)}** |\n")
    if not mbc:
        out.append("_No transactions \u2014 cash-flow statement below is not applicable._\n")

    # ---- 2.1 Balance Sheet Drill-Down (by currency -> bucket) ---------------
    if drilldown:
        out.append("## 2.1 Balance Sheet Drill-Down\n")
        out.append("Account-level derivation of every figure in the Balance Sheet above, grouped first by currency then by bucket (Cash / Time Deposit / Investment / Liability). Accounts that contribute nothing are listed under Dropped as data-quality flags. SGD Equivalent uses the same FX rate as section 2.\n")
        # Aggregate raw rows (sum across files, collapse duplicate account lines).
        agg: dict[tuple[str, str, str, str, str], tuple[float, str]] = {}
        for r in drilldown:
            key = (r["currency"], r["bucket"], r.get("institution", ""),
                   r["account_no"], r["account_type"])
            cur, deriv = agg.get(key, (0.0, r["derivation"]))
            agg[key] = (cur + (r.get("native_value") or 0.0), deriv)

        # Order currencies by SGD-equivalent total (descending).
        ccy_sgd: dict[str, float] = defaultdict(float)
        for (ccy, _b, _i, _a, _t), (val, _d) in agg.items():
            se = convert_to_sgd(val, ccy, fx_rates)
            ccy_sgd[ccy] += se if se is not None else 0.0
        used_ccies = [c for c in sorted(ccy_sgd, key=lambda x: -ccy_sgd[x])
                      if ccy_sgd[c] != 0.0 or any(k[0] == c and k[1] == "Dropped" for k in agg)]

        BUCKET_ORDER = ["Cash", "Time Deposit", "Investment", "Liability", "Dropped"]
        for ccy in used_ccies:
            rate_sgd = fx_rates["rates"].get(ccy) if (fx_rates and ccy != FX_BASE) else None
            rate_disp = f"1 {ccy} = {rate_sgd:.4f} SGD" if rate_sgd is not None else "base currency (1 SGD = 1 SGD)"
            out.append(f"### {ccy}  ")
            out.append(f"_FX: {rate_disp}_\n")
            for bucket in BUCKET_ORDER:
                bvals = [(i, a, t, d, v) for (c, b, i, a, t), (v, d) in agg.items()
                         if c == ccy and b == bucket and (v != 0.0 or bucket == "Dropped")]
                if not bvals:
                    continue
                out.append(f"**{bucket}**\n")
                out.append("| Institution | Account | Type | Native Value | Derivation | SGD Eq |")
                out.append("|---|---|---|---:|---|---:|")
                bsum = 0.0
                bsum_sgd = 0.0
                for i, a, t, d, v in sorted(bvals, key=lambda x: -x[4]):
                    se = convert_to_sgd(v, ccy, fx_rates)
                    out.append(f"| {i} | {a} | {t} | {fmt(v)} | {d} | {fmt(se)} |")
                    bsum += v
                    bsum_sgd += se if se is not None else 0.0
                out.append(
                    f"| **{bucket} subtotal** | | | **{fmt(bsum)}** | | **{fmt(bsum_sgd)}** |"
                )
                out.append("")
            out.append("")

    # ---- 3. Cash Flow Statement ----------------------------------------------
    out.append("## 3. Cash Flow Statement\n")
    if mbc:
        for ccy in sorted(mbc):
            m = mbc[ccy]
            out.append(f"### {ccy}\n")
            out.append("| Class | Inflow (+) | Outflow (\u2212) |")
            out.append("|---|---:|---:|")
            out.append(f"| Income | {fmt(m['income'])} | |")
            out.append(f"| Transfer In | {fmt(m['transfer_in'])} | |")
            out.append(f"| Expense | | {fmt(m['expense'])} |")
            out.append(f"| Transfer Out | | {fmt(m['transfer_out'])} |")
            out.append(f"| **Net Operating** (Income \u2212 Expense) | | **{fmt(m['net_operating'])}** |")
            out.append(f"| **Net Change in Cash** | | **{fmt(m['net_change_cash'])}** |")
            if m["reconciliation_ok"] is not None:
                flag = "\u2713 matches balance change" if m["reconciliation_ok"] else "\u26a0 gap vs balance (likely external transfer)"
                out.append(f"| Reconciliation | | {flag} |")
            out.append("")
    else:
        out.append("_Not applicable \u2014 statement has no transactions._\n")

    # ---- Income Breakdown ----------------------------------------------------
    if income_drilldown:
        out.append("## Income Breakdown\n")
        out.append("| Source | Currency | Amount |")
        out.append("|---|---:|---:|")
        tot_sgd = 0.0
        for entry in income_drilldown:
            source = entry["source"]
            by_ccy: dict[str, float] = entry["by_currency"]
            for ccy in sorted(by_ccy):
                amt = by_ccy[ccy]
                out.append(f"| {source} | {ccy} | {fmt(amt)} |")
            # If multi-currency and FX available, show SGD equivalent line.
            if use_fx and by_ccy:
                assert fx_rates is not None  # guaranteed by use_fx
                sgd_total = 0.0
                for ccy, amt in by_ccy.items():
                    conv = convert_to_sgd(amt, ccy, fx_rates)
                    if conv is not None:
                        sgd_total += conv
                tot_sgd += sgd_total
                source_label = f"{source} (SGD eq.)"
                out.append(f"| *{source_label}* | *SGD* | *{fmt(sgd_total)}* |")
            else:
                for ccy, amt in by_ccy.items():
                    tot_sgd += amt  # raw sum (single-currency or no FX)
        if use_fx and tot_sgd:
            out.append(f"| **Total Income (SGD)** | | **{fmt(tot_sgd)}** |")
        elif not use_fx:
            out.append(f"| **Total Income (per currency above)** | | |")
        out.append("")

        # ---- Per-source transaction drill-down ------------------------------
        for entry in income_drilldown:
            txns: list[dict[str, Any]] = entry.get("transactions", [])
            if not txns:
                continue
            source = entry["source"]
            out.append(f"### {source}\n")
            out.append("| Date | Institution | Account | Account Type | Description | Amount |")
            out.append("|---|---|---|---|---|---:|")
            for t in txns:
                desc = t["description"].strip().replace("|", "\\|")
                cur = t.get("currency", "")
                bank = t.get("bank", "")
                acct = t.get("account", "")
                acct_type = t.get("account_type", "")
                out.append(f"| {t['date']} | {bank} | {acct} | {acct_type} | {desc} | {fmt(t['amount'])} {cur} |")
            out.append("")

    # ---- Expense Breakdown ---------------------------------------------------
    if expense_drilldown:
        out.append("## Expense Breakdown\n")
        out.append("| Category | Currency | Amount |")
        out.append("|---|---:|---:|")
        totals_sgd = 0.0
        for entry in expense_drilldown:
            cat = entry["category"]
            by_ccy = entry["by_currency"]
            # Category row: one line per currency
            for ccy in sorted(by_ccy):
                amt = by_ccy[ccy]
                out.append(f"| {cat} | {ccy} | {fmt(abs(amt))} |")
            # SGD equivalent row if FX available
            if use_fx and by_ccy:
                assert fx_rates is not None
                sgd_total = 0.0
                for ccy, amt in by_ccy.items():
                    conv = convert_to_sgd(amt, ccy, fx_rates)
                    if conv is not None:
                        sgd_total += conv
                totals_sgd += abs(sgd_total)
                out.append(f"| *{cat} (SGD eq.)* | *SGD* | *{fmt(abs(sgd_total))}* |")
            else:
                for ccy, amt in by_ccy.items():
                    totals_sgd += abs(amt)
        if use_fx and totals_sgd:
            out.append(f"| **Total Expense (SGD)** | | **{fmt(totals_sgd)}** |")
        elif not use_fx:
            out.append(f"| **Total Expense (per currency above)** | | |")
        out.append("")

        # ---- Per-category transaction drill-down ---------------------------
        for entry in expense_drilldown:
            txns = entry.get("transactions", [])
            if not txns:
                continue
            cat = entry["category"]
            out.append(f"### {cat}\n")
            out.append("| Date | Institution | Account | Account Type | Description | Amount |")
            out.append("|---|---|---|---|---|---:|")
            for t in txns:
                desc = t["description"].strip().replace("|", "\\|")
                cur = t.get("currency", "")
                bank = t.get("bank", "")
                acct = t.get("account", "")
                acct_type = t.get("account_type", "")
                out.append(f"| {t['date']} | {bank} | {acct} | {acct_type} | {desc} | {fmt(t['amount'])} {cur} |")
            out.append("")

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
