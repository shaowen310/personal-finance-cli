"""Tests for base parser classes."""

from pfa_parser import Transaction


def test_transaction_creation():
    tx = Transaction(
        date="2026-07-15",
        description="Salary deposit",
        amount=5000.00,
    )
    assert tx.date == "2026-07-15"
    assert tx.description == "Salary deposit"
    assert tx.amount == 5000.00
    assert tx.currency == "SGD"  # default


def test_transaction_defaults():
    tx = Transaction(date="2026-07-15", description="test", amount=-10.50)
    assert tx.account_name == ""
    assert tx.account_no == ""
    assert tx.balance_after is None
    assert tx.category is None
    assert tx.link_labels == []
