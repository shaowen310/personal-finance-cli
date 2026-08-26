"""Tests for the legacy (2013-era) single-account DBS statement parser."""

from pathlib import Path

import pdfplumber
import pytest

from pfa_parser.convert_statement import detect_type
from pfa_parser.extractors.dbs_legacy_extractor import DBSTxn2013Extractor
from pfa_parser.extractors.registry import get_extractor

# Sample lives in the repo-root tests/cache directory.
_SAMPLE = Path(__file__).resolve().parents[3] / "tests" / "cache" / "DBS_201305_0350.pdf"

pytestmark = pytest.mark.skipif(
    not _SAMPLE.exists(), reason="DBS_201305_0350.pdf sample not present"
)


def test_detect_txn_2013():
    with pdfplumber.open(str(_SAMPLE)) as pdf:
        assert detect_type(pdf) == ("dbs", "txn_2013")


def test_extractor_supports_and_dispatch():
    assert DBSTxn2013Extractor.supports(_SAMPLE) is True
    bank, family = ("dbs", "txn_2013")
    assert get_extractor(bank, family) is DBSTxn2013Extractor


def test_parse_txn_2013():
    stmt = DBSTxn2013Extractor().to_ir(_SAMPLE)
    accounts = stmt.accounts
    assert len(accounts) == 1

    acct = accounts[0]
    assert acct.account_no == "003-0-100350"
    assert acct.name == "DBS REMIX eSAVINGS PLUS"
    assert acct.currency == "SGD"
    assert acct.opening_balance == pytest.approx(4333.90)
    assert acct.closing_balance == pytest.approx(2434.02)

    txns = acct.transactions
    assert len(txns) == 3

    # Two outward transfers + interest earned.
    assert txns[0].posted_date == "2013-05-07"
    assert txns[0].amount == pytest.approx(-1000.00)
    assert txns[0].balance_after == pytest.approx(3333.90)
    assert "003-0-102043" in txns[0].description

    assert txns[1].posted_date == "2013-05-07"
    assert txns[1].amount == pytest.approx(-900.00)

    assert txns[2].posted_date == "2013-05-31"
    assert txns[2].amount == pytest.approx(0.12)
    assert txns[2].balance_after == pytest.approx(2434.02)
