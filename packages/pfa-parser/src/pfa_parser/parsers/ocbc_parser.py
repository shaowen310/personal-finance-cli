"""OCBC consolidated statement & credit card statement parsers.

This module parses two flavors of OCBC statement PDFs:

  - ``parse_consolidated``  — OCBC bank-account statement (multi-section: STATEMENT
    SAVINGS / 360 ACCOUNT / TIME DEPOSITS, with Cheque / Withdrawal / Deposit /
    Balance columns).
  - ``parse_card``  — OCBC credit card statement (TRANSACTION DATE / AMOUNT
    (SGD) columns, with a card-name + masked-card-number header and a totals
    summary block).

Each parser returns a tuple ``(meta, records[, extras])`` that is consumed by the
OCBC extractor (``ocbc_extractor.py``) and rendered to Markdown by
``renderers.markdown.ocbc_consolidated_ir_to_markdown`` / ``ocbc_card_ir_to_markdown``.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TypedDict

from pfa_ir_schema.common import PDF, WordDict, group_lines, is_bank_num


# ----------------------------------------------------------------------------
# Type definitions
# ----------------------------------------------------------------------------

class BankMeta(TypedDict):
    statement_date: str
    currency: str
    page_count: int


class BankTxn(TypedDict):
    account: str
    account_no: str
    txn_date: str
    value_date: str
    description: str
    cheque: str
    withdrawal: str
    deposit: str
    balance: str
    opening: str


class TimeDeposit(TypedDict):
    account_no: str
    deposit_no: str
    rate: str
    maturity: str
    balance: str


class CardMeta(TypedDict):
    card_name: str
    card_no: str
    statement_date: str
    payment_due_date: str
    total_credit_limit: str
    total_available_credit_limit: str
    total_minimum_due: str
    last_month_balance: str
    subtotal: str
    total: str
    total_amount_due: str


class CardTxnBase(TypedDict):
    date: str
    description: str
    amount_display: str
    amount_signed: float | None
    foreign_currency: str


class CardTxn(CardTxnBase, total=False):
    date_iso: str


# ----------------------------------------------------------------------------
# Column x-positions (PDF points, measured empirically from the source PDFs).
# Stable across statements of the same bank and product family.
# ----------------------------------------------------------------------------

# --- OCBC consolidated statement ---
BANK_TXN_DATE_X = (40, 80)     # "DD MMM" token (two words: day, month)
BANK_VAL_DATE_X = (85, 120)    # "DD MMM" token
BANK_DESC_X_START = 130        # description begins here
BANK_CHEQUE_X1 = (255, 275)    # right edge of cheque column
BANK_WITHDRAWAL_X1 = (370, 390)
BANK_DEPOSIT_X1 = (455, 470)
BANK_BALANCE_X1 = (550, 565)

# --- OCBC credit card statement ---
CARD_DATE_X = (55, 80)         # single "DD/MM" token
CARD_DESC_X_START = 199        # description begins here
CARD_DESC_X_MAX = 480          # exclude rotated right-margin sidebar (x0 > 480)
CARD_AMOUNT_X1 = (510, 550)    # right edge of amount column

DATE_BANK_RE = re.compile(r"^\d{1,2}\s+[A-Z]{3}$")   # "04 JUN"
DATE_CARD_RE = re.compile(r"^\d{2}/\d{2}$")          # "19/05"
NUM_CARD_RE = re.compile(r"^\(?\d{1,3}(,\d{3})*\.\d{2}\)?$")


# ============================================================================
# Helpers
# ============================================================================

def is_card_num(s: str) -> bool:
    return bool(NUM_CARD_RE.match(s.strip()))


def parse_card_amount(s: str) -> float:
    """Return signed float. Parenthesized => negative (credit)."""
    s = s.strip()
    neg = s.startswith("(") and s.endswith(")")
    val = float(s.strip("()").replace(",", ""))
    return -val if neg else val


# ============================================================================
# OCBC CONSOLIDATED STATEMENT
# ============================================================================

def classify_bank_num(word: WordDict) -> str | None:
    x1 = word["x1"]
    if BANK_CHEQUE_X1[0] <= x1 <= BANK_CHEQUE_X1[1]:
        return "cheque"
    if BANK_WITHDRAWAL_X1[0] <= x1 <= BANK_WITHDRAWAL_X1[1]:
        return "withdrawal"
    if BANK_DEPOSIT_X1[0] <= x1 <= BANK_DEPOSIT_X1[1]:
        return "deposit"
    if BANK_BALANCE_X1[0] <= x1 <= BANK_BALANCE_X1[1]:
        return "balance"
    return None


def parse_consolidated(pdf: PDF) -> tuple[BankMeta, list[BankTxn], list[TimeDeposit]]:
    transactions: list[BankTxn] = []
    time_deposits: list[TimeDeposit] = []
    meta: BankMeta = {"statement_date": "", "currency": "SGD", "page_count": len(pdf.pages)}

    for page in pdf.pages:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines = group_lines(words)

        current_account = ""
        current_account_no = ""
        opening_balance = ""

        i = 0
        while i < len(lines):
            ln = lines[i]
            text = " ".join(w["text"] for w in ln)

            m = re.match(r"^(\d{1,2}\s+[A-Z]{3}\s+\d{4})$", text)
            if m and not meta["statement_date"]:
                meta["statement_date"] = m.group(1)

            m = re.match(r"^(STATEMENT SAVINGS|360 ACCOUNT|TIME DEPOSITS)\b", text)
            if m:
                current_account = m.group(1)
            m = re.search(r"Account\s+No\.?\s*(\d+)", text)
            if m:
                current_account_no = m.group(1)

            m = re.match(r"^BALANCE\s+B/F\s+([\d,]+\.\d{2})", text)
            if m:
                opening_balance = m.group(1)
            m = re.match(r"^BALANCE\s+C/F\s+([\d,]+\.\d{2})", text)
            if m and current_account == "TIME DEPOSITS":
                pass  # handled below

            # Time deposits row
            if current_account == "TIME DEPOSITS" and re.match(r"^\d{6,}\s+\d{6}", text):
                parts = text.split()
                if len(parts) >= 6:
                    time_deposits.append({
                        "account_no": parts[0],
                        "deposit_no": parts[1],
                        "rate": parts[2],
                        "maturity": " ".join(parts[3:6]),
                        "balance": parts[6] if len(parts) >= 7 else parts[-1],
                    })

            # Transaction table header -> parse section
            if ("Cheque" in text and "Withdrawal" in text
                    and "Deposit" in text and "Balance" in text):
                i += 1
                pending: BankTxn | None = None
                section_opening = opening_balance
                while i < len(lines):
                    ln2 = lines[i]
                    t2 = " ".join(w["text"] for w in ln2)

                    if re.match(r"^BALANCE\s+C/F", t2):
                        break
                    if re.match(r"^(Total\s|Average\s)", t2):
                        break
                    if re.search(r"Account\s+No\.?", t2):
                        break
                    if t2.startswith("Transaction Value"):
                        break
                    if "Deposit Insurance" in t2:
                        break
                    if re.match(r"^BALANCE\s+B/F", t2):
                        mb = re.match(r"^BALANCE\s+B/F\s+([\d,]+\.\d{2})", t2)
                        if mb:
                            section_opening = mb.group(1)
                        i += 1
                        continue

                    first_two = " ".join(w["text"] for w in ln2[:2])
                    if DATE_BANK_RE.match(first_two):
                        if pending:
                            transactions.append(pending)
                        date_words = [w for w in ln2 if BANK_TXN_DATE_X[0] <= w["x0"] <= BANK_TXN_DATE_X[1]]
                        vdate_words = [w for w in ln2 if BANK_VAL_DATE_X[0] <= w["x0"] <= BANK_VAL_DATE_X[1]]
                        txn_date = " ".join(w["text"] for w in date_words)
                        val_date = " ".join(w["text"] for w in vdate_words)
                        desc_words = [w for w in ln2
                                      if w["x0"] >= BANK_DESC_X_START
                                      and not is_bank_num(w["text"])
                                      and w not in date_words
                                      and w not in vdate_words]
                        desc = " ".join(w["text"] for w in desc_words).strip()
                        cheque = withdrawal = deposit = balance = ""
                        for w in ln2:
                            if is_bank_num(w["text"]):
                                col = classify_bank_num(w)
                                if col == "cheque":
                                    cheque = w["text"]
                                elif col == "withdrawal":
                                    withdrawal = w["text"]
                                elif col == "deposit":
                                    deposit = w["text"]
                                elif col == "balance":
                                    balance = w["text"]
                        txn: BankTxn = {
                            "account": current_account,
                            "account_no": current_account_no,
                            "txn_date": txn_date,
                            "value_date": val_date,
                            "description": desc,
                            "cheque": cheque, "withdrawal": withdrawal,
                            "deposit": deposit, "balance": balance,
                            "opening": section_opening,
                        }
                        pending = txn
                    else:
                        clean = " ".join(
                            w["text"] for w in ln2
                            if w["x0"] >= BANK_DESC_X_START and w["x0"] < 240
                        ).strip()
                        if (clean and pending is not None
                                and not clean.startswith("Page ")
                                and not re.match(r"^\d+\s+of\s+\d+$", clean)
                                and "Deposit Insurance" not in clean
                                and clean not in ("detimiL", "noitaroproC", "gniknaB",
                                                  "esenihC-aesrevO", ":.oN", ".geR", ".oC")):
                            pending["description"] += " " + clean
                    i += 1
                if pending:
                    transactions.append(pending)
                continue
            i += 1

    # Normalize dates
    for t in transactions:
        for key in ("txn_date", "value_date"):
            try:
                year = str(meta.get("statement_date", "")).split()[-1] if meta.get("statement_date") else str(datetime.now().year)
                d = datetime.strptime(t[key] + " " + year, "%d %b %Y")
                t[key] = d.strftime("%Y-%m-%d")
            except ValueError:
                pass

    return meta, transactions, time_deposits


# ============================================================================
# OCBC CREDIT CARD STATEMENT
# ============================================================================

def parse_card(pdf: PDF) -> tuple[CardMeta, list[CardTxn]]:
    transactions: list[CardTxn] = []
    all_text: list[str] = []
    meta: CardMeta = {
        "card_name": "", "card_no": "",
        "statement_date": "", "payment_due_date": "", "total_credit_limit": "",
        "total_available_credit_limit": "", "total_minimum_due": "",
        "last_month_balance": "", "subtotal": "", "total": "", "total_amount_due": "",
    }

    for page in pdf.pages:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines = group_lines(words)
        for ln in lines:
            all_text.append(" ".join(w["text"] for w in ln))

        i = 0
        while i < len(lines):
            ln = lines[i]
            first = ln[0]
            if (CARD_DATE_X[0] <= first["x0"] <= CARD_DATE_X[1] and DATE_CARD_RE.match(first["text"])):
                date_str = first["text"]
                desc_words = []
                for w in ln[1:]:
                    if CARD_DESC_X_START <= w["x0"] < CARD_DESC_X_MAX:
                        if is_card_num(w["text"]) and w["x1"] >= CARD_AMOUNT_X1[0]:
                            pass
                        else:
                            desc_words.append(w["text"])
                amts = [w for w in ln[1:]
                        if w["x0"] >= CARD_DESC_X_START and is_card_num(w["text"])
                        and w["x1"] >= CARD_AMOUNT_X1[0]]
                amount_token = amts[-1]["text"] if amts else None
                desc = " ".join(desc_words).strip()
                amount_val = parse_card_amount(amount_token) if amount_token else None

                txn: CardTxn = {
                    "date": date_str, "description": desc,
                    "amount_display": amount_token or "",
                    "amount_signed": amount_val, "foreign_currency": "",
                }
                # Foreign currency continuation line
                if i + 1 < len(lines):
                    nxt = lines[i + 1]
                    nxt_text = " ".join(w["text"] for w in nxt)
                    if nxt_text.startswith("FOREIGN CURRENCY") and nxt[0]["x0"] >= CARD_DESC_X_START:
                        fc_match = re.match(r"FOREIGN CURRENCY\s+([A-Z]{3}\s+[\d,]+\.\d{2})", nxt_text)
                        txn["foreign_currency"] = fc_match.group(1) if fc_match else nxt_text.replace("FOREIGN CURRENCY ", "")
                        i += 1
                transactions.append(txn)
            i += 1

    full = "\n".join(all_text)
    m = re.search(
        r"(\d{2}-\d{2}-\d{4})\s+(\d{2}-\d{2}-\d{4})\s+S\$([\d,]+)\s+S\$([\d,]+\.\d{2})\s+S\$([\d,]+\.\d{2})",
        full)
    if m:
        meta["statement_date"] = m.group(1)
        meta["payment_due_date"] = m.group(2)
        meta["total_credit_limit"] = m.group(3)
        meta["total_available_credit_limit"] = m.group(4)
        meta["total_minimum_due"] = m.group(5)
    for key, pat in [
        ("last_month_balance", r"LAST MONTH'S BALANCE\s+([\d,]+\.\d{2})"),
        ("subtotal", r"SUBTOTAL\s+([\d,]+\.\d{2})"),
        ("total", r"(?<!AMOUNT\s)TOTAL\s+([\d,]+\.\d{2})"),
        ("total_amount_due", r"TOTAL AMOUNT DUE\s+([\d,]+\.\d{2})"),
    ]:
        mm = re.search(pat, full)
        if mm:
            meta[key] = mm.group(1)
    # The card product name appears on its own line, immediately above the
    # all-caps cardholder name line, which in turn sits directly before the
    # masked card number. We find the card-number line first and then take the
    # line above it as the card name, so this works for any product (OCBC or
    # non-OCBC, e.g. "VISA PLATINUM", "MASTERCARD WORLD").
    text_lines = full.splitlines()
    card_no = ""
    for idx, ln in enumerate(text_lines):
        m_no = re.search(r"\b(\d{4}-\d{4}-\d{4}-\d{4})\b", ln)
        if not m_no:
            continue
        # The cardholder-name + card-number combo is on one line ("NAME
        # 1234-..."). The card-name line is the line directly before it.
        if idx >= 1 and re.search(r"^[A-Z][A-Z\s'\-/&.]+$", text_lines[idx - 1].strip()):
            card_no = m_no.group(1)
            meta["card_name"] = text_lines[idx - 1].strip()
            meta["card_no"] = card_no
            break

    for t in transactions:
        try:
            dd, mm2 = t["date"].split("/")
            year = str(meta.get("statement_date", "")).split("-")[-1] if meta.get("statement_date") else str(datetime.now().year)
            d = datetime.strptime(f"{dd} {mm2} {year}", "%d %m %Y")
            t["date_iso"] = d.strftime("%Y-%m-%d")
        except (ValueError, KeyError):
            t["date_iso"] = t["date"]
    return meta, transactions
