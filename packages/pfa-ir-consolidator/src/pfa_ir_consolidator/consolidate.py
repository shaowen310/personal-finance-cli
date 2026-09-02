#!/usr/bin/env python3
"""consolidate.py — merge multiple IR JSON files.

Reads N ``*.ir.json`` (``ParsedStatement``) files, merges accounts grouped by
``(institution, account_no, name)``, de-duplicates transactions by ``txn_id``,
and writes a single consolidated ``ParsedStatement`` as ``consolidated.ir.json``.

This module is a library; the CLI driver lives in the ``pfa-cli`` app
(``apps/pfa-cli``), which calls ``consolidate_statements``.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast

# Allow running as a standalone script from scripts/ or the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pfa_ir_schema import (  # noqa: E402
    Account,
    FixedDepositRecord,
    InvestmentHolding,
    ParserInfo,
    ParsedStatement,
    StatementMeta,
    Transaction
)

from pfa_ir_consolidator.link_transfers import (  # noqa: E402
    link_cc_payments,
    link_currency_conversions,
    link_inter_bank_transfers,
    link_intra_bank_transfers,
    link_investment_transfers,
)
# Internal-transfer verification (promotion, link integrity, orphan reconcile)
# now lives in pfa-ir-verifier — the canonical home for all IR verification.
# Apply the full ordered pipeline here so the persisted consolidated IR already
# carries correct flags/links and any surviving orphans are demoted.
from pfa_ir_verifier import (  # noqa: E402
    demote_orphan_internal_transfers,
    promote_internal_transfers,
    verify_txn_links,
)

VERSION = "0.1.0"
DEFAULT_MIN_IR_VERSION = "2026.4"


class _PromoteRow(TypedDict):
    """Row shape consumed by ``pfa_ir_verifier.promote_internal_transfers``."""

    amount: float
    description: str
    is_internal_transfer: bool
    txn_id: str
    linked_txn_ids: list[str]
    link_labels: list[str]


def _own_account_digits(accounts: list[Account]) -> set[str]:
    """Return the digit-only forms of every account number across ``accounts``.

    Used by self-reference promotion to recognise descriptions that reference
    one of the user's own accounts (a candidate inter-account transfer even when
    the detector did not flag it).
    """
    digits: set[str] = set()
    for acc in accounts:
        no = (getattr(acc, "account_no", "") or "").strip()
        if no:
            digits.add(re.sub(r"\D", "", no))
    digits.discard("")
    return digits


def _to_iso_date(value: str | None) -> str | None:
    """Best-effort normalization of a period date to ISO YYYY-MM-DD.

    Handles the formats actually emitted by the source extractors:
    ``YYYY-MM-DD`` (already ISO), ``YYYY/MM/DD`` (ICBC), and ``DD Mon YYYY``
    (OCBC consolidated). Returns the original string untouched if no pattern
    matches, so callers can still surface it (and warn) rather than silently
    dropping it.
    """
    if not value:
        return None
    s = str(value).strip()
    s_slash = s.replace("/", "-").replace(" ", "-")
    parts = s_slash.split("-")
    if len(parts) == 3:
        a, b, c = parts
        # ISO or slash form: all numeric → YYYY-MM-DD.
        if a.isdigit() and b.isdigit() and c.isdigit():
            return f"{a}-{b.zfill(2)}-{c.zfill(2)}"
        # OCBC consolidated form "DD Mon YYYY" (e.g. "30-JUN-2026"): middle
        # token is an alphabetic month abbreviation.
        if a.isdigit() and b.isalpha() and c.isdigit():
            try:
                return datetime.strptime(f"{a}-{b}-{c}", "%d-%b-%Y").strftime("%Y-%m-%d")
            except ValueError:
                return value
    return value


def _is_iso_date(value: str | None) -> bool:
    """Return True iff ``value`` is a valid ISO ``YYYY-MM-DD`` date."""
    if not value:
        return False
    try:
        _ = datetime.strptime(str(value).strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _merge_fd(accs: list[Account]) -> list[FixedDepositRecord] | None:
    """Concatenate fd_records across statements, de-dup by deposit_no."""
    recs: list[FixedDepositRecord] = []
    seen: set[str] = set()
    for acc in accs:
        for r in acc.fd_records or []:
            if r.deposit_no and r.deposit_no in seen:
                continue
            if r.deposit_no:
                seen.add(r.deposit_no)
            recs.append(r)
    return recs or None


def _merge_inv(accs: list[Account]) -> list[InvestmentHolding] | None:
    """Concatenate investment_holdings across statements, de-dup by name."""
    recs: list[InvestmentHolding] = []
    seen: set[str] = set()
    for acc in accs:
        for h in acc.investment_holdings or []:
            if h.name and h.name in seen:
                continue
            if h.name:
                seen.add(h.name)
            recs.append(h)
    return recs or None


def consolidate_statements(
    stmts_with_paths: list[tuple[str, ParsedStatement]],
    do_dedup: bool,
) -> tuple[ParsedStatement, int, int, int]:
    """``stmts_with_paths`` is a list of (path, ParsedStatement)."""
    groups: dict[tuple[str, str, str], list[Account]] = {}
    # Track the source statement meta for each account key so we can fill
    # period_from/period_to on the merged Account.
    key_periods: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}
    sources: list[dict[str, str | int]] = []
    total_txns_in = 0

    for src_path, stmt in stmts_with_paths:
        meta = stmt.statement_meta
        sources.append(
            {
                "source_file": stmt.source_file or src_path,
                "parser": f"{stmt.parser.name} {stmt.parser.version}".strip(),
                "parsed_at": stmt.parsed_at,
                "ir_version": stmt.ir_version,
                "institution": meta.institution,
                "n_accounts": len(stmt.accounts),
                "n_txns": sum(len(a.transactions) for a in stmt.accounts),
            }
        )
        total_txns_in += sum(len(a.transactions) for a in stmt.accounts)
        for acc in stmt.accounts:
            key = (meta.institution, acc.account_no, acc.currency)
            groups.setdefault(key, []).append(acc)
            # Record the source statement period for this account (first
            # source wins; later duplicates of the same account should carry
            # the same period).
            if key not in key_periods:
                key_periods[key] = (meta.period_from, meta.period_to)

    merged_accounts = []
    deduped = 0
    filtered = 0
    for (_inst, _no, _currency), accs in groups.items():
        base = accs[0]
        txns = []
        seen: set[str] = set()
        for acc in accs:
            for t in acc.transactions:
                # Drop transactions with no description AND zero amount.
                if (not (t.description or "").strip()) and t.amount == 0:
                    filtered += 1
                    continue
                if do_dedup and t.txn_id:
                    if t.txn_id in seen:
                        deduped += 1
                        continue
                    seen.add(t.txn_id)
                txns.append(t)
        def _txn_sort_key(t: Transaction) -> tuple[str, str]:
            return (t.posted_date, t.txn_id)

        txns.sort(key=_txn_sort_key)

        extras = dict(base.extras or {})

        p_from, p_to = key_periods.get((_inst, _no, _currency), (None, None))

        merged_accounts.append(
            Account(
                name=base.name,
                account_no=base.account_no,
                account_type=base.account_type,
                currency=base.currency,
                account_holder=base.account_holder,
                institution=_inst,
                period_from=p_from,
                period_to=p_to,
                opening_balance=base.opening_balance,
                closing_balance=base.closing_balance,
                transactions=txns,
                fd_records=_merge_fd(accs),
                investment_holdings=_merge_inv(accs),
                extras=extras,
            )
        )

    # Post-pass: promote description-based self-reference rows to internal
    # transfers. The detector only flags transfers it could actually link; some
    # genuine own-account moves (e.g. a generic "Transfer" whose legs weren't
    # auto-paired) carry an own account number in the description but no flag.
    # promote_internal_transfers upgrades them in memory only when a matching
    # opposite-leg partner exists, preventing false-positive self-references,
    # and cross-links the pair so it is a first-class transfer. Running it here
    # means the persisted consolidated IR already carries the correct flags and
    # links. Transactions in ``txns`` are the live objects, so we promote on a
    # parallel dict view and copy the flag / links / labels back.
    own_digits = _own_account_digits(merged_accounts)
    if own_digits:
        flat_rows: list[_PromoteRow] = []
        row_txn: list[Transaction] = []
        for acc in merged_accounts:
            for t in acc.transactions:
                flat_rows.append({
                    "amount": float(t.amount),
                    "description": str(t.description or ""),
                    "is_internal_transfer": bool(t.is_internal_transfer),
                    "txn_id": t.txn_id,
                    "linked_txn_ids": list(t.linked_txn_ids or []),
                    "link_labels": list(t.link_labels or []),
                })
                row_txn.append(t)
        promote_internal_transfers(cast(list[dict[str, str | float | bool | list[str]]], flat_rows), own_digits)
        for row, t in zip(flat_rows, row_txn):
            t.is_internal_transfer = row["is_internal_transfer"]
            t.linked_txn_ids = list(row["linked_txn_ids"])
            t.link_labels = list(row["link_labels"])

    periods_from = [
        s.statement_meta.period_from
        for _, s in stmts_with_paths
        if s.statement_meta.period_from
    ]
    periods_to = [
        s.statement_meta.period_to
        for _, s in stmts_with_paths
        if s.statement_meta.period_to
    ]
    # Normalize every input period to ISO before comparing, so a stray
    # non-ISO value can't win a lexicographic min/max and produce a
    # mixed-format pair (the original bug).
    periods_from_norm = [p for p in (_to_iso_date(x) for x in periods_from) if p]
    periods_to_norm = [p for p in (_to_iso_date(x) for x in periods_to) if p]
    non_iso_periods: list[str] = []
    for raw, norm in zip(periods_from + periods_to, periods_from_norm + periods_to_norm):
        if norm != raw and not (norm or "").startswith(("19", "20")):
            non_iso_periods.append(raw)
    # Collect unique, non-empty institution names from all sources.
    _insts: list[str] = []
    _seen_inst: set[str] = set()
    for _, s in stmts_with_paths:
        inst = s.statement_meta.institution or ""
        inst = inst.strip()
        if inst and inst not in _seen_inst:
            _seen_inst.add(inst)
            _insts.append(inst)
    meta = StatementMeta(
        institution=", ".join(_insts),
        account_holder=None,
        period_from=min(periods_from_norm) if periods_from_norm else None,
        period_to=max(periods_to_norm) if periods_to_norm else None,
        functional_currency="SGD",
    )
    min_ir = min((s.ir_version for _, s in stmts_with_paths), default=DEFAULT_MIN_IR_VERSION)
    warnings: list[str] = []
    for src_path, stmt in stmts_with_paths:
        for w in stmt.warnings:
            warnings.append(f"{stmt.source_file or src_path}: {w}")
    for raw in non_iso_periods:
        warnings.append(f"period date not ISO-normalizable: {raw!r}")
    # Final guard: the consolidated period pair must be ISO. If normalization
    # couldn't make it so, surface a warning rather than emit a mixed/raw value.
    for field in ("period_from", "period_to"):
        val = getattr(meta, field)
        if val is not None and not _is_iso_date(val):
            warnings.append(f"consolidated {field} is not an ISO date: {val!r}")

    consolidated = ParsedStatement(
        ir_version=min_ir,
        parsed_at=datetime.now(timezone.utc).isoformat(),
        parser=ParserInfo(name="bank-ir-consolidate", version=VERSION),
        source_file="",
        statement_meta=meta,
        accounts=merged_accounts,
        warnings=warnings,
        extras={
            "consolidation": {
                "sources": sources,
                "deduped": deduped,
                "filtered": filtered,
                "n_inputs": len(stmts_with_paths),
            }
        },
    )

    # Post-consolidation relationship pipeline. consolidate_statements owns the
    # full post-consolidation flow so callers (CLI, app) call only this function.
    # The link_* passes write txn links / internal-transfer flags; they run
    # before verify+demote so reconciliation sees a fully-linked IR.
    consolidated = link_inter_bank_transfers(consolidated)
    consolidated = link_intra_bank_transfers(consolidated)
    consolidated = link_currency_conversions(consolidated)
    consolidated = link_cc_payments(consolidated)
    consolidated = link_investment_transfers(consolidated)

    # Ordered internal-transfer verification pipeline (runs after link_transfers
    # + the promotion post-pass above, all on the consolidated IR):
    #   1. promote_internal_transfers  -- recover unlinked own-account pairs (done on merged_accounts above; == consolidated.accounts)
    #   2. verify_txn_links           -- link-integrity check on the finalized IR (promoted pairs are cross-linked, so not falsely orphaned)
    #   3. demote_orphan_internal_transfers -- demote any flagged row still lacking a partner leg
    # Running these here means the persisted consolidated IR is fully reconciled.
    verify_txn_links(consolidated)
    demote_orphan_internal_transfers(consolidated)
    return consolidated, total_txns_in, deduped, filtered


def embed_fx_rates(
    consolidated: ParsedStatement,
    as_of: str | None = None,
) -> ParsedStatement:
    """Embed an FX rate block into a consolidated ``ParsedStatement``.

    .. note::
        FX rates are **no longer embedded by default**. The analysis package
        fetches rates as of the reporting cut-off date and caches them in the
        on-disk cache (``pfa_fx.cache``), so the IR stays free of
        build-date snapshots that would mislead readers ("as of 2026-08-18"
        inside a July statement). This helper is kept only for callers that
        explicitly want an inline FX block (e.g. ``--embed-fx``); the app CLI
        calls it only when ``--embed-fx`` is passed.

    The embedded block, when present, is stored in the canonical shape:

        extras.consolidation.fx = {
            "rates_sgd_per_unit": {CCY: SGD per 1 unit, ...},
            "as_of": "YYYY-MM-DD",
            "source": "frankfurter ...",
        }

    ``as_of`` defaults to the consolidated statement's period end (``period_to``)
    then period start (``period_from``). The FX block is attached in place and
    the (mutated) statement is returned so the caller can persist it.
    """
    if as_of is None:
        meta = consolidated.statement_meta
        as_of = meta.period_to or meta.period_from or None

    rates_sgd_per_unit: dict[str, float] = {}
    source = "pfa_fx default"
    try:
        from pfa_fx.wrapper import fetch_fx_rates

        fx = fetch_fx_rates(as_of)
        if fx and fx.get("rates"):
            # The wrapper rates are SGD-per-unit already; strip the base (SGD=1.0)
            # so the embedded block only carries non-trivial foreign rates.
            rates_sgd_per_unit = {
                k: float(v) for k, v in fx["rates"].items() if k != "SGD"
            }
            source = fx.get("source", source)
            as_of = as_of or fx.get("date", "")
    except Exception as e:  # noqa: BLE001 - never block consolidation on FX failure
        print(f"[WARN] embed_fx_rates: FX fetch failed: {e}", file=sys.stderr)

    extras = dict(consolidated.extras or {})
    consolid = dict(extras.get("consolidation") or {})
    consolid["fx"] = {
        "rates_sgd_per_unit": rates_sgd_per_unit,
        "as_of": as_of or "",
        "source": source,
    }
    extras["consolidation"] = consolid
    consolidated.extras = extras
    return consolidated
