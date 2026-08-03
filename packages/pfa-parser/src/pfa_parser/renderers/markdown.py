"""IR → Markdown renderer — one function per bank/family.

Each ``*_ir_to_markdown()`` function accepts a ``ParsedStatement`` and returns
a Markdown string that is **byte-identical** to the old ``*_to_markdown()``
output — because masking (``sanitize_description`` + ``mask_names_in_description``)
is applied at render time rather than inside the parser.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from pfa_ir_schema import (
    Account,
    AccountType,
    FixedDepositRecord,
    ParsedStatement,
    Transaction,
    format_fd_period,
    mask_id,
)
from .helpers import md_masked_description


def _fd_rate_display(r: "FixedDepositRecord") -> str:
    """Return the rate for display, preferring the raw printed string.

    Falls back to formatting the canonical decimal (e.g. ``0.025`` → ``"2.5%"``)
    for round-tripped IR whose ``raw_interest_rate`` was dropped.
    """
    raw = r.raw_interest_rate
    if raw:
        return raw
    rate = r.interest_rate
    if rate is not None:
        return f"{rate * 100:g}%"
    return "—"


def _append_warnings(out: list[str], statement: ParsedStatement) -> None:
    """Append a Warnings section if the IR carries any parser/postprocess warnings.

    Keeps the warnings visible in every rendered statement (extraction gaps,
    inferred values, etc.) without special-casing any bank renderer.
    """
    if not statement.warnings:
        return
    out.append("## Warnings\n")
    for w in statement.warnings:
        out.append(f"- {w}")
    out.append("")


# ============================================================================
# DBS/POSB Consolidated Statement
# ============================================================================

def dbs_ir_to_markdown(statement: ParsedStatement, *, do_mask: bool = True) -> str:
    """Render a DBS/POSB consolidated statement from IR."""
    out: list[str] = []
    meta = statement.statement_meta

    out.append("# DBS/POSB — Consolidated Statement\n")
    sd = meta.period_to or meta.period_from or ""
    out.append(f"**Statement Date:** {sd}  ")
    currencies = ", ".join(sorted({a.currency for a in statement.accounts if a.currency}))
    if currencies:
        out.append(f"**Currency:** {currencies}  ")
    # Page count is not stored in IR; use placeholder
    out.append("")

    # --------------- extensions ---------------
    ext = statement.accounts  # list[Account]
    srs_acct = next((a for a in ext if a.account_type == AccountType.SRS), None)

    # --- Account Summary ---
    out.append("---\n")
    out.append("## Account Summary\n")
    if sd:
        out.append(f"_as at {sd}_\n")

    if ext:
        # Split cash (CASA) rows from time (fixed) deposits so the balance
        # category is unambiguous to a third party. SRS accounts are reported
        # in their own section below, not in the CASA summary.
        cash_rows = [
            r for r in ext
            if r.account_type not in (AccountType.FIXED_DEPOSIT, AccountType.SRS)
        ]
        fd_rows = [r for r in ext if r.account_type == AccountType.FIXED_DEPOSIT]

        if cash_rows:
            out.append("### Current and Savings Accounts\n")
            out.append("| Account | Account No. | Currency | Balance |")
            out.append("|---------|-------------|----------|---------|")
            for row in cash_rows:
                bal_str = f"{row.closing_balance:,.2f}" if row.closing_balance is not None else "—"
                out.append(
                    f"| {row.name} | {mask_id(row.account_no, do_mask=do_mask)} | {row.currency} | {bal_str} |"
                )
            out.append("")
        if fd_rows:
            out.append("### Fixed Deposits (Time Deposits)\n")
            out.append("| Account | Account No. | Currency | Balance |")
            out.append("|---------|-------------|----------|---------|")
            for row in fd_rows:
                bal_str = f"{row.closing_balance:,.2f}" if row.closing_balance is not None else "—"
                out.append(
                    f"| {row.name} | {mask_id(row.account_no, do_mask=do_mask)} | {row.currency} | {bal_str} |"
                )
            out.append("")

    if srs_acct is not None:
        srs_extras = srs_acct.extras or {}
        srs_total = srs_extras.get("total", "")
        contribs = srs_extras.get("contributions", {}) or {}

        out.append("## Supplementary Retirement Scheme\n")
        out.append(f"**Total (SGD):** {srs_total or '—'}\n")
        out.append("| Account | Account No. | Cash Balance (SGD) |")
        out.append("|---------|-------------|---------------------|")
        bal_str = f"{srs_acct.closing_balance:,.2f}" if srs_acct.closing_balance is not None else "—"
        out.append(
            f"| SRS Account | {mask_id(srs_acct.account_no, do_mask=do_mask)} | {bal_str} |"
        )
        out.append("")

        if any(contribs.values()):
            out.append("### Contribution Details (Year 2026)\n")
            out.append("| Item | Amount (SGD) |")
            out.append("|------|--------------|")
            out.append(f"| Max Contribution Amount | {contribs.get('max', '—')} |")
            out.append(f"| Total Contribution Made to Date | {contribs.get('made', '—')} |")
            out.append(f"| Balance Contribution Limit | {contribs.get('remaining', '—')} |")
            out.append("")

        holdings = srs_acct.investment_holdings or []
        if holdings:
            out.append("### Unit Trusts\n")
            out.append("| Name | Free Qty | Total Cost (SGD) | Market Value (SGD) | Unrealised P/L (SGD) |")
            out.append("|------|----------|-------------------|---------------------|-----------------------|")
            ut_total_cost = 0.0
            ut_total_mval = 0.0
            ut_total_pl = 0.0
            for h in holdings:
                tc = float(h.cost.replace(",", "")) if h.cost else 0.0
                mv = float(h.valuation.replace(",", "")) if h.valuation else 0.0
                pl_val = float(h.unrealised_pl.replace(",", "")) if h.unrealised_pl else 0.0
                ut_total_cost += tc
                ut_total_mval += mv
                ut_total_pl += pl_val
                out.append(
                    f"| {h.name} | {h.units} | {h.cost} | {h.valuation} | {h.unrealised_pl} |"
                )
            out.append(
                f"| **Total** | | **{ut_total_cost:,.2f}** | "
                + f"**{ut_total_mval:,.2f}** | **{ut_total_pl:,.2f}** |"
            )
            out.append("")

    # --- Transaction Details ---
    out.append("## Transaction Details\n")

    for acct in statement.accounts:
        if acct.account_type == AccountType.FIXED_DEPOSIT:
            _render_dbs_fd_account(out, acct, do_mask)
        elif acct.transactions:
            _render_dbs_standard_account(out, acct, do_mask)

    _append_warnings(out, statement)

    out.append("---\n")
    out.append("_Auto-generated from the DBS/POSB Consolidated Statement PDF._\n")
    return "\n".join(out)


def _render_dbs_standard_account(out: list[str], acct: "Account", do_mask: bool = True) -> None:
    """Render one standard (CASA/SRS) DBS account's transaction table."""
    out.append(f"### {acct.name}\n")
    out.append(f"**Account No.:** {mask_id(acct.account_no, do_mask=do_mask)}  ")
    if acct.currency:
        out.append(f"**Currency:** {acct.currency}  ")

    txns = acct.transactions
    if txns and txns[0].balance_after is not None:
        ob = None
        for t in txns:
            if t.balance_after is not None:
                ob = t.balance_after - t.amount
        if ob is not None:
            out.append(f"**Opening Balance (B/F):** {ob:,.2f}  ")

    closing = txns[-1].balance_after if txns else None
    if closing is not None:
        out.append(f"**Closing Balance (C/F):** {closing:,.2f}")
    out.append("")

    if txns:
        out.append("| Date | Description | Withdrawal | Deposit | Balance |")
        out.append("|------|-------------|------------|---------|---------|")
        tot_w = tot_d = 0.0
        for t in txns:
            desc = md_masked_description(t.description, do_mask=do_mask)
            if t.amount < 0:
                w = abs(t.amount)
                d = 0.0
            else:
                w = 0.0
                d = t.amount
            tot_w += w
            tot_d += d
            bal_str = f"{t.balance_after:,.2f}" if t.balance_after is not None else "—"
            w_str = f"{w:,.2f}" if w else "—"
            d_str = f"{d:,.2f}" if d else "—"
            out.append(
                f"| {t.posted_date} | {desc} | {w_str} | {d_str} | {bal_str} |"
            )
        out.append(
            f"| | **Total Withdrawals/Deposits** | **{tot_w:,.2f}** | **{tot_d:,.2f}** | |"
        )
        out.append("")


def _render_dbs_fd_account(out: list[str], acct: "Account", do_mask: bool = True) -> None:
    """Render a DBS Fixed Deposit account's record table (ICBC-style columns)."""
    out.append(f"### {acct.name}\n")
    out.append(f"**Account No.:** {mask_id(acct.account_no, do_mask=do_mask)}  ")
    if acct.currency:
        out.append(f"**Currency:** {acct.currency}  ")
    out.append("")

    records = acct.fd_records or []
    if records:
        out.append(
            "| Deposit No. | Value Date | Maturity Date | Period | Principal | "
            + "Interest Rate | Interest Amount |"
        )
        out.append(
            "|------------|------------|---------------|--------|-----------|"
            + "-------------|---------------|"
        )
        for r in records:
            vd = r.value_date or "—"
            mat = r.maturity_date or "—"
            ia = r.interest_amount
            ia_str = f"{ia:,.2f}" if ia is not None else "—"
            pr = r.principal
            pr_str = f"{pr:,.2f}"
            out.append(
                f"| {mask_id(r.deposit_no, do_mask=do_mask)} | {vd} | {mat} | "
                + f"{format_fd_period(r.value_date, r.maturity_date)} | "
                + f"{pr_str} | {_fd_rate_display(r)} | {ia_str} |"
            )
        out.append("")

    txns = acct.transactions or []
    if txns:
        out.append("**Transactions**\n")
        out.append("| Date | Description | Withdrawal | Deposit | Balance |")
        out.append("|------|-------------|------------|---------|---------|")
        for t in txns:
            desc = md_masked_description(t.description, do_mask=do_mask)
            if t.amount < 0:
                w_amt, d_amt = abs(t.amount), 0.0
            else:
                w_amt, d_amt = 0.0, t.amount
            bal_str = f"{t.balance_after:,.2f}" if t.balance_after is not None else "—"
            w_str = f"{w_amt:,.2f}" if w_amt else "—"
            d_str = f"{d_amt:,.2f}" if d_amt else "—"
            out.append(
                f"| {t.posted_date or '—'} | {desc} | {w_str} | {d_str} | {bal_str} |"
            )
        out.append("")


# ============================================================================
# UOB Single-Account Transaction
# ============================================================================

def uob_txn_ir_to_markdown(statement: ParsedStatement, *, do_mask: bool = True) -> str:
    """Render a UOB single-account transaction statement from IR."""
    out: list[str] = []
    meta = statement.statement_meta

    out.append("# UOB Bank — Statement of Account\n")
    out.append(f"**Period:** {meta.period_from or ''} → {meta.period_to or ''}  ")
    currencies = ", ".join(sorted({a.currency for a in statement.accounts if a.currency}))
    if currencies:
        out.append(f"**Currency:** {currencies}  ")
    out.append("\n")
    out.append("---\n")

    acct = statement.accounts[0] if statement.accounts else None
    name = (acct.name if acct else "") or "Account"
    out.append(f"## {name}\n")
    out.append(f"**Account No.:** {mask_id(acct.account_no, do_mask=do_mask) if acct else ''}  ")

    opening = acct.opening_balance if acct else None
    out.append(f"**Opening Balance (B/F):** {f'{opening:,.2f}' if opening is not None else '—'}  ")

    closing = acct.closing_balance if acct else None
    out.append(f"**Closing Balance (C/F):** {f'{closing:,.2f}' if closing is not None else '—'}\n")

    out.append("| Date | Description | Withdrawals | Deposits | Balance |")
    out.append("|------|-------------|-------------|----------|---------|")
    tot_w = tot_d = 0.0
    txns = acct.transactions if acct else []
    for t in txns:
        desc = md_masked_description(t.description, do_mask=do_mask)
        if t.amount < 0:
            w = abs(t.amount)
            d = 0.0
        else:
            w = 0.0
            d = t.amount
        tot_w += w
        tot_d += d
        bal_str = f"{t.balance_after:,.2f}" if t.balance_after is not None else "—"
        w_str = f"{w:,.2f}" if w else "—"
        d_str = f"{d:,.2f}" if d else "—"
        out.append(
            f"| {t.posted_date} | {desc} | {w_str} | {d_str} | {bal_str} |"
        )
    out.append(
        f"| | **Total Withdrawals/Deposits** | **{tot_w:,.2f}** | **{tot_d:,.2f}** | |"
    )
    out.append("")
    _append_warnings(out, statement)

    out.append("---\n")
    out.append("_Auto-generated from the UOB bank statement PDF._\n")
    return "\n".join(out)


# ============================================================================
# UOB Portfolio Summary
# ============================================================================

def uob_portfolio_ir_to_markdown(statement: ParsedStatement, *, do_mask: bool = True) -> str:
    """Render a UOB portfolio summary from IR."""
    out: list[str] = []
    meta = statement.statement_meta
    ext = statement.accounts
    ut_acct = next((a for a in ext if a.account_type == AccountType.UNIT_TRUST), None)
    inv = ut_acct.investment_holdings or [] if ut_acct is not None else []

    out.append("# UOB Bank — Portfolio Summary\n")
    out.append(f"**Period:** {meta.period_from or ''} → {meta.period_to or ''}  ")
    currencies = ", ".join(sorted({a.currency for a in statement.accounts if a.currency}))
    if currencies:
        out.append(f"**Currency:** {currencies}  ")
    out.append("\n")
    out.append("---\n")

    # ---- Portfolio Overview (native-currency breakdown from deposits + investments) ----
    if ext:
        ccy_deposits: dict[str, float] = {}
        for row in ext:
            if row.account_type == AccountType.UNIT_TRUST:
                continue  # unit trust account carries no cash balance
            ccy = row.currency or "SGD"
            ccy_deposits[ccy] = ccy_deposits.get(ccy, 0.0) + (row.closing_balance or 0.0)

        out.append("## Portfolio Overview\n")
        out.append("| Item | Currency | Amount |")
        out.append("|------|----------|--------|")
        for ccy in sorted(ccy_deposits):
            out.append(f"| Deposits | {ccy} | {ccy_deposits[ccy]:,.2f} |")

        # Investments totals by currency
        inv_by_ccy: dict[str, float] = {}
        for i in inv:
            ccy = i.currency or "SGD"
            try:
                val = float(i.valuation.replace(",", ""))
            except (ValueError, AttributeError):
                val = 0.0
            inv_by_ccy[ccy] = inv_by_ccy.get(ccy, 0.0) + val
        for ccy in sorted(inv_by_ccy):
            out.append(f"| Investments | {ccy} | {inv_by_ccy[ccy]:,.2f} |")
        out.append("")

    # ---- Deposits (read from first-class Account objects) ----
    deposit_accounts = [a for a in ext if a.account_type != AccountType.UNIT_TRUST]
    locked_accounts = [a for a in deposit_accounts if (a.extras or {}).get("locked_amount")]
    if deposit_accounts:
        out.append("## Deposits\n")
        out.append(
            "| Account | Account No. | Currency | Credit Line | "
            + "Interest Earned YTD | Interest Charged YTD | Balance |"
        )
        out.append(
            "|---------|-------------|----------|-------------|"
            + "--------------------|----------------------|---------|"
        )
        for a in deposit_accounts:
            ex = a.extras or {}
            ie = ex.get("interest_earned") or "—"
            ic = ex.get("interest_charged") or "—"
            bal = f"{a.closing_balance:,.2f}" if a.closing_balance is not None else "—"
            cr_line = ex.get("credit_line") or "—"
            out.append(
                f"| {a.name} | {mask_id(a.account_no, do_mask=do_mask)} | "
                + f"{a.currency or '—'} | {cr_line} | {ie} | {ic} | {bal} |"
            )
        out.append("")

        # Locked amount sub-table
        if locked_accounts:
            out.append("**Locked Amount**\n")
            out.append("| Account No. | Currency | Locked Amount |")
            out.append("|-------------|----------|-------------------|")
            for a in locked_accounts:
                out.append(
                    f"| {mask_id(a.account_no, do_mask=do_mask)} | {a.currency or ''} "+
                    f"| {(a.extras or {}).get('locked_amount', '')} |"
                )
            out.append("")

    # ---- Investments ----
    if inv:
        out.append("## Investments\n")
        out.append(
            "| Fund | Units | Currency | Indicative Market Price | "
            + "Indicative Market Valuation |"
        )
        out.append(
            "|------|-------|----------|-------------------------|"
            + "----------------------------|"
        )
        for i in inv:
            out.append(
                f"| {i.name} | {i.units} | {i.currency} | {i.unit_price} | {i.valuation} |"
            )
        out.append("")

    _append_warnings(out, statement)

    out.append("---\n")
    out.append("_Auto-generated from the UOB portfolio statement PDF._\n")
    return "\n".join(out)


# ============================================================================
# UOB One Multi-Account
# ============================================================================

def uob_one_ir_to_markdown(statement: ParsedStatement, *, do_mask: bool = True) -> str:
    """Render a UOB One multi-account statement from IR."""
    out: list[str] = []
    meta = statement.statement_meta
    ext = statement.accounts
    srs = statement.extras or {}
    deposit_totals = srs.get("deposit_totals", [])  # [(ccy, amount)]

    out.append("# UOB Bank — Statement of Account (UOB One)\n")
    out.append(f"**Period:** {meta.period_from or ''} → {meta.period_to or ''}  ")
    currencies = ", ".join(sorted({a.currency for a in statement.accounts if a.currency}))
    if currencies:
        out.append(f"**Currency:** {currencies}  ")
    out.append("\n")
    out.append("---\n")

    # ---- Account Overview (native-currency breakdown) ----
    if ext:
        # Group by currency
        ccy_balances: dict[str, float] = {}
        for row in ext:
            ccy = row.currency or "SGD"
            ccy_balances[ccy] = ccy_balances.get(ccy, 0.0) + (row.closing_balance or 0.0)

        out.append("## Account Overview\n")
        out.append("| Currency | Balance |")
        out.append("|----------|---------|")
        for ccy in sorted(ccy_balances):
            out.append(f"| {ccy} | {ccy_balances[ccy]:,.2f} |")
        out.append("")

    # ---- Deposits (read from first-class Account objects) ----
    deposit_accounts = [a for a in ext if a.account_type != AccountType.UNIT_TRUST]
    locked_accounts = [a for a in deposit_accounts if (a.extras or {}).get("locked_amount")]
    if deposit_accounts:
        out.append("## Deposits\n")
        out.append(
            "| Account | Account No. | Currency | Credit Line | "+
            "Interest Earned | Interest Charged | Balance |"
        )
        out.append(
            "|---------|-------------|----------|-------------|"
            + "--------------------|----------------------|---------|"
        )
        for a in deposit_accounts:
            ex = a.extras or {}
            ie = ex.get("interest_earned") or "—"
            ic = ex.get("interest_charged") or "—"
            bal = f"{a.closing_balance:,.2f}" if a.closing_balance is not None else "—"
            cr_line = ex.get("credit_line") or "—"
            out.append(
                f"| {a.name} | {mask_id(a.account_no, do_mask=do_mask)} | "
                + f"{a.currency or '—'} | {cr_line} | {ie} | {ic} | {bal} |"
            )

        # Per-currency subtotals
        for ccy, amt in deposit_totals:
            out.append(f"| **Total ({ccy})** | | | | | | **{amt}** |")

        out.append("")

        # Locked amount sub-table
        if locked_accounts:
            out.append("**Locked Amount**\n")
            out.append("| Account No. | Currency | Locked Amount |")
            out.append("|-------------|----------|-------------------|")
            for a in locked_accounts:
                out.append(
                    f"| {mask_id(a.account_no, do_mask=do_mask)} | {a.currency or ''} "+
                    f"| {(a.extras or {}).get('locked_amount', '')} |"
                )
            out.append("")

    # ---- Per-account transaction sections (skip empty) ----
    for acct in statement.accounts:
        txns = acct.transactions
        if not txns:
            continue  # skip empty sections

        ccy = acct.currency or ""
        heading = f"## {acct.name} ({ccy})" if ccy else f"## {acct.name}"
        out.append(f"{heading}\n")
        out.append(f"**Account No.:** {mask_id(acct.account_no, do_mask=do_mask)}  ")

        if txns and txns[0].balance_after is not None:
            ob = txns[0].balance_after - txns[0].amount
            out.append(f"**Opening Balance (B/F):** {ob:,.2f}  ")

        closing = txns[-1].balance_after if txns else None
        if closing is not None:
            out.append(f"**Closing Balance (C/F):** {closing:,.2f}\n")

        out.append("| Date | Description | Withdrawals | Deposits | Balance |")
        out.append("|------|-------------|-------------|----------|---------|")
        tot_w = tot_d = 0.0
        for t in txns:
            desc = md_masked_description(t.description, do_mask=do_mask)
            if t.amount < 0:
                w = abs(t.amount)
                d = 0.0
            else:
                w = 0.0
                d = t.amount
            tot_w += w
            tot_d += d
            bal_str = f"{t.balance_after:,.2f}" if t.balance_after is not None else "—"
            w_str = f"{w:,.2f}" if w else "—"
            d_str = f"{d:,.2f}" if d else "—"
            out.append(
                f"| {t.posted_date} | {desc} | {w_str} | {d_str} | {bal_str} |"
            )
        out.append(
            f"| | **Total Withdrawals/Deposits** | **{tot_w:,.2f}** | **{tot_d:,.2f}** | |"
        )
        out.append("")

    _append_warnings(out, statement)

    out.append("---\n")
    out.append("_Auto-generated from the UOB One bank statement PDF._\n")
    return "\n".join(out)


# ============================================================================
# OCBC Consolidated Statement
# ============================================================================

def ocbc_consolidated_ir_to_markdown(statement: ParsedStatement, *, do_mask: bool = True) -> str:
    """Render an OCBC consolidated statement from IR."""
    meta = statement.statement_meta

    out: list[str] = []
    out.append("# OCBC Bank — Statement of Account\n")
    sd = meta.period_to or meta.period_from or ""
    out.append(f"**Statement Date:** {sd}  ")
    currencies = ", ".join(sorted({a.currency for a in statement.accounts if a.currency}))
    if currencies:
        out.append(f"**Currency:** {currencies}  ")
    out.append("\n")
    out.append("---\n")

    # One section per nested account
    for acct in statement.accounts:
        if acct.account_type == AccountType.FIXED_DEPOSIT:
            _render_ocbc_fd_account(out, acct, do_mask)
            continue

        txns = acct.transactions
        ccy = acct.currency or ""
        name = acct.name or "Account"
        heading = f"## {name} ({ccy})" if ccy else f"## {name}"
        out.append(f"{heading}\n")
        out.append(f"**Account No.:** {mask_id(acct.account_no, do_mask=do_mask)}  ")

        if txns and txns[0].balance_after is not None:
            ob = txns[0].balance_after - txns[0].amount
            out.append(f"**Opening Balance (B/F):** {ob:,.2f}  ")

        closing = txns[-1].balance_after if txns else None
        if closing is not None:
            out.append(f"**Closing Balance (C/F):** {closing:,.2f}\n")

        out.append("| Txn Date | Value Date | Description | Cheque | Withdrawal | Deposit | Balance |")
        out.append("|----------|------------|-------------|--------|------------|---------|---------|")
        tot_w = tot_d = 0.0
        for t in txns:
            desc = md_masked_description(t.description, do_mask=do_mask)
            if t.amount < 0:
                w = abs(t.amount)
                d = 0.0
            else:
                w = 0.0
                d = t.amount
            tot_w += w
            tot_d += d
            bal_str = f"{t.balance_after:,.2f}" if t.balance_after is not None else "—"
            w_str = f"{w:,.2f}" if w else "—"
            d_str = f"{d:,.2f}" if d else "—"
            vd = t.value_date or ""
            out.append(
                f"| {t.posted_date} | {vd} | {desc} | — | "
                + f"{w_str} | {d_str} | {bal_str} |"
            )
        out.append(
            f"| | | **Total Withdrawals/Deposits** | | **{tot_w:,.2f}** | **{tot_d:,.2f}** | |"
        )
        out.append("")

    _append_warnings(out, statement)

    out.append("---\n")
    out.append("_Auto-generated from the OCBC Consolidated Statement PDF._\n")
    return "\n".join(out)


def _render_ocbc_fd_account(out: list[str], acct: "Account", do_mask: bool = True) -> None:
    """Render an OCBC Fixed Deposit account's record table."""
    ccy = acct.currency or ""
    heading = f"## {acct.name} ({ccy})" if ccy else f"## {acct.name}"
    out.append(f"{heading}\n")
    out.append(f"**Account No.:** {mask_id(acct.account_no, do_mask=do_mask)}  ")
    out.append("")

    records = acct.fd_records or []
    if records:
        out.append("| Value Date | Deposit No. | Interest Rate p.a. | Maturity Date | Principal |")
        out.append("|------------|-------------|---------------------|---------------|-----------|")
        for r in records:
            vd = r.value_date or "—"
            rate = _fd_rate_display(r)
            mat = r.maturity_date or "—"
            pr = r.principal
            pr_str = f"{pr:,.2f}"
            out.append(
                f"| {vd} | {mask_id(r.deposit_no, do_mask=do_mask)} | "
                + f"{rate} | {mat} | {pr_str} |"
            )
        out.append("")


# ============================================================================
# OCBC Credit Card
# ============================================================================

def ocbc_card_ir_to_markdown(statement: ParsedStatement, *, do_mask: bool = True) -> str:
    """Render an OCBC credit card statement from IR."""
    meta = statement.statement_meta
    srs = statement.extras or {}
    ccs = statement.credit_card_summary
    assert ccs is not None, "Credit card summary must be populated for OCBC card statements"

    card_acct = statement.accounts[0] if statement.accounts else None
    all_txns = [t for a in statement.accounts for t in a.transactions]
    total_charges = sum(t.amount for t in all_txns if t.amount > 0)
    total_credits = sum(t.amount for t in all_txns if t.amount < 0)

    out: list[str] = []
    card_name = meta.account_holder or "Credit Card"
    out.append(f"# {card_name} — Statement of Account\n")
    out.append(f"**Card:** {card_name}  ")
    out.append(f"**Card Number:** {mask_id(card_acct.account_no, do_mask=do_mask) if card_acct else ''}\n")
    out.append("## Statement Summary\n")
    out.append("| Field | Value |")
    out.append("|---|---|")

    sd = meta.period_to or meta.period_from or ""
    out.append(f"| Statement Date | {sd} |")

    out.append(f"| Payment Due Date | {ccs.payment_due_date or '—'} |")
    out.append(f"| Total Credit Limit | S${ccs.credit_limit or '—'} |")
    out.append(f"| Total Available Credit Limit | S${ccs.available_credit or '—'} |")
    out.append(f"| Total Minimum Due | S${ccs.minimum_due or '—'} |")
    out.append(f"| Last Month's Balance | S${ccs.previous_balance or '—'} |")
    out.append(f"| Subtotal | S${srs.get('subtotal', '—')} |")
    out.append(f"| Total | S${srs.get('total', '—')} |")
    out.append(f"| **Total Amount Due** | **S${ccs.total_amount_due or '—'}** |")
    out.append("")

    out.append("## Transactions\n")
    out.append(f"**Total transactions:** {len(all_txns)}  ")
    out.append(f"**Total charges:** S${total_charges:,.2f}  ")
    out.append(f"**Total credits (payments/rebates):** S${abs(total_credits):,.2f}\n")

    # ---- Reconciliation (three-tier fallback) ----
    out.append("### Reconciliation\n")
    out.append("| Item | Amount (SGD) |")
    out.append("|---|---|")

    previous_balance_raw = ccs.previous_balance
    total_amount_due_raw = ccs.total_amount_due

    if previous_balance_raw:
        # Tier 1 — full reconciliation with cross-validation
        previous_balance = float(previous_balance_raw.replace(",", ""))
        reconciled = previous_balance + total_charges + total_credits
        out.append(f"| Previous Balance | {previous_balance:,.2f} |")
        out.append(f"| Total Charges | {total_charges:,.2f} |")
        out.append(f"| Total Credits | {total_credits:,.2f} |")
        out.append(f"| **Total Amount Due** | **{reconciled:,.2f}** |")
        if total_amount_due_raw:
            pdf_value = float(total_amount_due_raw.replace(",", ""))
            if abs(reconciled - pdf_value) > 0.01:
                out.append(f"_(Note: computed {reconciled:,.2f} differs from stated S${total_amount_due_raw})_")
    elif total_amount_due_raw:
        # Tier 2 — cannot reconcile, show PDF stated value directly
        out.append(f"| Previous Balance | — |")
        out.append(f"| Total Charges | {total_charges:,.2f} |")
        out.append(f"| Total Credits | {total_credits:,.2f} |")
        out.append(f"| **Total Amount Due (as stated)** | **S${total_amount_due_raw}** |")
    else:
        # Tier 3 — compute from current period only, clearly flagged
        computed = total_charges + total_credits
        out.append(f"| Previous Balance | — |")
        out.append(f"| Total Charges | {total_charges:,.2f} |")
        out.append(f"| Total Credits | {total_credits:,.2f} |")
        out.append(f"| **Total Amount Due (computed)** | **{computed:,.2f}** |")
        out.append("_(Note: previous balance unavailable, computed from current period only)_")
    out.append("")

    out.append("| Date | Description | Currency / Amount | Amount (SGD) |")
    out.append("|------|-------------|-------------------|--------------|")
    if previous_balance_raw:
        pb = float(previous_balance_raw.replace(",", ""))
        out.append(f"| | PREVIOUS BALANCE | | {pb:,.2f} |")
    s_fc = statement.statement_meta.functional_currency
    for t in all_txns:
        if s_fc and t.currency != s_fc:
            fc = f"{t.currency} {abs(t.amount):,.2f}"
        else:
            fc = ""
        desc = md_masked_description(t.description, do_mask=do_mask)
        amount_display = f"{t.amount:,.2f}"
        out.append(
            f"| {t.posted_date} | {desc} | {fc} | {amount_display} |"
        )
    out.append(f"| | **TOTAL AMOUNT DUE** | | **{ccs.total_amount_due or '—'}** |")
    out.append("")
    _append_warnings(out, statement)

    out.append("---\n")
    out.append("_Auto-generated from the OCBC credit card statement PDF._\n")
    return "\n".join(out)


# ============================================================================
# ICBC Bank Statement
# ============================================================================

def icbc_ir_to_markdown(statement: ParsedStatement, *, do_mask: bool = True) -> str:
    """Render an ICBC bilingual statement from IR."""
    out: list[str] = []
    meta = statement.statement_meta
    ext = statement.accounts
    extras = statement.extras or {}
    reminders: list[str] = extras.get("reminders") or []  # type: ignore[assignment]
    notes: list[str] = extras.get("notes") or []          # type: ignore[assignment]

    out.append("# ICBC — Statement of Account\n")
    sd = meta.period_to or meta.period_from or ""
    out.append(f"**Statement Date:** {sd}  ")
    out.append("\n")
    out.append("---\n")

    # Split nested accounts into Current vs Fixed Deposit by account_type.
    ca_accounts = [a for a in ext if a.account_type != AccountType.FIXED_DEPOSIT]
    fd_accounts = [a for a in ext if a.account_type == AccountType.FIXED_DEPOSIT]

    out.append("## Account Summary\n")
    if ca_accounts:
        out.append("### Current Account\n")
        out.append("| A/c Type | Account No. | CCY | Balance |")
        out.append("|----------|-------------|-----|---------|")
        for a in ca_accounts:
            bal_str = f"{a.closing_balance:,.2f}" if a.closing_balance is not None else "—"
            out.append(
                f"| {a.name} | {mask_id(a.account_no, do_mask=do_mask)} | {a.currency} | {bal_str} |"
            )
        out.append("")

    if fd_accounts:
        out.append("### Fixed Deposit Account\n")
        out.append("| Account No. | CCY | Balance | Status |")
        out.append("|-------------|-----|---------|--------|")
        for a in fd_accounts:
            bal_str = f"{a.closing_balance:,.2f}" if a.closing_balance is not None else "—"
            out.append(f"| {mask_id(a.account_no, do_mask=do_mask)} | {a.currency} | {bal_str} | Active |")
        out.append("")

    # Group current account transactions by currency
    out.append("## Current Account Transactions\n")
    ca_txns_all = [t for a in ca_accounts for t in a.transactions]
    if ca_txns_all and ca_accounts:
        out.append(f"**Account No.:** {mask_id(ca_accounts[0].account_no, do_mask=do_mask)}\n")

    # Group by currency
    ccy_groups: dict[str, list[Transaction]] = {}
    for t in ca_txns_all:
        ccy = t.currency or "SGD"
        ccy_groups.setdefault(ccy, []).append(t)

    for ccy, txns in ccy_groups.items():
        out.append(f"### {ccy}\n")
        out.append("| Date | Remark | Deposit | Withdrawal | Balance |")
        out.append("|------|--------|---------|------------|---------|")
        tot_w = tot_d = 0.0
        for t in txns:
            # Remark column keeps the original PDF remark verbatim — the linked
            # fixed-deposit reference is intentionally NOT rendered here.
            desc = md_masked_description(t.description, do_mask=do_mask)
            if t.amount > 0:
                d = t.amount
                w = 0.0
            else:
                d = 0.0
                w = abs(t.amount)
            tot_w += w
            tot_d += d
            bal_str = f"{t.balance_after:,.2f}" if t.balance_after is not None else "—"
            d_str = f"{d:,.2f}" if d else "—"
            w_str = f"{w:,.2f}" if w else "—"
            out.append(
                f"| {t.posted_date} | {desc} | {d_str} | {w_str} | {bal_str} |"
            )
        out.append(
            f"| | **Total Dr./Cr.** | **{tot_d:,.2f}** | **{tot_w:,.2f}** | |"
        )
        out.append("")

    # Fixed Deposit accounts — render each account's records and transactions.
    if fd_accounts:
        for a in fd_accounts:
            fd_records = a.fd_records or []
            has_txn = bool(a.transactions)
            if not fd_records and not has_txn:
                continue
            out.append("## Fixed Deposit Account\n")
            out.append(f"**Account No.:** {mask_id(a.account_no, do_mask=do_mask)}\n")
            if fd_records:
                out.append(
                    "| Deposit No. | Value Date | Maturity Date | Period | CCY | Principal | "
                    + "Interest Rate | Interest Amount |"
                )
                out.append(
                    "|------------|------------|---------------|--------|-----|-----------|"
                    + "-------------|---------------|"
                )
                for r in fd_records:
                    vd = r.value_date or "—"
                    mat = r.maturity_date or "—"
                    ia = r.interest_amount
                    ia_str = f"{ia:,.2f}" if ia is not None else "—"
                    pr = r.principal
                    pr_str = f"{pr:,.2f}"
                    out.append(
                        f"| {mask_id(r.deposit_no, do_mask=do_mask)} | {vd} | {mat} | "
                        + f"{format_fd_period(r.value_date, r.maturity_date)} | "
                        + f"{r.currency} | {pr_str} | {_fd_rate_display(r)} | {ia_str} |"
                    )
                out.append("")
            if has_txn:
                out.append("**Transactions**\n")
                out.append("| Date | Remark | Deposit | Withdrawal | Balance |")
                out.append("|------|--------|---------|------------|---------|")
                for t in a.transactions:
                    # Original PDF remark verbatim — FD reference intentionally
                    # not rendered.
                    desc = md_masked_description(t.description, do_mask=do_mask)
                    if t.amount > 0:
                        d = t.amount
                        w = 0.0
                    else:
                        d = 0.0
                        w = abs(t.amount)
                    bal_str = f"{t.balance_after:,.2f}" if t.balance_after is not None else "—"
                    d_str = f"{d:,.2f}" if d else "—"
                    w_str = f"{w:,.2f}" if w else "—"
                    out.append(
                        f"| {t.posted_date} | {desc} | {d_str} | {w_str} | {bal_str} |"
                    )
                out.append("")

    # Reminders
    if reminders:
        out.append("## Reminders\n")
        combined = " ".join(reminders)
        combined = re.sub(r"\d{10,}", lambda m: mask_id(m.group(), do_mask=do_mask), combined)
        out.append(combined)
        out.append("")

    # Notes
    if notes:
        out.append("## Notes\n")
        dep_ins_lines: list[str] = []
        note_lines: list[str] = []
        in_dep_ins = False
        for note in notes:
            if note.strip() == "Deposit Insurance Scheme":
                in_dep_ins = True
                continue
            if in_dep_ins:
                dep_ins_lines.append(note)
            else:
                note_lines.append(note)

        for nl in note_lines:
            out.append(nl)
            out.append("")
        if dep_ins_lines:
            out.append("### Deposit Insurance Scheme\n")
            for il in dep_ins_lines:
                out.append(il)
                out.append("")

    _append_warnings(out, statement)

    out.append("---\n")
    out.append("_Auto-generated from the ICBC statement PDF._\n")
    return "\n".join(out)


# ============================================================================
# Renderer registry
# ============================================================================

MD_RENDERER_REGISTRY: dict[tuple[str, str], Callable[..., str]] = {
    ("dbs", "consolidated"): dbs_ir_to_markdown,
    ("uob", "txn"): uob_txn_ir_to_markdown,
    ("uob", "one"): uob_one_ir_to_markdown,
    ("uob", "portfolio"): uob_portfolio_ir_to_markdown,
    ("ocbc", "consolidated"): ocbc_consolidated_ir_to_markdown,
    ("ocbc", "card"): ocbc_card_ir_to_markdown,
    ("icbc", "consolidated"): icbc_ir_to_markdown,
}
