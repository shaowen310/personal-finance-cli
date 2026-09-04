#!/usr/bin/env python3
"""export_model.py — serialize a consolidated ParsedStatement to TxnTableModel JSON.

Produces the interchange artifact consumed by the standalone
transaction-categorization project: the JSON form of ``TxnTableModel.to_dict()``
(see ``txn_table_model.py``).

The export carries only the fields the categorizer needs — no account-holder PII:

    {
      "ir_version": str,
      "institutions": [str],
      "period_from": "YYYY-MM-DD", "period_to": "YYYY-MM-DD",
      "txn_tables_by_type": { "<type>": [ {"currency": str, "rows": [TxnRow, ...]} ] },
      "accounts": [ {"account_no", "account_type", "currency", "balance",
                     "fd_records": [{"deposit_no": ...}], "investment_holdings": [...]} ]
    }

Where each ``TxnRow`` is exactly::

    {"date": str, "bank": str, "account": str, "description": str,
     "withdrawal": float|null, "deposit": float|null, "balance_after": float|null,
     "net_deposits": float|null, "txn_id": str, "currency": str}

The ``account`` field keeps the raw ``account_no`` on purpose — the categorizer
uses it to detect inter-bank transfers.

Usage:
    python export_model.py consolidated.ir.json -o export.txn-table-model.json
    python export_model.py consolidated.ir.json --parser-dir ../sg-bank-pdf-parser
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pfa_ir_schema

from pfa_ir_consolidator.txn_table_model import build_txn_table_model
from pfa_ir_consolidator.txn_table_model_io import (
    embedded_fx_rates,
    save_txn_table_model,
)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Export a consolidated ParsedStatement to TxnTableModel JSON."
    )
    _ = ap.add_argument("input", help="Consolidated *.ir.json to export")
    _ = ap.add_argument(
        "-o", "--output", required=True,
        help="Path to write the TxnTableModel JSON export",
    )
    _ = ap.add_argument(
        "--parser-dir", default=None,
        help="Path to the sg-bank-pdf-parser skill dir (for ir_schema)",
    )
    _ = ap.add_argument("--indent", type=int, default=2, help="JSON indent")
    args = ap.parse_args()

    text = Path(args.input).read_text(encoding="utf-8")
    try:
        ir = pfa_ir_schema.from_json(text)
    except ValueError as e:
        sys.exit(f"[error] {args.input}: {e}")

    # Reuse FX already embedded in the consolidated IR (offline-safe); otherwise
    # build_txn_table_model falls back to its own internal resolution.
    fx_rates = embedded_fx_rates(ir)
    model = build_txn_table_model(ir, fx_rates=fx_rates)
    save_txn_table_model(model.to_dict(), Path(args.output), indent=args.indent)

    n_txns = sum(
        len(tbl.rows)
        for tables in model.txn_tables_by_type.values()
        for tbl in tables
    )
    print(f"Wrote TxnTableModel export -> {args.output}")
    print(
        f"  ir_version={model.ir_version} institutions={model.institutions} "+
        f"accounts={len(model.accounts)} txns={n_txns}"
    )


if __name__ == "__main__":
    main()
