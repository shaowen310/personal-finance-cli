"""pfa_ir_verifier — standalone verification utilities for the personal-finance IR."""

from .verify import (
    InternalTransferIssue,
    IrVerificationReport,
    RawRow,
    demote_orphan_internal_transfers,
    detect_internal_transfer_orphans,
    promote_internal_transfers,
    verify_account_balances,
    verify_fd_interest_amounts,
    verify_ir,
    verify_statement_meta,
    verify_txn_links,
)

__all__ = [
    "InternalTransferIssue",
    "IrVerificationReport",
    "RawRow",
    "detect_internal_transfer_orphans",
    "promote_internal_transfers",
    "demote_orphan_internal_transfers",
    "verify_account_balances",
    "verify_fd_interest_amounts",
    "verify_ir",
    "verify_statement_meta",
    "verify_txn_links",
]
