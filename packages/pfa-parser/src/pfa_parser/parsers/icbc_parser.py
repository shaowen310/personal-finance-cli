"""ICBC (Industrial and Commercial Bank of China) bank statement parser.

This module parses ICBC bank-account statement PDFs (bilingual English/Chinese)
with multi-currency Current Account transactions and Fixed Deposit transactions.

Exports:
  - ``parse_icbc``  — returns (meta, ca_summary, fd_summary, ca_txns, fd_txns, reminders, notes)
  - ``icbc_to_markdown`` — renders the parsed data to a masked Markdown string.
"""

from __future__ import annotations

import re
from typing import TypedDict

from pfa_ir_schema.common import PDF, is_bank_num


# ============================================================================
# Type definitions
# ============================================================================

class MetaDict(TypedDict):
    statement_date: str
    page_count: int


class CASummaryRow(TypedDict):
    acct_type: str
    acct_no: str
    ccy: str
    balance: str


class FDSummaryRow(TypedDict):
    acct_no: str
    ccy: str
    balance: str
    status: str


class CATxnRow(TypedDict, total=False):
    date: str
    remark: str
    ccy: str
    deposit: str
    withdrawal: str
    balance: str
    is_bf: bool
    is_total: bool
    total_field: str
    acct_no: str


class FDTxnRow(TypedDict, total=False):
    acct_no: str
    seq_no: str
    deal_date: str
    value_date: str
    ccy: str
    amount: str
    period: str
    maturity_date: str
    rate: str
    interest_amount: str
    balance: str
    remark: str


class CcyGroup(TypedDict):
    ccy: str
    txns: list[CATxnRow]


ICBCResult = tuple[
    MetaDict,
    list[CASummaryRow],
    list[FDSummaryRow],
    list[CATxnRow],
    list[FDTxnRow],
    list[str],
    list[str],
]

# ----------------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------------

DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
CCY_RE = re.compile(r"^(CNY|SGD|USD)$")
TOTAL_DR_RE = re.compile(r"^Total\s+Dr\.\s*")
TOTAL_CR_RE = re.compile(r"^Total\s+Cr\.\s*")
# Detects multi-line TIME DEPO AUTO ROLLOVER remark start lines.
TIME_DEPO_RE = re.compile(r"^TIME\s+DEPO\s+AUTO\s+ROLLOVER", re.IGNORECASE)


# ============================================================================
# Parser
# ============================================================================

def parse_icbc(pdf: PDF) -> ICBCResult:
    """Parse an ICBC statement PDF.

    Returns:
        (meta, ca_summary, fd_summary, ca_txns, fd_txns, reminders, notes)

    The statement is a bilingual (English/Chinese) PDF with:
      - Statement Date header line (e.g. "Statement Date 结单日期：YYYY/MM/DD")
      - Account Summary section with Current Account (multi-currency) and
        Fixed Deposit Account sub-tables
      - Transaction Record section with per-currency grouped Current Account
        transactions and Fixed Deposit transactions
      - Reminders and Notes at the end
    """
    # ---- Phase 1: extract all text ----
    full_text: str = ""
    for page in pdf.pages:
        full_text += "\n" + (page.extract_text() or "")

    lines: list[str] = full_text.splitlines()

    meta: MetaDict = {
        "statement_date": "",
        "page_count": len(pdf.pages),
    }
    ca_summary: list[CASummaryRow] = []   # Current Account summary rows
    fd_summary: list[FDSummaryRow] = []    # Fixed Deposit summary rows
    ca_txns: list[CATxnRow] = []           # Current Account transactions
    fd_txns: list[FDTxnRow] = []            # Fixed Deposit transactions
    reminders: list[str] = []               # Reminder entries
    notes: list[str] = []                   # Notes text

    # ---- Phase 2: parse header ----
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        if not raw:
            i += 1
            continue

        # Statement Date line
        sd_match = re.match(r"Statement\s+Date\s+结单日期：(\d{4}/\d{2}/\d{2})", raw)
        if sd_match:
            meta["statement_date"] = sd_match.group(1)
            i += 1
            continue

        # Account Summary boundary
        if "Account Summary" in raw:
            i += 1
            break

        i += 1

    # ---- Phase 3: parse Account Summary ----
    _ = _parse_summary_section(lines, i, ca_summary, fd_summary)

    # ---- Phase 4: find Transaction Record boundary ----
    i = _find_section_start(lines, i, "Transaction Record")
    if i is None:
        return meta, ca_summary, fd_summary, ca_txns, fd_txns, reminders, notes

    # ---- Phase 5: parse transaction sections ----
    i = _parse_transaction_sections(lines, i, ca_txns, fd_txns)

    # ---- Phase 6: parse Reminders ----
    i_rem = _find_section_start(lines, i, "Reminders")
    if i_rem is not None:
        i_rem += 1
        while i_rem < len(lines):
            raw = lines[i_rem].strip()
            if not raw:
                i_rem += 1
                continue
            if _looks_like_section_header(raw) or "Note " in raw:
                break
            if any(kw in raw for kw in ("Category", "Particulars", "类別", "摘要")):
                i_rem += 1
                continue
            reminders.append(raw)
            i_rem += 1
        i = i_rem

    # ---- Phase 7: parse Notes ----
    i_note = _find_section_start(lines, i, "Note ")
    if i_note is not None:
        i_note += 1
        while i_note < len(lines):
            raw = lines[i_note].strip()
            if not raw:
                i_note += 1
                continue
            if _looks_like_footer(raw):
                i_note += 1
                continue
            if "Deposit Insurance" in raw:
                # Include the heading line itself so the renderer can split on it
                notes.append(raw)
                i_note += 1
                continue
            notes.append(raw)
            i_note += 1

    return meta, ca_summary, fd_summary, ca_txns, fd_txns, reminders, notes


# ----------------------------------------------------------------------------
# Account Summary parsing
# ----------------------------------------------------------------------------

def _parse_summary_section(
    lines: list[str],
    start_idx: int,
    ca_summary: list[CASummaryRow],
    fd_summary: list[FDSummaryRow],
) -> int:
    """Parse Current Account and Fixed Deposit Account summary tables."""
    i = start_idx
    state: str | None = None  # None, "ca", "fd"
    ca_header_seen = False
    fd_header_seen = False

    while i < len(lines):
        raw = lines[i].strip()
        if not raw:
            i += 1
            continue

        # Detect sub-section boundaries
        if "Current Account" in raw:
            state = "ca"
            ca_header_seen = False
            i += 1
            continue
        if "Fixed Deposit Account" in raw:
            state = "fd"
            fd_header_seen = False
            i += 1
            continue
        if "Transaction Record" in raw:
            break

        # Skip bilingual header rows
        if not ca_header_seen and state == "ca":
            if ("A/c Type" in raw or "帐户类别" in raw):
                ca_header_seen = True
                i += 1
                continue
        if not fd_header_seen and state == "fd":
            if ("Account No." in raw or "帐户号码" in raw):
                fd_header_seen = True
                i += 1
                continue

        if state == "ca" and ca_header_seen:
            # Parse CA summary row: "C/A ACCT_NO CCY BALANCE [A/C_CODE]"
            parts = raw.split()
            if len(parts) >= 4 and parts[0] == "C/A":
                ca_summary.append({
                    "acct_type": parts[0],
                    "acct_no": parts[1],
                    "ccy": parts[2],
                    "balance": parts[3],
                })
        elif state == "fd" and fd_header_seen:
            # Parse FD summary row: "ACCT_NO CCY BALANCE STATUS [A/C_CODE]"
            parts = raw.split()
            if len(parts) >= 4 and is_bank_num(parts[2]):
                fd_summary.append({
                    "acct_no": parts[0],
                    "ccy": parts[1],
                    "balance": parts[2],
                    "status": parts[3],
                })

        i += 1
    return i


# ----------------------------------------------------------------------------
# Transaction sections parsing
# ----------------------------------------------------------------------------

def _parse_transaction_sections(
    lines: list[str],
    start_idx: int,
    ca_txns: list[CATxnRow],
    fd_txns: list[FDTxnRow],
) -> int:
    """Parse Current Account and Fixed Deposit transaction tables.

    Current Account rows have a standard form:
      YYYY/MM/DD  REMARK  CCY  AMOUNT  BALANCE

    Some remarks span multiple lines (e.g. TIME DEPO AUTO ROLLOVER).
    A "B/F" (Brought Forward) row opens each currency group, and
    "Total Dr." / "Total Cr." rows close each group.

    Fixed Deposit rows use a wider column layout:
      SEQ_NO  DEAL_DATE  VALUE_DATE  CCY  AMOUNT  PERIOD
      MATURITY_DATE  RATE  INTEREST  BALANCE  REMARK
    """
    i: int = start_idx
    in_ca = False
    in_fd = False
    ca_acct_no = ""
    fd_acct_no = ""
    # Remark accumulation for multi-line descriptions
    pending_remark_parts: list[str] = []
    current_txn: CATxnRow | None = None
    current_ccy = ""
    # Track previous CA balance so a single-amount row whose running
    # balance DECREASES can be inferred as a withdrawal (ICBC sometimes
    # drops the withdrawal column on extraction).
    prev_balance: float | None = None

    fd_header_seen = False

    while i < len(lines):
        raw = lines[i].strip()
        if not raw:
            i += 1
            continue

        # Detect Current Account transaction section
        if "Current Account" in raw and "往来帐户" in raw:
            in_ca = True
            in_fd = False
            pending_remark_parts = []
            current_txn = None
            i += 1
            continue

        # Detect Fixed Deposit transaction section
        if in_ca and "Fixed Deposit Account" in raw:
            in_ca = False
            in_fd = True
            fd_header_seen = False
            i += 1
            continue

        # Detect account number lines
        if in_ca:
            acc_match = re.search(r"Account\s+No\.?\s*帐户号码\s*(\d{10,})", raw)
            if acc_match:
                ca_acct_no = acc_match.group(1)
                i += 1
                continue
            # Header row for CA transactions
            if "Date " in raw and "Remark" in raw and "CCY" in raw:
                i += 1
                continue
            if "日期 " in raw and "备注" in raw and "货币" in raw:
                i += 1
                continue

            # Parse CA transaction lines
            if DATE_RE.match(raw.split()[0] if raw.split() else ""):
                # Finalize any pending transaction
                if current_txn:
                    ca_txns.append(current_txn)
                    current_txn = None

                txn = _parse_ca_txn_line(raw, ca_acct_no, pending_remark_parts,
                                        prev_balance)
                if txn:
                    current_txn = txn
                    current_ccy = txn.get("ccy", current_ccy)
                    bal = txn.get("balance", "")
                    if bal:
                        try:
                            prev_balance = float(str(bal).replace(",", ""))
                        except ValueError:
                            pass
                pending_remark_parts = []

            elif TOTAL_DR_RE.match(raw):
                if current_txn:
                    ca_txns.append(current_txn)
                    current_txn = None
                total_field = "total_dr"
                # Parse total amount
                amt = _extract_last_amount(raw)
                ca_txns.append({
                    "date": "", "remark": "Total Dr.", "ccy": current_ccy,
                    "deposit": "", "withdrawal": amt if amt else "0.00",
                    "balance": "", "is_total": True, "total_field": total_field,
                    "acct_no": ca_acct_no,
                })

            elif TOTAL_CR_RE.match(raw):
                if current_txn:
                    ca_txns.append(current_txn)
                    current_txn = None
                total_field = "total_cr"
                amt = _extract_last_amount(raw)
                ca_txns.append({
                    "date": "", "remark": "Total Cr.", "ccy": current_ccy,
                    "deposit": amt if amt else "0.00", "withdrawal": "",
                    "balance": "", "is_total": True, "total_field": total_field,
                    "acct_no": ca_acct_no,
                })

            elif TIME_DEPO_RE.match(raw):
                # Multi-line remark start — accumulate
                pending_remark_parts.append(raw)

            elif current_txn is not None:
                # Remark continuation for multi-line descriptions
                current_txn["remark"] += " " + raw

            else:
                # Free-standing remark before any date line
                pending_remark_parts.append(raw)

        elif in_fd:
            # Detect FD account number
            acc_match = re.search(r"Account\s+No\.?\s*帐户号码\s*(\d{10,})", raw)
            if acc_match:
                fd_acct_no = acc_match.group(1)
                i += 1
                continue

            # FD header lines (multi-line bilingual)
            if "Deal Date" in raw or "交易日期" in raw:
                fd_header_seen = True
                i += 1
                continue
            if fd_header_seen and ("Value Date" in raw or "Deposit" in raw
                                    or "Withdrawal" in raw or "Period" in raw
                                    or "Maturity" in raw or "Interest" in raw
                                    or "Balance" in raw or "Remark" in raw
                                    or "起息日期" in raw or "存款期" in raw
                                    or "到期日" in raw or "利率" in raw
                                    or "利息" in raw or "结余" in raw or "备注" in raw
                                    or "序号" in raw or "存/取金额" in raw
                                    or "Amount" in raw):
                i += 1
                continue

            # Parse FD transaction row
            fd_txn = _parse_fd_txn_line(raw, fd_acct_no)
            if fd_txn:
                fd_txns.append(fd_txn)

        # Check for end of transaction section
        if raw.startswith("Reminders") or raw.startswith("Note "):
            # Finalize any pending CA transaction
            if current_txn:
                ca_txns.append(current_txn)
                current_txn = None
            break

        i += 1

    # Finalize last pending transaction
    if current_txn:
        ca_txns.append(current_txn)

    return i


def _parse_ca_txn_line(
    raw: str,
    acct_no: str,
    pending_remark_parts: list[str],
    prev_balance: float | None = None,
) -> CATxnRow | None:
    """Parse a single Current Account transaction data line.

    Format: ``YYYY/MM/DD [REMARK] CCY [AMOUNT] [BALANCE]``

    Multi-line remarks (e.g. TIME DEPO AUTO ROLLOVER) are merged from
    ``pending_remark_parts`` collected before this date line.

    When a single transaction amount is present alongside the running
    balance, the deposit/withdrawal direction is inferred from whether the
    balance increased (deposit) or decreased (withdrawal) relative to the
    previous row. This guards against ICBC statements where the explicit
    Withdrawal column is blank/missing on extraction.
    """
    parts = raw.split()
    if len(parts) < 3:
        return None

    date_str = parts[0]
    if not DATE_RE.match(date_str):
        return None

    # Find CCY position
    ccy_idx: int | None = None
    for j in range(1, min(len(parts), 8)):
        if CCY_RE.match(parts[j]):
            ccy_idx = j
            break
    if ccy_idx is None:
        return None

    ccy = parts[ccy_idx]

    # Remark is everything between date and CCY
    if ccy_idx > 1:
        inline_remark = " ".join(parts[1:ccy_idx])
    else:
        inline_remark = ""

    # Merge pending remark (from TIME DEPO lines before this date line)
    if pending_remark_parts:
        full_remark = " ".join(pending_remark_parts)
        if inline_remark:
            full_remark += " " + inline_remark
    else:
        full_remark = inline_remark

    is_bf = (inline_remark == "B/F" and not pending_remark_parts)

    # Extract amounts after CCY
    amount_parts = parts[ccy_idx + 1:]
    amounts = [p for p in amount_parts if is_bank_num(p)]

    deposit = withdrawal = balance = ""
    if len(amounts) == 1:
        # B/F case — single balance value, no transaction amount
        balance = amounts[0]
    elif len(amounts) == 2:
        # Single transaction amount + running balance. ICBC may print the
        # amount with an unreliable sign (e.g. both legs of a
        # rejected-payment-then-refund show '-'), so direction is derived
        # from the RUNNING-BALANCE movement, the authoritative signal:
        #   - balance DECREASED  -> withdrawal (money out)
        #   - balance INCREASED  -> deposit (money in)
        # The amount magnitude is always stored positive.
        balance = amounts[-1]
        amount_str = amounts[0].strip().lstrip("+").lstrip("-")
        try:
            amt_val = round(float(amount_str.replace(",", "")), 2)
            bal_val = round(float(balance.replace(",", "")), 2)
            prev = round(prev_balance, 2) if prev_balance is not None else None
            decreased = prev is not None and round(bal_val + amt_val, 2) == prev
            increased = prev is not None and round(bal_val - amt_val, 2) == prev
        except ValueError:
            decreased = increased = False
        if decreased:
            withdrawal = amount_str
        elif increased:
            deposit = amount_str
        else:
            # Ambiguous (e.g. B/F adjacent); default to deposit.
            deposit = amount_str
    elif len(amounts) >= 3:
        # Deposit / Withdrawal / Balance columns explicitly present.
        balance = amounts[-1]
        # Normalize a negative deposit to a positive withdrawal.
        if amounts[0].strip().startswith("-"):
            withdrawal = amounts[0].strip().lstrip("-")
        else:
            deposit = amounts[0].strip().lstrip("+")
        withdrawal = withdrawal or amounts[1]

    return CATxnRow(
        date=date_str,
        remark=full_remark.strip(),
        ccy=ccy,
        deposit=deposit,
        withdrawal=withdrawal,
        balance=balance,
        is_bf=is_bf,
        is_total=False,
        total_field="",
        acct_no=acct_no,
    )


def _parse_fd_txn_line(raw: str, acct_no: str) -> FDTxnRow | None:
    """Parse a Fixed Deposit transaction row.

    Expected columns (10+ fields):
      SEQ_NO  DEAL_DATE  VALUE_DATE  CCY  AMOUNT  PERIOD
      MATURITY_DATE  RATE  INTEREST  BALANCE  [REMARK]
    """
    parts = raw.split()
    if len(parts) < 9:
        return None
    # First field should be a sequence number (digits)
    if not parts[0].isdigit():
        return None
    # Second field should be a date
    if not DATE_RE.match(parts[1]):
        return None

    return FDTxnRow(
        acct_no=acct_no,
        seq_no=parts[0],
        deal_date=parts[1],
        value_date=parts[2],
        ccy=parts[3],
        amount=parts[4],
        period=parts[5],
        maturity_date=parts[6] if DATE_RE.match(parts[6]) else parts[6],
        rate=parts[7],
        interest_amount=parts[8],
        balance=parts[9] if len(parts) > 9 else "",
        remark=parts[10] if len(parts) > 10 else "",
    )


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _looks_like_section_header(raw: str) -> bool:
    """True if line looks like a major section header."""
    return any(kw in raw for kw in (
        "Account Summary", "Transaction Record",
        "Current Account", "Fixed Deposit",
        "Reminders", "业务提醒",
        "Note ", "注意事项",
        "Deposit Insurance", "存款保险",
    ))


def _looks_like_footer(raw: str) -> bool:
    """True if line looks like a page footer."""
    return bool(re.match(r"^Page\s+\d+\s+of\s+\d+", raw)) or bool(re.match(r"^\d{10,}$", raw.strip()))


def _find_section_start(lines: list[str], from_idx: int, keyword: str) -> int | None:
    """Return the index of the first line containing ``keyword``, starting from ``from_idx``."""
    for idx in range(from_idx, len(lines)):
        if keyword in lines[idx]:
            return idx
    return None


def _extract_last_amount(raw: str) -> str | None:
    """Extract the last bank-num formatted value from a line."""
    parts = raw.split()
    for p in reversed(parts):
        if is_bank_num(p):
            return p
    return None
