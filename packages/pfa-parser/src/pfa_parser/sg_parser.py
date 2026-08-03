"""Parser that integrates PDF extraction for SG bank PDF statements.

Uses auto-detection (detect_type) to pick the right extractor, then calls
to_ir() and flattens all accounts' transactions into the unified
pfa_parser.Transaction model.
"""

from __future__ import annotations

from pathlib import Path
from typing import override

import pdfplumber

from .base import BankStatementParser, Transaction
from .convert_statement import detect_type
from .extractors.registry import get_extractor


class SGBankPDFParser(BankStatementParser):
    """Parse Singapore bank PDF statements."""

    @override
    def supports_format(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() == ".pdf"

    @override
    def parse(self, file_path: str) -> list[Transaction]:
        """Parse a PDF statement and return flat list of all transactions."""
        pdf_path = Path(file_path)

        # Step 1: Detect bank / statement family
        with pdfplumber.open(str(pdf_path)) as pdf:
            bank, family = detect_type(pdf)

        # Step 2: Get the matching extractor
        ExtractorCls = get_extractor(bank, family)
        if ExtractorCls is None:
            raise ValueError(
                f"No extractor found for bank={bank!r}, family={family!r}. "+
                f"File: {pdf_path.name}"
            )

        extractor = ExtractorCls()
        ir = extractor.to_ir(pdf_path)

        # Step 3: Flatten all accounts' transactions into unified model
        transactions: list[Transaction] = []
        for account in ir.accounts:
            for txn in account.transactions:
                transactions.append(
                    Transaction(
                        date=txn.posted_date,  # already ISO YYYY-MM-DD
                        description=txn.description,
                        amount=txn.amount,
                        currency=txn.currency,
                        account_name=account.name,
                        account_no=account.account_no,
                        account_type=account.account_type,
                        balance_after=txn.balance_after,
                        transfer_labels=list(txn.transfer_labels),
                    )
                )

        return transactions
