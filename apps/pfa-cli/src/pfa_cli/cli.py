"""Personal Finance CLI entry point."""

import sys
from pathlib import Path

import click

from pfa_parser import SGBankPDFParser
from pfa_ir_schema import from_json

from pfa_ir_consolidator import (
    consolidate_statements,
    embed_fx_rates,
    detect_inter_bank_transfers,
    detect_intra_bank_transfers,
    detect_currency_conversions,
    detect_cc_payments,
)
from pfa_parser.postprocess import verify_txn_links

from pfa_cli.dates import parse_month, parse_start_date, parse_end_date

DEFAULT_MIN_IR_VERSION = "2026.4"


def _version_ge(a: str, b: str) -> bool:
    def _parts(v: str) -> list[int]:
        return [int(x) for x in v.split(".") if x.isdigit()]

    return _parts(a) >= _parts(b)


@click.group()
def cli() -> None:
    """Personal Finance CLI — track your money."""
    pass


@cli.command()
@click.option("-i", "--input", "input_path", required=True, help="Path to bank statement PDF")
@click.option("-o", "--output", "output_path", default=None,
              help="Write the ParsedStatement IR as .ir.json (default: <input>.ir.json)")
def parse(input_path: str, output_path: str | None) -> None:
    """Parse a bank statement PDF via sg-bank-pdf-parser.

    Prints a flat transaction list to stdout, and optionally writes the full
    ParsedStatement IR to a JSON file for downstream consolidation / analysis.
    """
    parser = SGBankPDFParser()
    ir_stmt = parser.parse(input_path)

    # Flat transaction listing derived from the IR
    transactions = [
        txn
        for account in ir_stmt.accounts
        for txn in account.transactions
    ]
    click.echo(f"Parsed {len(transactions)} transactions from {input_path}")
    for tx in transactions:
        click.echo(f"  {tx.posted_date} | {tx.amount:>10.2f} {tx.currency} | {tx.description}")

    # Write IR JSON if requested (default: alongside input as <stem>.ir.json)
    ir_path = Path(output_path) if output_path else Path(input_path).with_suffix(".ir.json")
    _ = ir_path.write_text(ir_stmt.to_json(indent=2), encoding="utf-8")
    click.echo(f"IR written: {ir_path}")


@cli.command()
@click.option("-m", "--month", default=None,
              help="Month to analyze (YYYYMM format, e.g. 202608)")
@click.option("-s", "--start-date", default=None,
              help="Start date (YYYYMMDD or YYYYMM format)")
@click.option("-e", "--end-date", default=None,
              help="End date (YYYYMMDD or YYYYMM format)")
@click.option("-i", "--input", "input_path", default=None,
              help="Path to consolidated IR JSON (default: tests/outputs/consolidated.ir.json)")
@click.option("-o", "--output", default=None,
              help="Output directory (default: alongside input)")
def analyze(month: str | None, start_date: str | None, end_date: str | None,
            input_path: str | None, output: str | None) -> None:
    """Run financial analysis for a date range.

    Date formats:
      - YYYYMMDD for exact dates (e.g. 20260801)
      - YYYYMM for whole months (e.g. 202608 for August 2026)

    \b
    Examples:
      pfa analyze -m 202608
      pfa analyze -s 20260801 -e 20260810
      pfa analyze -s 202608 -e 202608
    """
    from pfa_analysis.analyze import render_consolidated_report

    # Resolve input path
    if input_path:
        in_path = Path(input_path)
    else:
        in_path = Path("tests/outputs/consolidated.ir.json")
    if not in_path.exists():
        raise click.BadParameter(
            f"Consolidated IR file not found: {in_path}. Run 'pfa run --full' first or provide --input."
        )

    # Resolve output dir
    out_dir = Path(output) if output else in_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve date range
    final_start: str | None = None
    final_end: str | None = None

    if month:
        try:
            parsed = parse_month(month)
            if parsed:
                final_start, final_end = parsed
        except ValueError as e:
            raise click.BadParameter(str(e))
    else:
        if start_date:
            try:
                final_start = parse_start_date(start_date)
            except ValueError as e:
                raise click.BadParameter(str(e))
        if end_date:
            try:
                final_end = parse_end_date(end_date)
            except ValueError as e:
                raise click.BadParameter(str(e))

    # Build date label for display
    if final_start and final_end and final_start != final_end:
        date_label = f"{final_start} to {final_end}"
    elif final_start:
        date_label = final_start
    elif final_end:
        date_label = final_end
    else:
        date_label = "all available"

    click.echo(f"Running analysis for {date_label}...")

    # Categories path (alongside consolidated IR)
    categories_path = in_path.with_name("categories.json")

    md = render_consolidated_report(
        in_path,
        categories_path=categories_path if categories_path.exists() else None,
        start_date=final_start,
        end_date=final_end,
    )
    out_path = out_dir / (in_path.stem + "_Finance_Report.md")
    _ = out_path.write_text(md, encoding="utf-8")
    click.echo(f"Report written: {out_path}")


@cli.command()
@click.option("--full", is_flag=True, help="Run full pipeline: parse → ir → categorize → analyze")
def run(full: bool) -> None:
    """Run the full personal finance pipeline."""
    _ = full
    click.echo("Full pipeline execution — coming soon.")


@cli.command()
@click.argument("inputs", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--out", "out_path", default="consolidated.ir.json",
              help="Output IR JSON path (default: consolidated.ir.json)")
@click.option("--min-ir-version", default=DEFAULT_MIN_IR_VERSION,
              help="Minimum accepted ir_version")
@click.option("--no-dedup", is_flag=True, help="Disable txn_id de-duplication")
@click.option("--indent", type=int, default=2, help="JSON indent")
def consolidate(inputs: tuple[str, ...], out_path: str, min_ir_version: str,
                no_dedup: bool, indent: int) -> None:
    """Consolidate multiple *.ir.json ParsedStatement files into one.

    This command directly invokes the ``pfa_ir_consolidator`` library (the
    same consolidation engine behind the standalone ``consolidate.py`` script),
    replicating its ``main()`` behaviour: merge accounts grouped by
    (institution, account_no, currency), de-duplicate transactions by
    ``txn_id``, detect transfer / currency-conversion links, attach FX rates,
    and verify transaction links.

    \b
    Examples:
      pfa consolidate a.ir.json b.ir.json -o consolidated.ir.json
      pfa consolidate *.ir.json --min-ir-version 2026.4
    """
    stmts_with_paths: list[tuple[str, object]] = []
    for path in inputs:
        text = Path(path).read_text(encoding="utf-8")
        try:
            stmt = from_json(text)
        except ValueError as e:
            sys.exit(f"[error] {path}: {e}")
        if not _version_ge(stmt.ir_version, min_ir_version):
            sys.exit(
                f"[error] {path}: ir_version {stmt.ir_version!r} < required "
                f"{min_ir_version!r}"
            )
        stmts_with_paths.append((str(path), stmt))

    consolidated, total_in, deduped, filtered = consolidate_statements(
        stmts_with_paths, do_dedup=not no_dedup
    )
    consolidated = detect_inter_bank_transfers(consolidated)
    consolidated = detect_intra_bank_transfers(consolidated)
    consolidated = detect_currency_conversions(consolidated)
    consolidated = detect_cc_payments(consolidated)
    consolidated = verify_txn_links(consolidated)
    consolidated = embed_fx_rates(consolidated)

    transfers = (consolidated.extras or {}).get("consolidation", {}).get("transfers", {})
    inter_bank_detected = transfers.get("inter_bank_detected", 0)
    intra_bank_detected = transfers.get("intra_bank_detected", 0)
    cc_detected = transfers.get("currency_conversion_detected", 0)
    cc_payments_detected = transfers.get("cc_payments_detected", 0)

    out = Path(out_path)
    _ = out.write_text(consolidated.to_json(indent=indent), encoding="utf-8")
    total_out = total_in - deduped - filtered
    click.echo(f"Wrote {out}")
    click.echo(
        f"  inputs={len(stmts_with_paths)} accounts={len(consolidated.accounts)} "
        f"txns_in={total_in} txns_out={total_out} deduped={deduped} filtered={filtered}"
    )
    if inter_bank_detected:
        click.echo(f"  inter_bank_transfers={inter_bank_detected} pairs")
    if intra_bank_detected:
        click.echo(f"  intra_bank_transfers={intra_bank_detected} pairs")
    if cc_detected:
        click.echo(f"  currency_conversion_transfers={cc_detected} pairs")
    if cc_payments_detected:
        click.echo(f"  cc_payments={cc_payments_detected} pairs")
