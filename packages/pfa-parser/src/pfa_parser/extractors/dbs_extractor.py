"""DBS/POSB Consolidated Statement → IR extractor.

Wraps ``dbs_parser.parse_dbs()`` and maps its output to a ``ParsedStatement``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar, override

from pfa_ir_schema import AccountType, InvestmentHolding, ParsedStatement
from .base import BaseExtractor


class DBSExtractor(BaseExtractor):
    parser_name: ClassVar[str] = "dbs_sg"
    parser_version: ClassVar[str] = "1.0"

    @classmethod
    @override
    def supports(cls, pdf_path: Path) -> bool:
        return True  # caller already matched via detect_type()

    @classmethod
    @override
    def bank_name(cls) -> str:
        return "DBS/POSB"

    @override
    def to_ir(self, pdf_path: Path) -> ParsedStatement:
        from ..parsers.dbs_parser import parse_dbs

        pdf = self._open_pdf(pdf_path)
        try:
            meta, summary, accounts, srs_data = parse_dbs(pdf)
        finally:
            pdf.close()

        builder = self._create_builder()
        _ = builder.set_source(str(pdf_path))

        base_ccy = str(meta.get("currency", "SGD"))

        _ = builder.set_meta(
            institution="DBS_SG",
            account_holder=str(meta.get("account_holder", "")),
        )

        # Period is derived from statement_date if available (DBS does not
        # expose an explicit period range, only a single statement_date).
        sd = str(meta.get("statement_date", ""))
        # DBS dates come as "DD Mon YYYY" (e.g. "30 Jun 2026"); normalise to ISO.
        if sd:
            from datetime import datetime
            try:
                dt = datetime.strptime(sd, "%d %b %Y")
                sd_iso = dt.strftime("%Y-%m-%d")
                _ = builder.set_period(sd_iso, sd_iso)
            except ValueError:
                pass  # silent fallback — period remains empty

        # Build per-account balance lookups keyed by (account_no, currency) so
        # multi-currency accounts (e.g. My Account SGD vs USD) with the same
        # account number are distinguished correctly.
        deposit_by_ckey: dict[tuple[str, str], dict[str, Any]] = {}
        for d in summary.get("deposits", []):
            ckey = (str(d.get("account_no", "")), str(d.get("currency", "")))
            deposit_by_ckey[ckey] = d
        fd_by_no = {str(d.get("account_no", "")): d for d in summary.get("fixed_deposits", [])}
        srs_by_no = {str(srs_data.get("account_no", ""))} if srs_data.get("account_no") else set()

        def _srs_holdings() -> list[InvestmentHolding]:
            """Map SRS unit trusts to per-account InvestmentHoldings."""
            holdings: list[InvestmentHolding] = []
            for ut in srs_data.get("unit_trusts", []):
                holdings.append(InvestmentHolding(
                    name=ut.get("name", ""),
                    units=ut.get("free_qty", ""),
                    currency="SGD",
                    valuation=ut.get("market_value", ""),
                    cost=ut.get("total_cost", ""),
                    unrealised_pl=ut.get("unrealised_pl", ""),
                ))
            return holdings

        for acct in accounts:
            acct_no = str(acct.get("account_no", ""))
            name = str(acct.get("name", ""))
            is_fd = (name == "Fixed Deposit") or (acct_no in fd_by_no)
            is_srs = (name == "SRS Account") or (acct_no in srs_by_no)

            if is_srs:
                cash_balance_raw = srs_data.get("cash_balance", "")
                srs_balance = _parse_float(cash_balance_raw) if cash_balance_raw else None
                account_type = AccountType.SRS
                txn_list = acct.get("transactions", [])
                _ = builder.add_account(
                    name=name or "SRS Account",
                    account_no=acct_no or str(srs_data.get("account_no", "")),
                    account_type=account_type.value,
                    currency=str(acct.get("currency", base_ccy)),
                    opening_balance=_parse_float(acct.get("opening_balance")),
                    closing_balance=srs_balance,
                    investment_holdings=_srs_holdings(),
                    extras={
                        "total": srs_data.get("total", ""),
                        "contributions": srs_data.get("contributions", {}),
                    },
                )
            elif is_fd:
                bal = fd_by_no.get(acct_no, {})
                account_type = AccountType.FIXED_DEPOSIT
                txn_list = acct.get("fd_transactions", [])
                _ = builder.add_account(
                    name=name,
                    account_no=acct_no,
                    account_type=account_type.value,
                    currency=str(acct.get("currency", base_ccy)),
                    opening_balance=_parse_float(acct.get("opening_balance")),
                    closing_balance=_parse_float(bal.get("balance")),
                )
            else:
                ckey = (acct_no, str(acct.get("currency", base_ccy)))
                bal = deposit_by_ckey.get(ckey, {})
                account_type = AccountType.CURRENT
                txn_list = acct.get("transactions", [])
                _ = builder.add_account(
                    name=name,
                    account_no=acct_no,
                    account_type=account_type.value,
                    currency=str(acct.get("currency", base_ccy)),
                    opening_balance=_parse_float(acct.get("opening_balance")),
                    closing_balance=_parse_float(bal.get("balance")),
                )

            prev_fd_date = ""
            for t in txn_list:
                withdrawal = float(str(t.get("withdrawal", "0") or "0").replace(",", ""))
                deposit = float(str(t.get("deposit", "0") or "0").replace(",", ""))
                amount = deposit - withdrawal
                balance_str = str(t.get("balance", "") or "").replace(",", "")
                balance = float(balance_str) if balance_str else None
                posted_date = _dbs_date_to_iso(str(t.get("txn_date", "")))
                description = str(t.get("description", ""))

                if is_fd:
                    principal = _parse_float(t.get("principal")) or 0.0
                    interest_rate = _parse_float(t.get("interest_rate"))
                    interest_amount = _parse_float(t.get("interest_amt"))
                    value_date_iso, maturity_date_iso = _dbs_period_to_dates_iso(
                        str(t.get("period", ""))
                    )
                    deposit_no = str(t.get("deposit_no", "")).strip()
                    currency = str(t.get("currency", base_ccy)).strip() or base_ccy
                    txn_type = str(t.get("txn_type", "")).strip()
                    is_placement = bool(
                        txn_type == "placement"
                        or (
                            txn_type == ""
                            and deposit_no
                            and (principal > 0 or interest_rate is not None)
                        )
                    )
                    fd_link = {
                        "fd_account_no": acct_no,
                        "deposit_no": deposit_no,
                        "value_date": value_date_iso,
                        "maturity_date": maturity_date_iso,
                    }
                    eff_date = posted_date or prev_fd_date
                    if not eff_date:
                        _ = builder.add_warning(
                            f"FD transaction (deposit {deposit_no or acct_no}, "
                            + f"'{description}') has no resolvable date: it carries no "
                            + f"own date and no preceding FD transaction was found in "
                            + f"this statement."
                        )
                    if eff_date:
                        prev_fd_date = eff_date

                    # Placement branch — funds placed INTO the FD (cash IN).
                    if is_placement:
                        # "withdraw" / "renew" in the description means
                        # money OUT (closure). interest_amount is purely
                        # informational and does NOT indicate closure vs
                        # placement.
                        is_closure = bool(
                            re.search(r"withdraw|renew", description or "", re.I)
                        )
                        fd_labels = []
                        if principal:
                            fd_labels.append("fd_principal")
                        if interest_amount:
                            fd_labels.append("fd_interest")
                        _ = builder.add_fd_record(
                            deposit_no=deposit_no,
                            value_date=value_date_iso,
                            maturity_date=maturity_date_iso,
                            interest_rate=str(t.get("interest_rate", "")),
                            assume_pct_rate=True,
                            interest_amount=interest_amount,
                            principal=principal,
                            currency=currency,
                        )
                        _ = builder.add_transaction(
                            posted_date=eff_date,
                            amount=-principal if is_closure else principal,
                            currency=currency,
                            description=description or f"FD {deposit_no}".strip(),
                            is_internal_transfer=True,
                            transfer_labels=fd_labels,
                            interest_amount=interest_amount,
                            balance_after=balance,
                            extras={"fd_link": fd_link},
                        )
                    # Withdrawal branch — premature withdrawal / closure paying OUT
                    # of the FD (cash OUT).
                    else:
                        is_withdrawal = bool(re.search(r"withdraw|premature", description, re.I))
                        is_closure = is_withdrawal
                        fd_labels = []
                        if principal:
                            fd_labels.append("fd_principal")
                        if interest_amount:
                            fd_labels.append("fd_interest")
                        _ = builder.add_transaction(
                            posted_date=eff_date,
                            amount=-principal if is_withdrawal else principal,
                            currency=currency,
                            description=description,
                            is_internal_transfer=True,
                            transfer_labels=fd_labels,
                            interest_amount=interest_amount,
                            balance_after=balance,
                            extras={"fd_link": fd_link},
                        )
                else:
                    _ = builder.add_transaction(
                        posted_date=posted_date,
                        amount=amount,
                        currency=str(t.get("currency", base_ccy)),
                        description=description,
                        balance_after=balance,
                    )

        return builder.build()


def _dbs_date_to_iso(date_str: str) -> str:
    """Convert DBS DD/MM/YYYY → ISO YYYY-MM-DD."""
    parts = date_str.split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return date_str


def _dbs_period_to_dates_iso(period: str) -> tuple[str | None, str | None]:
    """DBS FD period is 'DD/MM/YYYY - DD/MM/YYYY'.

    The former date is the start/deal date (value_date) and the latter date
    is the maturity date. Both are returned ISO-normalized.
    """
    parts = period.split("-")
    if len(parts) == 2:
        return _dbs_date_to_iso(parts[0].strip()), _dbs_date_to_iso(parts[1].strip())
    return None, None


def _parse_float(val: object) -> float | None:
    """Parse a potentially comma-formatted value to float, returning None on failure."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        s = str(val).replace(",", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None
