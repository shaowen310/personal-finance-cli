"""Tests for CLI."""

from click.testing import CliRunner
from pfa_cli.cli import cli


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Personal Finance CLI" in result.output


def test_parse_command():
    runner = CliRunner()
    result = runner.invoke(cli, ["parse", "--input", "nonexistent.pdf"])
    assert result.exit_code != 0
