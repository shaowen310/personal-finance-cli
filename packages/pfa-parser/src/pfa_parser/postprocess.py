"""Post-processing passes applied to a :class:`ParsedStatement`.

These steps fill in values that the source PDF does not print but which the IR
schema expects (and which downstream renderers read generically). By
materializing the value into the IR, the JSON becomes self-contained and
auditable, and the renderers can stay dumb (read ``t.balance_after`` directly).
"""

from __future__ import annotations

import re

from pfa_ir_schema import (
    Account,
    AccountType,
    ParsedStatement,
    Transaction,
    generate_txn_id,
    verify_fd_interest,
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
            f"Inferred opening_balance for fixed-deposit account "+
            f"'{acct.name}' ({acct.account_no}): opening {opening:,.2f} "+
            f"across {len(txns)} transactions."
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
            deposit_no = (fd_txn.extras or {}).get("fd_link", {}).get(
                "deposit_no", ""
            )
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
                    if re.search(r"renew|rollover", desc, re.I):
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
            labels = txn.transfer_labels or []
            if "fd_interest" in labels and "fd_principal" not in labels:
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
    for lbl in (fd_leg.transfer_labels or []):
        if lbl not in ca_txn.transfer_labels:
            ca_txn.transfer_labels = list(ca_txn.transfer_labels) + [lbl]
    # Mirror the fd_link extras onto the twin for traceability.
    fd_link = (fd_leg.extras or {}).get("fd_link", {})
    ca_txn.extras = {
        **(ca_txn.extras or {}),
        "fd_link": {
            "fd_account_no": fd_link.get("fd_account_no", ""),
            "deposit_no": fd_link.get("deposit_no", ""),
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
        return Transaction(
            txn_id=generate_txn_id(
                fd_leg.posted_date, amt,
                fd_leg.currency, f"Synthetic: {desc} ({label})",
            ),
            posted_date=fd_leg.posted_date,
            amount=amt,
            currency=fd_leg.currency,
            description=f"Synthetic: {desc}",
            transfer_labels=[label],
            is_internal_transfer=is_internal_transfer,
        )

    # Principal leg — always emitted.
    ca_principal = _make_ca("fd_principal", sign * principal, is_internal_transfer=True)
    funding_acct.transactions.append(ca_principal)
    _link_fd_ca(ca_principal, fd_leg, matched_on="synthesized CA twin (renewal/rollover)")
    # Keep only the atomic label this synthetic row represents.
    ca_principal.transfer_labels = ["fd_principal"]

    # Interest leg — only for closures.
    if interest > 0:
        ca_interest = _make_ca("fd_interest", sign * interest, is_internal_transfer=False)
        funding_acct.transactions.append(ca_interest)
        _link_fd_ca(ca_interest, fd_leg, matched_on="synthesized CA twin (renewal/rollover)")
        ca_interest.transfer_labels = ["fd_interest"]
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
    up with ``["fd_principal", "fd_interest"]`` after ``_link_fd_ca`` copies the
    labels.  This helper splits it so that each row carries exactly one label and
    its matching amount, allowing downstream matching to operate on atomic rows.
    """
    from dataclasses import replace

    for acct in statement.accounts:
        if acct.account_type == AccountType.FIXED_DEPOSIT.value:
            continue
        new_txns = []
        for ca_txn in acct.transactions or []:
            labels = ca_txn.transfer_labels or []
            if "fd_principal" not in labels or "fd_interest" not in labels:
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
                    ca_txn.transfer_labels = [l for l in labels if l != "fd_principal"]
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
                            fd_labels = fd_txn.transfer_labels or []
                            if "fd_principal" in fd_labels:
                                fd_principal_ids.add(fd_id)
                            if "fd_interest" in fd_labels:
                                fd_interest_ids.add(fd_id)
                            break

            # Principal portion — keeps original txn_id so existing FD
            # links stay intact for fd_principal legs.
            principal_txn = replace(ca_txn)
            principal_txn.amount = sign * (abs(ca_amt) - total_interest)
            principal_txn.transfer_labels = [l for l in labels if l != "fd_interest"]
            principal_txn.linked_txn_ids = list(fd_principal_ids)
            new_txns.append(principal_txn)

            # Interest portion — new txn_id, only links to fd_interest legs.
            # Interest earnings are NOT an internal transfer (they are investment
            # income), so clear the (combined-row) flag that was copied down.
            interest_txn = replace(ca_txn)
            interest_txn.amount = sign * total_interest
            interest_txn.transfer_labels = [l for l in labels if l != "fd_principal"]
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
                        fd_labels = fd_txn.transfer_labels or []
                        has_principal = "fd_principal" in fd_labels
                        has_interest = "fd_interest" in fd_labels
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
                    f"opening_balance for account '{acct.name}' ({acct.account_no}) "
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


def verify_account_balances(statement: ParsedStatement) -> ParsedStatement:
    """Verify account-level balance consistency against transactions.

    For accounts with transactions:
      * If both ``opening_balance`` and ``closing_balance`` are present, verify
        that ``closing = opening + Σtxn.amount`` (within tolerance ``1e-6``).
      * If the last transaction carries ``balance_after``, verify it matches
        ``closing_balance``.

    For accounts without transactions:
      * If both balances are present, verify ``opening == closing``.

    UNIT_TRUST accounts are skipped.
    Idempotent: warnings already present are not re-appended.

    Mutates and returns *statement*.
    """
    for acct in statement.accounts:
        if acct.account_type == AccountType.UNIT_TRUST.value:
            continue
        txns = acct.transactions or []

        if txns:
            sorted_txns = sorted(txns, key=lambda t: t.posted_date or "")

            # Verify closing_balance against last txn's balance_after
            last_ba = sorted_txns[-1].balance_after
            if last_ba is not None and acct.closing_balance is not None:
                if abs(float(acct.closing_balance) - last_ba) > 1e-6:
                    warn = (
                        f"closing_balance mismatch for account '{acct.name}' "
                        f"({acct.account_no}): closing_balance={acct.closing_balance:,.2f} "
                        f"but last transaction balance_after={last_ba:,.2f}"
                    )
                    if warn not in statement.warnings:
                        statement.warnings.append(warn)

            # Verify closing = opening + Σamount
            # Skip for fixed-deposit accounts: fill_fd_running_balances already
            # set correct opening_balance and balance_after using principal-only
            # deltas.  Raw t.amount may include non-principal amounts (e.g. interest
            # disbursements) that would cause false mismatches here.
            if (
                acct.account_type != AccountType.FIXED_DEPOSIT.value
                and acct.opening_balance is not None
                and acct.closing_balance is not None
            ):
                total_amount = sum(float(t.amount or 0.0) for t in sorted_txns)
                expected_closing = float(acct.opening_balance) + total_amount
                if abs(float(acct.closing_balance) - expected_closing) > 1e-6:
                    warn = (
                        f"closing_balance mismatch for account '{acct.name}' "
                        f"({acct.account_no}): closing_balance={acct.closing_balance:,.2f} "
                        f"but opening_balance + Σtransactions = "
                        f"{acct.opening_balance:,.2f} + {total_amount:,.2f} = "
                        f"{expected_closing:,.2f}"
                    )
                    if warn not in statement.warnings:
                        statement.warnings.append(warn)
        else:
            # No transactions: opening should equal closing
            if acct.opening_balance is not None and acct.closing_balance is not None:
                if abs(float(acct.opening_balance) - float(acct.closing_balance)) > 1e-6:
                    warn = (
                        f"closing_balance mismatch for account '{acct.name}' "
                        f"({acct.account_no}): opening_balance={acct.opening_balance:,.2f} "
                        f"but closing_balance={acct.closing_balance:,.2f} "
                        f"(no transactions to explain difference)"
                    )
                    if warn not in statement.warnings:
                        statement.warnings.append(warn)

    return statement


def verify_fd_interest_consistency(statement: ParsedStatement) -> ParsedStatement:
    """Verify every fixed-deposit interest amount against principal × rate × tenor.

    Runs on both the fresh-extraction and IR-reload paths, so ``--ir-only``
    re-validates FD interest even when the builder was never re-run. Idempotent:
    a warning already present in ``statement.warnings`` (e.g. loaded from a saved
    ``.ir.json``) is not re-appended.

    Mutates and returns *statement*.
    """
    for acct in statement.accounts:
        if acct.account_type != AccountType.FIXED_DEPOSIT:
            continue
        for rec in (acct.fd_records or []):
            warn = verify_fd_interest(
                principal=rec.principal,
                interest_rate=rec.interest_rate,
                value_date=rec.value_date,
                maturity_date=rec.maturity_date,
                interest_amount=rec.interest_amount,
            )
            if warn and warn not in statement.warnings:
                statement.warnings.append(warn)
    return statement


def verify_statement_meta(statement: ParsedStatement) -> ParsedStatement:
    """Verify that ``statement_meta.institution`` is present and non-empty.

    Without an institution the IR cannot identify the source bank, which breaks
    downstream consolidation (accounts are grouped by institution) and the
    renderer registry lookup.

    Idempotent: an already-present warning is not re-appended.

    Mutates and returns *statement*.
    """
    meta = statement.statement_meta
    if not meta.institution or not meta.institution.strip():
        warn = (
            "statement_meta.institution is missing or empty — "
            "extractors must set the bank name (e.g. 'DBS', 'OCBC', 'UOB', 'ICBC')"
        )
        if warn not in statement.warnings:
            statement.warnings.append(warn)
    return statement


def verify_txn_links(statement: ParsedStatement) -> ParsedStatement:
    """Verify transaction link integrity.

    Two independent checks apply:

    * **Orphan detection** — a transaction flagged ``is_internal_transfer`` with
      an empty ``linked_txn_ids`` list means the linker failed to find the other
      side, which would let the move be double-counted downstream.

    * **Symmetry verification** — ANY transaction (regardless of
      ``is_internal_transfer``) that carries entries in ``linked_txn_ids`` must
      be reciprocal: if A marks B as related, B must also mark A. A one-sided
      link indicates the linker only updated one side (e.g. a missed twin).

    Runs on every pipeline path (fresh extraction and IR reload) so a saved
    ``.ir.json`` whose links were lost is still flagged. Idempotent: an
    already-present warning is not re-appended.

    Mutates and returns *statement*.
    """
    # Index every transaction by id so we can resolve linked_txn_ids.
    txn_by_id: dict[str, Transaction] = {}
    for acct in statement.accounts:
        for txn in (acct.transactions or []):
            if txn.txn_id:
                txn_by_id[txn.txn_id] = txn

    # Single pass: two independent checks on the same iteration.
    for acct in statement.accounts:
        for txn in (acct.transactions or []):
            # Orphan detection — internal transfers must have linked twins.
            # NOT covered by symmetry below because orphans have no links to
            # iterate over.
            if txn.is_internal_transfer and not txn.linked_txn_ids:
                warn = (
                    f"transfer without linked twin: txn {txn.txn_id!r} "
                    f"(account {acct.account_no}, {txn.posted_date}, amount "
                    f"{txn.amount}) is_internal_transfer=true but linked_txn_ids is empty"
                )
                if warn not in statement.warnings:
                    statement.warnings.append(warn)

            # Symmetry verification — ANY transaction (regardless of
            # is_internal_transfer) with linked_txn_ids must be reciprocal.
            for twin_id in txn.linked_txn_ids:
                twin = txn_by_id.get(twin_id)
                if twin is None:
                    continue
                if txn.txn_id in twin.linked_txn_ids:
                    continue
                warn = (
                    f"transfer link not reciprocal: txn {txn.txn_id!r} "
                    f"(account {acct.account_no}) lists {twin_id!r} as related "
                    f"but {twin_id!r} does not list it back"
                )
                if warn not in statement.warnings:
                    statement.warnings.append(warn)
    return statement
