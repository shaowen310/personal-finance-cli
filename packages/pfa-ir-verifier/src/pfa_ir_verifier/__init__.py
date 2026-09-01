"""pfa_ir_verifier — standalone verification utilities for the personal-finance IR."""

from .verify import (
    InternalTransferIssue,
    IrVerificationReport,
    demote_orphan_internal_transfers,
    find_internal_transfer_orphans,
    promote_internal_transfers,
    verify_fd_interest,
    verify_ir,
    verify_txn_links,
)

__all__ = [
    "InternalTransferIssue",
    "IrVerificationReport",
    "find_internal_transfer_orphans",
    "promote_internal_transfers",
    "demote_orphan_internal_transfers",
    "verify_fd_interest",
    "verify_ir",
    "verify_txn_links",
]
