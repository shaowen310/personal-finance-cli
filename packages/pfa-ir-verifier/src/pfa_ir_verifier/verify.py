"""IR verification utilities.

These checks operate on the shared ``pfa-ir-schema`` contract: a consolidated
IR is a ``ParsedStatement`` (accounts -> transactions), but the core routines
also accept a flat ``list[dict]`` of transaction rows (the shape
``pfa-analysis`` passes around internally) so they can be reused without
pulling in the analysis stack.

Public API
----------
* :class:`IrVerificationReport` — structured result of a verification run.
* :func:`find_internal_transfer_orphans` — read-only check; finds internal
  transfers that have no equal-magnitude opposite leg.
* :func:`reconcile_internal_transfers` — fixer; demotes orphan internal-flagged
  rows in place and returns what changed.
* :func:`promote_internal_transfers` — fixer; promotes description-based
  self-reference rows to internal transfers only when a valid partner leg exists.
* :func:`verify_txn_links` — link-integrity check (orphan + reciprocal symmetry).
* :func:`verify_ir` — convenience loader + full check for a JSON file or
  ``ParsedStatement`` (links + internal-transfer reconciliation).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pfa_ir_schema import ParsedStatement, Transaction, from_json

# A transaction row as understood by the verifier. ``pfa-analysis`` passes plain
# dicts; ``ParsedStatement`` is flattened to the same shape.
# A transaction row supporting dict-like read/write. Both plain dicts (loaded
# IR) and the dataclass-backed ``_TxnView`` satisfy this interface, so the
# verifier can mutate either in place.
class Txn(Protocol):
    def get(self, key: str, default: Any = ...) -> Any: ...
    def __getitem__(self, key: str) -> Any: ...
    def __setitem__(self, key: str, value: Any) -> None: ...
    def __contains__(self, key: str) -> bool: ...


def _iter_txns(ir: ParsedStatement | dict[str, Any] | list[Any] | str | Path) -> list[Any]:
    """Normalise any supported IR input into a flat list of txn dicts.

    Accepts a ``ParsedStatement``, a plain ``dict`` (accounts/transactions), a
    flat ``list[dict]``, or a file path (loaded via ``pfa_ir_schema.from_json``).
    """
    if isinstance(ir, (str, Path)):
        ir = from_json(Path(ir).read_text(encoding="utf-8"))
    if isinstance(ir, list):
        return ir
    if isinstance(ir, ParsedStatement):
        # Return views backed by the dataclasses so that in-place mutations
        # (e.g. clearing is_internal_transfer) propagate to the source IR.
        return [_TxnView(t) for acct in ir.accounts for t in acct.transactions]
    if isinstance(ir, dict):
        if "accounts" in ir:
            rows = []
            for acct in ir["accounts"]:
                atype = acct.get("account_type", "")
                for t in acct.get("transactions", []):
                    d = dict(t)
                    d.setdefault("account_type", atype)
                    rows.append(d)
            return rows
        if "transactions" in ir:  # legacy flat format
            return list(ir["transactions"])
    raise TypeError(f"Unsupported IR input type: {type(ir)!r}")


class _TxnView:
    """dict-like view over a ``Transaction`` dataclass.

    Allows the verifier to read/write transaction fields uniformly whether the
    underlying row is a plain dict (loaded IR) or a ``Transaction`` dataclass
    (ParsedStatement). Writes propagate back to the source object.
    """

    __slots__ = ("_o",)

    def __init__(self, obj: Any) -> None:
        self._o = obj

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self._o, key, default)

    def __getitem__(self, key: str) -> Any:
        return getattr(self._o, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self._o, key, value)

    def __contains__(self, key: str) -> bool:
        return hasattr(self._o, key)


@dataclass
class InternalTransferIssue:
    """One internal-transfer row that fails verification."""

    currency: str
    amount: float
    txn_id: str
    description: str
    reason: str = "unpaired internal-transfer leg (no equal-magnitude opposite)"


@dataclass
class IrVerificationReport:
    """Result of a verification run.

    ``ok`` is True when no issues were found (safe to use as a boolean).
    """

    internal_transfer_issues: list[InternalTransferIssue] = field(default_factory=list)
    currencies_checked: list[str] = field(default_factory=list)
    link_warnings: list[str] = field(default_factory=list)

    @property
    def orphan_count(self) -> int:
        return len(self.internal_transfer_issues)

    @property
    def ok(self) -> bool:
        return self.orphan_count == 0 and not self.link_warnings

    def render_text(self) -> str:
        if self.ok:
            return (
                f"OK — internal transfers reconcile across "
                f"{len(self.currencies_checked)} currency(ies): "
                f"{', '.join(self.currencies_checked)}"
            )
        lines = [f"Found {self.orphan_count} unpaired internal-transfer row(s):"]
        for i in self.internal_transfer_issues:
            lines.append(
                f"  [{i.currency}] {i.amount:+.2f}  {i.txn_id}  {i.description!r}  "
                f"({i.reason})"
            )
        return "\n".join(lines)


def _detect_orphans(txns: list[Txn]) -> list[InternalTransferIssue]:
    """Return internal-flagged rows lacking an equal-magnitude opposite leg.

    Internal transfers always move money between two of the holder's own
    accounts, so for every currency the internal legs must pair up by equal
    magnitude and net to zero. A row whose magnitude has an odd count within
    its currency is an orphan (it has no twin). Pairing by magnitude parity is
    independent of cash-flow direction, so it is robust to a row being
    mis-classified as In vs Out.
    """
    by_ccy: dict[str, list[Txn]] = {}
    for t in txns:
        if t.get("is_internal_transfer"):
            by_ccy.setdefault(t["currency"], []).append(t)

    issues: list[InternalTransferIssue] = []
    for ccy, rows in by_ccy.items():
        counts = Counter(round(abs(float(t["amount"])), 2) for t in rows)
        orphan_mags = {mag for mag, n in counts.items() if n % 2 == 1}
        if not orphan_mags:
            continue
        for t in rows:
            if round(abs(float(t["amount"])), 2) in orphan_mags:
                issues.append(
                    InternalTransferIssue(
                        currency=ccy,
                        amount=float(t["amount"]),
                        txn_id=str(t.get("txn_id", "")),
                        description=str(t.get("description", "")),
                    )
                )
    issues.sort(key=lambda x: (x.currency, x.amount))
    return issues


def find_internal_transfer_orphans(
    ir: ParsedStatement | dict[str, Any] | list[Any],
) -> IrVerificationReport:
    """Read-only verification: detect unpaired internal transfers.

    Does not mutate the input. Returns an :class:`IrVerificationReport`.
    """
    txns = _iter_txns(ir)
    currencies = sorted({t["currency"] for t in txns if t.get("is_internal_transfer")})
    return IrVerificationReport(
        internal_transfer_issues=_detect_orphans(txns),
        currencies_checked=currencies,
    )


def reconcile_internal_transfers(
    ir: ParsedStatement | dict[str, Any] | list[Any],
) -> IrVerificationReport:
    """Fixer: demote orphan internal-flagged rows in place.

    Rows whose internal flag has no equal-magnitude opposite leg are re-classified
    by clearing ``is_internal_transfer`` (they fall back to their real
    Income/Expense class downstream). Returns the report of what was changed.

    When ``ir`` is a ``ParsedStatement`` or dict, the underlying objects are
    mutated. When it is a ``list[Txn]`` (plain dicts), the dicts are mutated.
    """
    txns = _iter_txns(ir)
    issues = _detect_orphans(txns)
    orphan_keys = {(i.currency, round(abs(i.amount), 2), i.txn_id) for i in issues}
    for t in txns:
        key = (t["currency"], round(abs(float(t["amount"])), 2), str(t.get("txn_id", "")))
        if key in orphan_keys:
            t["is_internal_transfer"] = False
    currencies = sorted({t["currency"] for t in txns if t.get("is_internal_transfer")})
    return IrVerificationReport(
        internal_transfer_issues=issues,
        currencies_checked=currencies,
    )


def _self_reference_accounts(description: str, own_digits: set[str]) -> set[str]:
    """Return the set of the user's own account numbers (digit-only) that appear
    as a *whole* digit run in ``description``.

    A description referencing one of the user's own account numbers is a candidate
    inter-account transfer (even when the consolidation module did not flag it
    ``is_internal_transfer``). Matching is deliberately strict: an account number
    must appear as a contiguous digit run, not as a substring buried inside a
    longer transaction-reference number — a naive substring check would, e.g.,
    match a 10-digit account number inside a 20-digit payment reference.

    Returns the *set* of matched account numbers (may be empty) rather than a bare
    bool, so callers can validate the match against a partner leg.
    """
    if not description or not own_digits:
        return set()
    digit_runs = set(re.findall(r"\d+", description))
    return {no for no in own_digits if no in digit_runs}


def promote_internal_transfers(
    rows: list[dict[str, Any]],
    own_digits: set[str],
    amount_key: str = "amount",
    desc_key: str = "description",
    flag_key: str = "is_internal_transfer",
    tol: float = 0.01,
) -> None:
    """Finalize ``is_internal_transfer`` for candidate self-reference rows.

    An internal transfer is a *relationship* between two legs, not a property of a
    single description. A lone ``Misc Debit … 8995976591`` whose reference number
    merely coincides with an own account number must NOT be promoted to a transfer
    unless an opposite-sign, equal-magnitude partner leg referencing the same
    account number exists elsewhere in the same IR. Requiring the partner prevents
    the false-positive self-reference gap (e.g. a phantom −100.00 transfer with no
    +100.00 inbound).

    This is the canonical owner of self-reference promotion; it was relocated from
    ``pfa-analysis`` so that all internal-transfer reconciliation lives in
    ``pfa-ir-verifier``.

    Mutates ``rows`` in place: rows already ``flag_key=True`` are left untouched
    (the consolidator is authoritative); candidate rows are promoted only when a
    valid partner is found.
    """
    # Index candidate-self-reference rows by the matched account number.
    by_acct: dict[str, list[int]] = {}
    ref_accounts: list[set[str]] = []
    for i, r in enumerate(rows):
        if r.get(flag_key):
            ref_accounts.append(set())  # already internal; not a candidate
            continue
        matched = _self_reference_accounts(str(r.get(desc_key, "")), own_digits)
        ref_accounts.append(matched)
        for acct_no in matched:
            by_acct.setdefault(acct_no, []).append(i)

    if not by_acct:
        return

    for i, r in enumerate(rows):
        matched = ref_accounts[i]
        if not matched or r.get(flag_key):
            continue
        amt = float(r.get(amount_key, 0.0))
        promoted = False
        for acct_no in matched:
            for j in by_acct.get(acct_no, []):
                if j == i:
                    continue
                partner = rows[j]
                # The partner need not already be flagged: a genuine internal
                # transfer is a pair of legs that both reference the same own
                # account with opposite sign and equal magnitude. Either leg may
                # be the candidate here.
                if not partner.get(flag_key) and acct_no not in ref_accounts[j]:
                    continue
                p_amt = float(partner.get(amount_key, 0.0))
                if amt * p_amt < 0 and abs(abs(amt) - abs(p_amt)) <= tol:
                    promoted = True
                    break
            if promoted:
                break
        r[flag_key] = promoted


def verify_txn_links(statement: ParsedStatement) -> ParsedStatement:
    """Verify transaction link integrity (moved here from ``pfa-parser``).

    Two independent checks apply:

    * **Orphan detection** — a transaction flagged ``is_internal_transfer`` with
      an empty ``linked_txn_ids`` list means the linker failed to find the other
      side, which would let the move be double-counted downstream.
    * **Symmetry verification** — ANY transaction (regardless of
      ``is_internal_transfer``) that carries entries in ``linked_txn_ids`` must be
      reciprocal: if A marks B as related, B must also mark A. A one-sided link
      indicates the linker only updated one side (e.g. a missed twin).

    Runs on every pipeline path (fresh extraction and IR reload) so a saved
    ``.ir.json`` whose links were lost is still flagged. Idempotent: an
    already-present warning is not re-appended.

    Mutates and returns *statement*. Link problems are recorded in
    ``statement.warnings`` (not in the verification report, since this operates on
    the ``ParsedStatement`` model directly).
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


def verify_ir(path_or_ir: str | Path | ParsedStatement | dict[str, Any] | list[Any]) -> IrVerificationReport:
    """Convenience: verify a JSON file path or an in-memory IR.

    For file paths the IR is parsed with :func:`pfa_ir_schema.from_json` so the
    canonical schema contract is enforced before verification. Runs the full
    internal-transfer check: link integrity (when a ``ParsedStatement``), orphan
    detection, and reconciliation.

    Note: when ``path_or_ir`` is a ``ParsedStatement``, link warnings are written
    to ``statement.warnings``; the returned report only carries the
    internal-transfer orphan issues.
    """
    ir = path_or_ir
    if isinstance(ir, (str, Path)):
        ir = from_json(Path(ir).read_text(encoding="utf-8"))
    if isinstance(ir, ParsedStatement):
        verify_txn_links(ir)
    return find_internal_transfer_orphans(ir)
