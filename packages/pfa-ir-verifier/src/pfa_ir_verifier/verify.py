"""IR verification utilities.

These checks operate on the shared ``pfa-ir-schema`` contract: a consolidated
IR is a ``ParsedStatement`` (accounts -> transactions), but the core routines
also accept a flat ``list[dict]`` of transaction rows (the shape
``pfa-analysis`` passes around internally) so they can be reused without
pulling in the analysis stack.

Naming conventions
------------------
Functions in this module (and the sibling linking passes in
``pfa-ir-consolidator``) follow a verb-based contract so callers can tell at a
glance whether a pass mutates the IR or merely reports on it:

* ``link_*`` / ``promote_*`` / ``demote_*`` — **mutating**. They write actual IR
  data (``is_internal_transfer``, ``linked_txn_ids``, ``link_labels``, or even add
  / remove transactions) and return the mutated ``ParsedStatement``. ``link_*``
  lives in ``pfa-ir-consolidator`` (e.g. ``link_transfers.py``) and the parser's
  ``link_fd_to_ca``; ``promote_*`` / ``demote_*`` are this module's in-place fixers.
* ``find_*`` — **read-only**. Detects problems and returns a structured
  ``IrVerificationReport``; it never mutates the input.
* ``verify_*`` — **read-only, warning-only**. Checks the IR and appends
  human-facing messages to ``statement.warnings``, returning the (otherwise
  unchanged) ``ParsedStatement``. A ``verify_*`` pass must NOT write link/data
  fields — appending a warning is *observability*, not mutation.

Public API
----------
* :class:`IrVerificationReport` — structured result of a verification run.
* :func:`find_internal_transfer_orphans` — read-only check; finds internal
  transfers that have no equal-magnitude opposite leg.
* :func:`demote_orphan_internal_transfers` — fixer; demotes orphan internal-flagged
  rows in place and returns what changed.
* :func:`promote_internal_transfers` — fixer; promotes description-based
  self-reference rows to internal transfers only when a valid partner leg exists.
* :func:`verify_txn_links` — link-integrity check (orphan + reciprocal symmetry).
* :func:`verify_account_balances` — account-level balance consistency check
  (closing vs opening + Σtxns, and vs last ``balance_after``).
* :func:`verify_fd_interest_amounts` — per-FD interest-amount check that
  delegates to :func:`_fd_interest_mismatch` for every ``fd_record``.
* :func:`verify_statement_meta` — required-metadata completeness check
  (``statement_meta.institution`` must be present and non-empty).
* :func:`verify_ir` — convenience loader + full check for a JSON file or
  ``ParsedStatement`` (links + internal-transfer reconciliation).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict, cast

from pfa_ir_schema import AccountType, ParsedStatement, Transaction, from_json
from pfa_ir_schema.common import _fd_day_count, _parse_fd_date, parse_fd_rate


# A transaction row as understood by the verifier. ``pfa-analysis`` passes plain
# dicts; ``ParsedStatement`` is flattened to the same shape. The canonical row is
# the ``Txn`` TypedDict below. A ``_TxnView`` wraps a ``Transaction`` dataclass so
# the verifier can mutate a ``ParsedStatement`` in place (writes propagate back to
# the source object); plain dict rows are mutated directly.
class Txn(TypedDict):
    """A transaction row (canonical shape shared by dict rows and ``Transaction``)."""

    currency: str
    amount: float
    is_internal_transfer: bool
    txn_id: str
    description: str
    account_type: str
    linked_txn_ids: list[str]
    link_labels: list[str]


# A loose flat transaction row as passed by callers. ``pfa-analysis``'s
# ``RawRowForVerifier`` and ``pfa-ir-consolidator``'s ``_PromoteRow`` are both
# structurally this shape (the inferred element type of their row lists). The
# fixers only read/write a small fixed set of string/number/flag/label keys, so
# a permissive value type is sufficient and callers need not ``cast`` at every
# call site.
RawRow = dict[str, str | float | bool | None | list[str]]


# Any IR input the verifier accepts: a ParsedStatement, a raw IR dict, a flat
# list of txn rows, or a file path. The dynamic branches use the *covariant*
# abstract containers ``Mapping[str, object]`` / ``Sequence[object]`` rather than
# ``dict[str, Any]`` / ``list[Any]``: the flat row shape is shared across packages
# as *different* TypedDicts (e.g. ``pfa-analysis``'s ``TxnRow``), and covariance
# lets those callers assign without casts (invariant ``dict``/``list`` would force
# a cast at every call site). ``RawRow`` (below) is the concrete flat-row shape
# accepted by ``promote_internal_transfers``.
IrInput = (
    ParsedStatement
    | Mapping[str, object]
    | Sequence[object]
    | str
    | Path
)


class _TxnView(dict):
    """dict-like view over a ``Transaction`` dataclass.

    Allows the verifier to read/write transaction fields uniformly whether the
    underlying row is a plain dict (loaded IR) or a ``Transaction`` dataclass
    (ParsedStatement). Writes propagate back to the source object.
    """

    def __init__(self, obj: Transaction) -> None:
        super().__init__(
            {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        )
        self._o = obj

    def __setitem__(self, key: str, value: object) -> None:
        super().__setitem__(key, value)
        setattr(self._o, key, value)


def _iter_txns(ir: IrInput) -> list[Txn]:
    """Normalise any supported IR input into a flat list of txn dicts.

    Accepts a ``ParsedStatement``, a plain ``dict`` (accounts/transactions), a
    flat ``list[dict]``, or a file path (loaded via ``pfa_ir_schema.from_json``).
    """
    if isinstance(ir, (str, Path)):
        ir = from_json(Path(ir).read_text(encoding="utf-8"))
    if isinstance(ir, list):
        # Caller's dict rows are mutated in place (no copy), so fixes propagate.
        return cast(list[Txn], ir)
    if isinstance(ir, ParsedStatement):
        # Return views backed by the dataclasses so that in-place mutations
        # (e.g. clearing is_internal_transfer) propagate to the source IR.
        return cast(list[Txn], [_TxnView(t) for acct in ir.accounts for t in acct.transactions])
    if isinstance(ir, dict):
        if "accounts" in ir:
            rows: list[Txn] = []
            accounts = ir.get("accounts")
            if isinstance(accounts, list):
                for acct in accounts:
                    if not isinstance(acct, dict):
                        continue
                    atype = str(acct.get("account_type", ""))
                    txns = acct.get("transactions")
                    if isinstance(txns, list):
                        for t in txns:
                            if not isinstance(t, dict):
                                continue
                            d: dict[str, object] = dict(t)
                            d.setdefault("account_type", atype)
                            rows.append(cast(Txn, d))
            return rows
        if "transactions" in ir:  # legacy flat format
            raw = ir.get("transactions")
            if isinstance(raw, list):
                return [cast(Txn, t) for t in raw if isinstance(t, dict)]
    raise TypeError(f"Unsupported IR input type: {type(ir)!r}")


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
    """Return internal-flagged rows lacking a valid twin.

    Internal transfers always move money between two of the holder's own
    accounts, so for every currency the internal legs must pair up. A row is an
    orphan when it has no twin, detected in either of two ways:

    * *Magnitude parity* -- within a currency the legs must pair by equal
      magnitude and net to zero; a magnitude with an odd count has an unpaired
      leg. Independent of cash-flow direction (robust to In/Out mis-class).
    * *Existing links* -- a flagged row whose ``linked_txn_ids`` references a
      *present* transaction is already a validated pair (e.g. a credit-card bill
      payment whose bank and card legs are linked but stored with the same sign,
      so magnitude parity alone would wrongly flag one side). Not an orphan even
      if only one side is flagged as internal.
    """
    present_ids = {str(t.get("txn_id", "")) for t in txns}
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
        # Rows already linked to a present partner are valid pairs, even if only
        # one side is flagged (orphan detection would otherwise see a single leg).
        linked_pairs = {
            str(t.get("txn_id", ""))
            for t in rows
            if any(str(p) in present_ids for p in (t.get("linked_txn_ids", []) or []))
        }
        for t in rows:
            # Investment transfers are allowed single-leg (the investment account
            # may not record its principal); never treat them as unpaired orphans.
            if "investment_transfer" in (t.get("link_labels", []) or []):
                continue
            if round(abs(float(t["amount"])), 2) in orphan_mags and str(t.get("txn_id", "")) not in linked_pairs:
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
    ir: IrInput,
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


# Account types treated as liabilities (a payment to one of these own accounts is
# a balance-sheet settlement, not new spending). Kept in sync with
# ``pfa-analysis``'s ``_LIABILITY_ACCOUNT_TYPES``.
_LIABILITY_ACCOUNT_TYPES = frozenset({"credit_card", "credit", "card"})


def _acct_digit_runs(description: str) -> set[str]:
    """Contiguous digit runs of length >= 4 (candidate own-account references)."""
    if not description:
        return set()
    return {r for r in re.findall(r"\d+", str(description)) if len(r) >= 4}


def _is_liability_settlement(row: Txn,
                             own_liability_digits: set[str] | None) -> bool:
    """True when *row* is a payment from an asset account to one of the user's own
    liability accounts (e.g. a credit-card bill payment).

    Such a move is an asset<->own-liability settlement, not new spending: the
    underlying expense was already captured when the liability was incurred (the
    card charge). It is therefore an internal transfer and must be excluded from
    operating cash flow. The rule fires *without* a partner leg — the common case
    where only the bank side of the payment is present in the IR — unlike
    self-reference pairing which requires both legs. To avoid mis-flagging a
    card's own charge (a debit on the liability account itself), the row's own
    account type must NOT be a liability.
    """
    if not own_liability_digits:
        return False
    at = str(row.get("account_type", "")).lower()
    if at in _LIABILITY_ACCOUNT_TYPES:
        return False
    amt = float(row.get("amount", 0.0))
    if amt >= 0:  # non-liability accounts: a debit is negative
        return False
    return bool(own_liability_digits & _acct_digit_runs(row.get("description", "")))


def demote_orphan_internal_transfers(
    ir: IrInput,
    own_liability_digits: set[str] | None = None,
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

    def _orphan_key(t) -> tuple:
        return (t["currency"], round(abs(float(t["amount"])), 2), str(t.get("txn_id", "")))

    orphan_keys = {(i.currency, round(abs(i.amount), 2), i.txn_id) for i in issues}
    # Keep single-leg liability settlements promoted by promote_internal_transfers:
    # they have no partner leg by design, so they would otherwise look orphaned.
    for t in txns:
        if _is_liability_settlement(t, own_liability_digits):
            orphan_keys.discard(_orphan_key(t))
    # Keep single-leg investment transfers flagged by link_investment_transfers:
    # they have no partner leg by design, so they would otherwise look orphaned.
    for t in txns:
        if "investment_transfer" in (t.get("link_labels", []) or []):
            orphan_keys.discard(_orphan_key(t))
    for t in txns:
        if _orphan_key(t) in orphan_keys:
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


def _as_str_list(value: object) -> list[str]:
    """Coerce a loosely-typed row value into a ``list[str]`` (best effort)."""
    return value if isinstance(value, list) else []


def promote_internal_transfers(
    rows: list[RawRow],
    own_digits: set[str],
    amount_key: str = "amount",
    desc_key: str = "description",
    flag_key: str = "is_internal_transfer",
    txn_id_key: str = "txn_id",
    links_key: str = "linked_txn_ids",
    labels_key: str = "link_labels",
    tol: float = 0.01,
    own_liability_digits: set[str] | None = None,
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

    Promotion also **cross-links** the discovered pair (sets ``linked_txn_ids``
    bidirectionally and appends a ``"self_reference"`` label on both legs). This
    makes promoted transfers first-class links, consistent with the pairs produced
    by ``link_transfers``, so a subsequent ``verify_txn_links`` pass will *not*
    flag them as orphaned ("transfer without linked twin"). Rows lacking a
    usable ``txn_id`` (e.g. some parser outputs) are still promoted by flag but
    left unlinked, since a link needs an id to resolve.

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
        amt = float(str(r.get(amount_key, 0.0)))
        promoted = False
        partner: RawRow | None = None
        for acct_no in matched:
            for j in by_acct.get(acct_no, []):
                if j == i:
                    continue
                cand = rows[j]
                # The partner need not already be flagged: a genuine internal
                # transfer is a pair of legs that both reference the same own
                # account with opposite sign and equal magnitude. Either leg may
                # be the candidate here.
                if not cand.get(flag_key) and acct_no not in ref_accounts[j]:
                    continue
                p_amt = float(str(cand.get(amount_key, 0.0)))
                if amt * p_amt < 0 and abs(abs(amt) - abs(p_amt)) <= tol:
                    promoted = True
                    partner = cand
                    break
            if promoted:
                break
        if not promoted or partner is None:
            continue
        r[flag_key] = True
        # Cross-link the partner pair so downstream link verification treats it
        # as a proper internal transfer.
        id_i = str(r.get(txn_id_key, "") or "").strip()
        id_j = str(partner.get(txn_id_key, "") or "").strip()
        if id_i and id_j:
            if id_j not in _as_str_list(r.setdefault(links_key, [])):
                r[links_key] = _as_str_list(r[links_key]) + [id_j]
            if "self_reference" not in _as_str_list(r.setdefault(labels_key, [])):
                r[labels_key] = _as_str_list(r[labels_key]) + ["self_reference"]
            if id_i not in _as_str_list(partner.setdefault(links_key, [])):
                partner[links_key] = _as_str_list(partner[links_key]) + [id_i]
            if "self_reference" not in _as_str_list(partner.setdefault(labels_key, [])):
                partner[labels_key] = _as_str_list(partner[links_key]) + ["self_reference"]


    # Asset -> own-liability settlements (e.g. credit-card bill payments). These
    # are internal transfers even without a partner leg: the spend was already an
    # expense at charge time, so the payment must be excluded from operating cash
    # flow. demote_orphan_internal_transfers honours the same rule and will not
    # strip these single-leg promotions (they have no twin by design).
    if own_liability_digits:
        id_index = {str(r.get(txn_id_key, "")): r for r in rows}
        for r in rows:
            if r.get(flag_key):
                continue
            if _is_liability_settlement(cast(Txn, r), own_liability_digits):
                r[flag_key] = True
                # Flag any linked partner so both legs are internal transfers.
                # Otherwise the partner (e.g. the card's credit leg) stays
                # unflagged, gets mis-classified as income/expense downstream, and
                # the promoted leg would look like a single-leg orphan. When the
                # partner is absent (only one leg in the IR) the demote guard on
                # _is_liability_settlement still protects the promoted leg.
                for pid in _as_str_list(r.get(links_key, [])):
                    partner = id_index.get(str(pid))
                    if partner is not None and not partner.get(flag_key):
                        partner[flag_key] = True


def _fd_interest_mismatch(
    principal: float,
    interest_rate: str | float | None,
    value_date: str | None,
    maturity_date: str | None,
    interest_amount: float | None,
    *,
    tol: float = 0.01,
    institution: str | None = None,
    day_count: int | None = None,
) -> str | None:
    """Return a warning string if ``principal × rate × period ≠ interest_amount``.

    Skips (returns ``None``) when ``interest_amount`` is ``None`` (e.g. OCBC),
    the rate is unparseable, or the dates are missing/invalid/non-positive.
    Period (years) is ``(maturity - value).days / basis`` where ``basis`` follows
    the institution's day-count convention (actual/365 by default; actual/360 for
    ICBC). ``day_count`` overrides the institution lookup when provided.

    ``interest_rate`` may be either an already-decimal value (``0.025``) or a
    raw string (``"2.5%"``); a decimal is passed through directly.
    """
    if interest_amount is None:
        return None
    if isinstance(interest_rate, (int, float)):
        rate = float(interest_rate)
    else:
        rate = parse_fd_rate(interest_rate)
    if rate is None:
        return None
    vd = _parse_fd_date(value_date)
    mat = _parse_fd_date(maturity_date)
    if vd is None or mat is None or mat <= vd:
        return None
    basis = day_count if (isinstance(day_count, int) and day_count > 0) else _fd_day_count(institution)
    period_years = (mat - vd).days / basis
    expected = principal * rate * period_years
    if abs(interest_amount - expected) > tol:
        return (
            f"FD interest mismatch: principal {principal:,.2f} × rate {rate:.6f} "
            f"× period {period_years:.6f}y (actual/{basis}d) = {expected:,.2f}, "
            f"but stated interest amount is {interest_amount:,.2f} "
            f"(diff={abs(interest_amount - expected):.2f})"
        )
    return None


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
            # Single-leg investment transfers (label "investment_transfer") are
            # allowed without a twin, so don't warn about a missing link.
            if (txn.is_internal_transfer and not txn.linked_txn_ids
                    and "investment_transfer" not in txn.link_labels):
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


def verify_ir(path_or_ir: IrInput) -> IrVerificationReport:
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
                        f"({acct.account_no}, {acct.currency or '?'}): "
                        f"closing_balance={acct.closing_balance:,.2f} "
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
                        f"({acct.account_no}, {acct.currency or '?'}): "
                        f"closing_balance={acct.closing_balance:,.2f} "
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
                        f"({acct.account_no}, {acct.currency or '?'}): "
                        f"opening_balance={acct.opening_balance:,.2f} "
                        f"but closing_balance={acct.closing_balance:,.2f} "
                        f"(no transactions to explain difference)"
                    )
                    if warn not in statement.warnings:
                        statement.warnings.append(warn)

    return statement


def verify_fd_interest_amounts(statement: ParsedStatement) -> ParsedStatement:
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
            warn = _fd_interest_mismatch(
                principal=rec.principal,
                interest_rate=rec.interest_rate,
                value_date=rec.value_date,
                maturity_date=rec.maturity_date,
                interest_amount=rec.interest_amount,
                institution=statement.statement_meta.institution,
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
