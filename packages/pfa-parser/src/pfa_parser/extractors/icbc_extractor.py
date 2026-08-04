"""ICBC bilingual statement → IR extractor.

Wraps ``icbc_parser.parse_icbc()``.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import ClassVar, override

from pfa_ir_schema import AccountType, ParsedStatement
from .base import BaseExtractor
from ..parsers.icbc_parser import CATxnRow, FDTxnRow


class ICBCExtractor(BaseExtractor):
    parser_name: ClassVar[str] = "icbc_sg"
    parser_version: ClassVar[str] = "1.0"

    @classmethod
    @override
    def supports(cls, pdf_path: Path) -> bool:
        return True

    @classmethod
    @override
    def bank_name(cls) -> str:
        return "ICBC"

    @override
    def _parse(self, pdf_path: Path) -> ParsedStatement:
        from ..parsers.icbc_parser import parse_icbc

        pdf = self._open_pdf(pdf_path)
        try:
            meta, ca_summary, fd_summary, ca_txns, fd_txns, reminders, notes = \
                parse_icbc(pdf)
        finally:
            pdf.close()

        builder = self._create_builder()
        _ = builder.set_source(str(pdf_path))
        _ = builder.set_meta(institution="ICBC_SG", functional_currency="SGD")

        sd = str(meta.get("statement_date", ""))
        if sd:
            # ICBC statement_date: "2026/06/30" (YYYY/MM/DD) → ISO.
            sd_iso = _icbc_date_to_iso(sd)
            _ = builder.set_period(sd_iso, sd_iso)

        # ---- Populate reminders and notes (in extras for ICBC) ----
        extras: dict[str, object] = {}
        if reminders:
            extras["reminders"] = reminders
        if notes:
            extras["notes"] = notes
        if extras:
            _ = builder.set_extras(extras)

        # ---- Current Account(s): group CA txns by (account_no, ccy) ----
        # ICBC repeats the same account number across currencies, so key on the
        # currency too to avoid replicating every currency's transactions.
        ca_by_key: "OrderedDict[tuple[str, str], list[CATxnRow]]" = OrderedDict()
        for t in ca_txns:
            if t.get("is_bf") or t.get("is_total"):
                continue  # skip B/F and Total rows
            key = (str(t.get("acct_no", "")), str(t.get("ccy", "SGD")))
            ca_by_key.setdefault(key, []).append(t)

        for row in ca_summary:
            acct_no = str(row.get("acct_no", ""))
            ccy = str(row.get("ccy", "SGD"))
            txns = ca_by_key.get((acct_no, ccy), [])
            _ = builder.add_account(
                name=str(row.get("acct_type", "Current Account")),
                account_no=acct_no,
                account_type=AccountType.CURRENT.value,
                currency=ccy,
                closing_balance=_icbc_parse_balance(row.get("balance")),
            )
            for t in txns:
                withdrawal = float(str(t.get("withdrawal", "0") or "0").replace(",", ""))
                deposit = float(str(t.get("deposit", "0") or "0").replace(",", ""))
                amount = deposit - withdrawal
                ccy = str(t.get("ccy", "SGD"))

                balance_str = str(t.get("balance", "") or "").replace(",", "")
                balance = float(balance_str) if balance_str else None

                _ = builder.add_transaction(
                    posted_date=_icbc_date_to_iso(str(t.get("date", ""))),
                    amount=amount,
                    currency=ccy,
                    description=str(t.get("remark", "")),
                    balance_after=balance,
                )

        # ---- Fixed Deposit(s): group FD txns by account number ----
        fd_by_no: "OrderedDict[str, list[FDTxnRow]]" = OrderedDict()
        for t in fd_txns:
            fd_by_no.setdefault(str(t.get("acct_no", "")), []).append(t)

        for row in fd_summary:
            acct_no = str(row.get("acct_no", ""))
            txns = fd_by_no.get(acct_no, [])
            _ = builder.add_account(
                name="Fixed Deposit",
                account_no=acct_no,
                account_type=AccountType.FIXED_DEPOSIT.value,
                currency=str(row.get("ccy", "SGD")),
                closing_balance=_icbc_parse_balance(row.get("balance")),
            )
            for t in txns:
                # The FD table's "+Deposit / -Withdrawal" column carries the
                # sign already (full-width minus － for withdrawals); preserve it.
                amt_str = (str(t.get("amount", "") or "")
                           .replace(",", "")
                           .replace("\uFF0D", "-")
                           .replace("\u2212", "-"))
                signed_amount = float(amt_str) if amt_str else 0.0
                principal = abs(signed_amount)
                ccy = str(t.get("ccy", "SGD"))

                _ = builder.add_fd_record(
                    deposit_no=str(t.get("seq_no", "")),
                    value_date=_icbc_date_to_iso(str(t.get("value_date", ""))) or None,
                    maturity_date=_icbc_date_to_iso(str(t.get("maturity_date", ""))) or None,
                    interest_rate=str(t.get("rate", "")),
                    interest_amount=_icbc_parse_balance(t.get("interest_amount")),
                    principal=principal,
                    currency=ccy,
                )

                # Build the FD *transaction* purely from the FD table (principal
                # leg only). Interest is an investment gain, not a balance change
                # of the FD account, so it is intentionally NOT mirrored here.
                # balance_after is read from the FD table's Balance column.
                _ = builder.add_transaction(
                    posted_date=_icbc_date_to_iso(
                        str(t.get("deal_date", "") or str(t.get("value_date", "")) or "")
                    ),
                    amount=signed_amount,
                    currency=ccy,
                    description=str(t.get("remark", "") or "CLO"),
                    is_internal_transfer=True,
                    transfer_labels=["fd_principal"],
                    balance_after=_icbc_parse_balance(t.get("balance")),
                    extras={"fd_link": {
                        "fd_account_no": acct_no,
                        "deposit_no": str(t.get("seq_no", "")),
                    }},
                )

        return builder.build()


def _icbc_date_to_iso(date_str: str) -> str:
    """Convert ICBC YYYY/MM/DD → ISO YYYY-MM-DD."""
    parts = date_str.split("/")
    if len(parts) == 3:
        return f"{parts[0]}-{parts[1]}-{parts[2]}"
    return date_str


def _icbc_parse_balance(val: object) -> float | None:
    """Parse ICBC balance/amount value to float.

    Handles comma grouping and ICBC's full-width minus (U+FF0D) / minus sign
    (U+2212) which appear on withdrawal rows.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        s = str(val).replace(",", "").strip()
        s = s.replace("\uFF0D", "-").replace("\u2212", "-")
        return float(s) if s else None
    except (ValueError, TypeError):
        return None
