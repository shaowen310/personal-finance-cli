"""Parser that integrates sg-bank-pdf-parser for SG bank PDF statements.

Uses auto-detection (detect_type) to pick the right extractor, then calls
to_ir() and flattens all accounts' transactions into the unified
pfa_parser.Transaction model.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from .base import BankStatementParser, Transaction


class SGBankPDFParser(BankStatementParser):
    """Parse Singapore bank PDF statements via sg-bank-pdf-parser."""

    def supports_format(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() == ".pdf"

    def parse(self, file_path: str) -> List[Transaction]:
        """Parse a PDF statement and return flat list of all transactions."""
        pdf_path = Path(file_path)

        # Lazy imports so sg-bank-pdf-parser is only needed when actually
        # parsing PDFs — pfa-parser can still be imported without it for
        # Transaction model usage.
        import pdfplumber
        from sg_bank_pdf_parser import ParsedStatement
        from sg_bank_pdf_parser.convert_statement import detect_type
        from sg_bank_pdf_parser.extractors.registry import get_extractor

        # Step 1: Detect bank / statement family
        with pdfplumber.open(str(pdf_path)) as pdf:
            bank, family = detect_type(pdf)

        # Step 2: Get the matching extractor
        ExtractorCls = get_extractor(bank, family)
        if ExtractorCls is None:
            raise ValueError(
                f"No extractor found for bank={bank!r}, family={family!r}. "
                f"File: {pdf_path.name}"
            )

        extractor = ExtractorCls()
        ir: ParsedStatement = extractor.to_ir(pdf_path)

        # Step 3: Flatten all accounts' transactions into unified model
        transactions: List[Transaction] = []
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
