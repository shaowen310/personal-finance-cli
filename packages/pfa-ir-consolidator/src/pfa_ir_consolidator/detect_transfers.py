"""detect_transfers.py — detect and link inter-bank and intra-bank transfers.

After consolidating IRs from multiple banks, these passes scan for transaction
pairs where money moved between accounts:

  * **Inter-bank**: accounts at *different* institutions (e.g.
    DBS FAST OUT $1,000 ←→ OCBC FAST IN $1,000).
  * **Intra-bank**: current accounts at the *same* institution (e.g.
    DBS Savings → DBS Current $500).
  * **Currency Conversion**: multi-currency internal transfers within the same
    bank (e.g. UOB "Currency Conversion" SGD↔JPY, SGD↔USD). Matched by
    description prefix "Currency Conversion" and a shared trailing numeric ID.
  * **CC Payment**: credit card payments from a current account to a credit
    card at the *same* institution (e.g. Current Account → Credit Card $500).
    Matched by absolute amount (not sum, since both sides can be negative).

These are internal transfers that should be flagged to avoid double-counting in
net-worth calculations.

Detection rules (all must hold):
  1. Same ``posted_date``
  2. Opposite amounts: ``abs(A.amount + B.amount) < 1e-2``
     (not required for currency conversions — amounts differ by exchange rate)
  3. Neither transaction already flagged ``is_internal_transfer``
     (avoids interfering with FD↔CA linking handled by ``link_fd_to_ca``)
  4. Inter-bank: different institutions; intra-bank: same institution, different
     current accounts.
  5. Currency conversion: same institution, both descriptions contain
     "Currency Conversion" and share the same trailing numeric reference ID.

When a pair is matched:
  * ``is_internal_transfer = True`` on BOTH transactions
  * ``linked_txn_ids`` cross-linked bidirectionally
  * ``transfer_labels`` appended with ``"inter_bank"``, ``"intra_bank"``,
    ``"currency_conversion"``, or ``"cc_payment"`` (deduped)
  * A warning emitted to ``statement.warnings``

Runs after ``consolidate_statements()`` and before ``verify_txn_links()``.
Idempotent: already-matched transactions are skipped.
"""

from __future__ import annotations

import re
from typing import Any


def detect_inter_bank_transfers(statement: Any) -> Any:
    """Detect and link inter-bank internal-transfer transaction pairs.

    Mutates and returns *statement*. Idempotent (skips already-linked txns).

    Parameters
    ----------
    statement : ParsedStatement
        The consolidated statement (post merge, pre verify_txn_links).

    Returns
    -------
    ParsedStatement
        The same statement, mutated in place with links and warnings added.
    """
    # Build (account_no → institution) lookup from Account.institution.
    institution_by_account: dict[str, str] = {}
    for acct in statement.accounts:
        inst = (acct.institution or "").strip()
        if inst:
            institution_by_account[acct.account_no] = inst

    # Index every transaction by (posted_date, institution) for efficient grouping.
    # We group by posted_date first, then cross-check across institutions.
    by_date: dict[str, list[tuple[int, Any, Any]]] = {}
    for ai, acct in enumerate(statement.accounts):
        for _, txn in enumerate(acct.transactions or []):
            if not txn.posted_date:
                continue
            # Skip already-internal-transfer txns (preserve FD↔CA links).
            if txn.is_internal_transfer:
                continue
            by_date.setdefault(txn.posted_date, []).append((ai, acct, txn))

    matched_pairs = 0
    for posted_date, entries in by_date.items():
        n = len(entries)
        # Skip dates with only one unfrozen entry — no pairing possible.
        if n < 2:
            continue

        used: set[int] = set()

        for i in range(n):
            if i in used:
                continue
            _, acct_a, txn_a = entries[i]
            inst_a = institution_by_account.get(acct_a.account_no, "")
            if not inst_a:
                continue

            for j in range(i + 1, n):
                if j in used:
                    continue
                _, acct_b, txn_b = entries[j]
                inst_b = institution_by_account.get(acct_b.account_no, "")

                # Must be different institutions.
                if not inst_b or inst_a == inst_b:
                    continue

                # Must be opposite amounts (within tolerance).
                if abs(txn_a.amount + txn_b.amount) > 1e-2:
                    continue

                # Match found — cross-link.
                used.add(i)
                used.add(j)

                _cross_link(txn_a, txn_b)
                matched_pairs += 1

                # Emit warning.
                warn = (
                    f"Inter-bank transfer detected: {txn_a.txn_id!r} "
                    f"(account {acct_a.account_no}, {inst_a}) ←→ "
                    f"{txn_b.txn_id!r} "
                    f"(account {acct_b.account_no}, {inst_b}), "
                    f"amount {abs(txn_a.amount):,.2f}, date {posted_date}"
                )
                if warn not in statement.warnings:
                    statement.warnings.append(warn)
                break  # One match per txn_a; move to next txn_a.

    # Record detection stats in extras.
    extras = dict(statement.extras or {})
    cons = dict(extras.get("consolidation", {}) or {})
    transfers_extras = dict(cons.get("transfers", {}) or {})
    transfers_extras["inter_bank_detected"] = matched_pairs
    cons["transfers"] = transfers_extras
    extras["consolidation"] = cons
    statement.extras = extras

    return statement


def _cross_link(txn_a: Any, txn_b: Any, labels: tuple[str, ...] = ("inter_bank",)) -> None:
    """Set ``is_internal_transfer``, cross-link ``linked_txn_ids``, and
    append *labels* to ``transfer_labels`` on both transactions.
    """
    txn_a.is_internal_transfer = True
    txn_b.is_internal_transfer = True

    if txn_b.txn_id not in txn_a.linked_txn_ids:
        txn_a.linked_txn_ids = list(txn_a.linked_txn_ids) + [txn_b.txn_id]
    if txn_a.txn_id not in txn_b.linked_txn_ids:
        txn_b.linked_txn_ids = list(txn_b.linked_txn_ids) + [txn_a.txn_id]

    for lbl in labels:
        if lbl not in txn_a.transfer_labels:
            txn_a.transfer_labels = list(txn_a.transfer_labels) + [lbl]
        if lbl not in txn_b.transfer_labels:
            txn_b.transfer_labels = list(txn_b.transfer_labels) + [lbl]


def detect_intra_bank_transfers(statement: Any) -> Any:
    """Detect and link intra-bank current-account transfer transaction pairs.

    In a consolidated statement, a transfer between two current accounts at
    the **same** bank (e.g. DBS Savings → DBS Current) appears twice — once
    as a debit and once as a credit on the same day. This pass identifies
    those matched pairs, marks both as ``is_internal_transfer``, cross-links
    ``linked_txn_ids``, and labels them ``"intra_bank"``.

    Detection rules (all must hold):
      1. Same institution (intra-bank)
      2. Different current accounts (``account_type == "current"``)
      3. Same ``posted_date``
      4. Opposite amounts: ``abs(A.amount + B.amount) < 1e-2``
      5. Neither transaction already flagged ``is_internal_transfer``

    Mutates and returns *statement*. Idempotent (skips already-linked txns).

    Parameters
    ----------
    statement : ParsedStatement
        The consolidated statement (post merge, pre verify_txn_links).

    Returns
    -------
    ParsedStatement
        The same statement, mutated in place with links and warnings added.
    """
    # Build (account_no → institution) lookup from Account.institution.
    institution_by_account: dict[str, str] = {}
    for acct in statement.accounts:
        inst = (acct.institution or "").strip()
        if inst:
            institution_by_account[acct.account_no] = inst

    # Index every non-frozen transaction from current accounts by (posted_date, institution).
    by_date: dict[str, list[tuple[int, Any, Any]]] = {}
    for ai, acct in enumerate(statement.accounts):
        if acct.account_type != "current":
            continue
        for _, txn in enumerate(acct.transactions or []):
            if not txn.posted_date:
                continue
            if txn.is_internal_transfer:
                continue
            by_date.setdefault(txn.posted_date, []).append((ai, acct, txn))

    matched_pairs = 0
    for posted_date, entries in by_date.items():
        n = len(entries)
        if n < 2:
            continue

        used: set[int] = set()

        for i in range(n):
            if i in used:
                continue
            _, acct_a, txn_a = entries[i]
            inst_a = institution_by_account.get(acct_a.account_no, "")
            if not inst_a:
                continue

            for j in range(i + 1, n):
                if j in used:
                    continue
                _, acct_b, txn_b = entries[j]
                inst_b = institution_by_account.get(acct_b.account_no, "")

                # Must be the SAME institution (intra-bank).
                if not inst_b or inst_a != inst_b:
                    continue
                # Must be DIFFERENT accounts.
                if acct_a.account_no == acct_b.account_no:
                    continue
                # Must be opposite amounts (within tolerance).
                if abs(txn_a.amount + txn_b.amount) > 1e-2:
                    continue

                # Match found — cross-link.
                used.add(i)
                used.add(j)

                _cross_link(txn_a, txn_b, labels=("intra_bank",))
                matched_pairs += 1

                warn = (
                    f"Intra-bank transfer detected: {txn_a.txn_id!r} "
                    f"(account {acct_a.account_no}, {inst_a}) ←→ "
                    f"{txn_b.txn_id!r} "
                    f"(account {acct_b.account_no}, {inst_b}), "
                    f"amount {abs(txn_a.amount):,.2f}, date {posted_date}"
                )
                if warn not in statement.warnings:
                    statement.warnings.append(warn)
                break  # One match per txn_a; move to next txn_a.

    # Record detection stats in extras.
    extras = dict(statement.extras or {})
    cons = dict(extras.get("consolidation", {}) or {})
    transfers_extras = dict(cons.get("transfers", {}) or {})
    transfers_extras["intra_bank_detected"] = matched_pairs
    cons["transfers"] = transfers_extras
    extras["consolidation"] = cons
    statement.extras = extras

    return statement


# ---------------------------------------------------------------------------
# Currency Conversion matching helpers
# ---------------------------------------------------------------------------

_CC_DESC_RE = re.compile(r"\bCurrency\s+Conversion\b", re.IGNORECASE)
_CC_ID_RE = re.compile(r"\b(\d{14,20})\b")


def _extract_cc_ref_id(description: str) -> str | None:
    """Extract the trailing numeric reference ID from a Currency Conversion
    description.

    UOB's descriptions look like::

        "Currency Conversion SGD/JPY@124.69 2606120664556908"
        "Currency Conversion SGD 200.00 SGD/JPY@124.69 2606120664556908"

    Returns the rightmost 14–20 digit substring, or ``None``.
    """
    if not description:
        return None
    # Walk all matches and pick the rightmost one.
    match: re.Match[str] | None = None
    for m in _CC_ID_RE.finditer(description):
        match = m
    return match.group(0) if match else None


def detect_currency_conversions(statement: Any) -> Any:
    """Detect and link in-house currency-conversion transaction pairs.

    When a multi-currency account at a bank performs a currency exchange (e.g.
    UOB One Account SGD → FX+ JPY), the statement prints one row per currency
    leg.  The SGD leg is a debit and the foreign-currency leg is a credit; they
    carry different numeric amounts because of the exchange rate, so ordinary
    intra-bank matching (``abs(A.amount + B.amount) < 1e-2``) will never pair
    them.

    Instead we group by description pattern: both legs contain ``"Currency
    Conversion"`` and end with the same numeric reference ID (e.g.
    ``2606120664556908``).

    Detection rules (all must hold):
      1. Both descriptions match ``Currency Conversion`` (case-insensitive).
      2. Both share the same numeric reference ID extracted from the
         description.
      3. Both are from the **same** institution.
      4. Neither is already flagged ``is_internal_transfer``.

    Mutates and returns *statement*. Idempotent (skips already-linked txns).

    Parameters
    ----------
    statement : ParsedStatement
        The consolidated statement (post merge, pre verify_txn_links).

    Returns
    -------
    ParsedStatement
        The same statement, mutated in place with links and warnings added.
    """
    # Build (account_no → institution) lookup.
    institution_by_account: dict[str, str] = {}
    for acct in statement.accounts:
        inst = (acct.institution or "").strip()
        if inst:
            institution_by_account[acct.account_no] = inst

    # Index candidate transactions by (institution, ref_id).
    by_key: dict[tuple[str, str], list[tuple[int, Any, Any]]] = {}
    for ai, acct in enumerate(statement.accounts):
        for _, txn in enumerate(acct.transactions or []):
            if txn.is_internal_transfer:
                continue
            desc = txn.description or ""
            if not _CC_DESC_RE.search(desc):
                continue
            ref_id = _extract_cc_ref_id(desc)
            if not ref_id:
                continue
            inst = institution_by_account.get(acct.account_no, "")
            if not inst:
                continue
            by_key.setdefault((inst, ref_id), []).append((ai, acct, txn))

    matched_pairs = 0
    for (inst, ref_id), entries in by_key.items():
        n = len(entries)
        if n != 2:
            # Only match *exactly* two transactions sharing the same reference.
            # If there are more (shouldn't happen in practice), skip to avoid
            # ambiguous pairing.
            continue

        _ai_a, acct_a, txn_a = entries[0]
        _ai_b, acct_b, txn_b = entries[1]

        # Must be different accounts (same account → likely a data error).
        if acct_a.account_no == acct_b.account_no:
            continue

        _cross_link(txn_a, txn_b, labels=("currency_conversion",))
        matched_pairs += 1

        warn = (
            f"Currency conversion transfer detected: {txn_a.txn_id!r} "
            f"(account {acct_a.account_no}, {txn_a.currency} {txn_a.amount:,.2f}, {inst}) ←→ "
            f"{txn_b.txn_id!r} "
            f"(account {acct_b.account_no}, {txn_b.currency} {txn_b.amount:,.2f}, {inst}), "
            f"ref_id {ref_id}"
        )
        if warn not in statement.warnings:
            statement.warnings.append(warn)

    # Record detection stats in extras.
    extras = dict(statement.extras or {})
    cons = dict(extras.get("consolidation", {}) or {})
    transfers_extras = dict(cons.get("transfers", {}) or {})
    transfers_extras["currency_conversion_detected"] = matched_pairs
    cons["transfers"] = transfers_extras
    extras["consolidation"] = cons
    statement.extras = extras

    return statement


def detect_cc_payments(statement: Any) -> Any:
    """Detect and link credit card payments from current accounts to credit card
    transactions at the same bank.

    When a current account makes a credit card payment (e.g. DBS Current Account
    $500 debit → DBS Credit Card $500 credit), the statement shows a debit on the
    current account side and a corresponding credit on the credit card side, both
    posted on the same date with opposite amounts.

    Detection rules (all must hold):
      1. Same institution
      2. One ``current`` account + one ``credit_card`` account
      3. Same ``posted_date``
      4. Same absolute amount (magnitudes match within ``1e-2``):
         ``abs(abs(A.amount) - abs(B.amount)) < 1e-2``
         (CC payment credits are negative in card accounting, same sign as
         the CA debit — we compare magnitudes, not sums.)
      5. Different account numbers
      6. Neither transaction already flagged ``is_internal_transfer``

    Mutates and returns *statement*. Idempotent (skips already-linked txns).

    Parameters
    ----------
    statement : ParsedStatement
        The consolidated statement (post merge, pre verify_txn_links).

    Returns
    -------
    ParsedStatement
        The same statement, mutated in place with links and warnings added.
    """
    # Build (account_no → institution) lookup from Account.institution.
    institution_by_account: dict[str, str] = {}
    for acct in statement.accounts:
        inst = (acct.institution or "").strip()
        if inst:
            institution_by_account[acct.account_no] = inst

    # Index candidate transactions by posted_date. A CC payment can only pair
    # a current account entry with a credit_card account entry, so each date
    # group must contain at least one of each role.
    by_date: dict[str, list[tuple[int, Any, Any, str]]] = {}
    for ai, acct in enumerate(statement.accounts):
        if acct.account_type not in ("current", "credit_card"):
            continue
        for _, txn in enumerate(acct.transactions or []):
            if not txn.posted_date:
                continue
            if txn.is_internal_transfer:
                continue
            by_date.setdefault(txn.posted_date, []).append(
                (ai, acct, txn, acct.account_type)
            )

    matched_pairs = 0
    for posted_date, entries in by_date.items():
        n = len(entries)
        if n < 2:
            continue

        used: set[int] = set()

        for i in range(n):
            if i in used:
                continue
            _, acct_a, txn_a, role_a = entries[i]
            inst_a = institution_by_account.get(acct_a.account_no, "")
            if not inst_a:
                continue

            for j in range(i + 1, n):
                if j in used:
                    continue
                _, acct_b, txn_b, role_b = entries[j]

                # Must be opposite roles: one current, one credit_card.
                if role_a == role_b:
                    continue

                # Must be same institution.
                inst_b = institution_by_account.get(acct_b.account_no, "")
                if not inst_b or inst_a != inst_b:
                    continue

                # Must be different accounts.
                if acct_a.account_no == acct_b.account_no:
                    continue

                # Must be matching absolute amounts (within tolerance).
                # In credit card accounting, a payment to the card is a credit
                # (negative amount reducing the balance owed), same sign as the
                # current account debit — so we compare magnitudes, not sums.
                if abs(abs(txn_a.amount) - abs(txn_b.amount)) > 1e-2:
                    continue

                # Match found — cross-link.
                used.add(i)
                used.add(j)

                _cross_link(txn_a, txn_b, labels=("cc_payment",))
                matched_pairs += 1

                # Determine which is current and which is CC for the warning.
                ca_acct = acct_a if role_a == "current" else acct_b
                cc_acct = acct_a if role_a == "credit_card" else acct_b
                warn = (
                    f"CC payment detected: "
                    f"(CA {ca_acct.account_no}, {inst_a}) → "
                    f"(CC {cc_acct.account_no}, {inst_a}), "
                    f"amount {abs(txn_a.amount):,.2f}, date {posted_date}"
                )
                if warn not in statement.warnings:
                    statement.warnings.append(warn)
                break  # One match per txn_a; move to next txn_a.

    # Record detection stats in extras.
    extras = dict(statement.extras or {})
    cons = dict(extras.get("consolidation", {}) or {})
    transfers_extras = dict(cons.get("transfers", {}) or {})
    transfers_extras["cc_payments_detected"] = matched_pairs
    cons["transfers"] = transfers_extras
    extras["consolidation"] = cons
    statement.extras = extras

    return statement
