"""Standalone CLI for the pfa_parser package (argparse).

The primary end-user CLI lives in ``apps/pfa-cli`` (the ``pfa parse`` command,
which calls ``SGBankPDFParser`` directly). This module exists so the parser can
also be invoked directly during development, e.g.::

    python -m pfa_parser statement.pdf
    python -m pfa_parser statement.ir.json --no-mask --ir-only

Pipeline:
  PDF:     detect -> extractor.to_ir() -> postprocess -> write IR JSON -> render MD
  IR JSON: load IR JSON -> postprocess -> render MD
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pdfplumber
from pfa_ir_schema import from_json as ir_from_json

from pfa_parser.convert_statement import detect_type, get_renderer, render_ir_to_md
from pfa_parser.postprocess import postprocess_statement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Singapore bank statement (PDF or IR JSON) to Markdown.",
    )
    _ = parser.add_argument("input", help="Input bank statement (PDF or .ir.json).")
    _ = parser.add_argument(
        "output",
        nargs="?",
        help="Output Markdown path (default: <input>.md in the same directory).",
    )
    _ = parser.add_argument(
        "--no-mask",
        action="store_true",
        help="Disable masking of account numbers / names (masking is on by default).",
    )
    _ = parser.add_argument(
        "--ir-only",
        action="store_true",
        help="Skip Markdown rendering (PDF: write IR JSON only; IR JSON: validate only).",
    )
    args = parser.parse_args(argv)

    do_mask = not args.no_mask
    ir_only = args.ir_only

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Input not found: {in_path}")
        return 1
    out_path = Path(args.output) if args.output else in_path.with_suffix(".md")

    # ---- Branch: IR JSON input -> load & render directly --------------------
    if in_path.suffix.lower() == ".json":
        ir = ir_from_json(in_path.read_text(encoding="utf-8"))
        ir = postprocess_statement(ir)
        print(
            f"Loaded IR: {in_path}  "
            f"({sum(len(a.transactions) for a in ir.accounts)} txns, "
            f"parser: {ir.parser.name})"
        )
        if ir_only:
            return 0
        _ = render_ir_to_md(ir, out_path, do_mask=do_mask)
        return 0

    # ---- Branch: PDF input -> detect -> extract -> write IR -> render MD ----
    with pdfplumber.open(str(in_path)) as pdf:
        bank, family = detect_type(pdf)

    if bank == "unknown":
        print("Error: This bank statement type is not supported yet.")
        print("The script could not identify the statement as DBS, UOB, ICBC, or OCBC.")
        print("Please update the skill with detection rules for this statement format.")
        return 1

    from pfa_parser.extractors.registry import get_extractor

    ir_cls = get_extractor(bank, family)
    renderer = get_renderer(bank, family)

    if ir_cls is None or renderer is None:
        print(f"Error: No extractor/renderer registered for ({bank}, {family})")
        return 1

    extractor = ir_cls()
    ir = extractor.to_ir(in_path)  # includes postprocessing

    # Write IR JSON (unmasked raw data)
    ir_path = out_path.with_suffix(".ir.json")
    _ = ir_path.write_text(ir.to_json(), encoding="utf-8")
    print(f"Wrote IR: {ir_path}  ({sum(len(a.transactions) for a in ir.accounts)} txns)")

    # Render Markdown (masking applied at render time)
    if not ir_only:
        _ = render_ir_to_md(ir, out_path, do_mask=do_mask)

    # Summary
    STATEMENT_LABELS: dict[tuple[str, str], str] = {
        ("dbs", "consolidated"): "DBS consolidated statement",
        ("icbc", "consolidated"): "ICBC bank account statement",
        ("ocbc", "consolidated"): "OCBC consolidated statement",
        ("ocbc", "card"): "OCBC credit card",
        ("uob", "txn"): "UOB transaction-style",
        ("uob", "one"): "UOB One multi-account",
        ("uob", "portfolio"): "UOB portfolio summary",
    }
    label = STATEMENT_LABELS.get((bank, family), f"{bank}/{family}")

    print(f"Statement type: {label}")
    print(f"Records: {sum(len(a.transactions) for a in ir.accounts)}")
    return 0
