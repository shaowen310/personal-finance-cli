"""UOB statement → IR extractors (3 flavors: txn, one, portfolio).

Wraps ``uob_parser.parse_uob_txn()``, ``parse_uob_one()``, and
``parse_uob_portfolio()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast, override

from pfa_ir_schema import AccountType, InvestmentHolding, ParsedStatement
from .base import BaseExtractor
from ..parsers.uob_parser import MONTH_MAP


def _uob_date_to_iso(date_str: str, year_hint: str = "") -> str:
    """Convert UOB "02 Jun" → ISO "2026-06-02" using year_hint if provided."""
    parts = date_str.split()
    if len(parts) < 2:
        return date_str
    day = parts[0]
    mon = MONTH_MAP.get(parts[1].lower(), 1)
    year = year_hint or "2026"
    return f"{year}-{mon:02d}-{int(day):02d}"


def _uob_account_type(name: str) -> str:
    """Map a UOB deposit product name to its canonical ``account_type``.

    Product decision: the UOB Stash Account is treated as a current
    (transaction) account. Other UOB deposit products fall back to "current"
    for backwards compatibility; extend this mapping as more products are
    distinguished.
    """
    lowered = (name or "").lower()
    if "stash" in lowered:
        return AccountType.CURRENT.value
    return AccountType.CURRENT.value


def _extract_year(meta: dict[str, Any]) -> str:
    """Try to extract a year from period_start or period_end."""
    for key in ("period_end", "period_start"):
        val = str(meta.get(key, ""))
        parts = val.split()
        if len(parts) == 3 and parts[2].isdigit():
            return parts[2]
    return ""


# ============================================================================
# UOB Single-Account Transaction
# ============================================================================

class UOBTxnExtractor(BaseExtractor):
    parser_name: ClassVar[str] = "uob_txn"
    parser_version: ClassVar[str] = "1.0"

    @classmethod
    @override
    def supports(cls, pdf_path: Path) -> bool:
        return True

    @classmethod
    @override
    def bank_name(cls) -> str:
        return "UOB"

    @override
    def to_ir(self, pdf_path: Path) -> ParsedStatement:
        from ..parsers.uob_parser import parse_uob_txn

        pdf = self._open_pdf(pdf_path)
        try:
            meta, transactions, opening_balance = parse_uob_txn(pdf)
        finally:
            pdf.close()

        builder = self._create_builder()
        _ = builder.set_source(str(pdf_path))

        base_ccy = str(meta.get("currency", "SGD"))
        year = _extract_year(meta)

        opening = _parse_amount(opening_balance)
        closing = _parse_amount(transactions[-1].get("balance", "")) if transactions else None

        _ = builder.set_meta(
            institution="UOB_SG",
        )

        _ = builder.add_account(
            name=str(meta.get("account_name", "")) or "Account",
            account_no=str(meta.get("account_no", "")),
            account_type=AccountType.CURRENT.value,
            currency=base_ccy,
            opening_balance=opening,
            closing_balance=closing,
        )

        period_from = _uob_date_to_iso(str(meta.get("period_start", "")), year)
        period_to = _uob_date_to_iso(str(meta.get("period_end", "")), year)
        _ = builder.set_period(period_from, period_to)

        for t in transactions:
            withdrawal = _parse_amount(t.get("withdrawal", ""))
            deposit = _parse_amount(t.get("deposit", ""))
            amount = (deposit or 0.0) - (withdrawal or 0.0)
            balance = _parse_amount(t.get("balance", ""))

            _ = builder.add_transaction(
                posted_date=str(t.get("txn_date", "")),
                amount=amount,
                currency=base_ccy,
                description=str(t.get("description", "")),
                balance_after=balance,
            )

        return builder.build()


# ============================================================================
# UOB One Multi-Account
# ============================================================================

class UOBOneExtractor(BaseExtractor):
    parser_name: ClassVar[str] = "uob_one"
    parser_version: ClassVar[str] = "1.0"

    @classmethod
    @override
    def supports(cls, pdf_path: Path) -> bool:
        return True

    @classmethod
    @override
    def bank_name(cls) -> str:
        return "UOB"

    @override
    def to_ir(self, pdf_path: Path) -> ParsedStatement:
        from ..parsers.uob_parser import parse_uob_one

        pdf = self._open_pdf(pdf_path)
        try:
            meta, accounts, summary = parse_uob_one(pdf)
        finally:
            pdf.close()

        builder = self._create_builder()
        _ = builder.set_source(str(pdf_path))

        base_ccy = str(meta.get("currency", "SGD"))
        year = _extract_year(meta)

        _ = builder.set_meta(
            institution="UOB_SG",
        )

        period_from = _uob_date_to_iso(str(meta.get("period_start", "")), year)
        period_to = _uob_date_to_iso(str(meta.get("period_end", "")), year)
        _ = builder.set_period(period_from, period_to)

        # Map account numbers to names from the Deposits summary table
        # (the section parser only extracts account_no, not account_name).
        deposits = summary.get("deposits", [])
        acct_no_to_name: dict[str, str] = {}
        deposit_by_no: dict[str, dict[str, Any]] = {}
        for d in deposits:
            acct_no = str(d.get("account_no", ""))
            name = str(d.get("name", ""))
            if acct_no:
                if name:
                    acct_no_to_name[acct_no] = name
                deposit_by_no[acct_no] = d

        for acct in accounts:
            acc_no = str(acct.get("account_no", ""))
            acc_name = acct_no_to_name.get(acc_no, "") or str(acct.get("account_name", ""))
            if not acc_name:
                acc_name = f"Account ({str(acct.get('currency', base_ccy))})"
            dep = deposit_by_no.get(acc_no, {})
            _ = builder.add_account(
                name=acc_name,
                account_no=acc_no,
                account_type=_uob_account_type(acc_name),
                currency=str(acct.get("currency", base_ccy)),
                closing_balance=_parse_amount(dep.get("balance")),
                extras={"credit_line": dep.get("credit_line"),
                        "interest_earned": dep.get("interest_earned"),
                        "interest_charged": dep.get("interest_charged"),
                        "locked_amount": dep.get("locked_amount")},
            )
            for t in acct.get("transactions", []):
                withdrawal = _parse_amount(t.get("withdrawal", ""))
                deposit = _parse_amount(t.get("deposit", ""))
                amount = (deposit or 0.0) - (withdrawal or 0.0)
                balance = _parse_amount(t.get("balance", ""))

                _ = builder.add_transaction(
                    posted_date=str(t.get("txn_date", "")),
                    amount=amount,
                    currency=str(acct.get("currency", base_ccy)),
                    description=str(t.get("description", "")),
                    balance_after=balance,
                )

        # Store UOB-specific aggregate fields in extras. Per-account deposit
        # details (credit_line, interest_*, locked_amount, balance) now live on
        # the Account objects themselves, so no duplicate "deposits" list here.
        _ = builder.set_extras({
            "deposit_totals": summary.get("deposit_totals", []),  # [(ccy, amount)]
            "deposits_total": summary.get("deposits_total", ""),
        })

        return builder.build()


# ============================================================================
# UOB Portfolio Summary
# ============================================================================

class UOBPortfolioExtractor(BaseExtractor):
    parser_name: ClassVar[str] = "uob_portfolio"
    parser_version: ClassVar[str] = "1.0"

    @classmethod
    @override
    def supports(cls, pdf_path: Path) -> bool:
        return True

    @classmethod
    @override
    def bank_name(cls) -> str:
        return "UOB"

    @override
    def to_ir(self, pdf_path: Path) -> ParsedStatement:
        from ..parsers.uob_parser import parse_uob_portfolio

        pdf = self._open_pdf(pdf_path)
        try:
            data = parse_uob_portfolio(pdf)
        finally:
            pdf.close()

        meta = cast(dict[str, Any], data.get("meta", data))
        year = _extract_year(meta)

        builder = self._create_builder()
        _ = builder.set_source(str(pdf_path))

        _ = builder.set_meta(
            institution="UOB_SG",
        )

        period_from = _uob_date_to_iso(str(meta.get("period_start", "")), year)
        period_to = _uob_date_to_iso(str(meta.get("period_end", "")), year)
        _ = builder.set_period(period_from, period_to)

        # ---- Deposits become first-class Accounts (balances nested) ----
        deposits_list = cast(list[dict[str, Any]], data.get("deposits", []))
        for d in deposits_list:
            _ = builder.add_account(
                name=str(d.get("name", "")),
                account_no=str(d.get("account_no", "")),
                account_type=_uob_account_type(str(d.get("name", ""))),
                currency=str(d.get("currency", "")),
                closing_balance=_parse_amount(d.get("balance")),
                extras={
                    "credit_line": d.get("credit_line"),
                    "interest_earned": d.get("interest_earned"),
                    "interest_charged": d.get("interest_charged"),
                    "locked_amount": d.get("locked_amount"),
                },
            )

        # ---- Unit Trust account (owns the investment holdings) ----
        # A UOB portfolio is guaranteed to contain a unit trust account, so the
        # Investments section header always yields one. Its holdings become a
        # first-class Account of type UNIT_TRUST rather than a top-level list.
        ut = cast(dict[str, Any], data.get("unit_trust", {}))
        investments = cast(list[dict[str, Any]], data.get("investments", []))
        holdings = [
            InvestmentHolding(
                name=str(inv.get("name", "")),
                units=str(inv.get("units", "")),
                currency=str(inv.get("currency", "")),
                unit_price=str(inv.get("price", "")),
                valuation=str(inv.get("valuation", "")),
            )
            for inv in investments
        ]
        _ = builder.add_account(
            name=str(ut.get("name", "Unit Trust")),
            account_no=str(ut.get("account_no", "")),
            account_type=AccountType.UNIT_TRUST.value,
            currency=str(meta.get("currency", "SGD")),
            investment_holdings=holdings,
        )

        return builder.build()


# ---- helpers ----

def _parse_amount(val: str | float | None) -> float | None:
    """Parse a numeric string (possibly with commas) to float."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    val = str(val).strip().replace(",", "")
    if val == "" or val == "—":
        return None
    try:
        return float(val)
    except ValueError:
        return None
