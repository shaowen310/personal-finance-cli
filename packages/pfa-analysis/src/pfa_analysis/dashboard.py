"""Dashboard JSON assembly.

This module contains only the dashboard-builder (``build_dashboard_json``),
extracted verbatim from ``analyze.py`` as part of a structural split. Its body
references analysis helpers that remain in ``analyze``; those are imported here
so behaviour and the public signature are unchanged.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TypedDict, cast

from pfa_fx import fetch_fx_rates

from pfa_analysis.analyze import (
    _NON_CASH_ACCOUNT_TYPES,
    UNCATEGORIZED,
    _analyze_file,
    _classify_discretionary,
    _load_ir_with_txn_id,
    _split_cat,
    classify_cash_flow,
    compute_fx_gain_loss,
    parse_date_to_iso,
    parse_num,
)
from pfa_analysis.render_md import FxWrapper, convert_to_sgd
from pfa_analysis.types import (
    AnalysisResult,
    FxPair,
    Meta,
    TxnRow,
)

# ---------------------------------------------------------------------------
# Typed shapes for the assembled dashboard (no ``Any``)
# ---------------------------------------------------------------------------

class DashboardAnalysisResult(AnalysisResult, total=False):
    """An ``AnalysisResult`` plus the extra keys the dashboard attaches."""

    _meta_raw: Meta
    _rows: list[TxnRow]


class IncomeSource(TypedDict):
    category: str
    subcategory: str
    SGD: float
    CNY: float
    total_sgd: float | None
    pct: float | None


class SpendingCategory(TypedDict):
    category: str
    subcategory: str
    amount_sgd: float
    pct: float
    discretionary: bool


class TopMerchant(TypedDict):
    merchant: str
    category: str
    amount_sgd: float


class AccountChange(TypedDict):
    name: str
    currency: str
    opening: float | None
    closing: float | None
    delta: float | None


class InvestmentHoldingDetail(TypedDict):
    name: str
    currency: str
    units: float
    price: float | None
    valuation: float
    cost_basis: float | None
    unrealized_pnl: float | None
    pnl_pct: float | None


class CashFlowSummary(TypedDict):
    income_total_sgd: float | None
    expense_total_sgd: float | None
    net_operating_sgd: float | None
    transfers_in_sgd: float | None
    transfers_out_sgd: float | None


class AssetComposition(TypedDict):
    by_currency: dict[str, dict[str, float]]
    total_sgd_equivalent: dict[str, float] | None


class CashFlow(TypedDict):
    summary: CashFlowSummary


class FxGainLoss(TypedDict):
    base_currency: str
    total_sgd: float
    as_of: str
    source: str
    by_received_currency: dict[str, float]
    pairs: list[FxPair]


class IncomeTrend(TypedDict):
    months: list[str]
    by_source: dict[str, list[float]]


class IncomeAnalysis(TypedDict):
    by_source: list[IncomeSource]
    by_currency: dict[str, float]
    trend: IncomeTrend


class DiscretionarySplit(TypedDict):
    discretionary_sgd: float
    non_discretionary_sgd: float
    discretionary_pct: float
    non_discretionary_pct: float


class SpendTrend(TypedDict):
    months: list[str]
    by_category: dict[str, list[float]]


class SpendingAnalysis(TypedDict):
    by_category: list[SpendingCategory]
    discretionary_split: DiscretionarySplit
    top_merchants: list[TopMerchant]
    trend: SpendTrend


class AccountValueChange(TypedDict):
    current_accounts: list[AccountChange]
    fixed_deposits: list[AccountChange]


class Trends(TypedDict):
    months: list[str]
    net_worth_sgd: list[float | None]
    cash_flow_net_sgd: list[float | None]
    income_total_sgd: list[float | None]
    expense_total_sgd: list[float | None]


class DashboardData(TypedDict):
    period: dict[str, str]
    base_currency: str
    fx_rates: dict[str, float]
    asset_composition: AssetComposition
    cash_flow: CashFlow
    fx_gain_loss: FxGainLoss
    income_analysis: IncomeAnalysis
    spending_analysis: SpendingAnalysis
    account_value_change: AccountValueChange
    investment_detail: list[InvestmentHoldingDetail]
    trends: Trends


def build_dashboard_json(
    ir_paths: list[Path],
    categories_path: Path | None = None,
    cost_basis_path: Path | None = None,
) -> DashboardData | dict[str, str]:
    """Build the ``dashboard_data.json`` structure from IR files + optional inputs.

    Args:
        ir_paths: One or more IR JSON file paths (multi-period = trends).
        categories_path: Optional ``categories.json`` from txn-categorize
            (``{txn_id: category}``).
        cost_basis_path: Optional ``cost_basis.json`` for unrealized P&L
            (``{fund_name: {purchase_cost: float, purchase_units: float}}``).

    Returns:
        A dictionary matching the dashboard schema (see README).
    """
    # ---- Load categories & cost basis ---------------------------------------
    categories: dict[str, str] = {}
    if categories_path and categories_path.exists():
        categories = json.loads(categories_path.read_text(encoding="utf-8"))

    cost_basis: dict[str, dict[str, float]] = {}
    if cost_basis_path and cost_basis_path.exists():
        cost_basis = json.loads(cost_basis_path.read_text(encoding="utf-8"))

    # ---- Load all IR files --------------------------------------------------
    all_results: list[DashboardAnalysisResult] = []
    all_rows: list[TxnRow] = []
    for p in ir_paths:
        meta_raw, ir_rows = _load_ir_with_txn_id(p)
        # Build the same structure as analyze_file() for reuse
        result = cast(DashboardAnalysisResult, _analyze_file(p, txn_categories=categories or None))
        result["_meta_raw"] = meta_raw
        result["_rows"] = ir_rows
        all_results.append(result)
        all_rows.extend(ir_rows)

    if not all_results:
        return {"error": "No IR files provided or all empty."}

    # ---- Determine period ---------------------------------------------------
    periods = []
    for r in all_results:
        m = r.get("_meta_raw", r.get("meta", {}))
        if m.get("period_start") and m.get("period_end"):
            periods.append({"from": str(m["period_start"]), "to": str(m["period_end"])})
    latest_period = periods[-1] if periods else {"from": "", "to": ""}

    # ---- FX rates -----------------------------------------------------------
    fx_rates: FxWrapper | None = None
    fx_data: dict[str, float] = {}
    # IR files no longer embed FX. Value FX as of the latest statement
    # period-end across the loaded results (cached in %TEMP% by fetch_fx_rates).
    iso_dates = [parse_date_to_iso(r.get("_meta_raw", r.get("meta", {})).get("period_end"))
                 for r in all_results]
    valid_dates = [d for d in iso_dates if d]
    if valid_dates:
        fx_rates = cast(FxWrapper, fetch_fx_rates(max(valid_dates)))
    if fx_rates:
        fx_data = {str(k).upper(): float(v) for k, v in fx_rates["rates"].items()}

    # ---- Asset Composition --------------------------------------------------
    # Merge assets across all files.
    merged_cash: dict[str, float] = defaultdict(float)
    merged_td: dict[str, float] = defaultdict(float)
    merged_inv: dict[str, float] = defaultdict(float)
    merged_lia: dict[str, float] = defaultdict(float)
    for r in all_results:
        for c, v in r["assets"]["cash"].items():
            merged_cash[c] += v
        for c, v in r["assets"]["time_deposits"].items():
            merged_td[c] += v
        for c, v in r["assets"]["investments"].items():
            merged_inv[c] += v
        for c, v in r["assets"].get("liabilities", {}).items():
            merged_lia[c] += v

    ccies = sorted(set(list(merged_cash) + list(merged_td) + list(merged_inv) + list(merged_lia)))

    by_currency: dict[str, dict[str, float]] = {}
    total = {"cash": 0.0, "fixed_deposit": 0.0, "investments": 0.0, "liabilities": 0.0}
    for ccy in ccies:
        csh = merged_cash.get(ccy, 0.0)
        td = merged_td.get(ccy, 0.0)
        inv = merged_inv.get(ccy, 0.0)
        lia = merged_lia.get(ccy, 0.0)
        # Skip currencies where every asset class is effectively zero.
        if abs(csh + td + inv + lia) < 1e-6:
            continue
        by_currency[ccy] = {"cash": csh, "fixed_deposit": td, "investments": inv, "liabilities": lia}
        sgd_csh = convert_to_sgd(csh, ccy, fx_rates)
        sgd_td = convert_to_sgd(td, ccy, fx_rates)
        sgd_inv = convert_to_sgd(inv, ccy, fx_rates)
        sgd_lia = convert_to_sgd(lia, ccy, fx_rates)
        # Only sum in SGD equivalent when FX rates are available.
        if sgd_csh is not None:
            total["cash"] += sgd_csh
        if sgd_td is not None:
            total["fixed_deposit"] += sgd_td
        if sgd_inv is not None:
            total["investments"] += sgd_inv
        if sgd_lia is not None:
            total["liabilities"] += sgd_lia

    # When FX unavailable and multiple currencies present, the SGD-equivalent
    # total is meaningless — set to None so the dashboard can show "n/a".
    if fx_rates is None and len(ccies) > 1:
        total = None
    asset_composition: AssetComposition = {"by_currency": by_currency, "total_sgd_equivalent": total}

    # ---- Cash Flow (aggregated across all files) -----------------------------
    cf_income = defaultdict(float)
    cf_expense = defaultdict(float)
    cf_tx_in = defaultdict(float)
    cf_tx_out = defaultdict(float)
    for r in all_results:
        for ccy, m in r["metrics_by_ccy"].items():
            cf_income[ccy] += m["income"]
            cf_expense[ccy] += m["expense"]
            cf_tx_in[ccy] += m["transfer_in"]
            cf_tx_out[ccy] += m["transfer_out"]

    total_income_sgd = 0.0
    total_expense_sgd = 0.0
    ccies_for_cf = set(list(cf_income) + list(cf_expense))
    # Cross-currency sums are only meaningful with FX rates or a single currency.
    _cf_valid = fx_rates is not None or len(ccies_for_cf) <= 1
    for ccy in ccies_for_cf:
        inc = cf_income.get(ccy, 0.0)
        exp = cf_expense.get(ccy, 0.0)
        inc_sgd = convert_to_sgd(inc, ccy, fx_rates)
        exp_sgd = convert_to_sgd(exp, ccy, fx_rates)
        total_income_sgd += inc_sgd if inc_sgd is not None else inc
        total_expense_sgd += exp_sgd if exp_sgd is not None else exp

    def _fx_sum(data: dict[str, float]) -> float | None:
        if not _cf_valid:
            return None
        return round(sum(
            (convert_to_sgd(v, c, fx_rates) or v) for c, v in data.items()), 2)

    cash_flow_summary: CashFlowSummary = {
        "income_total_sgd": round(total_income_sgd, 2) if _cf_valid else None,
        "expense_total_sgd": round(total_expense_sgd, 2) if _cf_valid else None,
        "net_operating_sgd": round(total_income_sgd - total_expense_sgd, 2) if _cf_valid else None,
        "transfers_in_sgd": _fx_sum(dict(cf_tx_in)),
        "transfers_out_sgd": _fx_sum(dict(cf_tx_out)),
    }

    # ---- Income Analysis (using categories.json) -----------------------------
    income_sources_list: list[IncomeSource] = []
    # Only use the latest period's rows for the period total (not all periods).
    # For trends we'll use all rows grouped by month.
    latest_rows = all_results[-1].get("_rows", [])
    latest_income: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in latest_rows:
        if row.get("is_internal_transfer"):
            continue
        cf = classify_cash_flow(row)
        if cf != "Income":
            continue
        txn_id = row.get("txn_id", "")
        cat = categories.get(txn_id, "")
        if cat and "Transfer" in cat and "Income" not in cat:
            continue
        cat_cls, cat_sub = _split_cat(cat) if cat else ("", "")
        source = cat_sub if cat_cls == "Income" else (cat or "Income")
        latest_income[source][row["currency"]] += abs(row["amount"])

    for source in sorted(latest_income):
        by_ccy = latest_income[source]
        sgd_total = sum((convert_to_sgd(v, c, fx_rates) or 0.0)
                        for c, v in by_ccy.items())
        pct = None
        if total_income_sgd > 0 and _cf_valid:
            pct = round(sgd_total / total_income_sgd * 100, 1)
        income_sources_list.append({
            "category": "Income",
            "subcategory": source,
            "SGD": round(by_ccy.get("SGD", 0.0), 2),
            "CNY": round(by_ccy.get("CNY", 0.0), 2),
            "total_sgd": round(sgd_total, 2) if _cf_valid else None,
            "pct": pct,
        })

    income_currency_totals: dict[str, float] = {}
    for source, ccy_map in latest_income.items():
        for ccy, val in ccy_map.items():
            income_currency_totals[ccy] = income_currency_totals.get(ccy, 0.0) + round(val, 2)

    # Helper: extract normalized YYYY-MM month from a period-end date string.
    def _extract_month(meta: Meta) -> str:
        pe = str(meta.get("period_end", ""))
        iso = parse_date_to_iso(pe)
        return iso[:7] if iso else pe[:7]

    # Income trend (multi-period)
    income_trend_months: list[str] = []
    income_trend_by_source: dict[str, list[float]] = defaultdict(list)
    for r in all_results:
        m = r.get("_meta_raw", r.get("meta", {}))
        month = _extract_month(m)
        income_trend_months.append(month)
        period_rows = r.get("_rows", [])
        period_income: dict[str, float] = defaultdict(float)
        for row in period_rows:
            if row.get("is_internal_transfer"):
                continue
            cf = classify_cash_flow(row)
            if cf != "Income":
                continue
            txn_id = row.get("txn_id", "")
            cat = categories.get(txn_id, "")
            if cat and "Transfer" in cat and "Income" not in cat:
                continue
            cat_cls, cat_sub = _split_cat(cat) if cat else ("", "")
            source = cat_sub if cat_cls == "Income" else (cat or "Income")
            conv = convert_to_sgd(abs(row["amount"]), row["currency"], fx_rates)
            period_income[source] += conv if conv is not None else abs(row["amount"])
        all_sources = set(list(income_trend_by_source) + list(period_income))
        for s in sorted(all_sources):
            income_trend_by_source[s].append(round(period_income.get(s, 0.0), 2))

    income_analysis: IncomeAnalysis = {
        "by_source": income_sources_list,
        "by_currency": income_currency_totals,
        "trend": {
            "months": income_trend_months,
            "by_source": {s: vals for s, vals in income_trend_by_source.items()},
        },
    }

    # ---- Spending Analysis (using categories.json) ---------------------------
    _has_categories = bool(categories)
    spending_by_cat: dict[str, float] = defaultdict(float)
    spending_top_merchants: dict[str, tuple[float, str]] = {}  # merchant → (amount, category)
    for row in latest_rows:
        if row.get("is_internal_transfer"):
            continue
        cf = classify_cash_flow(row)
        if cf != "Expense":
            continue
        txn_id = row.get("txn_id", "")
        cat = categories.get(txn_id, "")
        if not cat:
            # Fallback when no categories: use cash-flow classification
            cat = "Expense" if not _has_categories else UNCATEGORIZED
        if "Transfer" in cat or cat.startswith("Income:") or cat == "Income":
            continue
        amt = abs(row["amount"])  # make positive for display

        # Split two-level category into class + subtype for display grouping
        _cls_name, sub_name = _split_cat(cat)
        display_cat = sub_name if sub_name else cat

        spending_by_cat[display_cat] += amt
        merchant = row["description"]
        # Update top merchant
        existing = spending_top_merchants.get(merchant, (0.0, display_cat))
        spending_top_merchants[merchant] = (existing[0] + amt, display_cat)

    total_spending = sum(spending_by_cat.values())
    disc_sgd = sum(v for c, v in spending_by_cat.items() if _classify_discretionary(c))
    nondisc_sgd = total_spending - disc_sgd

    spending_categories_list: list[SpendingCategory] = []
    for cat in sorted(spending_by_cat, key=lambda c: spending_by_cat[c], reverse=True):
        amt = spending_by_cat[cat]
        spending_categories_list.append({
            "category": "Expense",
            "subcategory": cat,
            "amount_sgd": round(amt, 2),
            "pct": round(amt / total_spending * 100, 1) if total_spending > 0 else 0.0,
            "discretionary": _classify_discretionary(cat),
        })

    # Top merchants (top 10 by amount)
    sorted_merchants = sorted(spending_top_merchants.items(),
                              key=lambda x: x[1][0], reverse=True)[:10]
    top_merchants_list: list[TopMerchant] = []
    for merchant, (amt, cat) in sorted_merchants:
        top_merchants_list.append({
            "merchant": merchant,
            "category": cat,
            "amount_sgd": round(amt, 2),
        })

    # Spending trend (multi-period)
    spend_trend_months: list[str] = []
    spend_trend_by_cat: dict[str, list[float]] = defaultdict(list)
    for r in all_results:
        m = r.get("_meta_raw", r.get("meta", {}))
        month = _extract_month(m)
        spend_trend_months.append(month)
        period_rows = r.get("_rows", [])
        period_spend: dict[str, float] = defaultdict(float)
        for row in period_rows:
            if row.get("is_internal_transfer"):
                continue
            cf = classify_cash_flow(row)
            if cf != "Expense":
                continue
            txn_id = row.get("txn_id", "")
            cat = categories.get(txn_id, "")
            if not cat:
                cat = "Expense" if not _has_categories else UNCATEGORIZED
            if "Transfer" in cat or cat.startswith("Income:") or cat == "Income":
                continue
            # Use subtype as the trend key
            _, sub_name = _split_cat(cat)
            trend_cat = sub_name if sub_name else cat
            conv = convert_to_sgd(abs(row["amount"]), row["currency"], fx_rates)
            period_spend[trend_cat] += conv if conv is not None else abs(row["amount"])
        all_spend_cats = set(list(spend_trend_by_cat) + list(period_spend))
        for s in sorted(all_spend_cats):
            spend_trend_by_cat[s].append(round(period_spend.get(s, 0.0), 2))

    spending_analysis: SpendingAnalysis = {
        "by_category": spending_categories_list,
        "discretionary_split": {
            "discretionary_sgd": round(disc_sgd, 2),
            "non_discretionary_sgd": round(nondisc_sgd, 2),
            "discretionary_pct": round(disc_sgd / total_spending * 100, 1) if total_spending > 0 else 0.0,
            "non_discretionary_pct": round(nondisc_sgd / total_spending * 100, 1) if total_spending > 0 else 0.0,
        },
        "top_merchants": top_merchants_list,
        "trend": {
            "months": spend_trend_months,
            "by_category": {s: vals for s, vals in spend_trend_by_cat.items()},
        },
    }

    # ---- Account Value Change ------------------------------------------------
    current_accounts: list[AccountChange] = []
    fixed_deposits: list[AccountChange] = []
    # Use the first and last file for opening/closing comparison when available.
    for r in all_results:
        meta_r = r.get("_meta_raw", r.get("meta", {}))
        acct_summary = meta_r.get("account_summary", [])
        _NON_CASH = _NON_CASH_ACCOUNT_TYPES
        for acct in acct_summary:
            acct_type = str(acct.get("account_type", "")).lower()
            if acct_type in _NON_CASH:
                continue
            ccy = acct.get("currency", "")
            opening = parse_num(acct.get("opening_balance"))
            closing = acct.get("closing_balance") or 0.0  # already parsed in _build_meta
            delta = (closing - opening) if opening is not None else None
            current_accounts.append({
                "name": str(acct.get("account_no", "")),
                "currency": ccy,
                "opening": opening,
                "closing": closing,
                "delta": delta,
            })
        # Fixed deposits come from the consolidated time_deposits records.
        for td in meta_r.get("extras", {}).get("time_deposits", []):
            b = td.get("closing_balance")  # already parsed in _build_meta
            if b is None:
                principal = parse_num(td.get("principal")) or 0.0
                interest = parse_num(td.get("interest_amount")) or 0.0
                b = principal + interest
            # Look up the matching opening balance from account_summary.
            td_open: float | None = None
            for acct in acct_summary:
                if str(acct.get("account_no", "")) == str(td.get("account_no", td.get("deposit_no", ""))):
                    td_open = parse_num(acct.get("opening_balance"))
                    break
            td_delta = (b - td_open) if td_open is not None else None
            fixed_deposits.append({
                "name": str(td.get("account_no", td.get("deposit_no", ""))),
                "currency": td.get("currency", "SGD"),
                "opening": td_open,
                "closing": b or 0.0,
                "delta": td_delta,
            })

    account_value_change: AccountValueChange = {
        "current_accounts": current_accounts,
        "fixed_deposits": fixed_deposits,
    }

    # ---- Investment Detail ---------------------------------------------------
    investment_detail: list[InvestmentHoldingDetail] = []
    for r in all_results:
        meta_r = r.get("_meta_raw", r.get("meta", {}))
        for h in meta_r.get("investment_holdings", []):
            name = str(h.get("name", h.get("fund_name", "")))
            ccy = str(h.get("currency", ""))
            units = parse_num(h.get("units", h.get("holdings"))) or 0.0
            price = parse_num(h.get("price", h.get("nav_per_unit"))) or 0.0
            valuation = parse_num(h.get("valuation", h.get("market_value"))) or (units * price)
            cb = cost_basis.get(name, {})
            cb_total = float(cb.get("purchase_cost", 0.0) or 0.0)
            if cb_total <= 0:
                cb_total = parse_num(h.get("cost")) or 0.0  # consolidated embedded cost
            pnl = parse_num(h.get("unrealised_pl"))
            if pnl is None and cb_total > 0:
                pnl = valuation - cb_total
            pnl_pct = (pnl / cb_total * 100) if pnl is not None and cb_total > 0 else None
            investment_detail.append({
                "name": name,
                "currency": ccy,
                "units": round(units, 4),
                "price": round(price, 4),
                "valuation": round(valuation, 2),
                "cost_basis": round(cb_total, 2) if cb_total > 0 else None,
                "unrealized_pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            })
        for ut in meta_r.get("extras", {}).get("unit_trusts", []):
            name = str(ut.get("name", ""))
            ccy = str(ut.get("currency", "SGD"))
            units = parse_num(ut.get("units")) or 0.0
            valuation = parse_num(ut.get("market_value")) or 0.0
            cb = cost_basis.get(name, {})
            cb_total = cb.get("purchase_cost", 0.0)
            pnl = valuation - cb_total if cb_total > 0 else None
            pnl_pct = (pnl / cb_total * 100) if pnl is not None and cb_total > 0 else None
            investment_detail.append({
                "name": name,
                "currency": ccy,
                "units": round(units, 4),
                "price": None,
                "valuation": round(valuation, 2),
                "cost_basis": round(cb_total, 2) if cb_total > 0 else None,
                "unrealized_pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            })

    # ---- Trends (multi-period) -----------------------------------------------
    trend_months: list[str] = []
    trend_net_worth: list[float | None] = []
    trend_cf_net: list[float | None] = []
    trend_income: list[float | None] = []
    trend_expense: list[float | None] = []
    for r in all_results:
        m = r.get("_meta_raw", r.get("meta", {}))
        month = _extract_month(m)
        trend_months.append(month)
        np_val = 0.0
        for c, v in r["assets"]["cash"].items():
            conv = convert_to_sgd(v, c, fx_rates)
            np_val += conv if conv is not None else v
        for c, v in r["assets"]["time_deposits"].items():
            conv = convert_to_sgd(v, c, fx_rates)
            np_val += conv if conv is not None else v
        for c, v in r["assets"]["investments"].items():
            conv = convert_to_sgd(v, c, fx_rates)
            np_val += conv if conv is not None else v
        for c, v in r["assets"].get("liabilities", {}).items():
            conv = convert_to_sgd(v, c, fx_rates)
            np_val -= conv if conv is not None else v
        trend_net_worth.append(round(np_val, 2) if _cf_valid else None)
        # Net cash flow (SGD equivalent)
        cf_net = 0.0
        inc = 0.0
        exp = 0.0
        for ccy, m_val in r["metrics_by_ccy"].items():
            conv_cf = convert_to_sgd(m_val["net_change_cash"], ccy, fx_rates)
            conv_inc = convert_to_sgd(m_val["income"], ccy, fx_rates)
            conv_exp = convert_to_sgd(m_val["expense"], ccy, fx_rates)
            cf_net += conv_cf if conv_cf is not None else m_val["net_change_cash"]
            inc += conv_inc if conv_inc is not None else m_val["income"]
            exp += conv_exp if conv_exp is not None else m_val["expense"]
        trend_cf_net.append(round(cf_net, 2) if _cf_valid else None)
        trend_income.append(round(inc, 2) if _cf_valid else None)
        trend_expense.append(round(exp, 2) if _cf_valid else None)

    trends: Trends = {
        "months": trend_months,
        "net_worth_sgd": trend_net_worth,
        "cash_flow_net_sgd": trend_cf_net,
        "income_total_sgd": trend_income,
        "expense_total_sgd": trend_expense,
    }

    # ---- Realized FX Gain/Loss (per IR, aggregated) --------------------------
    fx_total_sgd = 0.0
    fx_pairs: list[FxPair] = []
    fx_by_recv: dict[str, float] = defaultdict(float)
    fx_as_of = ""
    fx_source = ""
    fx_rates_arg = fx_data if fx_rates else None
    for p in ir_paths:
        fxd = compute_fx_gain_loss(
            p, as_of=(max(valid_dates) if valid_dates else None),
            fx_rates=fx_rates_arg,
        )
        fx_total_sgd += fxd.get("total_sgd", 0.0)
        fx_pairs.extend(fxd.get("pairs", []))
        for c, v in (fxd.get("by_received_currency") or {}).items():
            fx_by_recv[c] += v
        if not fx_as_of and fxd.get("as_of"):
            fx_as_of = fxd["as_of"]
        if not fx_source and fxd.get("source"):
            fx_source = fxd["source"]
    fx_gain_loss: FxGainLoss = {
        "base_currency": "SGD",
        "total_sgd": round(fx_total_sgd, 2),
        "as_of": fx_as_of,
        "source": fx_source,
        "by_received_currency": {c: round(v, 2) for c, v in sorted(fx_by_recv.items())},
        "pairs": fx_pairs,
    }

    # ---- Assemble ------------------------------------------------------------
    cash_flow: CashFlow = {"summary": cash_flow_summary}
    return {
        "period": latest_period,
        "base_currency": "SGD",
        "fx_rates": fx_data,
        "asset_composition": asset_composition,
        "cash_flow": cash_flow,
        "fx_gain_loss": fx_gain_loss,
        "income_analysis": income_analysis,
        "spending_analysis": spending_analysis,
        "account_value_change": account_value_change,
        "investment_detail": investment_detail,
        "trends": trends,
    }
