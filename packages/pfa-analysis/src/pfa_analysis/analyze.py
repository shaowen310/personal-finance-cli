"""Analyse personal balance sheet and cash flow from processed bank-statement JSON.

Supports two input shapes, auto-detected:

1. **`.ir.json` parser output** (the real files in tests/cache):
       { "statement_meta": {institution, currency, period_from/to,
         opening_balance?, closing_balance?},
         "transactions": [{posted_date, amount (signed), currency,
           description, is_internal_transfer, balance_after, ...}],
         "account_summary": [{account_no, currency, balance}],
         "investment_holdings": [{currency, valuation}], "extras": {...} }

2. **Default/simple schema** (used by --demo and the documented example):
       { "account": {...}, "period": {...}, "balances": {opening, closing},
         "transactions": [{date, description, amount, currency, type}] }

The skill focuses on balance sheet + cash flow (NOT merchant spending
categorization). Cash flows are classified only as Income / Expense / Transfer
In / Transfer Out — the minimum needed for an honest cash-flow statement.

CLI:

    python analyze.py <statement.json> [output.md]
    python analyze.py <consolidated.ir.json> [output_dir]  # consolidated SGD report
    python analyze.py --demo                             # embedded synthetic data

Reports contain: Executive Summary, Balance Sheet (cash + deposits +
investments, per currency), Cash Flow Statement (per currency, reconciled to
balance), Key Observations, Notes & Caveats.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Internal transaction model
# ---------------------------------------------------------------------------
#
#   {"date": str, "description": str, "amount": float, "currency": str,
#    "is_internal_transfer": bool, "balance_after": float | None}
#
# `amount` is SIGNED: credits/income positive, debits/spend negative.

Txn = dict[str, Any]


# ---------------------------------------------------------------------------
# Numeric parsing helpers
# ---------------------------------------------------------------------------

def parse_num(s: Any) -> float | None:
    """Parse '1,234.56' / '0.00' / 1234.56 into a float; None if not parseable."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Schema adaptation points
# ---------------------------------------------------------------------------

def normalize_transactions(statement: dict[str, Any]) -> list[Txn]:
    """Map a raw statement JSON into the internal transaction model.

    Two schemas are supported; dispatched by ``load_statement``. This function
    handles the **default/simple** schema. Edit field mapping here if your
    default-schema file differs. The `.ir.json` mapping lives in
    ``_normalize_ir``.
    """
    raw = statement.get("transactions", [])
    if not isinstance(raw, list):
        print("[WARN] statement has no 'transactions' array.", file=sys.stderr)
        return []
    out: list[Txn] = []
    for t in raw:
        date = str(t.get("date", ""))
        desc = str(t.get("description", t.get("narrative", ""))).strip()
        amount = float(t.get("amount", 0.0))
        currency = str(t.get("currency", statement.get("account", {}).get("currency", "")))
        out.append(
            {
                "date": date,
                "description": desc,
                "amount": amount,
                "currency": currency,
                "is_internal_transfer": False,
                "balance_after": None,
            }
        )
    return out


def _normalize_ir(transactions: list[dict[str, Any]]) -> list[Txn]:
    """Map `.ir.json` transactions into the internal model."""
    out: list[Txn] = []
    for t in transactions:
        out.append(
            {
                "date": str(t.get("posted_date", t.get("value_date", ""))),
                "description": str(t.get("description", t.get("raw_description", ""))).strip(),
                "amount": float(t.get("amount", 0.0)),
                "currency": str(t.get("currency", "")),
                "is_internal_transfer": bool(t.get("is_internal_transfer", False)),
                "balance_after": parse_num(t.get("balance_after")),
            }
        )
    return out


def _is_consolidated(data: dict[str, Any]) -> bool:
    """A file is a consolidated IR when its parser name contains 'consolidate'.

    This is the single rule for detecting consolidation: the consolidated
    parser (``bank-ir-consolidate``) tags every emitted file, so we don't rely
    on incidental structural hints like the presence of ``accounts[]``.
    """
    name = str((data.get("parser") or {}).get("name", "")).lower()
    return "consolidate" in name


def load_statement(path: Path,
                   start_date: str | None = None,
                   end_date: str | None = None) -> tuple[dict[str, Any], list[Txn]]:
    """Load a JSON statement and return (meta, normalized transactions).

    Auto-detects the consolidated `.ir.json` (parser name contains
    ``consolidate``), the single statement `.ir.json` parser output, or the
    default schema.

    When *start_date* and/or *end_date* are provided, transactions are
    filtered to the inclusive date range (applies to consolidated IR only;
    non-consolidated formats are not filtered).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if _is_consolidated(data):
        return _load_consolidated_ir(data, path, start_date, end_date)
    if "statement_meta" in data:
        return _load_ir(data)
    return _load_default(data)


def _own_account_digits(accounts: list[dict[str, Any]]) -> set[str]:
    """Digits-only forms of every known account number (for self-reference detection)."""
    digits: set[str] = set()
    for acct in accounts:
        no = "".join(ch for ch in str(acct.get("account_no", "")) if ch.isdigit())
        if len(no) >= 4:
            digits.add(no)
    return digits


def _is_self_reference(description: str, own_digits: set[str]) -> bool:
    """True if a transaction description references one of the user's own
    account numbers — i.e. it is a transfer between the user's own accounts
    (inter-account moves) even when the
    consolidation module did not flag it ``is_internal_transfer``.
    """
    desc = "".join(ch for ch in description.upper() if ch.isdigit())
    return any(no and no in desc for no in own_digits)


def _load_consolidated_ir(data: dict[str, Any], path: Path,
                          start_date: str | None = None,
                          end_date: str | None = None) -> tuple[dict[str, Any], list[Txn]]:
    """Load a consolidated IR: flatten transactions across all accounts.

    When *start_date* and/or *end_date* are provided, only transactions whose
    ``posted_date`` falls within the inclusive range are included.
    """
    meta = _build_meta(data, path)
    own_digits = _own_account_digits(data.get("accounts", []))
    txns: list[Txn] = []
    for acct in data.get("accounts", []):
        acct_ccy = acct.get("currency", "")
        acct_type = acct.get("account_type", "")
        for t in acct.get("transactions", []):
            # Apply date filter early to skip unwanted transactions
            if start_date or end_date:
                posted = str(t.get("posted_date", "")).strip()
                if not posted:
                    continue
                if start_date and posted < start_date:
                    continue
                if end_date and posted > end_date:
                    continue
            desc = str(t.get("description", "")).strip()
            is_internal = bool(t.get("is_internal_transfer", False)) or _is_self_reference(desc, own_digits)
            txns.append({
                "date": str(t.get("posted_date", t.get("value_date", ""))),
                "description": desc,
                "amount": float(t.get("amount", 0.0)),
                "currency": str(t.get("currency", acct_ccy)),
                "account_type": acct_type,
                "is_internal_transfer": is_internal,
                "balance_after": parse_num(t.get("balance_after")),
            })
    return meta, txns


def _load_default(data: dict[str, Any]) -> tuple[dict[str, Any], list[Txn]]:
    meta: dict[str, Any] = {
        "bank": data.get("account", {}).get("bank", ""),
        "account_no": data.get("account", {}).get("account_no", ""),
        "currency": data.get("account", {}).get("currency", ""),
        "period_start": data.get("period", {}).get("start"),
        "period_end": data.get("period", {}).get("end"),
        "opening": data.get("balances", {}).get("opening"),
        "closing": data.get("balances", {}).get("closing"),
        "account_summary": [],
        "investment_holdings": [],
        "extras": {},
        "source_file": "",
    }
    return meta, normalize_transactions(data)


def _load_ir(data: dict[str, Any]) -> tuple[dict[str, Any], list[Txn]]:
    sm = data.get("statement_meta", {})
    meta: dict[str, Any] = {
        "bank": sm.get("institution", ""),
        "account_no": sm.get("account_id", ""),
        "currency": sm.get("currency", ""),
        "period_start": sm.get("period_from"),
        "period_end": sm.get("period_to"),
        "opening": sm.get("opening_balance"),
        "closing": sm.get("closing_balance"),
        "account_summary": data.get("account_summary", []),
        "investment_holdings": data.get("investment_holdings", []),
        "extras": data.get("extras", {}),
        "source_file": data.get("source_file", ""),
    }
    return meta, _normalize_ir(data.get("transactions", []))


# ---------------------------------------------------------------------------
# Cash-flow classification (balance-sheet relevant only)
# ---------------------------------------------------------------------------
#
# Income / Expense / Transfer In / Transfer Out. No merchant categories.

TRANSFER_KEYWORDS = [
    "FUNDS TRANSFER", "FDWD", "FIXED DEPOSIT", "TOP-UP TO PAYLAH",
    "TIME DEPO", "ROLLOVER", "PAYMENT BY INTERNET",
]


# Account types that follow the credit-card sign convention: a *positive*
# amount is a charge (debit / outflow), a negative amount is a payment / credit.
_CREDIT_LIKE_ACCOUNTS = {"credit_card"}


def _is_debit(txn: Txn) -> bool:
    """True when ``txn`` is a debit (outflow: expense or transfer out).

    The sign convention differs by account type:
      * current / savings / fixed / investment accounts: negative = debit.
      * credit_card accounts: positive = charge = debit (banking convention).
    Transactions without an ``account_type`` (legacy single-statement IR)
    fall back to the standard negative-is-debit rule.
    """
    at = str(txn.get("account_type", "")).lower()
    if at in _CREDIT_LIKE_ACCOUNTS:
        return txn["amount"] > 0
    return txn["amount"] < 0


def classify_cash_flow(txn: Txn) -> str:
    """Return one of Income / Expense / Transfer In / Transfer Out."""
    if txn.get("is_internal_transfer"):
        return "Transfer Out" if _is_debit(txn) else "Transfer In"
    u = txn["description"].upper()
    if any(k in u for k in TRANSFER_KEYWORDS):
        return "Transfer Out" if _is_debit(txn) else "Transfer In"
    return "Expense" if _is_debit(txn) else "Income"


# ---------------------------------------------------------------------------
# Date parsing utilities
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def parse_date_to_iso(s: Any) -> str | None:
    """Normalise a statement period string to ``YYYY-MM-DD`` for the FX API.

    Handles the variants seen in real `.ir.json` files:
      ``2026-06-30`` (ISO), ``2026/06/30`` (slashes), ``30 JUN 2026`` (DD MON YYYY).
    Returns ``None`` if unparseable.
    """
    if not s:
        return None
    s = str(s).strip().upper()
    # ISO or slash form: YYYY-MM-DD / YYYY/MM/DD
    m = re.fullmatch(r"(\d{4})[-/](\d{2})[-/](\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # DD MON YYYY
    m = re.fullmatch(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", s)
    if m:
        mon = _MONTHS.get(m.group(2))
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    return None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(txns: list[Txn], meta: dict[str, Any], ccy: str,
                    opening_override: float | None = None,
                    closing_override: float | None = None,
                    use_txn_balances: bool = False) -> dict[str, Any]:
    """Compute balance-sheet + cash-flow metrics for one currency in a statement.

    When ``opening_override``/``closing_override`` are supplied (e.g. derived
    from a consolidated ``accounts[].opening_balance``), they are used directly
    instead of inferring from ``meta`` or the first/last ``balance_after``.

    When ``use_txn_balances`` is True, overrides and statement-meta balances
    are ignored and opening/closing are always derived from the filtered
    transaction stream's ``balance_after``. This is the correct mode when
    transactions have been filtered by ``start_date``/``end_date``, because
    the statement-level balances reflect the full period rather than the
    truncated window.
    """
    income = expense = transfer_in = transfer_out = 0.0
    for t in txns:
        c = classify_cash_flow(t)
        a = abs(t["amount"])  # classify_cash_flow already set the direction
        if c == "Income":
            income += a
        elif c == "Transfer In":
            transfer_in += a
        elif c == "Expense":
            expense += a
        elif c == "Transfer Out":
            transfer_out += a

    total_inflow = income + transfer_in
    total_outflow = expense + transfer_out
    net_change_cash = total_inflow - total_outflow
    net_operating = income - expense
    savings_rate = net_operating / income if income > 0 else 0.0

    # Opening / closing for this currency.
    if use_txn_balances and txns:
        # Derive from the filtered transaction stream so that
        # opening/closing reflect the truncated date window.
        opening = (txns[0]["balance_after"] or 0.0) - txns[0]["amount"]
        closing = txns[-1]["balance_after"] or 0.0
    elif opening_override is not None and closing_override is not None:
        opening = opening_override
        closing = closing_override
    elif meta.get("currency") == ccy and meta.get("opening") is not None:
        opening = float(meta["opening"])
        closing = float(meta["closing"])
    elif txns:
        opening = (txns[0]["balance_after"] or 0.0) - txns[0]["amount"]
        closing = txns[-1]["balance_after"] or 0.0
    else:
        opening = closing = None

    balance_change = (closing - opening) if (opening is not None and closing is not None) else None
    recon_ok = abs(balance_change - net_change_cash) < 0.005 if balance_change is not None else None

    return {
        "income": income, "expense": expense, "transfer_in": transfer_in,
        "transfer_out": transfer_out, "total_inflow": total_inflow,
        "total_outflow": total_outflow, "net_change_cash": net_change_cash,
        "net_operating": net_operating, "savings_rate": savings_rate,
        "opening": opening, "closing": closing, "balance_change": balance_change,
        "reconciliation_ok": recon_ok, "txn_count": len(txns),
    }


def build_assets(meta: dict[str, Any], metrics_by_ccy: dict[str, dict[str, Any]],
                  cc_balances: dict[str, float] | None = None) -> dict[str, dict[str, float]]:
    """Assemble balance-sheet assets: cash, time deposits, investments (per currency).

    Credit-card balances are treated as liabilities (deducted from net worth).
    *cc_balances* is a ``{ccy: balance}`` dict derived from the last
    ``balance_after`` on credit-card transactions.
    """
    cash: dict[str, float] = defaultdict(float)
    liabilities: dict[str, float] = defaultdict(float)
    _NON_CASH = _NON_CASH_ACCOUNT_TYPES
    if meta.get("account_summary"):
        for a in meta["account_summary"]:
            at = str(a.get("account_type", "")).lower()
            if at in _NON_CASH or at in _LIABILITY_ACCOUNT_TYPES:
                continue
            c = a.get("currency")
            b = parse_num(a.get("closing_balance"))
            if c and b is not None:
                cash[c] += b
    else:
        for ccy, m in metrics_by_ccy.items():
            if m["closing"] is not None:
                cash[ccy] += m["closing"]
    if cc_balances:
        for ccy, bal in cc_balances.items():
            if bal > 0:
                liabilities[ccy] += bal
    elif not meta.get("account_summary"):
        for ccy, m in metrics_by_ccy.items():
            if m["closing"] is not None:
                cash[ccy] += m["closing"]

    time_dep: dict[str, float] = defaultdict(float)
    for td in meta.get("extras", {}).get("time_deposits", []):
        b = parse_num(td.get("closing_balance"))
        if b is None:
            principal = parse_num(td.get("principal")) or 0.0
            interest = parse_num(td.get("interest_amount")) or 0.0
            b = principal + interest
        c = td.get("currency", "SGD")
        time_dep[c] += b

    inv: dict[str, float] = defaultdict(float)
    for h in meta.get("investment_holdings", []):
        c = h.get("currency")
        b = parse_num(h.get("valuation"))
        if c and b is not None:
            inv[c] += b
    for u in meta.get("extras", {}).get("unit_trusts", []):
        b = parse_num(u.get("market_value"))
        if b is not None:
            inv["SGD"] += b

    return {"cash": dict(cash), "time_deposits": dict(time_dep), "investments": dict(inv), "liabilities": dict(liabilities)}


def _assert_drilldown_reconciles(assets: dict[str, dict[str, float]],
                                  drilldown: list[dict[str, Any]]) -> None:
    """Assert that drill-down subtotals match the Balance Sheet totals.

    Compares per-currency, per-bucket sums from the drill-down rows against
    the corresponding values in *assets*. Raises ``AssertionError`` with a
    descriptive message on mismatch — this catches double-counting bugs in
    ``build_assets`` and schema drift between the two code paths.
    """
    _BUCKET_MAP = {
        "Cash": "cash",
        "Time Deposit": "time_deposits",
        "Investment": "investments",
        "Liability": "liabilities",
    }
    dd_totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in drilldown:
        bucket = row.get("bucket", "")
        ccy = row.get("currency", "")
        val = row.get("native_value", 0.0)
        if bucket in _BUCKET_MAP:
            dd_totals[_BUCKET_MAP[bucket]][ccy] += val

    for asset_key in ("cash", "time_deposits", "investments", "liabilities"):
        asset_by_ccy = assets.get(asset_key, {})
        dd_by_ccy = dd_totals.get(asset_key, {})
        all_ccies = sorted(set(list(asset_by_ccy) + list(dd_by_ccy)))
        for ccy in all_ccies:
            asset_val = asset_by_ccy.get(ccy, 0.0)
            dd_val = dd_by_ccy.get(ccy, 0.0)
            assert abs(asset_val - dd_val) < 0.005, (
                f"Balance Sheet / drill-down mismatch for {asset_key} {ccy}: "
                f"Balance Sheet={asset_val:.2f}, Drill-Down={dd_val:.2f}"
            )


def build_balance_sheet_drilldown(raw: dict[str, Any],
                                  cc_balances: dict[str, float] | None = None,
                                  use_txn_balances: bool = False) -> list[dict[str, Any]]:
    """Account-level breakdown that feeds the Balance Sheet Drill-Down section.

    Mirrors the bucket assignment in :func:`build_assets` exactly so the
    drill-down reconciles with the Balance Sheet totals. Each row is::

        {"currency", "institution", "account_no", "account_type", "bucket",
         "native_value", "derivation"}

    ``bucket`` is one of ``Cash`` / ``Time Deposit`` / ``Investment`` /
    ``Dropped``. ``Dropped`` flags accounts that contribute nothing to the
    balance sheet (e.g. a liquid account whose ``closing_balance`` is null).
    """
    rows: list[dict[str, Any]] = []

    def _add(ccy: str, inst: str, acc: str, at: str, bucket: str,
             value: float, deriv: str) -> None:
        rows.append({
            "currency": ccy, "institution": inst, "account_no": acc,
            "account_type": at, "bucket": bucket, "native_value": value,
            "derivation": deriv,
        })

    # ---- Consolidated IR (accounts[] with closing_balance / fd_records / holdings)
    accounts = raw.get("accounts")
    if accounts is not None:
        for acct in accounts:
            ccy = acct.get("currency", "")
            inst = acct.get("institution", "")
            at = str(acct.get("account_type", "")).lower()
            acc = acct.get("account_no", "")
            cb = parse_num(acct.get("closing_balance"))
            if at in _LIABILITY_ACCOUNT_TYPES:
                if use_txn_balances and cc_balances:
                    # Use the cutoff-aware balance_after, matching build_assets.
                    bal = cc_balances.get(ccy)
                    if bal is not None:
                        _add(ccy, inst, acc, at, "Liability", bal,
                             "balance_after as of cutoff (credit-card debt)")
                        continue
                if cb is not None:
                    _add(ccy, inst, acc, at, "Liability", abs(cb),
                         "account closing_balance (credit-card debt)")
                else:
                    _add(ccy, inst, acc, at, "Dropped", 0.0,
                         "DROPPED: credit_card closing_balance is null")
            elif at in _NON_CASH_ACCOUNT_TYPES:
                if at in _FD_ACCOUNT_TYPES:
                    if cb is not None:
                        _add(ccy, inst, acc, at, "Time Deposit", cb,
                             "account closing_balance")
                    else:
                        _add(ccy, inst, acc, at, "Time Deposit", 0.0,
                             "DROPPED: fixed_deposit closing_balance is null")
                else:
                    holdings = acct.get("investment_holdings", [])
                    val = sum(parse_num(h.get("valuation")) or 0.0 for h in holdings)
                    if val > 0 or holdings:
                        _add(ccy, inst, acc, at, "Investment", val,
                             "investment_holdings valuation (sum)")
                    else:
                        _add(ccy, inst, acc, at, "Dropped", cb if cb is not None else 0.0,
                             f"DROPPED: non-cash, no holdings (closing_balance={acct.get('closing_balance')})")
            else:
                if cb is not None:
                    _add(ccy, inst, acc, at, "Cash", cb, "account closing_balance")
                else:
                    _add(ccy, inst, acc, at, "Dropped", 0.0,
                         "DROPPED: liquid account, closing_balance is null")
        return rows

    # ---- Individual statement (account_summary[] with balance)
    stmt_inst = raw.get("institution", "")
    for a in raw.get("account_summary", []):
        ccy = a.get("currency", "")
        inst = a.get("institution", stmt_inst)
        at = str(a.get("account_type", "")).lower()
        acc = a.get("account_no", "")
        bal = parse_num(a.get("closing_balance")) or 0.0
        if at in _LIABILITY_ACCOUNT_TYPES:
            _add(ccy, inst, acc, at, "Liability", abs(bal), "account_summary.balance (credit-card debt)")
        elif at in _FD_ACCOUNT_TYPES:
            _add(ccy, inst, acc, at, "Time Deposit", bal, "account_summary.balance")
        elif at in _NON_CASH_ACCOUNT_TYPES:
            _add(ccy, inst, acc, at, "Investment", bal, "account_summary.balance")
        else:
            _add(ccy, inst, acc, at, "Cash", bal, "account_summary.balance")
    return rows


def _analyze_file(path: Path,
                  start_date: str | None = None,
                  end_date: str | None = None) -> dict[str, Any]:
    """Analyze one statement file into a structured result.

    Dispatches to the consolidated analysis path (account-balance based) when
    the IR is consolidated, or the per-file analysis path otherwise.

    When *start_date* or *end_date* is provided, opening/closing balances are
    derived from the filtered transaction stream (``balance_after``) rather
    than from the full-period statement/account balances, ensuring Cash Flow
    reconciliation holds for the truncated window.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    meta, txns = load_statement(path, start_date, end_date)
    use_txn_balances = bool(start_date or end_date)
    if meta.get("_consolidated"):
        return _analyze_consolidated(raw, meta, txns, path,
                                     use_txn_balances=use_txn_balances)
    return analyze_statement(raw, meta, txns, path,
                             use_txn_balances=use_txn_balances)


def _analyze_consolidated(raw: dict[str, Any], meta: dict[str, Any],
                          txns: list[Txn], path: Path,
                          use_txn_balances: bool = False) -> dict[str, Any]:
    """Consolidated IR: opening/closing come from account balances, not txns.

    When ``use_txn_balances`` is True (e.g. transactions have been filtered by
    ``start_date``/``end_date``), account-summary balances are ignored and
    opening/closing are derived from the filtered transaction stream instead.
    """
    by_ccy: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        by_ccy[t["currency"]].append(t)
    for lst in by_ccy.values():
        lst.sort(key=lambda x: x["date"])

    # Opening/closing per currency from cash account balances. Credit-card
    # balances are handled separately via balance_after below.
    opening_by_ccy: dict[str, float] = {}
    closing_by_ccy: dict[str, float] = {}
    if not use_txn_balances:
        for a in meta["account_summary"]:
            atype = str(a.get("account_type", "")).lower()
            if atype in ("fixed", "time", "securities", "investment", *_LIABILITY_ACCOUNT_TYPES):
                continue
            ccy = a.get("currency", "")
            op = parse_num(a.get("opening_balance"))
            cl = parse_num(a.get("closing_balance"))
            if ccy:
                opening_by_ccy[ccy] = opening_by_ccy.get(ccy, 0.0) + (op or 0.0)
                closing_by_ccy[ccy] = closing_by_ccy.get(ccy, 0.0) + (cl or 0.0)

    # Credit-card liability: use balance_after when date-truncated
    # (reflects the filtered window), otherwise fall back to
    # account_summary (handles consolidated IRs with overlapping data).
    cc_balances: dict[str, float] = {}
    # Walk txns in reverse date order to pick the latest balance_after.
    cc_txns = sorted(
        (t for t in txns if str(t.get("account_type", "")).lower() in _LIABILITY_ACCOUNT_TYPES),
        key=lambda t: t["date"], reverse=True,
    )
    for t in cc_txns:
        ccy = t["currency"]
        if ccy in cc_balances:
            continue
        bal = t.get("balance_after")
        if bal is not None and bal > 0:
            cc_balances[ccy] = bal

    metrics_by_ccy: dict[str, dict[str, Any]] = {}
    for ccy, lst in by_ccy.items():
        op = opening_by_ccy.get(ccy)
        cl = closing_by_ccy.get(ccy)
        metrics_by_ccy[ccy] = compute_metrics(
            lst, meta, ccy,
            opening_override=op if (op is not None and cl is not None) else None,
            closing_override=cl if (op is not None and cl is not None) else None,
            use_txn_balances=use_txn_balances,
        )

    assets = build_assets(meta, metrics_by_ccy, cc_balances=cc_balances)
    drilldown = build_balance_sheet_drilldown(raw, cc_balances=cc_balances,
                                             use_txn_balances=use_txn_balances)
    _assert_drilldown_reconciles(assets, drilldown)
    return {
        "meta": meta, "metrics_by_ccy": metrics_by_ccy, "assets": assets,
        "drilldown": drilldown, "has_txns": bool(txns), "source": path.name,
    }


def analyze_statement(raw: dict[str, Any], meta: dict[str, Any],
                      txns: list[Txn], path: Path,
                      use_txn_balances: bool = False) -> dict[str, Any]:
    """Per-file IR: metrics inferred from statement_meta / transaction flow.

    When ``use_txn_balances`` is True (e.g. transactions have been filtered by
    ``start_date``/``end_date``), statement-meta balances are ignored and
    opening/closing are derived from the filtered transaction stream instead.
    """
    by_ccy: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        by_ccy[t["currency"]].append(t)
    for lst in by_ccy.values():
        lst.sort(key=lambda x: x["date"])

    metrics_by_ccy: dict[str, dict[str, Any]] = {}
    for ccy, lst in by_ccy.items():
        metrics_by_ccy[ccy] = compute_metrics(lst, meta, ccy,
                                              use_txn_balances=use_txn_balances)

    cc_balances: dict[str, float] = {}
    cc_txns = sorted(
        (t for t in txns if str(t.get("account_type", "")).lower() in _LIABILITY_ACCOUNT_TYPES),
        key=lambda t: t["date"], reverse=True,
    )
    for t in cc_txns:
        ccy = t["currency"]
        if ccy in cc_balances:
            continue
        bal = t.get("balance_after")
        if bal is not None and bal > 0:
            cc_balances[ccy] = bal

    assets = build_assets(meta, metrics_by_ccy, cc_balances=cc_balances)
    drilldown = build_balance_sheet_drilldown(raw, cc_balances=cc_balances,
                                             use_txn_balances=use_txn_balances)
    _assert_drilldown_reconciles(assets, drilldown)
    return {
        "meta": meta, "metrics_by_ccy": metrics_by_ccy, "assets": assets,
        "drilldown": drilldown, "has_txns": bool(txns), "source": path.name,
    }


# ---------------------------------------------------------------------------
# Report rendering — delegated to render_md.py
# ---------------------------------------------------------------------------

from pfa_analysis.categorize import UNCATEGORIZED  # noqa: E402
from pfa_analysis.render_md import render_report, fmt  # noqa: E402

# Demo data (default schema, single currency)
# ---------------------------------------------------------------------------

def process_one_file(path: Path, out_dir: Path) -> dict[str, Any]:
    result = _analyze_file(path)
    text = render_report(result)
    out_path = out_dir / (path.stem + "_Finance_Report.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ = out_path.write_text(text, encoding="utf-8")
    print(f"Written: {out_path}")
    np_ = (sum(result["assets"]["cash"].values()) + sum(result["assets"]["time_deposits"].values())
           + sum(result["assets"]["investments"].values()))
    print(f"  net_position={fmt(np_)}  currencies={sorted(result['metrics_by_ccy'])}")
    return result


def build_income_expense_drilldowns(
    consolidated_path: Path,
    categories_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build income/expense drilldowns for the consolidated report.

    Classifies every non-transfer inflow by income source and every outflow by
    category (from ``categories.json``), grouped per currency. Returns
    ``(income_drilldown, expense_drilldown)`` in the shape the report renderer
    expects. Safe to call on any consolidated IR path.

    When *start_date* and/or *end_date* are provided, only transactions within
    the inclusive date range are included.

    This is the canonical implementation of the logic duplicated historically in
    the ``batch_parse`` test driver; callers (CLI ``main``, scripts) should use
    this instead of re-implementing it.
    """
    income_drill: list[dict[str, Any]] = []
    expense_drill: list[dict[str, Any]] = []
    try:
        _, ir_rows = _load_ir_with_txn_id(consolidated_path, start_date, end_date)
    except Exception:
        return income_drill, expense_drill

    src_ccy: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    src_txns: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exp_ccy: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    exp_txns: dict[str, list[dict[str, Any]]] = defaultdict(list)

    categories: dict[str, str] = {}
    if Path(categories_path).exists():
        try:
            categories = json.loads(Path(categories_path).read_text(encoding="utf-8"))
        except Exception:
            categories = {}

    for row in ir_rows:
        if row.get("is_internal_transfer"):
            continue

        txn_id = row.get("txn_id", "")
        cat_full = categories.get(txn_id, "")
        cat_cls, cat_sub = _split_cat(cat_full) if cat_full else ("", "")

        if cat_cls == "Expense":
            cat_disp = cat_sub or cat_full or UNCATEGORIZED
            exp_ccy[cat_disp][row["currency"]] += float(row["amount"])
            exp_txns[cat_disp].append({
                "date": str(row.get("date", "")),
                "description": str(row.get("description", "")),
                "amount": abs(float(row["amount"])),
                "currency": str(row.get("currency", "")),
                "bank": str(row.get("bank", "")),
                "account": str(row.get("account", "")),
                "account_type": str(row.get("account_type", "")),
            })
        elif cat_cls == "Income":
            src = cat_sub or cat_full or UNCATEGORIZED
            src_ccy[src][row["currency"]] += abs(float(row["amount"]))
            src_txns[src].append({
                "date": str(row.get("date", "")),
                "description": str(row.get("description", "")),
                "amount": abs(float(row["amount"])),
                "currency": str(row.get("currency", "")),
                "bank": str(row.get("bank", "")),
                "account": str(row.get("account", "")),
                "account_type": str(row.get("account_type", "")),
            })
        elif cat_cls == "Transfer":
            # Transfers (internal and external) are excluded from income/expense.
            continue
        else:
            # Fallback: transaction not yet categorized — use cash-flow
            # classification to assign to income or expense.
            flow = classify_cash_flow(row)
            if flow == "Income":
                src = cat_sub or cat_full or UNCATEGORIZED
                src_ccy[src][row["currency"]] += abs(float(row["amount"]))
                src_txns[src].append({
                    "date": str(row.get("date", "")),
                    "description": str(row.get("description", "")),
                    "amount": abs(float(row["amount"])),
                    "currency": str(row.get("currency", "")),
                    "bank": str(row.get("bank", "")),
                    "account": str(row.get("account", "")),
                    "account_type": str(row.get("account_type", "")),
                })
            elif flow == "Expense":
                cat_disp = cat_sub or cat_full or UNCATEGORIZED
                exp_ccy[cat_disp][row["currency"]] += float(row["amount"])
                exp_txns[cat_disp].append({
                    "date": str(row.get("date", "")),
                    "description": str(row.get("description", "")),
                    "amount": abs(float(row["amount"])),
                    "currency": str(row.get("currency", "")),
                    "bank": str(row.get("bank", "")),
                    "account": str(row.get("account", "")),
                    "account_type": str(row.get("account_type", "")),
                })

    for src in sorted(src_ccy, key=lambda s: -sum(src_ccy[s].values())):
        income_drill.append({
            "source": src,
            "by_currency": dict(src_ccy[src]),
            "transactions": sorted(src_txns[src], key=lambda t: -t["amount"]),
        })
    for cat in sorted(exp_ccy, key=lambda c: -sum(abs(v) for v in exp_ccy[c].values())):
        expense_drill.append({
            "category": cat,
            "by_currency": dict(exp_ccy[cat]),
            "transactions": sorted(exp_txns[cat], key=lambda t: -t["amount"]),
        })

    return income_drill, expense_drill


def _internal_transfer_ids(
    consolidated_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[str]:
    """Return txn_ids of internal transfers in the consolidated IR (date-filtered)."""
    try:
        _, ir_rows = _load_ir_with_txn_id(consolidated_path, start_date, end_date,
                                          keep_internal=True)
    except Exception:
        return []
    return [r.get("txn_id", "") for r in ir_rows if r.get("is_internal_transfer")]


def build_transfer_drilldown(
    consolidated_path: Path,
    categories_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Build transfer drilldown from a consolidated IR and categories.

    Groups transactions by their transfer sub-category (from ``categories.json``,
    e.g. ``Transfer: Internal``, ``Transfer: External``) and by ``is_internal_transfer``
    flag. Returns a list of ``{"category": str, "by_currency": {ccy: amount}, "transactions": [...]}``
    dicts sorted by total absolute amount descending.
    """
    transfer_drill: list[dict[str, Any]] = []
    try:
        _, ir_rows = _load_ir_with_txn_id(consolidated_path, start_date, end_date)
    except Exception:
        return transfer_drill

    categories: dict[str, str] = {}
    if Path(categories_path).exists():
        try:
            categories = json.loads(Path(categories_path).read_text(encoding="utf-8"))
        except Exception:
            categories = {}

    tr_ccy: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    tr_txns: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in ir_rows:
        txn_id = row.get("txn_id", "")
        cat_full = categories.get(txn_id, "")
        cat_cls, cat_sub = _split_cat(cat_full) if cat_full else ("", "")

        # Determine transfer category.
        if cat_cls == "Transfer":
            cat_disp = cat_sub or cat_full or "Transfer"
        elif row.get("is_internal_transfer"):
            cat_disp = "Internal Transfer"
        else:
            # Skip non-transfer transactions.
            continue

        tr_ccy[cat_disp][row["currency"]] += float(row["amount"])
        tr_txns[cat_disp].append({
            "date": str(row.get("date", "")),
            "description": str(row.get("description", "")),
            "amount": abs(float(row["amount"])),
            "currency": str(row.get("currency", "")),
            "bank": str(row.get("bank", "")),
            "account": str(row.get("account", "")),
            "account_type": str(row.get("account_type", "")),
        })

    for cat in sorted(tr_ccy, key=lambda c: -sum(abs(v) for v in tr_ccy[c].values())):
        transfer_drill.append({
            "category": cat,
            "by_currency": dict(tr_ccy[cat]),
            "transactions": sorted(tr_txns[cat], key=lambda t: -t["amount"]),
        })

    return transfer_drill


def _split_default(txns: list[Txn], meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Helper for demo: group default-schema txns by currency and compute metrics."""
    by_ccy: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        by_ccy[t["currency"]].append(t)
    return {ccy: compute_metrics(lst, meta, ccy) for ccy, lst in by_ccy.items()}


# ---------------------------------------------------------------------------
# Dashboard JSON builder — produces dashboard_data.json per the plan at
# .plan/finance-dashboard-plan.md
# ---------------------------------------------------------------------------


# Discretionary vs non-discretionary classification.
_DISCRETIONARY_MAP: dict[str, bool] = {
    "Groceries": False,
    "Utilities": False,
    "Transport": False,
    "Health": False,
    "Fees": False,
    "Dining": True,
    "Shopping": True,
    "Entertainment": True,
    "Travel": True,
    "Personal Care": True,
}

# Account types that are NOT liquid cash and are instead reported as fixed
# deposits or investments. Covers both short and long IR `account_type` spellings.
_NON_CASH_ACCOUNT_TYPES = {
    "fixed", "time", "fixed_deposit", "time_deposit",
    "securities", "security", "investment", "srs", "unit_trust",
}
# Account types representing fixed-term deposits.
_FD_ACCOUNT_TYPES = {"fixed", "time", "fixed_deposit", "time_deposit"}
# Liability accounts — their balances represent debt, not assets.
_LIABILITY_ACCOUNT_TYPES = {"credit_card"}


def _classify_discretionary(category: str) -> bool:
    """Return True if the category is discretionary spending."""
    return _DISCRETIONARY_MAP.get(category, True)


def _split_cat(cat: str) -> tuple[str, str]:
    """Split a ``"Class:Subtype"`` or ``"Class: Subtype"`` string into ``(cls, sub)``."""
    if ": " in cat:
        parts = cat.split(": ", 1)
    elif ":" in cat:
        parts = cat.split(":", 1)
    else:
        return ("", cat)
    return (parts[0], parts[1])


def _build_meta(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """Extract statement meta from raw IR JSON (shared by both formats).

    When the payload is a consolidated IR (has ``accounts[]``), the account
    summary, investment holdings and fixed-deposit records are derived from
    the nested account structures.
    """
    sm = data.get("statement_meta", {})
    extras: dict[str, Any] = dict(data.get("extras", {}))
    acct_summary_list: list[dict[str, Any]] = []
    inv_holdings_list: list[dict[str, Any]] = []
    # Non-consolidated IR carries these at the top level; consolidated IR
    # derives them from the nested accounts[] instead.
    if "accounts" not in data:
        acct_summary_list = list(data.get("account_summary", []) or [])
        inv_holdings_list = list(data.get("investment_holdings", []) or [])
    meta: dict[str, Any] = {
        "bank": sm.get("institution", ""),
        "account_no": sm.get("account_id", ""),
        "currency": sm.get("currency", sm.get("functional_currency", "")),
        "period_start": sm.get("period_from"),
        "period_end": sm.get("period_to"),
        "opening": sm.get("opening_balance"),
        "closing": sm.get("closing_balance"),
        "account_summary": acct_summary_list,
        "investment_holdings": inv_holdings_list,
        "extras": extras,
        "source_file": data.get("source_file", path.name),
        "_consolidated": _is_consolidated(data),
    }
    if "accounts" in data:
        time_dep_list: list[dict[str, Any]] = []
        for acct in data.get("accounts", []):
            ccy = acct.get("currency", "")
            atype = str(acct.get("account_type", "")).lower()
            acct_summary_list.append({
                "account_no": acct.get("account_no", ""),
                "currency": ccy,
                "opening_balance": parse_num(acct.get("opening_balance")),
                "closing_balance": parse_num(acct.get("closing_balance")),
                "account_type": atype,
            })
            for h in acct.get("investment_holdings", []):
                inv_holdings_list.append(h)
            # Unified logic: a time deposit is always priced at the account's own
            # closing_balance. Every FD account is expected to carry one, so
            # fd_records is no longer consulted — an FD with no closing_balance is
            # simply dropped (data-quality) rather than reconstructed.
            if atype in _FD_ACCOUNT_TYPES:
                bal = parse_num(acct.get("closing_balance"))
                if bal is not None:
                    time_dep_list.append({
                        "account_no": acct.get("account_no", ""),
                        "currency": ccy,
                        "closing_balance": bal,
                        "deposit_no": acct.get("account_no", ""),
                    })
        if time_dep_list:
            extras["time_deposits"] = time_dep_list
    return meta


def _load_ir_with_txn_id(path: Path,
                         start_date: str | None = None,
                         end_date: str | None = None,
                         keep_internal: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load an IR JSON and return (meta, flat_txn_rows_with_txn_id).

    Handles both old (flat ``transactions[]``) and new
    (nested ``accounts[].transactions[]``) IR formats.
    Each returned row has: txn_id, date, description, amount, currency,
    balance_after, bank, account, is_internal_transfer.

    When *start_date* and/or *end_date* are provided, only transactions whose
    ``posted_date`` falls within the inclusive range are included.

    When *keep_internal* is True, internal transfers are not filtered out.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = _build_meta(data, path)
    rows: list[dict[str, Any]]

    # Detect new format: has top-level "accounts" array
    if "accounts" in data:
        rows = []
        own_digits = _own_account_digits(data.get("accounts", []))
        for acct in data.get("accounts", []):
            inst = acct.get("institution", "")
            acct_no = acct.get("account_no", "")
            acct_ccy = acct.get("currency", "")
            acct_type = acct.get("account_type", "")
            for txn in acct.get("transactions", []):
                # Apply date filter early to skip unwanted transactions
                if start_date or end_date:
                    posted = str(txn.get("posted_date", "")).strip()
                    if not posted:
                        continue
                    if start_date and posted < start_date:
                        continue
                    if end_date and posted > end_date:
                        continue
                desc = str(txn.get("description", "")).strip()
                is_internal = bool(txn.get("is_internal_transfer", False)) or _is_self_reference(desc, own_digits)
                if is_internal and not keep_internal:
                    continue
                rows.append({
                    "txn_id": str(txn.get("txn_id", "")),
                    "date": str(txn.get("posted_date", txn.get("value_date", ""))),
                    "description": desc,
                    "amount": float(txn.get("amount", 0.0)),
                    "currency": str(txn.get("currency", acct_ccy)),
                    "account_type": acct_type,
                    "balance_after": parse_num(txn.get("balance_after")),
                    "bank": inst,
                    "account": acct_no,
                    "is_internal_transfer": is_internal,
                })
        return meta, rows

    # Old format: flat transactions[]
    rows = []
    for txn in data.get("transactions", []):
        rows.append({
            "txn_id": str(txn.get("txn_id", "")),
            "date": str(txn.get("posted_date", txn.get("value_date", ""))),
            "description": str(txn.get("description", "")).strip(),
            "amount": float(txn.get("amount", 0.0)),
            "currency": str(txn.get("currency", "")),
            "balance_after": parse_num(txn.get("balance_after")),
            "bank": meta["bank"],
            "account": meta["account_no"],
            "is_internal_transfer": bool(txn.get("is_internal_transfer", False)),
        })
    return meta, rows
