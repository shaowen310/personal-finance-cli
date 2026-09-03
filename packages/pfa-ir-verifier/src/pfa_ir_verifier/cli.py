"""Command-line entry point for the IR verifier.

Usage
-----
    python -m pfa_ir_verifier verify <ir.json> [--fix] [--json]

* default (check mode): report unpaired internal transfers and exit non-zero
  if any are found (CI-friendly).
* ``--fix``: demote the orphan rows in place and rewrite the IR file.
* ``--json``: emit the machine-readable :class:`IrVerificationReport`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .verify import demote_orphan_internal_transfers, verify_ir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verify a personal-finance IR.")
    sub = p.add_subparsers(dest="command", required=True)

    vp = sub.add_parser("verify", help="Check internal-transfer reconciliation")
    vp.add_argument("ir", help="Path to a consolidated IR JSON file")
    vp.add_argument("--fix", action="store_true",
                    help="Demote unpaired internal-transfer rows in place and rewrite the file")
    vp.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    args = p.parse_args(argv)

    if args.command == "verify":
        return _cmd_verify(args.ir, fix=args.fix, as_json=args.json)
    return 2


def _cmd_verify(ir_path: str, *, fix: bool, as_json: bool) -> int:
    path = Path(ir_path)
    if fix:
        # Load once, demote orphan internal transfers in place, persist the healed ParsedStatement.
        from pfa_ir_schema import from_json
        stmt = from_json(path.read_text(encoding="utf-8"))
        report = demote_orphan_internal_transfers(stmt)
        if not report.ok:
            path.write_text(stmt.to_json(), encoding="utf-8")
    else:
        report = verify_ir(path)

    if as_json:
        print(json.dumps(_report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(report.render_text())

    return 0 if report.ok else 1


def _report_to_dict(report) -> dict:
    return {
        "ok": report.ok,
        "orphan_count": report.orphan_count,
        "currencies_checked": report.currencies_checked,
        "internal_transfer_issues": [
            {
                "currency": i.currency,
                "amount": i.amount,
                "txn_id": i.txn_id,
                "description": i.description,
                "reason": i.reason,
            }
            for i in report.internal_transfer_issues
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
