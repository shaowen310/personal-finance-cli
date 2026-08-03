"""DBS/POSB Consolidated Statement parser.

Parses multi-page DBS consolidated statements that include:
  - **Account Summary** (page 0): Deposits overview (CASA accounts, Fixed Deposits)
  - **SRS Summary** (page 1): Supplementary Retirement Scheme account, contributions,
    Unit Trust holdings
  - **Transaction Details** (pages 2-4): per-account transaction tables with
    Date / Description / Withdrawal(-) / Deposit(+) / Balance(SGD) columns,
    plus Fixed Deposit transaction tables with a 7-column FD-specific layout.

Usage via ``convert_statement.py``::

    from pfa_parser.parsers.dbs_parser import parse_dbs
    meta, summary, accounts, srs_data = parse_dbs(pdf)
"""
from __future__ import annotations

import re
from typing import Any

from pfa_ir_schema.common import PDF, WordDict, group_lines, is_bank_num

# ---------------------------------------------------------------------------
# Column x-positions (PDF points, measured empirically from the DBS source PDF)
# ---------------------------------------------------------------------------

# --- Transaction table (Date / Description / Withdrawal(-) / Deposit(+) / Balance) ---
DBS_TXN_DATE_X = (40, 92)      # "DD/MM/YYYY"
DBS_TXN_DESC_X_START = 100     # Description begins here
DBS_TXN_WITHDRAWAL_X1 = (350, 400)  # right edge of withdrawal column
DBS_TXN_DEPOSIT_X1 = (425, 480)     # right edge of deposit column
DBS_TXN_BALANCE_X1 = (505, 560)     # right edge of balance column

# --- SRS Transaction table (page 4) ---
# Same columns but slightly shifted x positions.
DBS_SRS_WITHDRAWAL_X1 = (350, 375)
DBS_SRS_DEPOSIT_X1 = (430, 455)
DBS_SRS_BALANCE_X1 = (525, 555)

# --- Fixed Deposit transaction table (pages 3-4) ---
# Headers: Date | Deposit No. | Period | Description | Interest Amt | Principal | Interest Rate (% p.a)
DBS_FD_DATE_X = (40, 92)
DBS_FD_DEPOSIT_NO_X = (95, 170)
DBS_FD_PERIOD_X = (185, 290)
DBS_FD_DESC_X_START = 295
DBS_FD_INTEREST_AMT_X1 = (420, 460)
DBS_FD_PRINCIPAL_X1 = (505, 560)
DBS_FD_RATE_X1 = (510, 560)

# FD numeric values: 2dp amounts (principal/interest) and 6dp rates both occur
# in the rightmost column band, so the parser must treat 6dp numbers as numbers
# (the rate) and not let them leak into the description.
_FD_AMOUNT_RE = re.compile(r"^\d{1,3}(?:,\d{3})*\.\d{2,6}$")


def _looks_like_rate(text: str) -> bool:
    """True for a rate value in the Principal/Rate column, e.g. ``2.450000``.

    Rates carry >=3 decimal places and no thousands separator, unlike
    principal/interest amounts which have 2dp and may carry commas. This
    distinguishes the two values that share the same x1 column band.
    """
    return bool(re.match(r"^\d+\.\d{3,}$", text))

# --- Account Summary table (page 0) ---
DBS_SUMMARY_ACCT_NAME_X = (38, 180)
DBS_SUMMARY_ACCT_NO_X = (265, 350)
DBS_SUMMARY_BAL_BASE_X1 = (440, 485)
DBS_SUMMARY_BAL_SGD_X1 = (510, 560)

# --- SRS Summary / Unit Trusts (page 1) ---
DBS_SRS_ACCT_NO_X = (250, 340)
DBS_UT_QTY_X1 = (200, 255)
DBS_UT_COST_X1 = (330, 375)
DBS_UT_MVAL_X1 = (420, 465)
DBS_UT_PL_X1 = (520, 560)

# --- Date regex ---
DATE_DBS_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# DBS account number pattern (varied lengths with dash separators).
DBS_ACCT_NO_RE = re.compile(
    r"\b\d{1,4}[-.]\d{1,8}[-.]\d{1,8}([-.]\d{1,8})?\b"
)

# FD deposit number: 12 digits
DBS_FD_DEP_NO_RE = re.compile(r"^\d{12}$")

# FD period: "02/06/2025 - 02/06/2026"
DBS_FD_PERIOD_RE = re.compile(r"\d{2}/\d{2}/\d{4}\s*-\s*\d{2}/\d{2}/\d{4}")

# DBS-specific sidebar noise (rotated margin text on every page footer).
# These words appear at x0 ≈ 11 and should be filtered out.
DBS_SIDEBAR_NOISE = {
    ".oN", "geR", "ziB", "BSOP", "3-0810058-RM",
    ":oN", "TSG", ".geR", ".oC", "SBD", ")4102/80(",
    "A84108825", "E603008691", "67800002004GS",
    "PDS_MMCON_LOC_ONSH_1068017600000047_02874",
}

# CID font noise — converted font glyphs that appear as "(cid:...)" in extraction.
CID_RE = re.compile(r"\(cid:\d+\)")


def _filter_line_words(words: list[WordDict]) -> list[WordDict]:
    """Remove sidebar noise and CID font artefacts from a list of words."""
    return [
        w for w in words
        if w["text"] not in DBS_SIDEBAR_NOISE
        and not CID_RE.match(w["text"])
        and w["x0"] > 20  # exclude extreme-left sidebar text
    ]


def _line_text(words: list[WordDict]) -> str:
    """Join a list of word dicts into a single string."""
    return " ".join(w["text"] for w in words)


# ============================================================================
# Main parser
# ============================================================================

def parse_dbs(pdf: PDF) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Parse a DBS/POSB Consolidated Statement PDF.

    Returns a 4-tuple ``(meta, summary, accounts, srs_data)`` where:

    - **meta** — dict with ``statement_date``, ``sn``, ``page_count``,
      ``currency``, and ``account_holder``.
    - **summary** — dict with ``deposits`` (CASA account rows),
      ``fixed_deposits`` (FD account rows), and ``totals`` for each group.
    - **accounts** — list of per-account transaction dicts, each with
      ``name``, ``account_no``, ``currency``, ``transactions``,
      ``opening_balance``, and optionally ``total`` (Total row values).
    - **srs_data** — dict with ``account_no``, ``cash_balance``, ``total``,
      ``contributions`` (limit / made / remaining), and a ``unit_trusts`` list.
      The SRS transactions live in the ``"SRS Account"`` record inside
      ``accounts``; this dict carries the SRS summary + holdings only.
    """
    meta: dict[str, Any] = {
        "statement_date": "",
        "sn": "",
        "page_count": len(pdf.pages),
        "currency": "SGD",
        "account_holder": "",
    }

    # --- Summary data ---
    summary: dict[str, Any] = {
        "deposits": [],        # [{name, account_no, currency, balance}]
        "fixed_deposits": [],  # [{name, account_no, balance}]
        "casa_total": "",
        "fd_total": "",
    }

    # --- SRS data ---
    srs_data: dict[str, Any] = {
        "total": "",
        "account_no": "",
        "cash_balance": "",
        "contributions": {"max": "", "made": "", "remaining": ""},
        "unit_trusts": [],  # [{name, free_qty, total_cost, market_value, unrealised_pl}]
    }

    accounts: list[dict[str, Any]] = []

    for pg_idx, page in enumerate(pdf.pages):
        if pg_idx >= 5:
            continue

        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines = group_lines(words)

        # Filter each line
        clean_lines = [_filter_line_words(ln) for ln in lines]
        # Remove empty lines after filtering
        clean_lines = [ln for ln in clean_lines if ln]

        if pg_idx == 0:
            _parse_page0_summary(clean_lines, meta, summary)
        elif pg_idx == 1:
            _parse_page1_srs(clean_lines, srs_data)
        elif pg_idx in (2, 3, 4):
            _parse_transaction_pages(clean_lines, accounts)

    # ---- Merge duplicate accounts across pages ----
    accounts = _merge_accounts(accounts)

    # ---- Normalize dates to ISO ----
    _normalize_dates(accounts)

    return meta, summary, accounts, srs_data


# ============================================================================
# Page 0: Account Summary
# ============================================================================

def _parse_page0_summary(lines: list[list[WordDict]], meta: dict[str, Any], summary: dict[str, Any]) -> None:
    """Parse the Account Summary page (page 0)."""
    full_text = "\n".join(_line_text(ln) for ln in lines)

    # Statement date from "Account Summary as at 30 Jun 2026"
    m = re.search(r"Account\s+Summary\s+as\s+at\s+(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", full_text)
    if m:
        meta["statement_date"] = m.group(1)

    # S/N
    m = re.search(r"S/N:\s*([A-Z0-9]+)", full_text)
    if m:
        meta["sn"] = m.group(1)

    # Account holder — scan for the name pattern (starts with uppercase surname or address prefix)
    holder_lines = []
    for ln in lines:
        text = _line_text(ln)
        if text.startswith(("BLK", "#", "SINGAPORE")):
            holder_lines.append(text)
    if holder_lines:
        meta["account_holder"] = " ".join(holder_lines)

    # --- Parse CASA (Current and Savings Account) total ---
    m = re.search(
        r"Current\s+and\s+Savings\s+Account\s+Total:\s+SGD\s+Equivalent\s+([\d,]+\.\d{2})",
        full_text,
    )
    if m:
        summary["casa_total"] = m.group(1)

    # --- Parse Fixed Deposit total ---
    m = re.search(
        r"Fixed\s+Deposit\s+Total:\s+SGD\s+Equivalent\s+([\d,]+\.\d{2})",
        full_text,
    )
    if m:
        summary["fd_total"] = m.group(1)

    # --- Parse deposit account rows ---
    # Scan for account-table rows by detecting lines with account numbers in the
    # summary-table column bands.  Account numbers use ``DBS_ACCT_NO_RE``.
    # DBS prints a separate sub-header before the Fixed-Deposit table:
    #   Account  Account No.  Balance  Balance
    #           (Base Currency)  (SGD Equivalent)
    # We detect that sub-header to switch from CASA → FD.
    #
    # Multi-currency "My Account" rows: the first row carries the account number
    # and SGD values; the second row (same visual line group) carries only the
    # foreign-currency data (e.g. "USD 0.10 0.12") with no account number.
    # We track the last CASA name + account_no so sub-rows can inherit them.
    in_fd = False
    last_casa_name = ""
    last_casa_acct_no = ""

    for ln in lines:
        text = _line_text(ln)

        # Detect FD sub-header: "Fixed Deposit ... Total: SGD Equivalent ..."
        if "Fixed Deposit" in text and "Total:" in text:
            in_fd = True
            continue
        # FD column-header row (second occurrence of the table header)
        if in_fd and "Account" in text and "No." in text and "Balance" in text:
            continue
        # Sub-header annotation line
        if "(Base Currency)" in text or "(SGD Equivalent)" in text:
            continue

        # Account number in the summary columns
        acct_no_words = [
            w for w in ln
            if DBS_SUMMARY_ACCT_NO_X[0] <= w["x0"] <= DBS_SUMMARY_ACCT_NO_X[1]
            and DBS_ACCT_NO_RE.search(w["text"])
        ]
        if not acct_no_words:
            # Multi-currency sub-row: no account number, but may carry a
            # foreign-currency balance that belongs to the previous CASA account.
            # Only inherit when the previous account was a CASA (not FD).
            if not in_fd and last_casa_acct_no:
                # Check if this line has a currency code and bank numbers
                sub_ccy = ""
                sub_base = ""
                sub_sgd = ""
                for w in ln:
                    if not is_bank_num(w["text"]) and re.match(r"^[A-Z]{3}$", w["text"]):
                        sub_ccy = w["text"]
                        continue
                    if not is_bank_num(w["text"]):
                        continue
                    if DBS_SUMMARY_BAL_SGD_X1[0] <= w["x1"] <= DBS_SUMMARY_BAL_SGD_X1[1]:
                        sub_sgd = w["text"]
                    elif DBS_SUMMARY_BAL_BASE_X1[0] <= w["x1"] <= DBS_SUMMARY_BAL_BASE_X1[1]:
                        sub_base = w["text"]
                if sub_ccy and (sub_base or sub_sgd):
                    summary["deposits"].append({
                        "name": last_casa_name,
                        "account_no": last_casa_acct_no,
                        "currency": sub_ccy,
                        "balance": sub_base or sub_sgd,
                    })
            continue

        acct_no = acct_no_words[0]["text"]
        if not re.match(r"^[\d\-\.]+$", acct_no):
            continue

        # Get currency and balance values
        base_balance = ""
        sgd_balance = ""
        ccy = ""

        for w in ln:
            if not is_bank_num(w["text"]) and re.match(r"^[A-Z]{3}$", w["text"]):
                ccy = w["text"]
                continue
            if not is_bank_num(w["text"]):
                continue
            if DBS_SUMMARY_BAL_SGD_X1[0] <= w["x1"] <= DBS_SUMMARY_BAL_SGD_X1[1]:
                sgd_balance = w["text"]
            elif DBS_SUMMARY_BAL_BASE_X1[0] <= w["x1"] <= DBS_SUMMARY_BAL_BASE_X1[1]:
                base_balance = w["text"]

        if not base_balance and not sgd_balance:
            continue

        # Name is from the left side of the line
        name_words = [w for w in ln if w["x0"] < 150]
        name = " ".join(w["text"] for w in name_words).strip()
        # Clean: remove currency codes and bank numbers from name
        name = re.sub(r"\s+[A-Z]{3}\s+[\d,]+\.\d{2}$", "", name).strip()
        if not name:
            continue

        # Determine currency from position if not already found
        if not ccy:
            ccy_words = [w for w in ln
                         if re.match(r"^[A-Z]{3}$", w["text"])
                         and 390 <= w["x0"] <= 425]
            if ccy_words:
                ccy = ccy_words[0]["text"]

        record = {
            "name": name,
            "account_no": acct_no,
            "currency": ccy,
            "balance": base_balance or sgd_balance,
        }

        if in_fd:
            summary["fixed_deposits"].append(record)
        else:
            summary["deposits"].append(record)
            # Track last CASA account so multi-currency sub-rows can inherit it
            last_casa_name = name
            last_casa_acct_no = acct_no


# ============================================================================
# Page 1: SRS Summary + Unit Trusts
# ============================================================================

def _parse_page1_srs(lines: list[list[WordDict]], srs_data: dict[str, Any]) -> None:
    """Parse SRS summary and Unit Trusts from page 1."""
    full_text = "\n".join(_line_text(ln) for ln in lines)

    # SRS Account Total
    m = re.search(
        r"Supplementary\s+Retirement\s+Scheme\s+Account\s+Total:\s+SGD\s+([\d,]+\.\d{2})",
        full_text,
    )
    if m:
        srs_data["total"] = m.group(1)

    # SRS Account No.
    for ln in lines:
        text = _line_text(ln)
        m = DBS_ACCT_NO_RE.search(text)
        if m:
            srs_data["account_no"] = m.group(0)
            break

    # Cash Balance
    m = re.search(r"SRS\s+Account\s+\S+\s+([\d,]+\.\d{2})", full_text)
    if m:
        srs_data["cash_balance"] = m.group(1)

    # Contributions
    m_max = re.search(r"Max\s+Contribution\s+Amount\s+([\d,]+\.\d{2})", full_text)
    m_made = re.search(r"Total\s+Contribution\s+Made\s+to\s+Date\s+([\d,]+\.\d{2})", full_text)
    m_rem = re.search(r"Balance\s+Contribution\s+Limit\s+([\d,]+\.\d{2})", full_text)
    if m_max:
        srs_data["contributions"]["max"] = m_max.group(1)
    if m_made:
        srs_data["contributions"]["made"] = m_made.group(1)
    if m_rem:
        srs_data["contributions"]["remaining"] = m_rem.group(1)

    # Unit Trusts table
    # Look for "Unit Trusts" section marker and parse rows after it
    in_ut = False
    for ln in lines:
        text = _line_text(ln)
        if text.startswith("l Unit Trusts"):
            in_ut = True
            continue
        if in_ut and text.startswith("Total:"):
            continue
        if in_ut and (
            text.startswith("Account Summary")
            or text.startswith("Page ")
            or "PDS_MMCON" in text
        ):
            break
        if not in_ut:
            continue
        if not text.strip():
            continue
        # Skip header line
        if text.startswith("Name Free Qty"):
            continue
        if text.startswith("(SGD)"):
            continue
        if "Profit/Loss" in text or "(SGD)" in text:
            continue

        # Parse UT row: Name ... | Free Qty | Total Cost | Market Value | Unrealised P/L
        name_parts = []
        qty = cost = mval = pl = ""
        for w in ln:
            if w["text"] in DBS_SIDEBAR_NOISE:
                continue
            # Check if word is a numeric value (2dp or 4dp)
            is_ut_num = bool(
                is_bank_num(w["text"])
                or re.match(r"^\d{1,3}(,\d{3})*\.\d{4}$", w["text"])
            )
            if is_ut_num:
                if DBS_UT_PL_X1[0] <= w["x1"] <= DBS_UT_PL_X1[1]:
                    pl = w["text"]
                elif DBS_UT_MVAL_X1[0] <= w["x1"] <= DBS_UT_MVAL_X1[1]:
                    mval = w["text"]
                elif DBS_UT_COST_X1[0] <= w["x1"] <= DBS_UT_COST_X1[1]:
                    cost = w["text"]
                elif DBS_UT_QTY_X1[0] <= w["x1"] <= DBS_UT_QTY_X1[1]:
                    qty = w["text"]
                continue
            # Non-numeric: part of name, unless it's a sub-header
            if w["x0"] < 200:
                name_parts.append(w["text"])
        if name_parts and (qty or cost or mval):
            srs_data["unit_trusts"].append({
                "name": " ".join(name_parts),
                "free_qty": qty,
                "total_cost": cost,
                "market_value": mval,
                "unrealised_pl": pl,
            })


# ============================================================================
# Pages 2-4: Transaction Details
# ============================================================================

def _parse_transaction_pages(lines: list[list[WordDict]], accounts: list[dict[str, Any]]) -> None:
    """Parse transaction pages (2, 3, 4)."""
    # Determine which sections are present on this page
    # Look for account headers (name + "Account No." pattern)
    sections = _split_into_sections(lines)

    for section_lines in sections:
        section_text = "\n".join(_line_text(ln) for ln in section_lines)

        if "DBS Savings Plus" in section_text and "Account No." in section_text:
            _parse_standard_txn_section(section_lines, accounts, "DBS Savings Plus Account")
        elif "My Account" in section_text and "Account No." in section_text:
            _parse_my_account_section(section_lines, accounts)
        elif "Fixed Deposit" in section_text and "Account No." in section_text:
            _parse_fd_section(section_lines, accounts)
        elif "Supplementary Retirement Scheme" in section_text and "Account No." in section_text:
            _parse_srs_txn_section(section_lines, accounts)


def _split_into_sections(lines: list[list[WordDict]]) -> list[list[list[WordDict]]]:
    """Split lines into sections based on account headers.

    Only lines that start a new account section (containing a known account-name
    prefix AND "Account No.") trigger a section boundary.  Lines before the first
    such header are discarded.
    """
    sections = []
    current = []

    for ln in lines:
        text = _line_text(ln)
        # Detect account header: line containing both account name and "Account No."
        has_acct_no = "Account No." in text
        has_acct_name = any(
            name in text for name in
            ("DBS Savings Plus", "My Account", "Fixed Deposit",
             "Supplementary Retirement Scheme")
        )

        if has_acct_no and has_acct_name:
            if current:
                sections.append(current)
            current = [ln]
        else:
            current.append(ln)

    if current:
        sections.append(current)

    return sections


def _parse_standard_txn_section(lines: list[list[WordDict]], accounts: list[dict[str, Any]], account_name: str) -> None:
    """Parse a standard transaction table (Date / Description / Withdrawal(-) / Deposit(+) / Balance)."""
    # Find the account number
    account_no = ""
    for ln in lines:
        text = _line_text(ln)
        if "Account No." in text:
            m = DBS_ACCT_NO_RE.search(text)
            if m:
                account_no = m.group(0)
            break

    if not account_no:
        return

    # Find the transaction table header
    header_idx = -1
    for i, ln in enumerate(lines):
        text = _line_text(ln)
        if "Date" in text and "Description" in text and "Withdrawal" in text:
            header_idx = i
            break

    if header_idx < 0:
        return

    # Parse transactions
    txns = []
    opening_balance = ""
    pending_txn = None
    total_withdrawal = ""
    total_deposit = ""
    total_balance = ""
    currency = "SGD"

    i = header_idx + 1
    while i < len(lines):
        ln = lines[i]
        text = _line_text(ln)
        if not text.strip():
            i += 1
            continue

        # Balance Brought Forward
        m_bf = re.search(r"Balance\s+Brought\s+Forward\s+([\d,]+\.\d{2})", text)
        if not m_bf:
            # Also check "BALANCE B/F" pattern
            m_bf = re.search(r"(?:Balance\s+Brought\s+Forward|Balance\s+B/F)\s+(?:SGD\s+)?([\d,]+\.\d{2})", text)
        if m_bf:
            opening_balance = m_bf.group(1)
            i += 1
            continue

        # Balance Brought Forward with currency prefix
        m_bf2 = re.search(r"Balance\s+Brought\s+Forward\s+([A-Z]{3})\s+([\d,]+\.\d{2})", text)
        if m_bf2:
            opening_balance = m_bf2.group(2)
            currency = m_bf2.group(1)
            i += 1
            continue

        # Total Balance Carried Forward
        if "Total Balance Carried Forward" in text:
            nums = [w for w in ln if is_bank_num(w["text"])]
            if len(nums) >= 3:
                total_withdrawal = nums[0]["text"]
                total_deposit = nums[1]["text"]
                total_balance = nums[2]["text"]
            elif len(nums) >= 1:
                total_balance = nums[-1]["text"]
            break

        # Balance Carried Forward
        if "Balance Carried Forward" in text:
            for w in ln:
                if is_bank_num(w["text"]) and DBS_TXN_BALANCE_X1[0] <= w["x1"] <= DBS_TXN_BALANCE_X1[1]:
                    total_balance = w["text"]
            break

        # CURRENCY: SINGAPORE DOLLAR / UNITED STATES DOLLAR
        if text.strip().startswith("CURRENCY:"):
            m_ccy = re.search(r"CURRENCY:\s*(.+?)\s*$", text)
            if m_ccy:
                ccy_name = m_ccy.group(1).strip()
                if "SINGAPORE" in ccy_name.upper():
                    currency = "SGD"
                elif "UNITED STATES" in ccy_name.upper():
                    currency = "USD"
            i += 1
            continue

        # "Indicative in SGD @" line - skip
        if text.startswith("Indicative in SGD"):
            i += 1
            continue

        # "4 4 4 4 4" decorative row
        if re.match(r"^[\d\s]+$", text.strip()) and len(text.strip().split()) >= 3:
            i += 1
            continue

        # New transaction: line begins with a DD/MM/YYYY date
        date_words = [
            w for w in ln
            if DBS_TXN_DATE_X[0] <= w["x0"] <= DBS_TXN_DATE_X[1]
            and DATE_DBS_RE.match(w["text"])
        ]
        if date_words:
            if pending_txn:
                txns.append(pending_txn)
            txn_date = date_words[0]["text"]

            # Collect description words (after date, before numeric columns)
            desc_words = [
                w for w in ln
                if DBS_TXN_DESC_X_START <= w["x0"]
                and not is_bank_num(w["text"])
                and w not in date_words
            ]
            desc = " ".join(w["text"] for w in desc_words).strip()

            withdrawal = deposit = balance = ""
            for w in ln:
                if not is_bank_num(w["text"]):
                    continue
                if DBS_TXN_WITHDRAWAL_X1[0] <= w["x1"] <= DBS_TXN_WITHDRAWAL_X1[1]:
                    withdrawal = w["text"]
                elif DBS_TXN_DEPOSIT_X1[0] <= w["x1"] <= DBS_TXN_DEPOSIT_X1[1]:
                    deposit = w["text"]
                elif DBS_TXN_BALANCE_X1[0] <= w["x1"] <= DBS_TXN_BALANCE_X1[1]:
                    balance = w["text"]

            pending_txn = {
                "txn_date": txn_date,
                "description": desc,
                "withdrawal": withdrawal,
                "deposit": deposit,
                "balance": balance,
            }
            i += 1
            continue

        # Continuation line: words in description band, no date
        if pending_txn is not None:
            clean_words = [
                w for w in ln
                if DBS_TXN_DESC_X_START <= w["x0"] < 350
            ]
            if clean_words:
                clean = " ".join(w["text"] for w in clean_words).strip()
                if clean and not clean.startswith("Page ") and "PDS_MMCON" not in clean:
                    pending_txn["description"] = (pending_txn["description"] + " " + clean).strip()

        i += 1

    if pending_txn:
        txns.append(pending_txn)

    total = {}
    if total_withdrawal or total_deposit or total_balance:
        total = {
            "withdrawal": total_withdrawal,
            "deposit": total_deposit,
            "balance": total_balance,
        }

    accounts.append({
        "name": account_name,
        "account_no": account_no,
        "currency": currency,
        "opening_balance": opening_balance,
        "transactions": txns,
        "total": total,
    })


def _parse_my_account_section(lines: list[list[WordDict]], accounts: list[dict[str, Any]]) -> None:
    """Parse My Account section (can have SGD and USD sub-sections)."""
    account_no = ""
    for ln in lines:
        text = _line_text(ln)
        if "Account No." in text:
            m = DBS_ACCT_NO_RE.search(text)
            if m:
                account_no = m.group(0)
            break

    if not account_no:
        return

    # Parse inline: track current currency as we scan lines.
    # Reuse the first "Date Description Withdrawal..." header for each sub-section.
    # Sub-sections are separated by "CURRENCY:" headers.
    _parse_my_account_inline(lines, accounts, account_no)


def _parse_my_account_inline(lines: list[list[WordDict]], accounts: list[dict[str, Any]], account_no: str) -> None:
    """Parse My Account transactions with inline currency tracking.

    Sub-sections are delimited by ``CURRENCY: <name>`` lines.  Each sub-section
    reuses the first ``Date Description ...`` header line found in the section.
    """
    current_ccy = ""
    current_data: dict[str, Any] | None = None  # dict with txns, opening, total

    def _flush():
        nonlocal current_data, current_ccy
        if current_data is not None and current_ccy:
            accounts.append({
                "name": f"My Account ({current_ccy})",
                "account_no": account_no,
                "currency": current_ccy,
                **current_data,
            })
        current_data = None

    i = 0
    while i < len(lines):
        ln = lines[i]
        text = _line_text(ln)

        if text.strip().startswith("CURRENCY:"):
            _flush()
            m = re.search(r"CURRENCY:\s*(.+?)\s*$", text)
            if m:
                ccy_name = m.group(1).strip()
                if "SINGAPORE" in ccy_name.upper():
                    current_ccy = "SGD"
                elif "UNITED STATES" in ccy_name.upper():
                    current_ccy = "USD"
                else:
                    current_ccy = ccy_name.split()[-1] if ccy_name.split() else ccy_name
            current_data = {"transactions": [], "opening_balance": "", "total": {}}
            i += 1
            continue

        if current_data is None:
            i += 1
            continue

        # Balance Brought Forward
        m_bf = re.search(r"Balance\s+Brought\s+Forward\s+(?:([A-Z]{3})\s+)?([\d,]+\.\d{2})", text)
        if m_bf:
            current_data["opening_balance"] = m_bf.group(2)
            i += 1
            continue

        # Total Balance Carried Forward (don't break — USD section may follow)
        if "Total Balance Carried Forward" in text:
            nums = [w for w in ln if is_bank_num(w["text"])]
            if len(nums) >= 3:
                current_data["total"] = {
                    "withdrawal": nums[0]["text"],
                    "deposit": nums[1]["text"],
                    "balance": nums[2]["text"],
                }
            elif len(nums) >= 1:
                current_data["total"] = {"balance": nums[-1]["text"]}
            i += 1
            continue

        # Indicative in SGD — end of USD sub-section
        if text.startswith("Indicative in SGD"):
            break

        # New transaction: DD/MM/YYYY
        date_words = [
            w for w in ln
            if DBS_TXN_DATE_X[0] <= w["x0"] <= DBS_TXN_DATE_X[1]
            and DATE_DBS_RE.match(w["text"])
        ]
        if date_words:
            txn_date = date_words[0]["text"]
            desc_words = [
                w for w in ln
                if DBS_TXN_DESC_X_START <= w["x0"]
                and not is_bank_num(w["text"])
                and w not in date_words
            ]
            desc = " ".join(w["text"] for w in desc_words).strip()

            withdrawal = deposit = balance = ""
            for w in ln:
                if not is_bank_num(w["text"]):
                    continue
                if DBS_TXN_WITHDRAWAL_X1[0] <= w["x1"] <= DBS_TXN_WITHDRAWAL_X1[1]:
                    withdrawal = w["text"]
                elif DBS_TXN_DEPOSIT_X1[0] <= w["x1"] <= DBS_TXN_DEPOSIT_X1[1]:
                    deposit = w["text"]
                elif DBS_TXN_BALANCE_X1[0] <= w["x1"] <= DBS_TXN_BALANCE_X1[1]:
                    balance = w["text"]

            current_data["transactions"].append({
                "txn_date": txn_date,
                "description": desc,
                "withdrawal": withdrawal,
                "deposit": deposit,
                "balance": balance,
            })

        i += 1

    _flush()


# (unused function _split_my_account_currencies removed)


def _classify_fd_txn_type(desc: str) -> str:
    """Classify an FD row as a 'placement' (fixed deposit record) or a 'movement'.

    A row is a *movement* (transaction only, not an FD record) when it removes
    the deposit before maturity. Signal: the word "premature" (DBS prints the
    premature-withdrawal penalty as a "Interest Due To Premature Withdrawal"
    line). A plain maturity "Withdrawal" that credits the principal back is
    still a placement, as are "New Rollover Deposit" and other placements.
    """
    return (
        "movement"
        if re.search(r"premature", desc, re.I)
        else "placement"
    )


def _parse_fd_section(lines: list[list[WordDict]], accounts: list[dict[str, Any]]) -> None:
    """Parse Fixed Deposit transaction table."""
    account_no = ""
    for ln in lines:
        text = _line_text(ln)
        if "Account No." in text:
            m = DBS_ACCT_NO_RE.search(text)
            if m:
                account_no = m.group(0)
            break

    if not account_no:
        return

    # Find the FD table header
    header_idx = -1
    for i, ln in enumerate(lines):
        text = _line_text(ln)
        if "Date" in text and "Deposit" in text and "Period" in text:
            header_idx = i
            break

    if header_idx < 0:
        return

    fd_txns = []
    pending_fd = None

    i = header_idx + 1
    while i < len(lines):
        ln = lines[i]
        text = _line_text(ln)
        if not text.strip():
            i += 1
            continue

        # End conditions
        if text.startswith("Total Principal Amount"):
            break
        if text.startswith("Transaction Details"):
            break
        if text.startswith("Supplementary Retirement Scheme"):
            break
        if text.startswith("Page ") and "of" in text:
            break
        if "PDS_MMCON" in text:
            break

        # New FD row: starts with DD/MM/YYYY
        date_words = [
            w for w in ln
            if DBS_TXN_DATE_X[0] <= w["x0"] <= DBS_TXN_DATE_X[1]
            and DATE_DBS_RE.match(w["text"])
        ]
        if date_words:
            if pending_fd:
                fd_txns.append(pending_fd)
            txn_date = date_words[0]["text"]

            deposit_no = ""
            period = ""
            desc_parts = []
            interest_amt = ""
            principal = ""
            interest_rate = ""

            # Parse period from text via regex
            m_period = DBS_FD_PERIOD_RE.search(text)
            if m_period:
                period = m_period.group(0)

            # Parse deposit number
            for w in ln:
                if w in date_words:
                    continue
                if DBS_FD_DEPOSIT_NO_X[0] <= w["x0"] <= DBS_FD_DEPOSIT_NO_X[1] and DBS_FD_DEP_NO_RE.match(w["text"]):
                    deposit_no = w["text"]
                    continue

            # If no deposit_no on this line (continuation sub-row), carry from previous
            if not deposit_no and fd_txns:
                deposit_no = fd_txns[-1]["deposit_no"]

            # Description and numeric values from x0 >= DESC_X_START.
            # The rate column shares the same x1 band as principal, so a value
            # there with >=3 dp (no comma) is the rate; otherwise it is principal.
            for w in ln:
                if w["x0"] < DBS_FD_DESC_X_START:
                    continue
                if is_bank_num(w["text"]) or _FD_AMOUNT_RE.match(w["text"]):
                    if DBS_FD_INTEREST_AMT_X1[0] <= w["x1"] <= DBS_FD_INTEREST_AMT_X1[1]:
                        if not interest_amt:
                            interest_amt = w["text"]
                    elif DBS_FD_PRINCIPAL_X1[0] <= w["x1"] <= DBS_FD_PRINCIPAL_X1[1]:
                        if _looks_like_rate(w["text"]):
                            if not interest_rate:
                                interest_rate = w["text"]
                        elif not principal:
                            principal = w["text"]
                    continue
                desc_parts.append(w["text"])

            desc = " ".join(desc_parts).strip()
            # A dated row is a *movement* only when it is a *premature* withdrawal
            # (money removed before maturity). A plain maturity "Withdrawal" that
            # credits back is still a placement (FD record). Premature ->
            # movement; everything else -> placement.
            txn_type = _classify_fd_txn_type(desc)

            pending_fd = {
                "txn_type": txn_type,
                "txn_date": txn_date,
                "deposit_no": deposit_no,
                "period": period,
                "description": desc,
                "interest_amt": interest_amt,
                "principal": principal,
                "interest_rate": interest_rate,
            }
            i += 1
            continue

        # Deposit-No sub-row: begins with the 12-digit Deposit No. (no leading
        # date) and represents a separate action on the same deposit — either a
        # *movement* (Premature Withdrawal) or a *placement* (a "New Rollover
        # Deposit" that stays invested as a new fixed deposit). Classified by the
        # shared helper, so Rollover is correctly treated as a placement.
        # See references/dbs-layouts.md, "FD sub-rows".
        first_word = min(ln, key=lambda w: w["x0"]) if ln else None
        if (
            first_word is not None
            and not date_words
            and DBS_FD_DEPOSIT_NO_X[0] <= first_word["x0"] <= DBS_FD_DEPOSIT_NO_X[1]
            and DBS_FD_DEP_NO_RE.match(first_word["text"])
        ):
            if pending_fd:
                fd_txns.append(pending_fd)
            deposit_no = first_word["text"]
            m_period = DBS_FD_PERIOD_RE.search(text)
            period = m_period.group(0) if m_period else ""
            interest_amt = ""
            principal = ""
            interest_rate = ""
            desc_parts = []
            for w in ln:
                if w["x0"] < DBS_FD_DESC_X_START:
                    continue
                if is_bank_num(w["text"]) or _FD_AMOUNT_RE.match(w["text"]):
                    if DBS_FD_INTEREST_AMT_X1[0] <= w["x1"] <= DBS_FD_INTEREST_AMT_X1[1]:
                        if not interest_amt:
                            interest_amt = w["text"]
                    elif DBS_FD_PRINCIPAL_X1[0] <= w["x1"] <= DBS_FD_PRINCIPAL_X1[1]:
                        if _looks_like_rate(w["text"]):
                            if not interest_rate:
                                interest_rate = w["text"]
                        elif not principal:
                            principal = w["text"]
                    continue
                desc_parts.append(w["text"])
            desc = " ".join(desc_parts).strip()
            pending_fd = {
                "txn_type": _classify_fd_txn_type(desc),
                "txn_date": "",
                "deposit_no": deposit_no,
                "period": period,
                "description": desc,
                "interest_amt": interest_amt,
                "principal": principal,
                "interest_rate": interest_rate,
            }
            i += 1
            continue

        # Continuation lines for current FD transaction
        if pending_fd is not None:
            # Check for numeric values on continuation line (2dp or 6dp)
            for w in ln:
                is_fd_num = bool(
                    is_bank_num(w["text"])
                    or re.match(r"^\d{1,3}(?:,\d{3})*\.\d{4,6}$", w["text"])
                )
                if not is_fd_num:
                    continue
                # Use separate if blocks (not elif) because principal and rate
                # x-bands overlap and we need to check rate even when principal
                # is already set.
                if (DBS_FD_INTEREST_AMT_X1[0] <= w["x1"] <= DBS_FD_INTEREST_AMT_X1[1]
                        and not pending_fd["interest_amt"]):
                    pending_fd["interest_amt"] = w["text"]
                if (DBS_FD_PRINCIPAL_X1[0] <= w["x1"] <= DBS_FD_PRINCIPAL_X1[1]
                        and not pending_fd["principal"]):
                    pending_fd["principal"] = w["text"]
                if (DBS_FD_RATE_X1[0] <= w["x1"] <= DBS_FD_RATE_X1[1]
                        and "." in w["text"]):
                    pending_fd["interest_rate"] = w["text"]

            # Append description text from continuation
            clean_words = [
                w for w in ln
                if DBS_FD_DESC_X_START <= w["x0"] < 500
                and not is_bank_num(w["text"])
                and not re.match(r"^\d{1,3}(?:,\d{3})*\.\d{4,6}$", w["text"])
            ]
            if clean_words:
                clean = " ".join(w["text"] for w in clean_words).strip()
                if clean and not clean.startswith("Page ") and "PDS_MMCON" not in clean:
                    pending_fd["description"] = (pending_fd["description"] + " " + clean).strip()
                    # A continuation that reveals a premature-withdrawal penalty
                    # flips the whole row from placement to movement: it is no
                    # longer a fixed-deposit record but a transaction.
                    if re.search(r"premature", clean, re.I):
                        pending_fd["txn_type"] = "movement"

        i += 1

    if pending_fd:
        fd_txns.append(pending_fd)

    accounts.append({
        "name": "Fixed Deposit",
        "account_no": account_no,
        "currency": "SGD",
        "opening_balance": "",
        "transactions": [],
        "fd_transactions": fd_txns,
        "total": {},
    })


def _parse_srs_txn_section(lines: list[list[WordDict]], accounts: list[dict[str, Any]]) -> None:
    """Parse SRS Account transaction section (page 4)."""
    account_no = ""
    for ln in lines:
        text = _line_text(ln)
        if "Account No." in text:
            m = DBS_ACCT_NO_RE.search(text)
            if m:
                account_no = m.group(0)
            break

    if not account_no:
        return

    # Find transaction table header
    header_idx = -1
    for i, ln in enumerate(lines):
        text = _line_text(ln)
        if "Date" in text and "Description" in text and "Withdrawal" in text:
            header_idx = i
            break

    if header_idx < 0:
        return

    txns = []
    opening_balance = ""
    total_balance = ""

    i = header_idx + 1
    while i < len(lines):
        ln = lines[i]
        text = _line_text(ln)
        if not text.strip():
            i += 1
            continue

        # Balance Brought Forward
        m_bf = re.search(r"Balance\s+Brought\s+Forward\s+([\d,]+\.\d{2})", text)
        if m_bf:
            opening_balance = m_bf.group(1)
            i += 1
            continue

        # Total Balance Carried Forward
        if "Total Balance Carried Forward" in text:
            for w in ln:
                if is_bank_num(w["text"]):
                    if DBS_SRS_BALANCE_X1[0] <= w["x1"] <= DBS_SRS_BALANCE_X1[1]:
                        total_balance = w["text"]
            break

        # New transaction: date line
        date_words = [
            w for w in ln
            if DBS_TXN_DATE_X[0] <= w["x0"] <= DBS_TXN_DATE_X[1]
            and DATE_DBS_RE.match(w["text"])
        ]
        if date_words:
            txn_date = date_words[0]["text"]
            desc_words = [
                w for w in ln
                if DBS_TXN_DESC_X_START <= w["x0"]
                and not is_bank_num(w["text"])
                and w not in date_words
            ]
            desc = " ".join(w["text"] for w in desc_words).strip()

            withdrawal = deposit = balance = ""
            for w in ln:
                if not is_bank_num(w["text"]):
                    continue
                if DBS_SRS_WITHDRAWAL_X1[0] <= w["x1"] <= DBS_SRS_WITHDRAWAL_X1[1]:
                    withdrawal = w["text"]
                elif DBS_SRS_DEPOSIT_X1[0] <= w["x1"] <= DBS_SRS_DEPOSIT_X1[1]:
                    deposit = w["text"]
                elif DBS_SRS_BALANCE_X1[0] <= w["x1"] <= DBS_SRS_BALANCE_X1[1]:
                    balance = w["text"]

            txns.append({
                "txn_date": txn_date,
                "description": desc,
                "withdrawal": withdrawal,
                "deposit": deposit,
                "balance": balance,
            })

        i += 1

    accounts.append({
        "name": "SRS Account",
        "account_no": account_no,
        "currency": "SGD",
        "opening_balance": opening_balance,
        "transactions": txns,
        "total": {"balance": total_balance},
    })


# ============================================================================
# Account merging (same account across multiple pages)
# ============================================================================

def _merge_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge accounts that share the same name and account number."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []

    for acct in accounts:
        key = (acct.get("name", ""), acct.get("account_no", ""))
        if key in merged:
            existing = merged[key]
            existing.setdefault("transactions", []).extend(acct.get("transactions", []))
            existing.setdefault("fd_transactions", []).extend(acct.get("fd_transactions", []))
            # Use the later page's total (the final Total row on the last page)
            if acct.get("total", {}).get("withdrawal") or acct.get("total", {}).get("balance"):
                existing["total"] = acct["total"]
        else:
            merged[key] = dict(acct)  # shallow copy
            order.append(key)

    return [merged[k] for k in order]


# ============================================================================
# Date normalization
# ============================================================================

def _normalize_dates(accounts: list[dict[str, Any]]) -> None:
    """Convert DD/MM/YYYY dates to ISO YYYY-MM-DD format."""
    for acct in accounts:
        for t in acct.get("transactions", []):
            txn_date = t.get("txn_date", "")
            try:
                d, m, y = txn_date.split("/")
                t["txn_date"] = f"{y}-{m}-{d}"
            except (ValueError, AttributeError):
                pass
        for t in acct.get("fd_transactions", []):
            txn_date = t.get("txn_date", "")
            try:
                d, m, y = txn_date.split("/")
                t["txn_date"] = f"{y}-{m}-{d}"
            except (ValueError, AttributeError):
                pass
