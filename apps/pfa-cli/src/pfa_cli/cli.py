"""Personal Finance CLI entry point."""

from pathlib import Path

import click  # type: ignore[import-untyped]

from pfa_parser import SGBankPDFParser

from pfa_cli.dates import parse_month, parse_start_date, parse_end_date


@click.group()
def cli() -> None:
    """Personal Finance CLI — track your money."""
    pass


@cli.command()
@click.option("-i", "--input", "input_path", required=True, help="Path to bank statement PDF")
def parse(input_path: str) -> None:
    """Parse a bank statement PDF via sg-bank-pdf-parser."""
    parser = SGBankPDFParser()
    transactions = parser.parse(input_path)
    click.echo(f"Parsed {len(transactions)} transactions from {input_path}")
    for tx in transactions:
        click.echo(f"  {tx.date} | {tx.amount:>10.2f} {tx.currency} | {tx.description}")


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
