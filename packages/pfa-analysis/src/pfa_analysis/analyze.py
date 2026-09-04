"""Analyse personal balance sheet and cash flow from processed bank-statement JSON.

Input format: the consolidated ``.ir.json`` produced by the statement parser /
consolidator, which nests every account under a top-level ``accounts[]`` array:

    { "statement_meta": {institution, currency, period_from/to, ...},
      "accounts": [ { "account_no", "account_type", "currency",
                      "opening_balance", "closing_balance",
                      "transactions": [{txn_id, posted_date, amount (signed),
                        currency, description, is_internal_transfer,
                        balance_after, ...}] } ],
      "investment_holdings": [...], "extras": {...} }

Single-account statements are simply a one-element ``accounts[]``. The legacy
flat ``transactions[]`` and the simple ``{account, period, balances}`` schema are
no longer supported.

The skill focuses on balance sheet + cash flow (NOT merchant spending
categorization). Cash flows are classified only as Income / Expense / Transfer
In / Transfer Out — the minimum needed for an honest cash-flow statement.

CLI (entry point: ``report.py``):

    python -m pfa_analysis.report <consolidated.ir.json> [output_dir]
    python -m pfa_analysis.report --demo                  # embedded synthetic data

Reports contain: Summary, Balance Sheet (cash + deposits +
investments, per currency), Cash Flow Statement (per currency, reconciled to
balance), Key Observations, Notes & Caveats.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import cast

# FX rate retrieval (cached, base = SGD). Falls back to pfa_fx defaults.
from pfa_fx import DEFAULT_FX_RATES, fetch_fx_rates

# Relationship labels describing how transactions link to each other.
from pfa_ir_schema.relations import REL_CURRENCY_CONVERSION

# Internal-transfer reconciliation (and self-reference promotion) lives in the
# standalone pfa-ir-verifier package so it can be run independently (e.g. CI)
# without the analysis stack.
from pfa_ir_verifier import demote_orphan_internal_transfers, promote_internal_transfers

# ---------------------------------------------------------------------------
# Internal transaction model
# ---------------------------------------------------------------------------
#
# `amount` is SIGNED: credits/income positive, debits/spend negative.
from pfa_analysis.types import (
    AccountSummaryEntry,
    AnalysisResult,
    DrilldownRow,
    DrilldownTxn,
    Extras,
    FxPair,
    FxResult,
    IncomeExpenseGroup,
    Meta,
    Metrics,
    RawAccount,
    RawHolding,
    RawIr,
    TimeDeposit,
    TransferGroup,
    Txn,
)

# The verifier's ``promote_internal_transfers`` takes ``list[dict[str, Any]]``;
# the row's real value types are a strict subset, so this alias lets us bridge
# the call without leaking ``Any`` into the analysis types.
RawRowForVerifier = dict[str, str | float | bool | None | list[str]]


# ---------------------------------------------------------------------------
# Numeric parsing helpers
# ---------------------------------------------------------------------------

def parse_num(s: object) -> float | None:
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
#
# Only the consolidated ``accounts[]`` IR is supported (see ``load_statement`` and
# ``_load_consolidated_ir``). The legacy flat ``transactions[]`` mapping and the
# simple ``{account, period, balances}`` schema were dropped.


def _is_consolidated(data: RawIr) -> bool:
    """A file is a consolidated IR when it carries a top-level ``accounts[]`` array.

    Every supported input is a consolidated IR (single-account statements simply
    have one entry in ``accounts[]``), so the structural presence of ``accounts[]``
    is the single detection rule. Legacy flat ``transactions[]`` / simple-schema
    files (which lack ``accounts[]``) are no longer accepted.
    """
    return "accounts" in data


def load_statement(path: Path,
                   start_date: str | None = None,
                   end_date: str | None = None) -> tuple[Meta, list[Txn]]:
    """Load a consolidated IR JSON and return (meta, normalized transactions).

    The only supported input is a consolidated IR that nests every account under
    a top-level ``accounts[]`` array (single-account statements are just a
    one-element ``accounts[]``). Legacy flat ``transactions[]`` and simple
    ``{account, period, balances}`` schemas raise ``ValueError``.

    When *start_date* and/or *end_date* are provided, transactions are
    filtered to the inclusive date range.
    """
    data = cast(RawIr, json.loads(path.read_text(encoding="utf-8")))
    if "accounts" not in data:
        raise ValueError(
            f"{path.name}: unsupported statement format. Provide a consolidated "
            f"`.ir.json` with a top-level `accounts[]` array (old flat-"
            f"transactions / simple-schema formats are no longer supported)."
        )
    return _load_consolidated_ir(data, path, start_date, end_date)


def _own_account_digits(accounts: list[RawAccount]) -> set[str]:
    """Digits-only forms of every known account number (for self-reference detection)."""
    digits: set[str] = set()
    for acct in accounts:
        no = "".join(ch for ch in str(acct.get("account_no", "")) if ch.isdigit())
        if len(no) >= 4:
            digits.add(no)
    return digits


def _own_liability_digits(accounts: list[RawAccount]) -> set[str]:
    """Digits-only forms of the user's own *liability* account numbers (credit cards).

    A payment from an asset account to one of these is a balance-sheet settlement
    (asset -> own liability), not new spending, and is treated as an internal
    transfer. See ``promote_internal_transfers`` / ``_is_liability_settlement``.
    """
    digits: set[str] = set()
    for acct in accounts:
        if str(acct.get("account_type", "")).lower() in _LIABILITY_ACCOUNT_TYPES:
            no = "".join(ch for ch in str(acct.get("account_no", "")) if ch.isdigit())
            if len(no) >= 4:
                digits.add(no)
    return digits


def _load_consolidated_ir(data: RawIr, path: Path,
                          start_date: str | None = None,
                          end_date: str | None = None) -> tuple[Meta, list[Txn]]:
    """Load a consolidated IR: flatten transactions across all accounts.

    When *start_date* and/or *end_date* are provided, only transactions whose
    ``posted_date`` falls within the inclusive range are included.
    """
    meta = _build_meta(data, path)
    own_digits = _own_account_digits(data.get("accounts", []))
    own_liability_digits = _own_liability_digits(data.get("accounts", []))
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
            # Start from the consolidator's authoritative flag only. Description-
            # based self-reference promotion is deferred to promote_internal_transfers,
            # which validates that an opposite-sign, equal-magnitude partner leg
            # exists (prevents false-positive self-reference gaps).
            is_internal = bool(t.get("is_internal_transfer", False))
            txns.append({
                "date": str(t.get("posted_date", t.get("value_date", ""))),
                "description": desc,
                "amount": float(t.get("amount", 0.0)),
                "currency": str(t.get("currency", acct_ccy)),
                "account_type": acct_type,
                "account": acct.get("account_no", ""),
                "institution": acct.get("institution", ""),
                "is_internal_transfer": is_internal,
                "balance_after": parse_num(t.get("balance_after")),
                "txn_id": str(t.get("txn_id", "") or ""),
                "linked_txn_ids": list(t.get("linked_txn_ids", []) or []),
                "link_labels": list(t.get("link_labels", []) or []),
            })
    # Promote candidate self-reference rows only when a valid partner leg exists.
    promote_internal_transfers(cast(list[RawRowForVerifier], txns), own_digits, amount_key="amount",
                                desc_key="description", flag_key="is_internal_transfer",
                                own_liability_digits=own_liability_digits)
    return meta, txns


# Legacy loaders (_load_default / _load_ir) were removed; `load_statement` now
# requires the consolidated `accounts[]` IR (see `_load_consolidated_ir`).


# ---------------------------------------------------------------------------
# Cash-flow classification (balance-sheet relevant only)
# ---------------------------------------------------------------------------
#
# Income / Expense / Transfer In / Transfer Out. No merchant categories.

TRANSFER_KEYWORDS = [
    "FUNDS TRANSFER", "FDWD", "FIXED DEPOSIT", "TOP-UP TO PAYLAH",
    "PAYMENT BY INTERNET",
]


# Account types that follow the credit-card sign convention: a *positive*
# amount is a charge (debit / outflow), a negative amount is a payment / credit.
_CREDIT_LIKE_ACCOUNTS = {"credit_card"}


def _is_debit(txn: Txn) -> bool:
    """True when ``txn`` is a debit (outflow: expense or transfer out).

    The sign convention differs by account type:
      * current / savings / fixed / investment accounts: negative = debit.
      * credit_card accounts: positive = charge = debit (banking convention).
    Every supported IR carries ``account_type`` via its ``accounts[]`` entry.
    """
    at = str(txn.get("account_type", "")).lower()
    if at in _CREDIT_LIKE_ACCOUNTS:
        return txn["amount"] > 0
    return txn["amount"] < 0


def _expense_contrib(amount: float, account_type: str) -> float:
    """Signed expense (outflow) contribution for one row.

    Asset-side expenses are stored negative and liability-side (credit-card)
    expenses are stored positive, so the true outflow is ``-amount`` on the
    asset side and ``+amount`` on the liability side. Refund rows net out
    automatically (a credit-card refund is stored negative -> -amount < 0).
    """
    at = str(account_type).lower()
    return -amount if at not in _CREDIT_LIKE_ACCOUNTS else amount


def _income_contrib(amount: float, account_type: str) -> float:
    """Signed income (inflow) contribution for one row (opposite sign rule)."""
    at = str(account_type).lower()
    return amount if at not in _CREDIT_LIKE_ACCOUNTS else -amount


def classify_cash_flow(txn: Txn, category: str | None = None) -> str:
    """Return one of Income / Expense / Transfer In / Transfer Out.

    A row is a transfer when either:
      * ``is_internal_transfer`` is set (internal move between own accounts), or
      * its resolved ``category`` starts with ``"Transfer:"`` (e.g. the
        ``Transfer: External`` produced by categorize.py for a PayNow transfer
        to a person). Honouring the category — rather than a bare keyword — keeps
        merchant PayNow (e.g. ``PAYNOW TO SWEE HENG``, categorised as Dining)
        correctly out of the transfer bucket while still promoting person-to-
        person PayNow to Transfer Out.
    """
    if txn.get("is_internal_transfer") or (category or "").startswith("Transfer:"):
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


def parse_date_to_iso(s: object) -> str | None:
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

def compute_metrics(txns: list[Txn], meta: Meta, ccy: str,
                    opening_override: float | None = None,
                    closing_override: float | None = None,
                    use_txn_balances: bool = False,
                    txn_categories: dict[str, str] | None = None) -> Metrics:
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

    ``txn_categories`` optionally maps ``txn_id`` -> category (e.g. the
    ``Transfer: External`` produced by categorize.py). When supplied, a row whose
    category starts with ``"Transfer:"`` is counted as a transfer even though its
    description may not contain a transfer keyword (e.g. a PayNow transfer to a
    person). Rows without a category fall back to keyword/``is_internal_transfer``
    detection.
    """
    income = expense = transfer_in = transfer_out = 0.0
    transfer_in_int = transfer_in_ext = 0.0
    transfer_out_int = transfer_out_ext = 0.0
    fx_conv_in = fx_conv_out = 0.0
    for t in txns:
        cat = (txn_categories or {}).get(t.get("txn_id", "")) if txn_categories is not None else None
        c = classify_cash_flow(t, cat)
        # Outflows (expense / transfer-out) are debits; inflows (income /
        # transfer-in) are credits. The signed contribution depends on the
        # account side (asset vs liability), per _expense_contrib / _income_contrib.
        a = (_expense_contrib(t["amount"], t.get("account_type", ""))
             if c in ("Expense", "Transfer Out")
             else _income_contrib(t["amount"], t.get("account_type", "")))
        is_int = bool(t.get("is_internal_transfer", False))
        if REL_CURRENCY_CONVERSION in (t.get("link_labels") or []):
            # Currency conversions are neither income/expense nor transfers — they
            # live in the dedicated "Currency Conversions" section. They stay in
            # total inflow/outflow so per-currency cash still reconciles, but are
            # kept out of the operating (income/expense) and transfer buckets.
            if _is_debit(t):
                fx_conv_out += a
            else:
                fx_conv_in += a
            continue
        if c == "Income":
            income += a
        elif c == "Transfer In":
            transfer_in += a
            if is_int:
                transfer_in_int += a
            else:
                transfer_in_ext += a
        elif c == "Expense":
            expense += a
        elif c == "Transfer Out":
            transfer_out += a
            if is_int:
                transfer_out_int += a
            else:
                transfer_out_ext += a

    total_inflow = income + transfer_in + fx_conv_in
    total_outflow = expense + transfer_out + fx_conv_out
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
    elif meta.get("currency") == ccy and (mo := meta.get("opening")) is not None:
        opening = float(mo)
        closing = float(meta.get("closing") or 0.0)
    elif txns:
        opening = (txns[0]["balance_after"] or 0.0) - txns[0]["amount"]
        closing = txns[-1]["balance_after"] or 0.0
    else:
        opening = closing = None

    balance_change = (closing - opening) if (opening is not None and closing is not None) else None
    recon_ok = abs(balance_change - net_change_cash) < 0.005 if balance_change is not None else None

    return {
        "income": income, "expense": expense, "transfer_in": transfer_in,
        "transfer_out": transfer_out,
        "transfer_in_internal": transfer_in_int,
        "transfer_in_external": transfer_in_ext,
        "transfer_out_internal": transfer_out_int,
        "transfer_out_external": transfer_out_ext,
        "fx_conversion_in": fx_conv_in,
        "fx_conversion_out": fx_conv_out,
        "total_inflow": total_inflow,
        "total_outflow": total_outflow, "net_change_cash": net_change_cash,
        "net_operating": net_operating, "savings_rate": savings_rate,
        "opening": opening, "closing": closing, "balance_change": balance_change,
        "reconciliation_ok": recon_ok, "txn_count": len(txns),
    }


def build_assets(meta: Meta,
                  cc_balances: dict[str, float] | None = None,
                  use_txn_balances: bool = False) -> dict[str, dict[str, float]]:
    """Assemble balance-sheet assets: cash, time deposits, investments (per currency).

    Credit-card balances are treated as liabilities (deducted from net worth).
    *cc_balances* is a ``{account_no: balance}`` dict (per credit-card account)
    derived from the cutoff-aware ``balance_after`` on each card's transactions.

    Every supported IR carries ``accounts[]``; :func:`_build_meta` derives
    ``account_summary`` from them, so the cash/liability balances always come
    from the statement's account closings (the legacy transaction-derived
    fallback has been removed).
    """
    cash: dict[str, float] = defaultdict(float)
    liabilities: dict[str, float] = defaultdict(float)
    _NON_CASH = _NON_CASH_ACCOUNT_TYPES
    # Per-account credit-card liability: (currency, value). Start from the
    # statement closing_balance, then override per account with the cutoff-aware
    # balance_after when a date window is applied (so overlapping *and*
    # non-overlapping CC statement periods both reconcile with the drill-down,
    # and two cards in the same currency stay distinct).
    liab_detail: dict[str, tuple[str, float]] = {}
    for a in meta.get("account_summary") or []:
        at = str(a.get("account_type", "")).lower()
        c = a.get("currency")
        b = parse_num(a.get("closing_balance"))
        if at in _LIABILITY_ACCOUNT_TYPES:
            acc_no = a.get("account_no", "")
            if c and b is not None:
                liab_detail[acc_no] = (c, abs(b))
            continue
        if at in _NON_CASH:
            continue
        if c and b is not None:
            cash[c] += b
    if use_txn_balances and cc_balances:
        for acc_no, bal in cc_balances.items():
            if bal > 0 and acc_no in liab_detail:
                liab_detail[acc_no] = (liab_detail[acc_no][0], bal)
    for c, v in liab_detail.values():
        liabilities[c] += v

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
                                  drilldown: list[DrilldownRow]) -> list[str]:
    """Check that drill-down subtotals match the Balance Sheet totals.

    Compares per-currency, per-bucket sums from the drill-down rows against
    the corresponding values in *assets*. Historically this raised an
    ``AssertionError`` on mismatch (catching double-counting bugs in
    ``build_assets`` and schema drift between the two code paths). It now
    returns a list of human-readable warning strings instead, so a mismatch
    is surfaced in the report rather than aborting the analysis run.
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

    warnings: list[str] = []
    for asset_key in ("cash", "time_deposits", "investments", "liabilities"):
        asset_by_ccy = assets.get(asset_key, {})
        dd_by_ccy = dd_totals.get(asset_key, {})
        all_ccies = sorted(set(list(asset_by_ccy) + list(dd_by_ccy)))
        for ccy in all_ccies:
            asset_val = asset_by_ccy.get(ccy, 0.0)
            dd_val = dd_by_ccy.get(ccy, 0.0)
            if abs(asset_val - dd_val) >= 0.005:
                warnings.append(
                    f"Balance Sheet / drill-down mismatch for {asset_key} {ccy}: "
                    f"Balance Sheet={asset_val:.2f}, Drill-Down={dd_val:.2f}"
                )
    return warnings


def build_balance_sheet_drilldown(raw: RawIr,
                                  cc_balances: dict[str, float] | None = None,
                                  use_txn_balances: bool = False,
                                  start_date: str | None = None,
                                  end_date: str | None = None) -> list[DrilldownRow]:
    """Account-level breakdown that feeds the Balance Sheet Drill-Down section.

    Mirrors the bucket assignment in :func:`build_assets` exactly so the
    drill-down reconciles with the Balance Sheet totals. Each row is::

        {"currency", "institution", "account_no", "account_type", "bucket",
         "native_value", "derivation"}

    ``bucket`` is one of ``Cash`` / ``Time Deposit`` / ``Investment`` /
    ``Dropped``. ``Dropped`` flags accounts that contribute nothing to the
    balance sheet (e.g. a liquid account whose ``closing_balance`` is null).
    """
    rows: list[DrilldownRow] = []

    def _add(ccy: str, inst: str, acc: str, at: str, bucket: str,
             value: float, deriv: str, carried_forward: bool = False,
             period_to: str | None = None,
             missing_early: bool = False,
             earliest_covered: str | None = None,
             latest_covered: str | None = None) -> None:
        rows.append({
            "currency": ccy, "institution": inst, "account_no": acc,
            "account_type": at, "bucket": bucket, "native_value": value,
            "derivation": deriv, "carried_forward": carried_forward,
            "period_to": period_to, "missing_early": missing_early,
            "earliest_covered": earliest_covered, "latest_covered": latest_covered,
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
                last_stmt = acct.get("period_to")
                cc_txns = acct.get("transactions") or []
                txn_dts = [pd for t in cc_txns if (pd := t.get("posted_date"))]
                # Coverage window derived from the actual transactions. period_to /
                # period_from are per-statement and unreliable once statements are
                # merged into one account, so prefer the real posted_date extremes
                # (fall back to the statement period fields when no txns exist).
                earliest = min(txn_dts) if txn_dts else acct.get("period_from")
                latest = max(txn_dts) if txn_dts else last_stmt
                # Stale / carried forward = the card's data ends before the
                # report-window end, so whatever balance we show (cutoff
                # balance_after or closing balance) is NOT the position as of the
                # window end — even if the card has transactions inside the window.
                # Flag it so the reader knows the liability is approximate.
                cf = bool(use_txn_balances and end_date and latest and latest < end_date)
                # Missing-early = the card's earliest covered transaction is AFTER the
                # window start, i.e. card activity in [start_date, earliest) is not in
                # this report at all (it lives in a prior statement we don't have).
                missing_early = bool(use_txn_balances and start_date
                                     and earliest and start_date < earliest)
                cutoff_bal = cc_balances.get(acc) if (use_txn_balances and cc_balances) else None
                if cutoff_bal is not None:
                    deriv = "balance_after as of cutoff (credit-card debt)"
                    if cf:
                        deriv += (f" — carried forward; data ends {latest}, "
                                  f"before window end {end_date}")
                    if missing_early:
                        deriv += (f" — card transactions before {earliest} missing; "
                                  f"earliest covered date after window start {start_date}")
                    _add(ccy, inst, acc, at, "Liability", cutoff_bal, deriv,
                         carried_forward=cf, period_to=last_stmt,
                         missing_early=missing_early, earliest_covered=earliest,
                         latest_covered=latest)
                elif cb is not None:
                    deriv = "account closing_balance (credit-card debt)"
                    if cf:
                        deriv += (f" — carried forward; data ends {latest}, "
                                  f"before window end {end_date}")
                    if missing_early:
                        deriv += (f" — card transactions before {earliest} missing; "
                                  f"earliest covered date after window start {start_date}")
                    _add(ccy, inst, acc, at, "Liability", abs(cb), deriv,
                         carried_forward=cf, period_to=last_stmt,
                         missing_early=missing_early, earliest_covered=earliest,
                         latest_covered=latest)
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
            last_stmt = a.get("period_to")
            cc_txns = a.get("transactions") or []
            dts = [t.get("posted_date") for t in cc_txns if t.get("posted_date")]
            earliest = min(dts) if dts else a.get("period_from")
            latest = max(dts) if dts else last_stmt
            cf = bool(use_txn_balances and end_date and latest and latest < end_date)
            missing_early = bool(use_txn_balances and start_date
                                 and earliest and start_date < earliest)
            deriv = "account_summary.balance (credit-card debt)"
            if cf:
                deriv += (f" — carried forward; data ends {latest}, "
                          f"before window end {end_date}")
            if missing_early:
                deriv += (f" — card transactions before {earliest} missing; "
                          f"earliest covered date after window start {start_date}")
            _add(ccy, inst, acc, at, "Liability", abs(bal), deriv,
                 carried_forward=cf, period_to=last_stmt,
                 missing_early=missing_early, earliest_covered=earliest,
                 latest_covered=latest)
        elif at in _FD_ACCOUNT_TYPES:
            _add(ccy, inst, acc, at, "Time Deposit", bal, "account_summary.balance")
        elif at in _NON_CASH_ACCOUNT_TYPES:
            _add(ccy, inst, acc, at, "Investment", bal, "account_summary.balance")
        else:
            _add(ccy, inst, acc, at, "Cash", bal, "account_summary.balance")
    return rows


def _analyze_file(path: Path,
                  start_date: str | None = None,
                  end_date: str | None = None,
                  txn_categories: dict[str, str] | None = None) -> AnalysisResult:
    """Analyze one statement file into a structured result.

    Dispatches to the consolidated analysis path (account-balance based) when
    the IR is consolidated, or the per-file analysis path otherwise.

    When *start_date* or *end_date* is provided, opening/closing balances are
    derived from the filtered transaction stream (``balance_after``) rather
    than from the full-period statement/account balances, ensuring Cash Flow
    reconciliation holds for the truncated window.

    ``txn_categories`` optionally maps ``txn_id`` -> category and is forwarded to
    the metrics computation so transfer categories (e.g. PayNow to a person) are
    honoured.
    """
    raw = cast(RawIr, json.loads(Path(path).read_text(encoding="utf-8")))
    meta, txns = load_statement(path, start_date, end_date)
    use_txn_balances = bool(start_date or end_date)
    return _analyze_consolidated(raw, meta, txns, path,
                                 use_txn_balances=use_txn_balances,
                                 txn_categories=txn_categories,
                                 start_date=start_date, end_date=end_date)


def _analyze_consolidated(raw: RawIr, meta: Meta,
                          txns: list[Txn], path: Path,
                          use_txn_balances: bool = False,
                          txn_categories: dict[str, str] | None = None,
                          start_date: str | None = None,
                          end_date: str | None = None) -> AnalysisResult:
    """Consolidated IR: opening/closing come from account balances, not txns.

    When ``use_txn_balances`` is True (e.g. transactions have been filtered by
    ``start_date``/``end_date``), account-summary balances are ignored and
    opening/closing are derived from the filtered transaction stream instead.
    """
    demote_orphan_internal_transfers(txns, own_liability_digits=_own_liability_digits(raw.get("accounts", [])))
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
        for a in meta.get("account_summary") or []:
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
    # Walk txns in reverse date order to pick, per credit-card account, the
    # latest balance_after within the window (cutoff-aware liability). Keyed by
    # account_no so multiple cards in the same currency stay distinct.
    cc_txns = sorted(
        (t for t in txns if str(t.get("account_type", "")).lower() in _LIABILITY_ACCOUNT_TYPES),
        key=lambda t: t["date"], reverse=True,
    )
    for t in cc_txns:
        acc = t.get("account")
        if not acc:
            continue
        if acc in cc_balances:
            continue
        bal = t.get("balance_after")
        if bal is not None and bal > 0:
            cc_balances[acc] = bal

    metrics_by_ccy: dict[str, Metrics] = {}
    for ccy, lst in by_ccy.items():
        op = opening_by_ccy.get(ccy)
        cl = closing_by_ccy.get(ccy)
        metrics_by_ccy[ccy] = compute_metrics(
            lst, meta, ccy,
            opening_override=op if (op is not None and cl is not None) else None,
            closing_override=cl if (op is not None and cl is not None) else None,
            use_txn_balances=use_txn_balances,
            txn_categories=txn_categories,
        )

    assets = build_assets(meta, cc_balances=cc_balances,
                         use_txn_balances=use_txn_balances)
    drilldown = build_balance_sheet_drilldown(raw, cc_balances=cc_balances,
                                             use_txn_balances=use_txn_balances,
                                             start_date=start_date, end_date=end_date)
    reconcile_warnings = _assert_drilldown_reconciles(assets, drilldown)
    return {
        "meta": meta, "metrics_by_ccy": metrics_by_ccy, "assets": assets,
        "drilldown": drilldown, "has_txns": bool(txns), "source": path.name,
        "warnings": reconcile_warnings,
    }


# The per-file (non-consolidated) analysis path was removed: every supported IR
# now carries ``accounts[]`` and is analysed by ``_analyze_consolidated``.


# ---------------------------------------------------------------------------
# Report rendering — delegated to render_md.py
# ---------------------------------------------------------------------------

from pfa_analysis.categorize import UNCATEGORIZED, _split_cat
from pfa_analysis.render_md import fmt, render_report

# Per-file processing helper
# ---------------------------------------------------------------------------

def process_one_file(path: Path, out_dir: Path) -> AnalysisResult:
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
) -> tuple[list[IncomeExpenseGroup], list[IncomeExpenseGroup]]:
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
    income_drill: list[IncomeExpenseGroup] = []
    expense_drill: list[IncomeExpenseGroup] = []
    try:
        _, ir_rows = _load_ir_with_txn_id(consolidated_path, start_date, end_date)
    except Exception:  # noqa: BLE001 -- degrade to empty drilldown, never crash the report
        return income_drill, expense_drill

    src_ccy: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    src_txns: dict[str, list[DrilldownTxn]] = defaultdict(list)
    exp_ccy: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    exp_txns: dict[str, list[DrilldownTxn]] = defaultdict(list)

    categories: dict[str, str] = {}
    if Path(categories_path).exists():
        try:
            categories = json.loads(Path(categories_path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- malformed categories.json degrades to {}
            categories = {}

    for row in ir_rows:
        if row.get("is_internal_transfer"):
            continue
        # Currency conversions are not third-party transfers; they are surfaced in
        # the dedicated "Currency Conversions" section, so exclude them from the
        # income/expense breakdowns (including Transfer In/Out (External)).
        if REL_CURRENCY_CONVERSION in (row.get("link_labels") or []):
            continue

        txn_id = row.get("txn_id", "")
        cat_full = categories.get(txn_id, "")
        cat_cls, cat_sub = _split_cat(cat_full) if cat_full else ("", "")

        if cat_cls == "Expense":
            cat_disp = cat_sub or cat_full or UNCATEGORIZED
            amt = _expense_contrib(float(row["amount"]), row.get("account_type", ""))
            exp_ccy[cat_disp][row["currency"]] += amt
            exp_txns[cat_disp].append({
                "date": str(row.get("date", "")),
                "description": str(row.get("description", "")),
                "amount": amt,
                "currency": str(row.get("currency", "")),
                "bank": str(row.get("bank", "")),
                "account": str(row.get("account", "")),
                "account_type": str(row.get("account_type", "")),
            })
        elif cat_cls == "Income":
            src = cat_sub or cat_full or UNCATEGORIZED
            amt_inc = _income_contrib(float(row["amount"]), row.get("account_type", ""))
            src_ccy[src][row["currency"]] += amt_inc
            src_txns[src].append({
                "date": str(row.get("date", "")),
                "description": str(row.get("description", "")),
                "amount": amt_inc,
                "currency": str(row.get("currency", "")),
                "bank": str(row.get("bank", "")),
                "account": str(row.get("account", "")),
                "account_type": str(row.get("account_type", "")),
            })
        elif cat_cls == "Transfer":
            # Internal transfers are own-account moves with no net effect, so they
            # stay excluded from income/expense. External transfers (e.g. PayNow to
            # a person, FAST to another bank) are real inflows/outflows and are
            # surfaced in the breakdowns: Transfer In (External) under income,
            # Transfer Out (External) under expense.
            if cat_sub != "External":
                continue
            flow = classify_cash_flow(row, cat_full)
            if flow == "Transfer In":
                src = "Transfer In (External)"
                amt_inc = _income_contrib(float(row["amount"]), row.get("account_type", ""))
                src_ccy[src][row["currency"]] += amt_inc
                src_txns[src].append({
                    "date": str(row.get("date", "")),
                    "description": str(row.get("description", "")),
                    "amount": amt_inc,
                    "currency": str(row.get("currency", "")),
                    "bank": str(row.get("bank", "")),
                    "account": str(row.get("account", "")),
                    "account_type": str(row.get("account_type", "")),
                })
            elif flow == "Transfer Out":
                cat_disp = "Transfer Out (External)"
                amt = _expense_contrib(float(row["amount"]), row.get("account_type", ""))
                exp_ccy[cat_disp][row["currency"]] += amt
                exp_txns[cat_disp].append({
                    "date": str(row.get("date", "")),
                    "description": str(row.get("description", "")),
                    "amount": amt,
                    "currency": str(row.get("currency", "")),
                    "bank": str(row.get("bank", "")),
                    "account": str(row.get("account", "")),
                    "account_type": str(row.get("account_type", "")),
                })
        else:
            # Fallback: transaction not yet categorized — use cash-flow
            # classification to assign to income or expense.
            flow = classify_cash_flow(row)
            if flow == "Income":
                src = cat_sub or cat_full or UNCATEGORIZED
                amt_inc = _income_contrib(float(row["amount"]), row.get("account_type", ""))
                src_ccy[src][row["currency"]] += amt_inc
                src_txns[src].append({
                    "date": str(row.get("date", "")),
                    "description": str(row.get("description", "")),
                    "amount": amt_inc,
                    "currency": str(row.get("currency", "")),
                    "bank": str(row.get("bank", "")),
                    "account": str(row.get("account", "")),
                    "account_type": str(row.get("account_type", "")),
                })
            elif flow == "Expense":
                cat_disp = cat_sub or cat_full or UNCATEGORIZED
                amt = _expense_contrib(float(row["amount"]), row.get("account_type", ""))
                exp_ccy[cat_disp][row["currency"]] += amt
                exp_txns[cat_disp].append({
                    "date": str(row.get("date", "")),
                    "description": str(row.get("description", "")),
                    "amount": amt,
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
    except Exception:  # noqa: BLE001 -- degrade to an empty internal-transfer id list
        return []
    return [r.get("txn_id", "") for r in ir_rows if r.get("is_internal_transfer")]


def _group_linked_adjacent(txns: list[DrilldownTxn]) -> list[DrilldownTxn]:
    """Reorder *txns* so transactions linked via ``linked_txn_ids`` sit next to each other.

    Used for the internal-transfer breakdown: both legs of an own-account move
    (e.g. a current->savings transfer) are surfaced adjacent rather than scattered
    by amount. A stable base order (descending absolute amount) is preserved for
    the first-seen member of each linked group; each of its partners is inserted
    immediately after it. Transactions with no in-list partner keep their base
    position. Order does not affect any totals (they are summed independently).
    """
    by_id = {t.get("txn_id", ""): t for t in txns}
    placed: set[str] = set()
    ordered: list[DrilldownTxn] = []
    for t in sorted(txns, key=lambda x: -abs(float(x.get("amount", 0.0)))):
        tid = t.get("txn_id", "")
        if tid in placed:
            continue
        ordered.append(t)
        placed.add(tid)
        for pid in (t.get("linked_txn_ids", []) or []):
            p = by_id.get(pid)
            if p is not None and pid not in placed:
                ordered.append(p)
                placed.add(pid)
    return ordered


def build_transfer_drilldown(
    consolidated_path: Path,
    categories_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[TransferGroup]:
    """Build transfer drilldown from a consolidated IR and categories.

    Groups transactions by their transfer sub-category (from ``categories.json``,
    e.g. ``Transfer: Internal``, ``Transfer: External``) and by ``is_internal_transfer``
    flag. Returns a list of ``{"category": str, "by_currency": {ccy: amount}, "transactions": [...]}``
    dicts sorted by total absolute amount descending.
    """
    transfer_drill: list[TransferGroup] = []
    try:
        # Internal (own-account) transfers are the only thing this breakdown now
        # surfaces, so keep them in the loaded rows.
        _, ir_rows = _load_ir_with_txn_id(consolidated_path, start_date, end_date,
                                          keep_internal=True)
    except Exception:  # noqa: BLE001 -- degrade to empty transfer drilldown
        return transfer_drill

    tr_ccy: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    tr_txns: dict[str, list[DrilldownTxn]] = defaultdict(list)

    for row in ir_rows:

        # Only own-account (internal) transfers are shown here. External transfers
        # now live in the income/expense breakdowns; currency conversions have their
        # own dedicated section.
        if REL_CURRENCY_CONVERSION in (row.get("link_labels") or []):
            continue
        if not row.get("is_internal_transfer"):
            continue
        cat_disp = "Internal Transfer"

        tr_ccy[cat_disp][row["currency"]] += float(row["amount"])
        tr_txns[cat_disp].append({
            "date": str(row.get("date", "")),
            "description": str(row.get("description", "")),
            "amount": float(row["amount"]),
            "currency": str(row.get("currency", "")),
            "bank": str(row.get("bank", "")),
            "account": str(row.get("account", "")),
            "account_type": str(row.get("account_type", "")),
            "txn_id": str(row.get("txn_id", "")),
            "linked_txn_ids": list(row.get("linked_txn_ids", []) or []),
        })

    for cat in sorted(tr_ccy, key=lambda c: -sum(abs(v) for v in tr_ccy[c].values())):
        transfer_drill.append({
            "category": cat,
            "by_currency": dict(tr_ccy[cat]),
            "transactions": _group_linked_adjacent(tr_txns[cat]),
        })

    return transfer_drill


def _empty_fx_result(base_ccy: str) -> FxResult:
    """Empty realized-FX result (no conversions or load failure)."""
    return {
        "base_currency": base_ccy,
        "total_sgd": 0.0,
        "as_of": "",
        "source": "",
        "by_received_currency": {},
        "pairs": [],
    }


def _resolve_fx_rates(as_of: str | None, fx_rates: dict[str, float] | None
                      ) -> tuple[dict[str, float], str, str]:
    """Resolve FX rates (SGD per 1 unit) and provenance.

    Priority: caller-supplied *fx_rates* → cached fetch as of *as_of* →
    ``pfa_fx.DEFAULT_FX_RATES`` hardcoded fallback. Returns
    ``(rates, as_of_used, source)``.
    """
    if fx_rates:
        return dict(fx_rates), as_of or "", "caller-provided"
    date = as_of or ""
    fx = fetch_fx_rates(date) if date else None
    if fx and fx.get("rates"):
        return (
            {k: float(v) for k, v in fx["rates"].items()},
            fx.get("date", date),
            fx.get("source", "pfa_fx"),
        )
    return dict(DEFAULT_FX_RATES), date, "pfa_fx default"


def compute_fx_gain_loss(
    consolidated_path: Path,
    as_of: str | None = None,
    fx_rates: dict[str, float] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> FxResult:
    """Compute realized FX gain/loss from currency-conversion transaction pairs.

    Walks every transaction whose ``link_labels`` contains
    ``REL_CURRENCY_CONVERSION``. For each pair — a *given* leg (negative amount)
    and a *received* leg (positive amount) in a *different* currency — the
    realized FX gain/loss is the SGD value the received leg is worth at the
    reference rate minus the SGD value of what was given:

        fx_gl_sgd = |amount_received| * rate[recv_ccy]
                    - |amount_given]   * rate[given_ccy]

    where ``rate[ccy]`` is SGD per 1 unit of *ccy* (SGD = 1.0). A positive value
    is a gain (the conversion realised a better rate than the reference); a
    negative value is a loss.

    Only **realized** gain/loss is computed (no foreign-balance revaluation).
    Returns a dict with ``total_sgd``, ``pairs`` (per-pair detail) and
    ``by_received_currency`` (gain/loss grouped by the currency received).
    """
    base_ccy = "SGD"
    try:
        meta, rows = _load_ir_with_txn_id(consolidated_path, start_date, end_date)
    except Exception:  # noqa: BLE001 -- degrade to an empty FX result
        return _empty_fx_result(base_ccy)

    if as_of is None:
        as_of = meta.get("period_to") or meta.get("period_from") or ""

    rates, rate_as_of, source = _resolve_fx_rates(as_of, fx_rates)

    # Index rows by txn_id so we can resolve each conversion's partner leg.
    by_id = {r["txn_id"]: r for r in rows}

    total = 0.0
    by_recv: dict[str, float] = defaultdict(float)
    pairs: list[FxPair] = []

    for r in rows:
        if REL_CURRENCY_CONVERSION not in (r.get("link_labels") or []):
            continue
        # Process each pair exactly once, from the given (outflow) leg.
        if r["amount"] >= 0:
            continue
        given_ccy = r["currency"]
        given_amt = abs(r["amount"])

        recv = None
        for pid in r.get("linked_txn_ids", []) or []:
            p = by_id.get(pid)
            if not p:
                continue
            if p["currency"] != given_ccy and p["amount"] > 0:
                recv = p
                break
        if recv is None:
            # Partner leg missing or not a genuine cross-currency pair; skip.
            continue

        recv_ccy = recv["currency"]
        recv_amt = recv["amount"]
        rg = rates.get(given_ccy)
        rr = rates.get(recv_ccy)
        if rg is None or rr is None:
            # Reference rate unavailable for one of the currencies; skip.
            continue

        given_base = given_amt * rg
        recv_base = recv_amt * rr
        gl = recv_base - given_base
        implied = (recv_amt / given_amt) if given_amt else 0.0

        total += gl
        by_recv[recv_ccy] += gl
        pairs.append({
            "date": r.get("date", ""),
            "given": {"currency": given_ccy, "amount": -given_amt},
            "received": {"currency": recv_ccy, "amount": recv_amt},
            "implied_rate": round(implied, 6),
            "fx_gl_sgd": round(gl, 2),
            "txn_ids": [r["txn_id"], recv["txn_id"]],
        })

    return {
        "base_currency": base_ccy,
        "total_sgd": round(total, 2),
        "as_of": rate_as_of,
        "source": source,
        "by_received_currency": {c: round(v, 2) for c, v in sorted(by_recv.items())},
        "pairs": pairs,
    }


def build_fx_drilldown(
    consolidated_path: Path,
    as_of: str | None = None,
    fx_rates: dict[str, float] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> FxResult:
    """Build the realized FX gain/loss drilldown for the consolidated report.

    Thin wrapper over :func:`compute_fx_gain_loss` so report/dashboard callers
    use the same naming convention as :func:`build_transfer_drilldown`. Returns
    the per-pair realized FX gain/loss detail (base currency SGD).
    """
    return compute_fx_gain_loss(
        consolidated_path, as_of=as_of, fx_rates=fx_rates,
        start_date=start_date, end_date=end_date,
    )


# The demo helper `_split_default` was removed when --demo moved to the
# consolidated `accounts[]` IR (see `report.py::demo_ir`).


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


def _build_meta(data: RawIr, path: Path) -> Meta:
    """Extract statement meta from raw IR JSON (shared by both formats).

    When the payload is a consolidated IR (has ``accounts[]``), the account
    summary, investment holdings and fixed-deposit records are derived from
    the nested account structures.
    """
    sm = data.get("statement_meta", {})
    extras: Extras = cast(Extras, dict(data.get("extras", {})))
    acct_summary_list: list[AccountSummaryEntry] = []
    inv_holdings_list: list[RawHolding] = []
    # Non-consolidated IR carries these at the top level; consolidated IR
    # derives them from the nested accounts[] instead.
    if "accounts" not in data:
        acct_summary_list = list(data.get("account_summary", []) or [])
        inv_holdings_list = list(data.get("investment_holdings", []) or [])
    meta: Meta = {
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
        time_dep_list: list[TimeDeposit] = []
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
                         keep_internal: bool = False) -> tuple[Meta, list[Txn]]:
    """Load a consolidated IR JSON and return (meta, flat_txn_rows_with_txn_id).

    Only the nested ``accounts[].transactions[]`` IR format is supported; the
    legacy flat ``transactions[]`` format is no longer accepted.
    Each returned row has: txn_id, date, description, amount, currency,
    balance_after, bank, account, is_internal_transfer.

    When *start_date* and/or *end_date* are provided, only transactions whose
    ``posted_date`` falls within the inclusive range are included.

    When *keep_internal* is True, internal transfers are not filtered out.
    """
    data = cast(RawIr, json.loads(path.read_text(encoding="utf-8")))
    meta = _build_meta(data, path)
    rows: list[Txn] = []

    # Only the nested accounts[] IR format is supported.
    if "accounts" in data:
        rows = []
        own_digits = _own_account_digits(data.get("accounts", []))
        own_liability_digits = _own_liability_digits(data.get("accounts", []))
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
                # Start from the consolidator's authoritative flag only; defer
                # self-reference promotion to the validated pass below.
                is_internal = bool(txn.get("is_internal_transfer", False))
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
                    "link_labels": list(txn.get("link_labels", []) or []),
                    "linked_txn_ids": list(txn.get("linked_txn_ids", []) or []),
                })
        # Promote candidate self-reference rows only when a valid partner leg exists.
        promote_internal_transfers(cast(list[RawRowForVerifier], rows), own_digits, amount_key="amount",
                                    desc_key="description", flag_key="is_internal_transfer",
                                    own_liability_digits=own_liability_digits)
        if not keep_internal:
            rows = [r for r in rows if not r["is_internal_transfer"]]
        return meta, rows

    # Legacy flat transactions[] format is no longer supported; every IR must
    # carry a top-level accounts[] array (handled by the branch above).
    raise ValueError(
        f"{path.name}: unsupported IR format. Provide a consolidated `.ir.json` "
        f"with a top-level `accounts[]` array (old flat-transactions format is no "
        f"longer supported)."
    )
