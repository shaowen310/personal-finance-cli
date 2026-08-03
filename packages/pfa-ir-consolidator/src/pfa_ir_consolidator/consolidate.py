#!/usr/bin/env python3
"""consolidate.py — merge multiple sg-bank-pdf-parser IR JSON files.

Reads N ``*.ir.json`` (``ParsedStatement``) files, merges accounts grouped by
``(institution, account_no, name)``, de-duplicates transactions by ``txn_id``,
and writes a single consolidated ``ParsedStatement`` as ``consolidated.ir.json``.

Usage:
    python consolidate.py a.ir.json b.ir.json -o consolidated.ir.json
    python consolidate.py *.ir.json -o consolidated.ir.json --min-ir-version 2026.4
"""
from __future__ import annotations

import argparse
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a standalone script from scripts/ or the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pfa_ir_consolidator._parser_loader import load_parser_modules  # noqa: E402
from pfa_ir_consolidator.detect_transfers import (  # noqa: E402
    detect_cc_payments,
    detect_currency_conversions,
    detect_inter_bank_transfers,
    detect_intra_bank_transfers,
)

VERSION = "0.1.0"
DEFAULT_MIN_IR_VERSION = "2026.4"


def _version_ge(a: str, b: str) -> bool:
    def _parts(v: str) -> list[int]:
        return [int(x) for x in v.split(".") if x.isdigit()]

    return _parts(a) >= _parts(b)


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


def _merge_fd(accs: list[Any]) -> list[Any] | None:
    """Concatenate fd_records across statements, de-dup by deposit_no."""
    recs: list[Any] = []
    seen: set[str] = set()
    for acc in accs:
        for r in acc.fd_records or []:
            if r.deposit_no and r.deposit_no in seen:
                continue
            if r.deposit_no:
                seen.add(r.deposit_no)
            recs.append(r)
    return recs or None


def _merge_inv(accs: list[Any]) -> list[Any] | None:
    """Concatenate investment_holdings across statements, de-dup by name."""
    recs: list[Any] = []
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
    stmts_with_paths: list[tuple[str, Any]],
    ir: types.ModuleType,
    do_dedup: bool,
) -> tuple[Any, int, int, int]:
    """``stmts_with_paths`` is a list of (path, ParsedStatement)."""
    groups: dict[tuple[str, str, str], list[Any]] = {}
    # Track the source statement meta for each account key so we can fill
    # period_from/period_to on the merged Account.
    key_periods: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}
    sources: list[dict[str, Any]] = []
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
        def _txn_sort_key(t: Any) -> tuple[Any, Any]:
            return (t.posted_date, t.txn_id)

        txns.sort(key=_txn_sort_key)

        extras = dict(base.extras or {})

        p_from, p_to = key_periods.get((_inst, _no, _currency), (None, None))

        merged_accounts.append(
            ir.Account(
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
    meta = ir.StatementMeta(
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

    consolidated = ir.ParsedStatement(
        ir_version=min_ir,
        parsed_at=datetime.now(timezone.utc).isoformat(),
        parser=ir.ParserInfo(name="bank-ir-consolidate", version=VERSION),
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
    return consolidated, total_txns_in, deduped, filtered


def main() -> None:
    ap = argparse.ArgumentParser(description="Consolidate sg-bank-pdf-parser IR JSON files.")
    _ = ap.add_argument("inputs", nargs="+", help="Input *.ir.json files")
    _ = ap.add_argument("-o", "--out", default="consolidated.ir.json", help="Output IR JSON path")
    _ = ap.add_argument("--parser-dir", default=None, help="Path to sg-bank-pdf-parser skill dir")
    _ = ap.add_argument("--min-ir-version", default=DEFAULT_MIN_IR_VERSION, help="Minimum accepted ir_version")
    _ = ap.add_argument("--no-dedup", action="store_true", help="Disable txn_id de-duplication")
    _ = ap.add_argument("--indent", type=int, default=2, help="JSON indent")
    args = ap.parse_args()

    pm = load_parser_modules(args.parser_dir)
    ir = pm.ir_schema

    stmts_with_paths: list[tuple[str, Any]] = []
    for path in args.inputs:
        text = Path(path).read_text(encoding="utf-8")
        try:
            stmt = ir.from_json(text)
        except ValueError as e:
            sys.exit(f"[error] {path}: {e}")
        if not _version_ge(stmt.ir_version, args.min_ir_version):
            sys.exit(
                f"[error] {path}: ir_version {stmt.ir_version!r} < required "+
                f"{args.min_ir_version!r}"
            )
        stmts_with_paths.append((str(path), stmt))

    consolidated, total_in, deduped, filtered = consolidate_statements(
        stmts_with_paths, ir, do_dedup=not args.no_dedup
    )
    consolidated = detect_inter_bank_transfers(consolidated)
    consolidated = detect_intra_bank_transfers(consolidated)
    consolidated = detect_currency_conversions(consolidated)
    consolidated = detect_cc_payments(consolidated)
    consolidated = pm.postprocess.verify_txn_links(consolidated)

    transfers = (consolidated.extras or {}).get("consolidation", {}).get("transfers", {})
    inter_bank_detected = transfers.get("inter_bank_detected", 0)
    intra_bank_detected = transfers.get("intra_bank_detected", 0)
    cc_detected = transfers.get("currency_conversion_detected", 0)
    cc_payments_detected = transfers.get("cc_payments_detected", 0)

    out = Path(args.out)
    _ = out.write_text(consolidated.to_json(indent=args.indent), encoding="utf-8")
    total_out = total_in - deduped - filtered
    print(f"Wrote {out}")
    print(
        f"  inputs={len(stmts_with_paths)} accounts={len(consolidated.accounts)} "+
        f"txns_in={total_in} txns_out={total_out} deduped={deduped} filtered={filtered}"
    )
    if inter_bank_detected:
        print(f"  inter_bank_transfers={inter_bank_detected} pairs")
    if intra_bank_detected:
        print(f"  intra_bank_transfers={intra_bank_detected} pairs")
    if cc_detected:
        print(f"  currency_conversion_transfers={cc_detected} pairs")
    if cc_payments_detected:
        print(f"  cc_payments={cc_payments_detected} pairs")


if __name__ == "__main__":
    main()
