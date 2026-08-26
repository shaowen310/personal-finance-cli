"""Extractor for legacy (pre-~2015) single-account DBS savings statements.

Example input: a DBS REMIX eSAVINGS PLUS statement (e.g. ``DBS_201305_0350.pdf``).

This layout differs fundamentally from the current *consolidated* DBS format
handled by :mod:`dbs_extractor`:

* It is a **single account** statement with a non-rotated ``DBS Bank Ltd``
  letterhead and an ``As at <date>`` period line (no rotated left-margin
  banner).
* The transaction table uses **separate WITHDRAWAL / DEPOSIT / BALANCE**
  columns rather than a single signed amount column.
* Dates are printed as ``DD Mon`` (e.g. ``07 May``) instead of ``DD/MM/YYYY``.

The table geometry (from the 2013 sample) is approximately:

    DATE(46)  DETAILS(93)  WITHDRAWAL(350)  DEPOSIT(438)  BALANCE(487)

so we bin each word by its ``x0`` position. Continuation lines (the second
line of a transaction's details, e.g. a transfer target) start at the DETAILS
x-position and carry no date token — they are appended to the preceding
transaction's description.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pdfplumber
from datetime import date
from pfa_ir_schema import ParsedStatement

from .base import BaseExtractor
from ..ir_builder import IRBuilder, AccountType

# Month-name → number lookup (legacy DBS prints dates as "07 May").
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_dd_mon_year(text: str) -> tuple[int, int, int]:
    """Parse ``"DD Mon YYYY"`` (e.g. ``"07 May 2013"``) → (year, month, day)."""
    parts = text.strip().split()
    day = int(parts[0])
    month = _MONTHS[parts[1].lower()[:3]]
    year = int(parts[2])
    return (year, month, day)


def _normalize_amount(raw: str) -> float:
    """Convert a printed amount like ``"1,000.00"`` to float."""
    return float(raw.replace(",", "").strip())

# Column x0 boundaries (points) derived from the 2013 sample.
_X_DATE_MAX = 90        # date token region ends here
_X_DETAILS_MAX = 340    # details region ends here
_X_WITHDRAWAL_MAX = 415  # withdrawal col ends; deposit begins
_X_DEPOSIT_MAX = 475    # deposit col ends; balance begins
# balance col: x0 >= _X_DEPOSIT_MAX

_DATE_TOKEN_RE = re.compile(r"^\d{1,2}$")


class DBSTxn2013Extractor(BaseExtractor):
    """Parse legacy single-account DBS savings PDFs into a ParsedStatement IR."""

    parser_name = "dbs_txn_2013"
    parser_version = "1.0"

    # ------------------------------------------------------------------
    @classmethod
    def supports(cls, pdf_path: Path) -> bool:
        full_text = ""
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                full_text += "\n" + (page.extract_text() or "")
        up = full_text.upper()
        legacy = (
            "DBS BANK LTD" in up
            and "AS AT" in up
            and ("REMIX" in up or "ESAVINGS" in up)
        )
        return legacy

    @classmethod
    def bank_name(cls) -> str:
        return "DBS"

    # ------------------------------------------------------------------
    def _parse(self, pdf_path: Path) -> ParsedStatement:
        with pdfplumber.open(str(pdf_path)) as pdf:
            return self._build(pdf, pdf_path)

    # ------------------------------------------------------------------
    def _build(self, pdf, pdf_path: Path) -> ParsedStatement:
        builder = IRBuilder(self.parser_name, self.parser_version)
        builder.set_source(str(pdf_path))

        year = None
        period_from = period_to = None
        # Account identity, captured from the first page's header line.
        acc_no: str | None = None
        product: str | None = None
        opening_balance: float | None = None
        closing_balance: float | None = None

        txns: list[dict] = []  # collected transactions (in order)

        for page in pdf.pages:
            words = page.extract_words()
            lines = self._group_lines(words)

            for _top, line_words in lines:
                line_text = " ".join(w["text"] for w in line_words)

                # --- statement period (once) ---
                if year is None:
                    m = re.search(
                        r"As at\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", line_text
                    )
                    if m:
                        pyear, pmonth, pday = _parse_dd_mon_year(m.group(1))
                        d = date(pyear, pmonth, pday)
                        year = pyear
                        period_from = period_to = d.isoformat()

                # --- account header line (once) ---
                if acc_no is None and "Account No.:" in line_text:
                    m = re.search(r"Account No\.:\s*([\d-]+)", line_text)
                    if m:
                        acc_no = m.group(1)
                    pm = re.search(r"DBS\s+(.+?)\s+Account No\.", line_text)
                    if pm:
                        product = "DBS " + pm.group(1).strip()

            # We need the period/year before scanning transactions.
            if year is None:
                continue

            table_top = self._find_table_top(lines)
            if table_top is None:
                continue

            current: dict | None = None  # current transaction being assembled
            for _top, line_words in lines:
                if _top <= table_top:
                    continue

                line_text_upper = " ".join(w["text"] for w in line_words).upper()

                # --- summary rows (no date token) ---
                if "BALANCE BROUGHT FORWARD" in line_text_upper:
                    bal = self._first_amount_in_region(line_words, _X_DEPOSIT_MAX)
                    if bal is not None:
                        opening_balance = bal
                    continue
                if "BALANCE CARRIED FORWARD" in line_text_upper:
                    bal = self._first_amount_in_region(line_words, _X_DEPOSIT_MAX)
                    if bal is not None:
                        closing_balance = bal
                    # End of transaction table — nothing after is a transaction.
                    break
                if line_text_upper.strip().startswith("TOTAL"):
                    continue

                date_token = line_words[0] if line_words else None
                is_txn_line = (
                    date_token is not None
                    and _DATE_TOKEN_RE.match(date_token["text"]) is not None
                    and date_token["x0"] < _X_DATE_MAX
                )

                if is_txn_line:
                    if current is not None:
                        txns.append(current)
                    current = self._parse_row(line_words, year)
                else:
                    # Continuation line → append to current transaction details.
                    if current is not None:
                        cont = " ".join(
                            w["text"] for w in line_words if w["x0"] >= _X_DATE_MAX
                        )
                        if cont:
                            current["details"] = (current["details"] + " " + cont).strip()
            if current is not None:
                txns.append(current)

        # Fallback if period parsing failed.
        if year is None:
            year = 1900
            period_from = period_to = ""

        builder.set_meta(institution="DBS")
        builder.set_period(period_from or "", period_to or "")

        # Legacy DBS savings accounts have no dedicated AccountType; the IR
        # models them as a standard deposit ("current") account.
        acc_type = AccountType.normalize("current").value
        builder.add_account(
            name=product or "DBS Account",
            account_no=acc_no or "",
            account_type=acc_type,
            currency="SGD",
            opening_balance=opening_balance,
            closing_balance=closing_balance,
        )
        for t in txns:
            self._emit(builder, t)
        return builder.build()

    # ------------------------------------------------------------------
    @staticmethod
    def _group_lines(words: list[dict], gap: float = 6.0) -> list[tuple[float, list[dict]]]:
        """Cluster words into rows by vertical proximity.

        A new row starts whenever a word sits more than ``gap`` points above
        the current row's top. This tolerates the 1px splits between a date
        token (e.g. ``07 May``) and its transaction details/amounts on the
        next line, while keeping distinct transactions (11+px apart) separate.
        """
        rows: list[list[dict]] = []
        for w in sorted(words, key=lambda x: (x["top"], x["x0"])):
            if rows and w["top"] <= rows[-1][0]["top"] + gap:
                rows[-1].append(w)
            else:
                rows.append([w])
        return [(round(r[0]["top"]), sorted(r, key=lambda x: x["x0"])) for r in rows]

    @staticmethod
    def _first_amount_in_region(line_words: list[dict], x_min: float) -> float | None:
        """Return the first amount word at x0 >= x_min, normalized to float."""
        for w in line_words:
            if w["x0"] >= x_min:
                try:
                    return _normalize_amount(w["text"])
                except ValueError:
                    continue
        return None

    @staticmethod
    def _find_table_top(lines: list[tuple[float, list[dict]]]) -> float | None:
        """Return the ``top`` of the column-header line (contains DATE + BALANCE)."""
        for top, line_words in lines:
            texts = {w["text"].upper() for w in line_words}
            if "DATE" in texts and "BALANCE" in texts:
                return top
        return None

    @staticmethod
    def _parse_row(line_words: list[dict], year: int) -> dict:
        """Parse one transaction row into a dict."""
        day = line_words[0]["text"]
        month = line_words[1]["text"] if len(line_words) > 1 else ""
        details_parts = [
            w["text"] for w in line_words if _X_DATE_MAX <= w["x0"] < _X_DETAILS_MAX
        ]
        details = " ".join(details_parts).strip()

        withdrawal = deposit = balance = None
        for w in line_words:
            x = w["x0"]
            if _X_DETAILS_MAX <= x < _X_WITHDRAWAL_MAX:
                withdrawal = w["text"]
            elif _X_WITHDRAWAL_MAX <= x < _X_DEPOSIT_MAX:
                deposit = w["text"]
            elif x >= _X_DEPOSIT_MAX:
                balance = w["text"]

        # Build ISO date from day + month + statement year.
        date_iso = ""
        try:
            dyear, dmonth, dday = _parse_dd_mon_year(f"{day} {month} {year}")
            date_iso = date(dyear, dmonth, dday).isoformat()
        except Exception:
            date_iso = ""

        if withdrawal is not None:
            amount = -_normalize_amount(withdrawal)
        elif deposit is not None:
            amount = _normalize_amount(deposit)
        else:
            amount = 0.0

        balance_after = _normalize_amount(balance) if balance is not None else None

        return {
            "date": date_iso,
            "details": details,
            "amount": amount,
            "balance_after": balance_after,
        }

    @staticmethod
    def _emit(builder: IRBuilder, row: dict) -> None:
        builder.add_transaction(
            posted_date=row["date"],
            amount=row["amount"],
            currency="SGD",
            description=row["details"],
            balance_after=row["balance_after"],
            transfer_labels=[],
        )
