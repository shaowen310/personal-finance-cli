"""pfa_ir_schema — Shared IR schema data models for personal finance analysis.

Defines the structured data types that all bank-parser extractors produce
and all downstream consumers read, independent of any source PDF layout.
"""

from .account_type import AccountType, VALID_ACCOUNT_TYPES
from .ir_schema import (
    Account,
    CreditCardSummary,
    DebugInfo,
    FixedDepositRecord,
    InvestmentHolding,
    ParsedStatement,
    ParserInfo,
    StatementMeta,
    Transaction,
    from_dict,
    from_json,
    generate_txn_id,
    to_json,
)
from .common import (
    format_fd_period,
    is_bank_num,
    mask_chinese_name,
    mask_id,
    mask_name,
    mask_names_in_description,
    parse_fd_rate,
    sanitize_description,
    verify_fd_interest,
)

__all__ = [
    "Account",
    "AccountType",
    "CreditCardSummary",
    "DebugInfo",
    "FixedDepositRecord",
    "InvestmentHolding",
    "ParsedStatement",
    "ParserInfo",
    "StatementMeta",
    "Transaction",
    "VALID_ACCOUNT_TYPES",
    "format_fd_period",
    "from_dict",
    "from_json",
    "generate_txn_id",
    "is_bank_num",
    "mask_chinese_name",
    "mask_id",
    "mask_name",
    "mask_names_in_description",
    "parse_fd_rate",
    "sanitize_description",
    "to_json",
    "verify_fd_interest",
]
