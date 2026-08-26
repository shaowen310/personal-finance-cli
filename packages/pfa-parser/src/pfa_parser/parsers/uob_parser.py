"""UOB statement parsers.

Three flavors are supported:

  - ``parse_uob_txn``        — single-account transaction statement
    (``eStatement_<acct>_<YYYYMM>.pdf``): one ``Account Transaction Details``
    table with Date / Description / Withdrawals / Deposits / Balance columns,
    preceded by ``Period: <DD Mon YYYY> to <DD Mon YYYY>``.

  - ``parse_uob_portfolio``  — multi-account portfolio summary
    (``eStatement_<YYYYMM>.pdf``): ``Portfolio Overview`` totals table plus a
    ``Deposits`` sub-table (one row per account) and an ``Investments``
    sub-table (one row per fund).

  - ``parse_uob_one``        — UOB One multi-account transaction statement
    (``eStatement_<YYYYMM>_<acct>.pdf``): a page-zero ``Account Overview``
    with a ``Deposits`` total and per-account summary table, followed by
    multiple ``Account Transaction Details`` sections (one per currency
    sub-account), and a ``Foreign Exchange, Gold, Silver Reference Rates``
    table on the last page.

Each parser returns a structure suitable for direct Markdown rendering; the
matching ``*_to_markdown`` function renders it to a Markdown string.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pfa_ir_schema.common import (
    SIDEBAR_NOISE,
    WordDict,
    group_lines,
    is_bank_num,
)

# ----------------------------------------------------------------------------
# Column x-positions (PDF points, measured empirically from the source PDFs).
# Stable across statements of the same product family.
# ----------------------------------------------------------------------------

# --- UOB transaction-style statement ---
UOB_TXN_DATE_DAY_X = (52, 62)  # "DD" token
UOB_TXN_DATE_MON_X = (63, 80)  # "Mon" token (mixed-case 3-letter)
UOB_TXN_DESC_X_START = 120     # description begins here
UOB_TXN_DESC_X_MAX = 240       # exclude anything that drifts too far right
UOB_TXN_WITHDRAWAL_X1 = (355, 390)
UOB_TXN_DEPOSIT_X1 = (430, 470)
UOB_TXN_BALANCE_X1 = (500, 560)  # widened to 560: OD-suffixed balances (e.g. "1,408.00OD") sit at x1≈557

# --- UOB portfolio-style statement ---
# The Deposits summary table shares the same x-edges as the transaction table.
# The Investments table uses different edges (see UOB_INV_* constants below).
UOB_INV_NAME_X_START = 60
UOB_INV_UNITS_X1 = (300, 360)
UOB_INV_CURRENCY_X = (350, 390)
UOB_INV_PRICE_X1 = (430, 470)
UOB_INV_VAL_X1 = (500, 550)

DATE_UOB_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}$")  # "02 Jun" (UOB mixed case)
UOB_ACC_NO_RE = re.compile(r"\b\d{3}-\d{3}-\d{3}-\d{1,3}\b")  # e.g. "XXX-XXX-XXX-X"

# UOB renders an overdrawn (overdraft) balance with a trailing "OD" suffix glued
# to the number, e.g. "1,408.00OD" — meaning the account is overdrawn by 1,408.00
# (a negative balance / amount owed). Strip that suffix and flag it as negative so
# the value survives is_bank_num and is recorded with the correct sign.
_OD_BALANCE_RE = re.compile(r"^(?P<num>-?\d{1,3}(?:,\d{3})*\.\d{2})(?P<suffix>OD)$", re.IGNORECASE)


def _balance_token(text: str) -> tuple[str | None, bool]:
    """Normalise a deposits-table balance word.

    Returns ``(clean_number, is_negative)`` where ``clean_number`` is the bare
    ``X,XXX.XX`` string (suitable for ``is_bank_num`` / ``_parse_amount``) or
    ``None`` if the word is not a balance figure. A trailing ``OD`` suffix marks
    the balance as negative (overdraft).
    """
    if is_bank_num(text):
        return text, text.startswith("-")
    m = _OD_BALANCE_RE.match((text or "").strip())
    if m:
        return m.group("num"), True
    return None, False

MONTH_MAP = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"]
)}


# ---------------------------------------------------------------------------
# Shared FX-rates helper (used by both portfolio and UOB One parsers)
# ---------------------------------------------------------------------------

def parse_uob_txn(pdf: Any):
    transactions: list[dict[str, str]] = []
    meta = {
        "bank": "UOB",
        "period_start": "",
        "period_end": "",
        "currency": "SGD",
        "page_count": len(pdf.pages),
        "account_name": "",
        "account_no": "",
    }
    opening_balance: str = ""

    for page in pdf.pages:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines = group_lines(words)
        all_text = "\n".join(" ".join(w["text"] for w in ln) for ln in lines)

        # Period line — appears on page 0.
        m_period = re.search(
            r"Period:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            all_text,
        )
        if m_period and not meta["period_start"]:
            meta["period_start"] = m_period.group(1)
            meta["period_end"] = m_period.group(2)

        # Locate the "Account Transaction Details" section.
        try:
            sec_idx = next(
                i for i, ln in enumerate(lines)
                if " ".join(w["text"] for w in ln) == "Account Transaction Details"
            )
        except StopIteration:
            continue

        # In the sample PDFs the account identifier line ("<Account Name>
        # <dashed no.>") appears 1 line BELOW the section header (the page
        # header sits above the section header). Walk forward a few lines and
        # pick the first line that contains a dashed UOB account number.
        for j in range(sec_idx + 1, min(sec_idx + 6, len(lines))):
            head_text = " ".join(w["text"] for w in lines[j])
            m_acc = UOB_ACC_NO_RE.search(head_text)
            if m_acc and not meta["account_no"]:
                meta["account_no"] = m_acc.group(0)
                meta["account_name"] = head_text[: m_acc.start()].strip()
                break

        # Walk forward until the next table header row.
        i = sec_idx + 1
        # Skip the header band: "Date Description Withdrawals Deposits Balance"
        # and the "SGD SGD SGD" sub-header band.
        while i < len(lines):
            text = " ".join(w["text"] for w in lines[i])
            if text.startswith("Date ") and "Description" in text and "Withdrawals" in text:
                i += 1
                # Skip the "SGD SGD SGD" sub-header band if present.
                if i < len(lines) and " ".join(w["text"] for w in lines[i]).strip() == "SGD SGD SGD":
                    i += 1
                break
            i += 1
        else:
            continue

        pending = None
        while i < len(lines):
            ln2 = lines[i]
            t2 = " ".join(w["text"] for w in ln2)

            if t2.startswith("End of Transaction Details"):
                break
            if t2.startswith("Total "):
                break

            # BALANCE B/F — opening balance row, no date column. Must be
            # checked BEFORE the date-row check, because a "DD Mon" prefix
            # also matches the x-bands.
            m_bf = re.match(r"^(\d{1,2}\s+[A-Za-z]{3}\s+BALANCE\s+B/F)\s+([\d,]+\.\d{2})$", t2)
            if not m_bf:
                m_bf = re.match(r"^BALANCE\s+B/F\s+([\d,]+\.\d{2})$", t2)
            if m_bf:
                opening_balance = m_bf.group(1) if m_bf.lastindex and m_bf.lastindex >= 2 else m_bf.group(0).split()[-1]
                if not re.match(r"^[\d,]+\.\d{2}$", opening_balance):
                    # m_bf was the "DD Mon BALANCE B/F" form; grab the amount.
                    opening_balance = m_bf.group(2) if m_bf.lastindex and m_bf.lastindex >= 2 else ""
                i += 1
                continue

            # New transaction: row begins with a "DD" token followed by a "Mon"
            # token in the date band.
            day_words = [w for w in ln2 if UOB_TXN_DATE_DAY_X[0] <= w["x0"] <= UOB_TXN_DATE_DAY_X[1]]
            mon_words = [w for w in ln2 if UOB_TXN_DATE_MON_X[0] <= w["x0"] <= UOB_TXN_DATE_MON_X[1]]
            if day_words and mon_words and DATE_UOB_RE.match(f"{day_words[0]['text']} {mon_words[0]['text']}"):
                if pending:
                    transactions.append(pending)
                txn_date = f"{day_words[0]['text']} {mon_words[0]['text']}"
                desc_words = [
                    w for w in ln2
                    if UOB_TXN_DESC_X_START <= w["x0"] <= UOB_TXN_DESC_X_MAX
                    and not is_bank_num(w["text"])
                    and w not in day_words
                    and w not in mon_words
                ]
                desc = " ".join(w["text"] for w in desc_words).strip()
                withdrawal = deposit = balance = ""
                for w in ln2:
                    if not is_bank_num(w["text"]):
                        continue
                    if UOB_TXN_WITHDRAWAL_X1[0] <= w["x1"] <= UOB_TXN_WITHDRAWAL_X1[1]:
                        withdrawal = w["text"]
                    elif UOB_TXN_DEPOSIT_X1[0] <= w["x1"] <= UOB_TXN_DEPOSIT_X1[1]:
                        deposit = w["text"]
                    elif UOB_TXN_BALANCE_X1[0] <= w["x1"] <= UOB_TXN_BALANCE_X1[1]:
                        balance = w["text"]
                pending = {
                    "txn_date": txn_date,
                    "description": desc,
                    "withdrawal": withdrawal,
                    "deposit": deposit,
                    "balance": balance,
                }
                i += 1
                continue

            # Continuation line: words in the description band, no date tokens.
            if (pending is not None
                    and not day_words
                    and any(UOB_TXN_DESC_X_START <= w["x0"] <= UOB_TXN_DESC_X_MAX for w in ln2)
                    and not t2.startswith("Page ")):
                clean = " ".join(
                    w["text"] for w in ln2
                    if UOB_TXN_DESC_X_START <= w["x0"] <= UOB_TXN_DESC_X_MAX
                ).strip()
                if clean and not re.match(r"^\d+\s+of\s+\d+$", clean):
                    pending["description"] = (pending["description"] + " " + clean).strip()
            i += 1

        if pending:
            transactions.append(pending)

    # Normalize dates to ISO using the year from period_end.
    year_src: str = str(meta.get("period_end") or meta.get("period_start") or "")
    year = ""
    parts = year_src.split()
    if len(parts) == 3 and parts[2].isdigit():
        year = parts[2]
    if not year:
        year = str(datetime.now().year)
    for t in transactions:
        try:
            d, mon_name = t["txn_date"].split()
            mm = MONTH_MAP[mon_name.lower()]
            t["txn_date"] = f"{year}-{mm:02d}-{int(d):02d}"
        except (KeyError, ValueError):
            pass

    return meta, transactions, opening_balance


def parse_uob_portfolio(pdf: Any):
    meta = {
        "bank": "UOB",
        "period_start": "",
        "period_end": "",
        "as_at": "",
        "currency": "SGD",
        "page_count": len(pdf.pages),
    }
    overview: list[tuple[str, str]] = []     # [(label, amount)]
    deposits: list[dict[str, str]] = []     # [{name, account_no, currency, credit_line, interest_earned, interest_charged, balance, locked_amount}]
    investments: list[dict[str, str]] = []  # [{name, units, currency, price, valuation}]
    unit_trust: dict[str, str] = {}  # {name, account_no} from the Investments section header
    deposit_grand_total = ""
    investment_grand_total = ""
    deposit_total = ""
    investment_total = ""
    # Section flags persist across pages so multi-page tables (e.g. Investments
    # continued on page 2) are parsed in full.
    in_deposits = False
    in_investments = False

    for page in pdf.pages:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines = group_lines(words)
        all_text = "\n".join(" ".join(w["text"] for w in ln) for ln in lines)

        m_period = re.search(
            r"Period:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            all_text,
        )
        if m_period and not meta["period_start"]:
            meta["period_start"] = m_period.group(1)
            meta["period_end"] = m_period.group(2)

        m_asat = re.search(r"as at\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", all_text)
        if m_asat and not meta["as_at"]:
            meta["as_at"] = m_asat.group(1)

        # --- Portfolio Overview table (Deposits / Investments / Loans) ---
        # Lines look like: "<Label>  <amount>" where the amount's x1 lies in the
        # rightmost numeric band.
        for ln in lines:
            t = " ".join(w["text"] for w in ln)
            for label in [
                "Deposits",
                "Investments",
                "Structured Investments",
                "Loans (Total Outstanding Loan Amount)",
                "Total Deposits and Investments1",
            ]:
                m = re.match(rf"^{re.escape(label)}\s+([\d,]+\.\d{{2}})$", t)
                if m:
                    overview.append((label, m.group(1)))

        # --- Deposits table ---
        # The "Deposits" header is followed by a column-header line, then a
        # "Savings" sub-header, then one or more "<Account Name> <DashedAcctNo>"
        # rows. We treat any line that carries a UOB_ACC_NO_RE as the start of a
        # row, then attach the numeric columns from the same line and the
        # account name from the line above.
        for idx, ln in enumerate(lines):
            t = " ".join(w["text"] for w in ln)
            if t.strip() == "Deposits":
                in_deposits = True
                in_investments = False
                continue
            if t.strip() == "Investments" or t.strip().startswith("Investments "):
                in_investments = True
                in_deposits = False
                # Capture the unit-trust account header that precedes the fund
                # rows. It appears either on the same line
                # (``Investments Unit Trust Account 899-597-659-1``) or on the
                # following line (``Unit Trust Account 899-597-659-1``). Fund
                # data rows never carry a dashed account number, so this match
                # is header-only and safe across page continuation.
                if not unit_trust:
                    m_acc = UOB_ACC_NO_RE.search(t)
                    src = t
                    if not m_acc:
                        for k in range(idx + 1, min(idx + 4, len(lines))):
                            src = " ".join(w["text"] for w in lines[k])
                            m_acc = UOB_ACC_NO_RE.search(src)
                            if m_acc:
                                break
                    if m_acc:
                        acct_name = src[: m_acc.start()].strip()
                        acct_name = re.sub(
                            r"\s*\(continued\)\s*$", "", acct_name, flags=re.IGNORECASE
                        )
                        acct_name = re.sub(r"^Investments\s+", "", acct_name).strip()
                        if acct_name:
                            unit_trust = {
                                "name": acct_name,
                                "account_no": m_acc.group(0),
                            }
                continue

            m_acc = UOB_ACC_NO_RE.search(t)
            if in_deposits and m_acc and t.strip() == m_acc.group(0):
                # Account-number-only line. The account name sits on the
                # previous line, in the leftmost x-band. The same line also
                # carries the numeric data (Currency | Credit Line | Interest
                # Earned | Interest Charged | Balance) on the right. We take
                # the leftmost words up to a clear gap (>20pt between the end
                # of one word and the start of the next).
                name_text = ""
                if idx >= 1:
                    prev_line = list(lines[idx - 1])
                    kept = []
                    for w in prev_line:
                        if kept and w["x0"] - kept[-1]["x1"] > 20:
                            break
                        kept.append(w)
                    name_text = " ".join(w["text"] for w in kept).strip()
                deposits.append({
                    "name": name_text,
                    "account_no": m_acc.group(0),
                    "currency": "",
                    "credit_line": "",
                    "interest_earned": "",
                    "interest_charged": "",
                    "balance": "",
                    "locked_amount": "",
                })
                continue

            # A "Locked Amount ... is <amount>" line attaches to the most recent
            # deposit row.
            m_lock = re.search(r"Locked Amount\d?\s+as of\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+is\s+([\d,]+\.\d{2})", t)
            if m_lock and deposits:
                deposits[-1]["locked_amount"] = m_lock.group(1)
                continue

            # Total / Grand Total rows in the deposits section.
            if in_deposits and t.startswith("Total (SGD) "):
                deposit_total = t
                continue
            if in_deposits and t.startswith("Grand Total (SGD"):
                deposit_grand_total = t
                continue

            # --- Investments table rows ---
            # A row looks like: "<Fund Name words> <units> <ccy> <price> <valuation>"
            # where Units sits at x1 in UOB_INV_UNITS_X1, Currency at x0 in
            # UOB_INV_CURRENCY_X, Price at x1 in UOB_INV_PRICE_X1, Valuation at
            # x1 in UOB_INV_VAL_X1. The fund name is everything to the left of
            # the first numeric token.
            if in_investments and idx >= 1:
                # Skip header rows.
                if t.strip().startswith("Units Currency") or t.strip() == "Price Valuation":
                    continue
                if t.startswith("Total (SGD) "):
                    investment_total = t
                    continue
                if t.startswith("Grand Total (SGD"):
                    investment_grand_total = t
                    continue
                # A row that contains a Units token + a Currency + a Price +
                # a Valuation is an investment row.
                units = currency = price = valuation = ""
                name_words = []
                for w in ln:
                    if w["text"] in SIDEBAR_NOISE:
                        continue
                    if re.match(r"^[\d,]+\.\d{4}$", w["text"]) and UOB_INV_UNITS_X1[0] <= w["x1"] <= UOB_INV_UNITS_X1[1]:
                        units = w["text"]
                    elif re.match(r"^[A-Z]{3}$", w["text"]) and UOB_INV_CURRENCY_X[0] <= w["x0"] <= UOB_INV_CURRENCY_X[1]:
                        currency = w["text"]
                    elif re.match(r"^[\d,]+\.\d{4}$", w["text"]) and UOB_INV_PRICE_X1[0] <= w["x1"] <= UOB_INV_PRICE_X1[1]:
                        price = w["text"]
                    elif re.match(r"^[\d,]+\.\d{2,4}$", w["text"]) and UOB_INV_VAL_X1[0] <= w["x1"] <= UOB_INV_VAL_X1[1]:
                        valuation = w["text"]
                    else:
                        name_words.append(w)
                if units and currency and valuation:
                    investments.append({
                        "name": " ".join(w["text"] for w in name_words).strip(),
                        "units": units,
                        "currency": currency,
                        "price": price,
                        "valuation": valuation,
                    })

        # If a deposit row's numeric columns (Currency | Credit Line | Interest
        # Earned | Interest Charged | Balance) are on a separate line, walk
        # forward to attach them. In the sample PDF, the numeric row is the
        # one directly above the dashed account-number line.
        for d in deposits:
            # The numeric row sits 1 line above the dashed account number line
            # in the sample. We re-derive it by searching the page for a line
            # whose first word matches the account name's first word and which
            # contains numeric tokens at the right x-edges.
            name_first = d["name"].split()[0] if d["name"] else ""
            for ln in lines:
                ln_text = " ".join(w["text"] for w in ln)
                if not ln_text.startswith(name_first + " "):
                    continue
                if UOB_ACC_NO_RE.search(ln_text):
                    continue
                # Capture: currency, credit_line, interest_earned,
                # interest_charged, balance.
                # Currency column (x0 ≈ 197, aligns with "Currency" header).
                ccy_word = next((w["text"] for w in ln if 195 <= w["x0"] <= 230), None)
                if ccy_word:
                    d["currency"] = ccy_word
                elif not d["currency"]:
                    d["currency"] = "SGD"  # UOB SG portfolio: default to SGD
                for w in ln:
                    _bt, _bt_neg = _balance_token(w["text"])
                    if _bt is None:
                        continue
                    if 280 <= w["x1"] <= 305:
                        d["credit_line"] = d["credit_line"] or w["text"]
                    elif 355 <= w["x1"] <= 390:
                        d["interest_earned"] = d["interest_earned"] or w["text"]
                    elif 440 <= w["x1"] <= 470:
                        d["interest_charged"] = d["interest_charged"] or w["text"]
                    elif 500 <= w["x1"] <= 560:
                        # Preserve the sign: an OD-suffixed balance is negative.
                        d["balance"] = d["balance"] or (("-" + _bt) if _bt_neg else _bt)
                break

        # (handled via the shared helper, which also returns fx_rates for
        # the last page of the PDF — we accumulate across pages).

    return {
        "meta": meta,
        "overview": overview,
        "deposits": deposits,
        "investments": investments,
        "unit_trust": unit_trust,
        "deposit_total": deposit_total,
        "deposit_grand_total": deposit_grand_total,
        "investment_total": investment_total,
        "investment_grand_total": investment_grand_total,
    }


_UOB_ONE_CURRENCY_RE = re.compile(r"^([A-Z]{3})\s+\1\s+\1$")


def _uob_one_year(meta: dict[str, Any]) -> str:
    """Return the year to use for date normalization from statement metadata."""
    year_src = str(meta.get("period_end") or meta.get("period_start") or "")
    parts = year_src.split()
    if len(parts) == 3 and parts[2].isdigit():
        return parts[2]
    return str(datetime.now().year)


def _uob_one_first_txn_index(lines: list[list[WordDict]]) -> int:
    """Return the index of the first transaction row in a section.

    Header lines are skipped until we see a BALANCE B/F row or a date token.
    """
    for i, ln in enumerate(lines):
        text = " ".join(w["text"] for w in ln)
        if re.match(r"^(\d{1,2}\s+[A-Za-z]{3}\s+)?BALANCE\s+B/F(\s+[\d,]+\.\d{2})?$", text):
            return i
        day_words = [w for w in ln if UOB_TXN_DATE_DAY_X[0] <= w["x0"] <= UOB_TXN_DATE_DAY_X[1]]
        mon_words = [w for w in ln if UOB_TXN_DATE_MON_X[0] <= w["x0"] <= UOB_TXN_DATE_MON_X[1]]
        if (day_words and mon_words
                and DATE_UOB_RE.match(f"{day_words[0]['text']} {mon_words[0]['text']}")):
            return i
    return len(lines)


def _parse_uob_one_section(lines: list[list[WordDict]]) -> dict[str, Any] | None:
    """Parse one Account Transaction Details section.

    Returns a dict with name, account_no, currency, opening_balance,
    transactions, and total. Returns None if no account number is found.
    """
    # Locate the account-name / account-number line near the top of the section.
    account_name = ""
    account_no = ""
    for j in range(0, min(4, len(lines))):
        head_text = " ".join(w["text"] for w in lines[j])
        m_acc = UOB_ACC_NO_RE.search(head_text)
        if m_acc:
            account_no = m_acc.group(0)
            account_name = head_text[: m_acc.start()].strip()
            account_name = re.sub(r"\s*\(continued\)\s*$", "", account_name, flags=re.IGNORECASE)
            break

    if not account_no:
        return None

    start_idx = _uob_one_first_txn_index(lines)

    # Infer the section currency from a "SGD SGD SGD" / "AUD AUD AUD" sub-header.
    currency = ""
    for ln in lines[:start_idx]:
        t = " ".join(w["text"] for w in ln)
        m = _UOB_ONE_CURRENCY_RE.match(t)
        if m:
            currency = m.group(1)
            break

    transactions: list[dict[str, Any]] = []
    opening_balance = ""
    total = {"withdrawal": "", "deposit": "", "balance": ""}
    pending: dict[str, Any] | None = None

    i = start_idx
    while i < len(lines):
        ln = lines[i]
        text = " ".join(w["text"] for w in ln)

        # End-of-section Total row.
        if text.startswith("Total "):
            for w in ln:
                if not is_bank_num(w["text"]):
                    continue
                if UOB_TXN_WITHDRAWAL_X1[0] <= w["x1"] <= UOB_TXN_WITHDRAWAL_X1[1]:
                    total["withdrawal"] = w["text"]
                elif UOB_TXN_DEPOSIT_X1[0] <= w["x1"] <= UOB_TXN_DEPOSIT_X1[1]:
                    total["deposit"] = w["text"]
                elif UOB_TXN_BALANCE_X1[0] <= w["x1"] <= UOB_TXN_BALANCE_X1[1]:
                    total["balance"] = w["text"]
            break

        # BALANCE B/F — opening balance, may carry a date prefix.
        m_bf = re.match(r"^(\d{1,2}\s+[A-Za-z]{3}\s+BALANCE\s+B/F)\s+([\d,]+\.\d{2})$", text)
        if not m_bf:
            m_bf = re.match(r"^BALANCE\s+B/F\s+([\d,]+\.\d{2})$", text)
        if m_bf:
            opening_balance = m_bf.group(2) if m_bf.lastindex and m_bf.lastindex >= 2 else m_bf.group(1)
            if not re.match(r"^[\d,]+\.\d{2}$", opening_balance):
                opening_balance = m_bf.group(2) if m_bf.lastindex and m_bf.lastindex >= 2 else ""
            i += 1
            continue

        # New transaction row beginning with a date token.
        day_words = [w for w in ln if UOB_TXN_DATE_DAY_X[0] <= w["x0"] <= UOB_TXN_DATE_DAY_X[1]]
        mon_words = [w for w in ln if UOB_TXN_DATE_MON_X[0] <= w["x0"] <= UOB_TXN_DATE_MON_X[1]]
        if day_words and mon_words and DATE_UOB_RE.match(f"{day_words[0]['text']} {mon_words[0]['text']}"):
            if pending:
                transactions.append(pending)
            txn_date = f"{day_words[0]['text']} {mon_words[0]['text']}"
            desc_words = [
                w for w in ln
                if UOB_TXN_DESC_X_START <= w["x0"] <= UOB_TXN_DESC_X_MAX
                and not is_bank_num(w["text"])
                and w not in day_words
                and w not in mon_words
            ]
            desc = " ".join(w["text"] for w in desc_words).strip()
            withdrawal = deposit = balance = ""
            for w in ln:
                if not is_bank_num(w["text"]):
                    continue
                if UOB_TXN_WITHDRAWAL_X1[0] <= w["x1"] <= UOB_TXN_WITHDRAWAL_X1[1]:
                    withdrawal = w["text"]
                elif UOB_TXN_DEPOSIT_X1[0] <= w["x1"] <= UOB_TXN_DEPOSIT_X1[1]:
                    deposit = w["text"]
                elif UOB_TXN_BALANCE_X1[0] <= w["x1"] <= UOB_TXN_BALANCE_X1[1]:
                    balance = w["text"]
            pending = {
                "txn_date": txn_date,
                "description": desc,
                "withdrawal": withdrawal,
                "deposit": deposit,
                "balance": balance,
            }
            i += 1
            continue

        # Continuation line: words in the description band, no date tokens.
        if (pending is not None
                and not day_words
                and any(UOB_TXN_DESC_X_START <= w["x0"] <= UOB_TXN_DESC_X_MAX for w in ln)
                and not text.startswith("Page ")):
            clean = " ".join(
                w["text"] for w in ln
                if UOB_TXN_DESC_X_START <= w["x0"] <= UOB_TXN_DESC_X_MAX
            ).strip()
            if clean and not re.match(r"^\d+\s+of\s+\d+$", clean):
                pending["description"] = (pending["description"] + " " + clean).strip()
        i += 1

    if pending:
        transactions.append(pending)

    return {
        "name": account_name,
        "account_no": account_no,
        "currency": currency,
        "opening_balance": opening_balance,
        "transactions": transactions,
        "total": total,
    }


def parse_uob_one(pdf: Any):
    """Parse a UOB One multi-account transaction statement.

    Returns (meta, accounts, summary) where:
      - meta     — statement metadata (period, currency, pages, as_at).
      - accounts — per-currency transaction sections (unchanged).
      - summary  — dict with the Account Overview deposits total, the
        per-account Deposits summary table, Totals/Grand Total,
        and FX reference rates.
    """
    meta = {
        "bank": "UOB",
        "period_start": "",
        "period_end": "",
        "as_at": "",
        "currency": "SGD",
        "page_count": len(pdf.pages),
    }
    accounts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    # Locked-amount line counter — persists across non-txn pages because
    # locked amount lines appear in the same order as deposit rows but may
    # span multiple pages (e.g. 8 lines on page 0, 3 on page 1).
    _lock_idx = 0

    # Summary data from page 0/1 (Account Overview / Deposits table).
    summary: dict[str, Any] = {
        "deposits_total": "",
        "deposits": [],
        "deposit_totals": [],
        "deposit_grand_total": "",
    }
    in_deposits_section = False

    for page in pdf.pages:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines = group_lines(words)
        all_text = "\n".join(" ".join(w["text"] for w in ln) for ln in lines)

        m_period = re.search(
            r"Period:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            all_text,
        )
        if m_period and not meta["period_start"]:
            meta["period_start"] = m_period.group(1)
            meta["period_end"] = m_period.group(2)

        m_asat = re.search(r"as at\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", all_text)
        if m_asat and not meta["as_at"]:
            meta["as_at"] = m_asat.group(1)

        # --- Account Overview / Deposits summary (page 0 and 1) ---
        # These pages do NOT contain "Account Transaction Details", so we
        # detect them by the "Account Overview" or "Deposits" header.
        has_txn = any(
            " ".join(w["text"] for w in ln) == "Account Transaction Details"
            for ln in lines
        )
        if not has_txn:
            # Parse the Deposits total from "Deposits <amount>" on the
            # Account Overview line (page 0).
            if not summary["deposits_total"]:
                m_dep = re.search(r"^Deposits\s+([\d,]+\.\d{2})$", all_text, re.MULTILINE)
                if m_dep:
                    summary["deposits_total"] = m_dep.group(1)

            # Detect the Deposits summary table region.
            for ln in lines:
                t = " ".join(w["text"] for w in ln)
                if t.strip() == "Deposits":
                    in_deposits_section = True
                    break

            if in_deposits_section:
                # Parse deposit rows from the Deposits table.
                for idx, ln in enumerate(lines):
                    t = " ".join(w["text"] for w in ln)
                    if t.strip() in ("Deposits", "Current"):
                        continue
                    if t.strip().startswith("Currency Credit Line"):
                        continue
                    if t.strip().startswith("Locked Amount"):
                        continue
                    if t.strip().startswith("Page "):
                        continue
                    if t.strip().startswith("ONE Account Interest Overview"):
                        in_deposits_section = False
                        break
                    if t.strip().startswith("-----------------------------------------------------------------"):
                        in_deposits_section = False
                        break

                    # --- Total / Grand Total rows ---
                    m_total = re.match(r"^Total\s+\(([A-Z]{3})\)\s+([\d,]+\.\d{2})$", t)
                    if m_total:
                        summary["deposit_totals"].append(
                            (m_total.group(1), m_total.group(2))
                        )
                        continue
                    m_gt = re.match(
                        r"Grand Total\s+\(SGD Equivalent\s*([*]?)\)\s+([\d,]+\.\d{2})", t
                    )
                    if m_gt:
                        summary["deposit_grand_total"] = t
                        continue

                    # A deposit data row has numeric tokens at the deposit
                    # x-bands (credit line, interest earned, interest charged,
                    # balance).
                    credit_line = interest_earned = interest_charged = balance = ""
                    ccy_token = ""
                    acc_no = ""
                    name_words: list[WordDict] = []
                    for w in ln:
                        if w["text"] in SIDEBAR_NOISE:
                            continue
                        if re.match(r"^[A-Z]{3}$", w["text"]) and 195 <= w["x0"] <= 215:
                            ccy_token = w["text"]
                            continue
                        _bt, _bt_neg = _balance_token(w["text"])
                        if _bt is None:
                            name_words.append(w)
                            continue
                        if 280 <= w["x1"] <= 305:
                            credit_line = credit_line or w["text"]
                        elif 355 <= w["x1"] <= 390:
                            interest_earned = interest_earned or w["text"]
                        elif 440 <= w["x1"] <= 470:
                            interest_charged = interest_charged or w["text"]
                        elif 500 <= w["x1"] <= 560:
                            # Preserve the sign: an OD-suffixed balance is negative.
                            balance = balance or (("-" + _bt) if _bt_neg else _bt)

                    # Only process if we detected meaningful numeric data.
                    if not (bool(credit_line) or bool(balance)):
                        continue

                    currency = ccy_token if ccy_token else ""
                    prev_name = ""

                    # FX+ style: account number is inline on the data line.
                    m_acc = UOB_ACC_NO_RE.search(t)
                    if m_acc:
                        acc_no = m_acc.group(0)
                        # Name is on the previous line.
                        if idx >= 1:
                            prev_t = " ".join(w["text"] for w in lines[idx - 1])
                            if prev_t.strip() not in (
                                "Deposits", "Current",
                                "Currency Credit Line Interest Earned2 Interest Charged2 Balance",
                            ):
                                prev_name = prev_t.strip()
                        # Strip account-number tokens from name_words.
                        name_words = [w for w in name_words
                                      if not UOB_ACC_NO_RE.search(w["text"])]

                    # One Account style: check if the NEXT line is an
                    # account-number-only line.
                    if not acc_no and idx + 1 < len(lines):
                        next_t = " ".join(w["text"] for w in lines[idx + 1])
                        m_next = UOB_ACC_NO_RE.match(next_t.strip())
                        if m_next and next_t.strip() == m_next.group(0):
                            acc_no = m_next.group(0)
                            # Name is the leading words of the current line.
                            kept = []
                            for w in ln:
                                if kept and w["x0"] - kept[-1]["x1"] > 50:
                                    break
                                keep = True
                                if re.match(r"^[A-Z]{3}$", w["text"]) and 195 <= w["x0"] <= 215:
                                    keep = False
                                if is_bank_num(w["text"]) and (
                                    280 <= w["x1"] <= 305 or 355 <= w["x1"] <= 390
                                    or 440 <= w["x1"] <= 470 or 500 <= w["x1"] <= 550
                                ):
                                    keep = False
                                if keep:
                                    kept.append(w)
                            prev_name = " ".join(w["text"] for w in kept).strip()

                    if not acc_no:
                        continue

                    name = prev_name if prev_name else (
                        " ".join(w["text"] for w in name_words).strip()
                    )

                    summary["deposits"].append({
                        "name": name,
                        "account_no": acc_no,
                        "currency": currency,
                        "credit_line": credit_line,
                        "interest_earned": interest_earned,
                        "interest_charged": interest_charged,
                        "balance": balance,
                        "locked_amount": "",
                    })

                # Parse Locked Amount lines (attach to the corresponding deposit
                # row by position — locked amount lines appear in the same
                # order as deposit rows, potentially spanning multiple pages).
                for ln in lines:
                    t = " ".join(w["text"] for w in ln)
                    m_lock = re.search(
                        r"Locked Amount\d?\s+as of\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+is\s+([\d,]+\.\d{2})",
                        t,
                    )
                    if m_lock and _lock_idx < len(summary["deposits"]):
                        summary["deposits"][_lock_idx]["locked_amount"] = m_lock.group(1)
                        _lock_idx += 1

            continue  # skip transaction parsing for non-txn pages

        # --- Transaction pages ---
        # Find all account-header lines on this page. A header line contains a
        # dashed UOB account number near the left edge (e.g. "One Account
        # XXX-XXX-XXX-X" or "FX+ XXX-XXX-XXX-X"). Transaction lines never carry
        # this account-number format, so this is a reliable section delimiter.
        header_indices = []
        for i, ln in enumerate(lines):
            for w in ln:
                if w["x0"] < 150 and UOB_ACC_NO_RE.search(w["text"]):
                    header_indices.append(i)
                    break
        header_indices.append(len(lines))

        for sec_idx in range(len(header_indices) - 1):
            start = header_indices[sec_idx]
            end = header_indices[sec_idx + 1]
            section = _parse_uob_one_section(lines[start:end])
            if not section:
                continue

            # Append currency to the account name for FX+ sub-accounts so each
            # section is visually distinct in the rendered Markdown.
            name = section["name"]
            currency = section["currency"]
            if currency and f"({currency})" not in name:
                name = f"{name} ({currency})"
            section["name"] = name

            if current and current["account_no"] == section["account_no"]:
                # Same account continues from a previous page.
                current["transactions"].extend(section["transactions"])
                if any(section["total"].values()):
                    current["total"] = section["total"]
            else:
                if current:
                    accounts.append(current)
                current = section

        # carry both End of Transaction Details.

    if current:
        accounts.append(current)

    # Normalize transaction dates to ISO using the statement period year.
    year = _uob_one_year(meta)
    for account in accounts:
        for t in account["transactions"]:
            try:
                d, mon_name = t["txn_date"].split()
                mm = MONTH_MAP[mon_name.lower()]
                t["txn_date"] = f"{year}-{mm:02d}-{int(d):02d}"
            except (KeyError, ValueError):
                pass

    return meta, accounts, summary






