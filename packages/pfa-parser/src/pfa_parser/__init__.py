"""pfa_parser — Parse Singapore bank statement PDFs into structured IR.

Supported banks: DBS/POSB, OCBC, UOB, ICBC.

Usage:
    from pfa_parser import ParsedStatement, detect_type, SGBankPDFParser
"""

from .base import BankStatementParser, Transaction
from .sg_parser import SGBankPDFParser
from .convert_statement import detect_type, main

__all__ = [
    "BankStatementParser",
    "ParsedStatement",
    "SGBankPDFParser",
    "Transaction",
    "detect_type",
    "main",
]
