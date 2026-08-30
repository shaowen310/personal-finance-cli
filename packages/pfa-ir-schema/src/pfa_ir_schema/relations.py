"""Relationship / link-label vocabulary for the IR.

These labels are carried in ``Transaction.link_labels`` and accompany
``Transaction.linked_txn_ids``. They describe a *relationship* between two
transactions (typically on different accounts), NOT a classification of the
transaction itself.

Important distinction:
  * ``link_labels``  -> relationship between transactions (twin legs, FD legs).
  * ``tags``         -> standalone classification (e.g. "salary"), independent
                        of any linkage.

Only ``REL_INTERNAL_TRANSFER`` implies ``is_internal_transfer = True`` and is
subject to the equal-amount pairing verifier. FD interest/principal links are
relationships but NOT internal transfers (the money is credited by the bank,
not moved between the holder's own accounts).
"""

REL_INTERNAL_TRANSFER = "internal_transfer"
REL_INTER_BANK = "inter_bank"
REL_INTRA_BANK = "intra_bank"
REL_FD_PRINCIPAL = "fd_principal"
REL_FD_INTEREST = "fd_interest"
REL_CURRENCY_CONVERSION = "currency_conversion"
REL_CC_PAYMENT = "cc_payment"
