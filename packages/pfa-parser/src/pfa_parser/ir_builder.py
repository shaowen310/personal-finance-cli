"""IRBuilder — chainable API for constructing a ParsedStatement.

Usage inside an Extractor::

    builder = IRBuilder("dbs_sg", "1.0")
    builder.set_source(str(pdf_path))
    builder.set_meta(
        institution="DBS",
        account_holder="JOHN DOE",
    )
    builder.set_period("2026-06-01", "2026-06-30")

    builder.add_account(
        name="DBS Savings Plus",
        account_no="XXX-XXX-XXX-X",
        account_type="current",
        currency="SGD",
        opening_balance=12345.67,
        closing_balance=12600.00,
    )
    for row in raw_transactions:
        builder.add_transaction(
            posted_date=row["date"],
            amount=-50.00,
            currency="SGD",
            description=row["desc"],
            balance_after=12600.00,
        )

    statement = builder.build()
    json_str = builder.to_json()
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from pfa_ir_schema import (
    Account,
    AccountType,
    CreditCardSummary,
    DebugInfo,
    FixedDepositRecord,
    InvestmentHolding,
    ParsedStatement,
    ParserInfo,
    StatementMeta,
    Transaction,
    generate_txn_id,
    parse_fd_rate,
    to_json as ir_to_json,
)


def _fd_records_complementary(existing: "FixedDepositRecord", new_principal: float,
                              new_interest_amount: float | None) -> bool:
    """Return True if ``new`` carries the field ``existing`` is missing.

    This prevents two genuinely distinct deposits that merely share the same
    deposit_no/value_date/maturity_date from being collapsed into one, while
    still merging a principal line with its matching interest line.
    """
    existing_principal = bool(existing.principal)
    existing_interest = bool(existing.interest_amount)
    new_principal_b = bool(new_principal)
    new_interest_b = bool(new_interest_amount)
    if existing_principal and new_interest_b and not existing_interest:
        return True
    if existing_interest and new_principal_b and not existing_principal:
        return True
    return False


class IRBuilder:
    """Builds a ``ParsedStatement`` incrementally through chainable methods."""

    def __init__(self, parser_name: str, parser_version: str) -> None:
        self._parser: ParserInfo = ParserInfo(name=parser_name, version=parser_version)
        self._meta: StatementMeta = StatementMeta()
        self._source_file: str = ""
        self._accounts: list[Account] = []
        self._active_account: Account | None = None
        self._warnings: list[str] = []
        self._period_from: str | None = None
        self._period_to: str | None = None
        self._functional_currency: str = ""

        # Phase 3 extension accumulators
        self._investment_holdings: list[InvestmentHolding] = []
        self._reconciliation: dict[str, Any] | None = None
        self._extras: dict[str, Any] | None = None
        self._credit_card_summary: CreditCardSummary | None = None

    # -- Chainable setters ---------------------------------------------------

    def set_source(self, path: str) -> "IRBuilder":
        """Record the source PDF filename (directory stripped)."""
        self._source_file = os.path.basename(path)
        return self

    def set_meta(
        self,
        *,
        institution: str = "",
        account_holder: str | None = None,
        functional_currency: str = "",
    ) -> "IRBuilder":
        """Set statement-level metadata (institution, holder, functional currency)."""
        self._meta.institution = institution
        self._meta.account_holder = account_holder
        if functional_currency:
            self._functional_currency = functional_currency
        return self

    def add_account(
        self,
        *,
        name: str = "",
        account_no: str = "",
        account_type: str = "unknown",
        currency: str = "",
        account_holder: str | None = None,
        opening_balance: float | None = None,
        closing_balance: float | None = None,
        extras: dict[str, Any] | None = None,
        investment_holdings: list[InvestmentHolding] | None = None,
    ) -> "IRBuilder":
        """Append an ``Account`` and make it the active account for transactions.

        Subsequent ``add_transaction`` / ``add_transaction_dict`` calls route
        into this account until the next ``add_account`` call.

        ``investment_holdings`` attaches a per-account list of
        ``InvestmentHolding`` (e.g. SRS unit trusts, or the UOB portfolio's
        UNIT_TRUST fund holdings). An account owns its holdings rather than the
        statement — the top-level ``statement.investment_holdings`` list is no
        longer populated by the UOB portfolio extractor.
        """
        acct = Account(
            name=name,
            account_no=account_no,
            account_type=AccountType.normalize(account_type).value,
            currency=currency,
            account_holder=account_holder,
            opening_balance=opening_balance,
            closing_balance=closing_balance,
            extras=extras or None,
            investment_holdings=investment_holdings or None,
        )
        self._accounts.append(acct)
        self._active_account = acct
        # Set functional currency once from the first account; subsequent
        # add_account calls (e.g. DBS multi-currency sub-accounts) must not
        # overwrite it. The functional currency is the statement's reporting
        # currency (always SGD for SG banks), not the account's denomination.
        if not self._functional_currency and currency:
            self._functional_currency = currency
        return self

    def set_period(self, period_from: str, period_to: str) -> "IRBuilder":
        """Set statement date range (ISO 8601 YYYY-MM-DD)."""
        self._period_from = period_from
        self._period_to = period_to
        self._meta.period_from = period_from
        self._meta.period_to = period_to
        return self

    def add_warning(self, message: str) -> "IRBuilder":
        """Append a parse warning."""
        self._warnings.append(message)
        return self

    # -- Phase 3 extension methods --------------------------------------------

    def add_investment_holding(
        self,
        *,
        name: str = "",
        units: str = "",
        currency: str = "",
        unit_price: str = "",
        valuation: str = "",
    ) -> "IRBuilder":
        """Append an investment/holding row."""
        self._investment_holdings.append(InvestmentHolding(
            name=name,
            units=units,
            currency=currency,
            unit_price=unit_price,
            valuation=valuation,
        ))
        return self

    def set_reconciliation(self, data: dict[str, Any]) -> "IRBuilder":
        """Set the reconciliation data dict."""
        self._reconciliation = dict(data)
        return self

    def set_extras(self, data: dict[str, Any]) -> "IRBuilder":
        """Set bank-specific supplementary data dict."""
        self._extras = dict(data)
        return self

    def set_credit_card_summary(
        self,
        *,
        payment_due_date: str | None = None,
        credit_limit: str | None = None,
        available_credit: str | None = None,
        minimum_due: str | None = None,
        previous_balance: str | None = None,
        total_amount_due: str | None = None,
    ) -> "IRBuilder":
        """Set credit card statement summary fields (bank-agnostic)."""
        self._credit_card_summary = CreditCardSummary(
            payment_due_date=payment_due_date,
            credit_limit=credit_limit,
            available_credit=available_credit,
            minimum_due=minimum_due,
            previous_balance=previous_balance,
            total_amount_due=total_amount_due,
        )
        return self

    # -- Transaction builder -------------------------------------------------

    def add_transaction(
        self,
        *,
        posted_date: str,
        amount: float,
        currency: str = "",
        interest_amount: float | None = None,
        description: str = "",
        value_date: str | None = None,
        transfer_labels: list[str] | None = None,
        is_reversal: bool = False,
        is_internal_transfer: bool = False,
        linked_txn_ids: list[str] | None = None,
        balance_after: float | None = None,
        extras: dict[str, Any] | None = None,
        _debug: DebugInfo | None = None,
    ) -> "IRBuilder":
        """Append one transaction to the active account."""
        if self._active_account is None:
            _ = self.add_account(name="Account")

        txn = Transaction(
            txn_id=generate_txn_id(posted_date, amount, currency, description),
            posted_date=posted_date,
            value_date=value_date,
            amount=amount,
            currency=currency or self._functional_currency,
            interest_amount=interest_amount,
            description=description,
            transfer_labels=transfer_labels or [],
            is_reversal=is_reversal,
            is_internal_transfer=is_internal_transfer,
            linked_txn_ids=linked_txn_ids or [],
            balance_after=balance_after,
            extras=extras,
            _debug=_debug,
        )
        assert self._active_account is not None
        self._active_account.transactions.append(txn)
        return self

    def add_fd_record(
        self,
        *,
        deposit_no: str = "",
        value_date: str | None = None,
        maturity_date: str | None = None,
        interest_rate: str | None = None,
        interest_amount: float | None = None,
        principal: float = 0.0,
        currency: str = "",
        assume_pct_rate: bool = False,
    ) -> "IRBuilder":
        """Append one fixed-deposit record to the active account.

        Fixed deposits are NOT transactions. They are stored on
        ``Account.fd_records`` rather than ``Account.transactions``.
        """
        if self._active_account is None:
            # Guard: ensure there is always an account to hold the record.
            _ = self.add_account(name="Fixed Deposit")
        acct = self._active_account
        assert acct is not None
        if acct.fd_records is None:
            acct.fd_records = []
        rate_dec = parse_fd_rate(interest_rate, assume_pct=assume_pct_rate)
        existing = None
        for rec in acct.fd_records:
            if rec.deposit_no == deposit_no and rec.value_date == value_date \
                    and rec.maturity_date == maturity_date:
                existing = rec
                break

        if existing is not None and _fd_records_complementary(existing, principal, interest_amount):
            # Merge the complementary FD row into the existing record instead of
            # appending a duplicate (one row carries principal, the other interest).
            if not existing.principal:
                existing.principal = principal or 0.0
            if existing.interest_amount is None or existing.interest_amount == 0:
                existing.interest_amount = interest_amount
            if not existing.raw_interest_rate:
                existing.raw_interest_rate = interest_rate or ""
            if existing.interest_rate is None and rate_dec is not None:
                existing.interest_rate = rate_dec
            if not existing.currency:
                existing.currency = currency or self._functional_currency
            merged = existing
        else:
            merged = FixedDepositRecord(
                deposit_no=deposit_no,
                value_date=value_date,
                maturity_date=maturity_date,
                interest_rate=rate_dec,
                raw_interest_rate=interest_rate,
                interest_amount=interest_amount,
                principal=principal,
                currency=currency or self._functional_currency,
            )
            acct.fd_records.append(merged)

        return self

    def add_transaction_dict(self, row: dict[str, Any]) -> "IRBuilder":
        """Convenience: add a transaction from a raw parser dict.

        Keys recognised (all optional, missing keys get defaults):
        ``posted_date``, ``amount``, ``currency``, ``description``,
        ``value_date``, ``balance_after``, ``extras``.
        """
        return self.add_transaction(
            posted_date=row.get("posted_date", ""),
            amount=row.get("amount", 0),
            currency=row.get("currency", ""),
            description=row.get("description", ""),
            value_date=row.get("value_date"),
            transfer_labels=row.get("transfer_labels"),
            is_reversal=row.get("is_reversal", False),
            is_internal_transfer=row.get("is_internal_transfer", False),
            linked_txn_ids=row.get("linked_txn_ids") or [],
            balance_after=row.get("balance_after"),
            extras=row.get("extras"),
        )

    # -- Build & serialise ---------------------------------------------------

    def build(self) -> ParsedStatement:
        """Assemble and return the final ``ParsedStatement``."""
        self._meta.functional_currency = self._functional_currency
        return ParsedStatement(
            ir_version="2026.4",
            parsed_at=datetime.now(timezone.utc).isoformat(),
            parser=self._parser,
            source_file=self._source_file,
            statement_meta=self._meta,
            accounts=list(self._accounts),
            warnings=list(self._warnings),
            investment_holdings=list(self._investment_holdings) if self._investment_holdings else None,
            reconciliation=self._reconciliation,
            extras=self._extras,
            credit_card_summary=self._credit_card_summary,
        )

    def to_json(self, *, indent: int = 2) -> str:
        """Build and serialise to a JSON string in one step."""
        return ir_to_json(self.build(), indent=indent)
