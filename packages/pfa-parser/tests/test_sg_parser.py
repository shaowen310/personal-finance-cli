"""Tests for SGBankPDFParser."""

from pfa_parser import SGBankPDFParser, Transaction


def test_supports_format_pdf():
    parser = SGBankPDFParser()
    assert parser.supports_format("statement.pdf") is True
    assert parser.supports_format("statement.PDF") is True
    assert parser.supports_format("statement.csv") is False
    assert parser.supports_format("statement.ofx") is False


def test_transaction_model():
    """Verify the unified Transaction model works for downstream consumers."""
    tx = Transaction(
        date="2026-07-15",
        description="Salary deposit",
        amount=5000.00,
        currency="SGD",
        account_name="DBS Savings Plus",
        account_no="123-456-789",
        account_type="current",
        balance_after=15000.00,
        transfer_labels=["salary"],
    )
    assert tx.date == "2026-07-15"
    assert tx.amount == 5000.00
    assert tx.currency == "SGD"
    assert tx.account_name == "DBS Savings Plus"
