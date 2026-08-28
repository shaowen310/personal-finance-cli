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
* :func:`verify_ir` — convenience loader + check for a JSON file or
  ``ParsedStatement``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pfa_ir_schema import ParsedStatement, from_json

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

    @property
    def orphan_count(self) -> int:
        return len(self.internal_transfer_issues)

    @property
    def ok(self) -> bool:
        return self.orphan_count == 0

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


def verify_ir(path_or_ir: str | Path | ParsedStatement | dict[str, Any] | list[Any]) -> IrVerificationReport:
    """Convenience: verify a JSON file path or an in-memory IR.

    For file paths the IR is parsed with :func:`pfa_ir_schema.from_json` so the
    canonical schema contract is enforced before verification.
    """
    if isinstance(path_or_ir, (str, Path)):
        return find_internal_transfer_orphans(from_json(Path(path_or_ir).read_text(encoding="utf-8")))
    return find_internal_transfer_orphans(path_or_ir)
