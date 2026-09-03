"""Post-processing passes applied to a :class:`ParsedStatement`.

These steps fill in values that the source PDF does not print but which the IR
schema expects (and which downstream renderers read generically). By
materializing the value into the IR, the JSON becomes self-contained and
auditable, and the renderers can stay dumb (read ``t.balance_after`` directly).
"""

from __future__ import annotations

import re
from typing import cast

from pfa_ir_schema import (
    Account,
    AccountType,
    ParsedStatement,
    Transaction,
    generate_txn_id,
)
from pfa_ir_schema.relations import (
    REL_FD_INTEREST,
    REL_FD_PRINCIPAL,
)

# Statement-level verification passes (verify_fd_interest_amounts, verify_account_balances,
# verify_statement_meta, verify_txn_links) live in pfa-ir-verifier (the canonical
# home for IR verification); re-use them here. FD<->CA *linking* (link_fd_to_ca) is
# still defined locally below, not in the verifier.
from pfa_ir_verifier import (
    verify_account_balances,
    verify_fd_interest_amounts,
    verify_statement_meta,
    verify_txn_links,
)


def fill_fd_running_balances(statement: ParsedStatement) -> ParsedStatement:
    """Reconstruct ``balance_after`` and ``opening_balance`` for fixed-deposit
    accounts lacking them.

    FD movement tables don't carry a per-row balance, so we rebuild the running
    *outstanding-principal* balance from the deposit ledger (``fd_records``) plus
    the principal movements (``transactions``):

    * ``opening_balance``: when already present it is used as-is; otherwise it is
      derived as the sum of principals of deposits placed *before* the first
      movement (i.e. still live at the start of the period) and persisted back
      to the account so downstream passes don't re-derive a conflicting value.
    * ``closing_balance`` is intentionally NOT set here — ``fill_account_balances``
      (which runs after this pass) derives it from the ``balance_after`` rebuilt
      below, so this pass must stay ordered before it.
    * Each movement adds (positive amount) or removes (negative amount)
      principal. Interest-only
      rows leave the balance unchanged, populating ``balance_after`` on every
      transaction.

    Accounts that already carry a per-row ``balance_after`` (e.g. ICBC, which
    prints running balances in its FD table) are left untouched — this pass is
    idempotent and bank-agnostic.

    Mutates and returns *statement*.
    """
    for acct in statement.accounts:
        if acct.account_type != AccountType.FIXED_DEPOSIT:
            continue
        txns = acct.transactions or []
        if not txns:
            continue
        # Skip accounts whose balances were already extracted from the PDF.
        if any(t.balance_after is not None for t in txns):
            continue

        earliest = min((t.posted_date for t in txns if t.posted_date), default=None)
        if acct.opening_balance is not None:
            opening = float(acct.opening_balance)
        else:
            opening = 0.0
            for r in (acct.fd_records or []):
                vd = r.value_date
                if vd and earliest and vd < earliest:
                    opening += float(r.principal or 0.0)
            acct.opening_balance = opening

        bal = opening
        for t in txns:
            amt = float(t.amount or 0.0)
            bal += amt
            t.balance_after = bal

        statement.warnings.append(
            "Inferred opening_balance for fixed-deposit account "+
            f"'{acct.name}' ({acct.account_no}, {acct.currency or '?'}): "+
            f"opening {opening:,.2f} across {len(txns)} transactions."
        )
    return statement


def _already_linked(fd_legs: list[Transaction]) -> bool:
    """Return True once every FD leg in the group has at least one CA twin."""
    return all(fl.linked_txn_ids for fl in fd_legs)


def link_fd_to_ca(statement: ParsedStatement) -> ParsedStatement:
    """Link fixed-deposit principal movements to their funding-account twin.

    In a consolidated statement a placement / rollover / withdrawal of a fixed
    deposit is usually printed twice — once in the FIXED_DEPOSIT account and once
    as a transaction in the funding current/savings account on the same day with
    the opposite sign and equal magnitude. This pass annotates the funding-account
    twin for traceability and sets ``linked_txn_ids`` (a list) bidirectionally so
    downstream consumers can collapse the double-count.

    Bank-agnostic: works for any extractor that emits FIXED_DEPOSIT accounts with
    transactions — e.g. ICBC and DBS. It links *every* FD-account transaction
    regardless of transfer_label, because FD accounts only ever carry principal
    movements (interest is modelled separately, so it never appears as an FD
    transaction to match). No-op when there are no FD accounts, and idempotent on
    reload (skips txns that already carry a ``linked_txn_ids`` entry for this
    group).

    A closure can carry interest. In the combined-row model the FD transaction
    carries an ``interest_amount`` and is labelled ``fd_interest``; the
    funding-account credit equals ``principal + interest``, so the match compares
    ``CA amount`` against the sum of ``|FD principal| + interest``. In the
    separate-leg model the principal and interest are emitted as two FD
    transactions (``fd_principal`` and ``fd_interest``); the CA credit equals
    their combined ``|amount|`` and the CA twin links to *both* legs. Either way,
    both atomic labels are copied onto the twin.

    Mutates and returns *statement*.

    Naming convention: this uses the ``link_*`` verb per the contract documented in
    ``pfa_ir_verifier.verify`` (module docstring) — ``link_*`` denotes a *mutating*
    relationship pass that writes ``linked_txn_ids`` / ``link_labels``. Read-only /
    warning-only checks use ``find_*`` / ``verify_*`` instead.
    """
    fd_accounts = [a for a in statement.accounts
                   if a.account_type == AccountType.FIXED_DEPOSIT.value]
    if not fd_accounts:
        return statement

    # Candidate funding transactions (exclude FD accounts themselves).
    funding_txns: list[Transaction] = []
    for acct in statement.accounts:
        if acct.account_type == AccountType.FIXED_DEPOSIT.value:
            continue
        funding_txns.extend(acct.transactions)

    for acct in fd_accounts:
        # Group this FD account's transactions by (deposit_no, posted_date).
        groups: dict[tuple[str, str], list[Transaction]] = {}
        for fd_txn in acct.transactions:
            if not fd_txn.posted_date:
                continue
            # ``extras`` values are JSONValue (may be list/num/None), so narrow
            # the nested fd_link dict and coerce the deposit_no to str — it is
            # used as part of the (str, str) grouping key below.
            fd_link = cast("dict[str, object]", (fd_txn.extras or {}).get("fd_link", {}))
            deposit_no = str(fd_link.get("deposit_no") or "")
            groups.setdefault((deposit_no, fd_txn.posted_date), []).append(fd_txn)

        for (deposit_no, posted_date), fd_legs in groups.items():
            # Idempotency: skip groups where every FD leg already has a
            # linked CA twin (e.g. on IR-JSON reload after postprocess has
            # already run). Prevents duplicate synthetic transactions.
            if _already_linked(fd_legs):
                continue
            # Magnitude the funding-account twin must equal: sum of each leg's
            # principal amount plus any interest. On a combined row, interest is
            # separate from ``amount``; on a standalone interest leg it is folded
            # into ``interest_amount`` (with amount 0) — adding ``interest_amount``
            # keeps both cases correct.
            total_mag = sum(
                abs(t.amount) + (t.interest_amount or 0.0) for t in fd_legs
            )
            if total_mag <= 0:
                continue

            # PASS 1: split CA credits — each FD leg matches a *distinct* CA
            # credit on the same day whose |amount| equals that leg's magnitude
            # (principal, plus interest when carried on the leg). Used by DBS
            # maturities, which emit the principal and interest as two separate
            # CA credits.
            consumed: set[int] = set()
            leg_ca: dict[str, Transaction] = {}
            for fl in fd_legs:
                leg_mag = abs(fl.amount) + (fl.interest_amount or 0.0)
                if leg_mag <= 0:
                    continue
                for idx, ca_txn in enumerate(funding_txns):
                    if idx in consumed or ca_txn.posted_date != posted_date:
                        continue
                    if abs(ca_txn.amount - leg_mag) > 1e-6 and abs(ca_txn.amount + leg_mag) > 1e-6:
                        continue
                    consumed.add(idx)
                    leg_ca[fl.txn_id] = ca_txn
                    break

            if len(leg_ca) == len(fd_legs):
                for fl in fd_legs:
                    if fl.txn_id in leg_ca:
                        _link_fd_ca(
                            leg_ca[fl.txn_id], fl,
                            matched_on="CA amount == FD leg (principal + interest)",
                        )
                continue

            # PASS 2: combined CA credit — a single CA credit on the same day
            # equals the summed magnitude of all FD legs (ICBC, premature
            # withdrawals, closures with interest on one row).
            combined: Transaction | None = None
            for ca_txn in funding_txns:
                if ca_txn.posted_date != posted_date:
                    continue
                if abs(ca_txn.amount - total_mag) > 1e-6 and abs(ca_txn.amount + total_mag) > 1e-6:
                    continue
                combined = ca_txn
                break
            if combined is not None:
                for fl in fd_legs:
                    _link_fd_ca(
                        combined, fl,
                        matched_on="CA amount == sum(FD legs: principal + interest)",
                    )
                continue

            # PASS 3: no matching CA twin at all. For renewals / rollovers
            # the money stays within the bank system — synthesise a virtual
            # CA twin so the principal + interest legs can be linked and
            # split downstream.
            for fl in fd_legs:
                if fl.txn_id in leg_ca:
                    _link_fd_ca(
                        leg_ca[fl.txn_id], fl,
                        matched_on="CA amount == FD leg (principal + interest)",
                    )
                else:
                    desc = fl.description or ""
                    if re.search(r"renew|rollover", desc, re.IGNORECASE):
                        _ = _synthesize_ca_twin(statement, fl)
                    else:
                        fl.is_internal_transfer = False

    # Post-linking: split CA transactions that carry both fd_principal +
    # fd_interest so each atomic label maps to its own row (mirroring what
    # DBS e-statements print as separate principal / interest credits).
    _split_ca_combined_labels(statement, fd_accounts)

    # Interest earnings are investment income, not internal transfers.
    for acct in statement.accounts:
        for txn in (acct.transactions or []):
            labels = txn.link_labels or []
            if REL_FD_INTEREST in labels and REL_FD_PRINCIPAL not in labels:
                txn.is_internal_transfer = False

    return statement


def _link_fd_ca(ca_txn: Transaction, fd_leg: Transaction,
                matched_on: str = "") -> None:
    """Record a bidirectional FD <-> CA transfer link on both transactions.

    Marks both the funding-account twin and the FD leg as ``is_internal_transfer`` and
    cross-links their ``linked_txn_ids`` (deduped). FD atomic labels
    (``fd_principal`` / ``fd_interest``) are copied onto the twin, and the
    ``fd_link`` extras are mirrored for traceability.
    """
    ca_txn.is_internal_transfer = True
    fd_leg.is_internal_transfer = True
    if fd_leg.txn_id not in ca_txn.linked_txn_ids:
        ca_txn.linked_txn_ids.append(fd_leg.txn_id)
    if ca_txn.txn_id not in fd_leg.linked_txn_ids:
        fd_leg.linked_txn_ids.append(ca_txn.txn_id)
    # Copy FD transfer labels (fd_principal / fd_interest) from the leg onto the twin, deduped.
    for lbl in (fd_leg.link_labels or []):
        if lbl not in ca_txn.link_labels:
            ca_txn.link_labels = list(ca_txn.link_labels) + [lbl]
    # Mirror the fd_link extras onto the twin for traceability. Values are
    # coerced to str so the literal stays assignable to dict[str, JSONValue].
    fd_link = cast("dict[str, object]", (fd_leg.extras or {}).get("fd_link", {}))
    ca_txn.extras = {
        **(ca_txn.extras or {}),
        "fd_link": {
            "fd_account_no": str(fd_link.get("fd_account_no") or ""),
            "deposit_no": str(fd_link.get("deposit_no") or ""),
            "matched_on": matched_on,
        },
    }


def _synthesize_ca_twin(
    statement: ParsedStatement,
    fd_leg: Transaction,
) -> Transaction | None:
    """Create synthetic CA transactions for a renewal / rollover FD leg.

    Renewals and rollovers keep the money within the banking system — no
    explicit CA entry appears in the statement.  This helper emits
    pre-split rows (never a combined row that downstream would split again).
    """
    assert not fd_leg.linked_txn_ids, (
        f"_synthesize_ca_twin called for already-linked FD leg {fd_leg.txn_id!r}"
    )

    # Find the first non-FD account to host the synthetic transactions.
    funding_acct = None
    for acct2 in statement.accounts:
        if acct2.account_type != AccountType.FIXED_DEPOSIT.value:
            funding_acct = acct2
            break
    if funding_acct is None:
        return None

    is_closure = (fd_leg.amount or 0.0) < 0
    principal = abs(fd_leg.amount or 0.0)
    interest = abs(fd_leg.interest_amount or 0.0) if is_closure else 0.0
    sign = 1.0 if is_closure else -1.0
    desc = fd_leg.description or ""

    def _make_ca(label: str, amt: float, *, is_internal_transfer: bool) -> Transaction:
        desc_with_label = f"Synthetic: {desc} [{label}]"
        return Transaction(
            txn_id=generate_txn_id(
                fd_leg.posted_date, amt,
                fd_leg.currency, desc_with_label,
            ),
            posted_date=fd_leg.posted_date,
            amount=amt,
            currency=fd_leg.currency,
            description=desc_with_label,
            link_labels=[label],
            is_internal_transfer=is_internal_transfer,
        )

    # Principal leg — always emitted.
    ca_principal = _make_ca(REL_FD_PRINCIPAL, sign * principal, is_internal_transfer=True)
    funding_acct.transactions.append(ca_principal)
    _link_fd_ca(ca_principal, fd_leg, matched_on="synthesized CA twin (renewal/rollover)")
    # Keep only the atomic label this synthetic row represents.
    ca_principal.link_labels = [REL_FD_PRINCIPAL]

    # Interest leg — only for closures.
    if interest > 0:
        ca_interest = _make_ca(REL_FD_INTEREST, sign * interest, is_internal_transfer=False)
        funding_acct.transactions.append(ca_interest)
        _link_fd_ca(ca_interest, fd_leg, matched_on="synthesized CA twin (renewal/rollover)")
        ca_interest.link_labels = [REL_FD_INTEREST]
        return ca_interest

    return ca_principal


def _split_ca_combined_labels(
    statement: ParsedStatement,
    fd_accounts: list[Account],
) -> None:
    """Split CA transactions that carry both ``fd_principal`` and ``fd_interest``
    into two rows — one per atomic label — mirroring the DBS e-statement model
    where principal and interest credits arrive as distinct CA entries.

    A CA twin that was linked to a combined FD leg (e.g. maturity closure) ends
    up with ``[REL_FD_PRINCIPAL, REL_FD_INTEREST]`` after ``_link_fd_ca`` copies the
    labels.  This helper splits it so that each row carries exactly one label and
    its matching amount, allowing downstream matching to operate on atomic rows.
    """
    from dataclasses import replace

    for acct in statement.accounts:
        if acct.account_type == AccountType.FIXED_DEPOSIT.value:
            continue
        new_txns = []
        for ca_txn in acct.transactions or []:
            labels = ca_txn.link_labels or []
            if REL_FD_PRINCIPAL not in labels or REL_FD_INTEREST not in labels:
                new_txns.append(ca_txn)
                continue

            # Sum interest from linked FD legs to compute the split point.
            total_interest = 0.0
            for fd_id in ca_txn.linked_txn_ids or []:
                for fd_acct2 in fd_accounts:
                    for fd_txn in fd_acct2.transactions or []:
                        if fd_txn.txn_id == fd_id:
                            total_interest += abs(fd_txn.interest_amount or 0.0)
                            break

            # Nothing to split, or the CA row is entirely interest — strip
            # the stray fd_principal label that _link_fd_ca copied down from
            # a combined FD leg.
            if total_interest <= 0 or abs(total_interest) >= abs(ca_txn.amount or 0.0) - 1e-6:
                if abs(total_interest) >= abs(ca_txn.amount or 0.0) - 1e-6 and total_interest > 0:
                    ca_txn.link_labels = [l for l in labels if l != REL_FD_PRINCIPAL]
                new_txns.append(ca_txn)
                continue

            ca_amt = float(ca_txn.amount or 0.0)
            sign = 1.0 if ca_amt >= 0 else -1.0

            # Build per-label FD-leg-ID sets so each split CA row only
            # links to FD legs that carry the matching label.
            fd_principal_ids: set[str] = set()
            fd_interest_ids: set[str] = set()
            for fd_id in ca_txn.linked_txn_ids or []:
                for fd_acct2 in fd_accounts:
                    for fd_txn in fd_acct2.transactions or []:
                        if fd_txn.txn_id == fd_id:
                            fd_labels = fd_txn.link_labels or []
                            if REL_FD_PRINCIPAL in fd_labels:
                                fd_principal_ids.add(fd_id)
                            if REL_FD_INTEREST in fd_labels:
                                fd_interest_ids.add(fd_id)
                            break

            # Principal portion — keeps original txn_id so existing FD
            # links stay intact for fd_principal legs.
            principal_txn = replace(ca_txn)
            principal_txn.amount = sign * (abs(ca_amt) - total_interest)
            principal_txn.link_labels = [l for l in labels if l != REL_FD_INTEREST]
            principal_txn.linked_txn_ids = list(fd_principal_ids)
            new_txns.append(principal_txn)

            # Interest portion — new txn_id, only links to fd_interest legs.
            # Interest earnings are NOT an internal transfer (they are investment
            # income), so clear the (combined-row) flag that was copied down.
            interest_txn = replace(ca_txn)
            interest_txn.amount = sign * total_interest
            interest_txn.link_labels = [l for l in labels if l != REL_FD_PRINCIPAL]
            interest_txn.linked_txn_ids = list(fd_interest_ids)
            interest_txn.is_internal_transfer = False
            interest_txn.txn_id = generate_txn_id(
                interest_txn.posted_date,
                interest_txn.amount,
                interest_txn.currency,
                interest_txn.description,
            )
            new_txns.append(interest_txn)

            # Update FD-side linked_txn_ids:
            # - legs with fd_principal keep principal_txn.txn_id (= original)
            # - legs without fd_principal drop the original CA txn_id
            # - legs with fd_interest gain interest_txn.txn_id
            for fd_id in ca_txn.linked_txn_ids or []:
                for fd_acct2 in fd_accounts:
                    for fd_txn in fd_acct2.transactions or []:
                        if fd_txn.txn_id != fd_id:
                            continue
                        fd_labels = fd_txn.link_labels or []
                        has_principal = REL_FD_PRINCIPAL in fd_labels
                        has_interest = REL_FD_INTEREST in fd_labels
                        if has_interest:
                            if interest_txn.txn_id not in fd_txn.linked_txn_ids:
                                fd_txn.linked_txn_ids.append(interest_txn.txn_id)
                            # Interest-only FD legs are investment earnings,
                            # not internal transfers.
                            if not has_principal:
                                fd_txn.is_internal_transfer = False
                        if not has_principal:
                            # Drop the original CA txn_id since the principal
                            # split row no longer links to this leg.
                            fd_txn.linked_txn_ids = [
                                lid for lid in fd_txn.linked_txn_ids
                                if lid != principal_txn.txn_id
                            ]
                        break

        acct.transactions = new_txns


def fill_account_balances(statement: ParsedStatement) -> ParsedStatement:
    """Derive missing ``opening_balance`` / ``closing_balance`` from transactions.

    For accounts with transactions:
      * If ``closing_balance`` is None but the last transaction carries
        ``balance_after``, populate it from that value.
      * If ``opening_balance`` is None but ``closing_balance`` is available,
        derive ``opening_balance = closing_balance - Σtxn.amount`` and emit a
        warning that the opening balance was derived.

    For accounts without transactions:
      * If only ``opening_balance`` → set ``closing_balance = opening_balance``.
      * If only ``closing_balance`` → set ``opening_balance = closing_balance``.

    UNIT_TRUST accounts are skipped (they carry investment holdings, not monetary
    balances).

    Mutates and returns *statement*.
    """
    for acct in statement.accounts:
        if acct.account_type == AccountType.UNIT_TRUST.value:
            continue
        txns = acct.transactions or []

        if txns:
            sorted_txns = sorted(txns, key=lambda t: t.posted_date or "")
            total_amount = sum(float(t.amount or 0.0) for t in sorted_txns)

            # Fill closing_balance from last txn's balance_after if missing
            if acct.closing_balance is None:
                last_ba = sorted_txns[-1].balance_after
                if last_ba is not None:
                    acct.closing_balance = last_ba

            # Derive opening_balance from closing_balance if missing
            if acct.opening_balance is None and acct.closing_balance is not None:
                derived_opening = float(acct.closing_balance) - total_amount
                acct.opening_balance = derived_opening
                warn = (
                    f"opening_balance for account '{acct.name}' "
                    f"({acct.account_no}, {acct.currency or '?'}) "
                    f"derived from closing_balance and transactions: "
                    f"{acct.closing_balance:,.2f} - {total_amount:,.2f} = "
                    f"{derived_opening:,.2f}"
                )
                if warn not in statement.warnings:
                    statement.warnings.append(warn)
        else:
            # No transactions: propagate the available balance to fill the gap
            if acct.opening_balance is not None and acct.closing_balance is None:
                acct.closing_balance = acct.opening_balance
            elif acct.closing_balance is not None and acct.opening_balance is None:
                acct.opening_balance = acct.closing_balance

    return statement


_CREDIT_CARD_TYPES: frozenset[str] = frozenset({"credit_card", "credit", "card"})


def fill_cc_running_balances(statement: ParsedStatement) -> ParsedStatement:
    """Fill ``balance_after`` on credit-card transactions.

    Sorts transactions by ``posted_date``, then computes a running balance
    starting from the account's ``opening_balance``. Mutates and returns
    *statement*.
    """
    for acct in statement.accounts:
        atype = str(acct.account_type or "").lower()
        if atype not in _CREDIT_CARD_TYPES:
            continue
        txns = sorted(acct.transactions, key=lambda t: t.posted_date or "")
        running = acct.opening_balance or 0.0
        for txn in txns:
            amt = txn.amount or 0.0
            running += amt
            txn.balance_after = running
    return statement


def postprocess_statement(statement: ParsedStatement) -> ParsedStatement:
    """Run the full post-processing pipeline on a :class:`ParsedStatement`.

    This is the canonical entry point for all post-extraction passes:
    meta validation → FD-CA linking → running-balance fill →
    account-balance fill → balance verification → FD interest verification →
    credit-card balance fill → transfer-link verification.

    Mutates and returns *statement*. Callers that produce a
    ``ParsedStatement`` (extractors, batch scripts) should always run
    this before persisting the IR JSON.
    """
    statement = verify_statement_meta(statement)
    statement = link_fd_to_ca(statement)
    statement = fill_fd_running_balances(statement)
    statement = fill_account_balances(statement)
    statement = verify_account_balances(statement)
    statement = verify_fd_interest_amounts(statement)
    statement = fill_cc_running_balances(statement)
    statement = verify_txn_links(statement)
    return statement
