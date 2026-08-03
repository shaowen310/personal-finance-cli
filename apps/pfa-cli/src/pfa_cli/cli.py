"""Personal Finance CLI entry point."""

import click  # type: ignore[import-untyped]

from pfa_parser import SGBankPDFParser


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
@click.option("-m", "--month", required=True, help="Month to analyze (YYYY-MM)")
def analyze(month: str) -> None:
    """Run financial analysis for a given month."""
    click.echo(f"Running analysis for {month}...")
    click.echo("TODO: implement analysis pipeline")


@cli.command()
@click.option("--full", is_flag=True, help="Run full pipeline: parse → ir → categorize → analyze")
def run(full: bool) -> None:
    """Run the full personal finance pipeline."""
    _ = full
    click.echo("Full pipeline execution — coming soon.")
