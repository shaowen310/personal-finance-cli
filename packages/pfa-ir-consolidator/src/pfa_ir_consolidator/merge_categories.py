#!/usr/bin/env python3
"""merge_categories.py — fold categorizer output back into the consolidated IR.

The standalone transaction-categorization project emits ``categories.json``:

    {"txn_0001": "Groceries", ...}                 # object form
    [{"txn_id": "txn_0001", "category": "..."}]   # list form

This script merges those categories back into the consolidated ``ParsedStatement``:

1. For every transaction whose ``txn_id`` appears in the map, set
   ``txn.extras["category"] = category``.
2. When the category indicates a transfer (case-insensitive, starts with
   "transfer"), also flag ``txn.is_internal_transfer = True`` and ensure the ``"transfer"``
   tag is present, so downstream rendering / netting agrees with the categorizer.
3. Validate coverage. The export that the categorizer consumed only contains the
   transactions that ``build_render_model`` surfaces (FD legs / reversals are
   intentionally skipped), so coverage is measured against that export — *not*
   against every transaction in the raw IR. Provide the export via
   ``--render-model`` to enforce 1:1 coverage (every exported txn_id categorized
   exactly once, no stray ids). Unknown ids (in categories but absent from the IR)
   always error.
4. Write ``merged.ir.json`` and a ``merged.render-model.json`` (with the category
   injected into each ``TxnRow``) for the next pipeline stage.

Usage:
    python merge_categories.py consolidated.ir.json categories.json -o merged.ir.json
    python merge_categories.py consolidated.ir.json categories.json \
        --render-model export.render-model.json \
        --merged-ir merged.ir.json --merged-model merged.render-model.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pfa_ir_schema

# JSONValue lives in the schema submodule but is not re-exported by the
# package __init__; import it directly for the model-dict annotations below.
from pfa_ir_schema.ir_schema import JSONValue

from pfa_ir_consolidator.render_model import build_render_model
from pfa_ir_consolidator.render_model_io import (
    embedded_fx_rates,
    load_render_model,
    save_render_model,
)


def _iter_model_rows(model: dict[str, JSONValue]) -> Iterator[dict[str, JSONValue]]:
    """Yield every row dict under ``model["txn_tables_by_type"][*]["rows"]``.

    The RenderModel interchange is plain JSON, so each level is narrowed with
    ``isinstance`` before descending; malformed levels are skipped.
    """
    tables_by_type = model.get("txn_tables_by_type")
    if not isinstance(tables_by_type, dict):
        return
    for tables in tables_by_type.values():
        if not isinstance(tables, list):
            continue
        for tbl in tables:
            if not isinstance(tbl, dict):
                continue
            rows = tbl.get("rows")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    yield row


def _export_txn_ids(model: dict[str, JSONValue]) -> set[str]:
    ids: set[str] = set()
    for row in _iter_model_rows(model):
        tid = row.get("txn_id")
        if tid:
            ids.add(str(tid))
    return ids


def _load_category_map(path: Path) -> dict[str, str]:
    """Accept both ``{txn_id: category}`` and ``[{txn_id, category}]`` forms."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out: dict[str, str] = {}
        for item in data:
            tid = item.get("txn_id")
            cat = item.get("category")
            if tid is not None and cat is not None:
                out[str(tid)] = str(cat)
        return out
    raise ValueError(
        f"{path}: categories must be a dict or a list of {{txn_id, category}}"
    )


def _is_transfer_category(cat: str) -> bool:
    return cat.strip().lower().startswith("transfer")


def _inject_into_model(model: dict[str, JSONValue], cat_map: dict[str, str]) -> None:
    """Stamp each TxnRow with its category so the merged export is self-describing."""
    for row in _iter_model_rows(model):
        tid = row.get("txn_id")
        if tid is not None and str(tid) in cat_map:
            row["category"] = cat_map[str(tid)]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Merge categories.json back into the consolidated IR."
    )
    _ = ap.add_argument("input", help="Consolidated *.ir.json")
    _ = ap.add_argument("categories", help="categories.json from the categorizer")
    _ = ap.add_argument(
        "-o", "--merged-ir", default="merged.ir.json",
        help="Output merged IR JSON path",
    )
    _ = ap.add_argument(
        "--merged-model", default="merged.render-model.json",
        help="Output merged RenderModel JSON path",
    )
    _ = ap.add_argument("--parser-dir", default=None)
    _ = ap.add_argument("--indent", type=int, default=2)
    _ = ap.add_argument(
        "--render-model", default=None,
        help="The RenderModel export the categorizer consumed; when given, "+
             "enforces 1:1 coverage against its txn_ids",
    )
    _ = ap.add_argument(
        "--allow-uncategorized", action="store_true",
        help="With --render-model, do not fail when some exported txns are "+
             "left uncategorized",
    )
    args = ap.parse_args()

    in_path = Path(args.input)
    ir = pfa_ir_schema.from_json(in_path.read_text(encoding="utf-8"))

    cat_map = _load_category_map(Path(args.categories))

    ir_ids: set[str] = set()
    total = 0
    matched = 0
    transfer_flagged = 0
    for acc in ir.accounts:
        for txn in acc.transactions:
            total += 1
            ir_ids.add(txn.txn_id)
            cat = cat_map.get(txn.txn_id)
            if cat is None:
                continue
            matched += 1
            if txn.extras is None:
                txn.extras = {}
            txn.extras["category"] = cat
            if _is_transfer_category(cat):
                txn.is_internal_transfer = True
                if "transfer" not in txn.tags:
                    txn.tags.append("transfer")
                transfer_flagged += 1

    export_ids = (
        _export_txn_ids(load_render_model(args.render_model))
        if args.render_model
        else None
    )

    errors: list[str] = []
    unknown = set(cat_map.keys()) - ir_ids
    if unknown:
        sample = ", ".join(sorted(unknown)[:3])
        errors.append(
            f"{len(unknown)} txn_id(s) in categories.json not found in IR "+
            f"(e.g. {sample})"
        )
    if export_ids is not None:
        missing = export_ids - set(cat_map.keys())
        extra = set(cat_map.keys()) - export_ids
        if missing and not args.allow_uncategorized:
            errors.append(
                f"coverage gap: {len(missing)} exported txn_id(s) left "+
                "uncategorized"
            )
        if extra:
            errors.append(
                f"{len(extra)} txn_id(s) in categories.json were not in the "+
                "export (cannot be matched)"
            )
    if errors:
        for e in errors:
            print(f"[error] {e}")
        sys.exit(1)

    out_ir = Path(args.merged_ir)
    out_ir.parent.mkdir(parents=True, exist_ok=True)
    _ = out_ir.write_text(ir.to_json(indent=args.indent), encoding="utf-8")

    fx_rates = embedded_fx_rates(ir)
    model = build_render_model(ir, fx_rates=fx_rates).to_dict()
    _inject_into_model(model, cat_map)
    save_render_model(model, Path(args.merged_model), indent=args.indent)

    print(f"Merged categories -> {out_ir}")
    print(f"Wrote merged RenderModel -> {args.merged_model}")
    uncovered = (len(export_ids) if export_ids is not None else total) - matched
    print(
        f"  ir_txns={total} categorized={matched} "+
        f"transfer_flagged={transfer_flagged} uncovered={uncovered}"
    )


if __name__ == "__main__":
    main()
