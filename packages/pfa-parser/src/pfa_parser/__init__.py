"""pfa_parser — Parse Singapore bank statement PDFs into structured IR.

Supported banks: DBS/POSB, OCBC, UOB, ICBC.

Usage:
    from pfa_parser import ParsedStatement, detect_type, SGBankPDFParser
"""

from .base import BankStatementParser, Transaction
from .cli import main
from .convert_statement import detect_type
from .sg_parser import SGBankPDFParser

__all__ = [
    "BankStatementParser",
    "SGBankPDFParser",
    "Transaction",
    "detect_type",
    "main",
]
