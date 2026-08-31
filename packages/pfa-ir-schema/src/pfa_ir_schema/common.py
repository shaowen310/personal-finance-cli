"""Shared helpers for bank statement parsers.

This module holds primitives that are imported by every parser module as well as
the main entrypoint. Keeping them here breaks the would-be import cycle between
convert_statement and the bank-specific parser modules.

Includes text masking, date parsing, FD interest verification, and PDF word
grouping utilities.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol, TypedDict

# Right-edge artefacts from the rotated right-margin sidebar that appears on
# some OCBC pages (e.g. "Limited", "Corporation", "Banking", "Oversea-Chinese",
# "No:", "Reg.", "Co."). The order here is reverse-alphabetical because the
# sidebar prints the words rotated 180°.
SIDEBAR_NOISE: set[str] = {"detimiL", "noitaroproC", "gniknaB", "esenihC-aesrevO", ":.oN", ".geR", ".oC"}


def is_bank_num(s: str) -> bool:
    """True if ``s`` looks like a bank-style currency number, e.g. ``"X,XXX.XX"``.

    Also accepts a leading minus sign (``"-X,XXX.XX"``) so negative balances —
    e.g. an overdraft (OD) account drawn below zero — are recognised as numeric
    tokens rather than dropped (which previously left ``closing_balance`` null).
    """
    return bool(re.match(r"^-?\d{1,3}(,\d{3})*\.\d{2}$", (s or "").strip()))


def mask_id(s: str, *, do_mask: bool = True) -> str:
    """Mask an account/card number, keeping only the last 4 digits visible.

    All non-digit characters (e.g. the dashes in a card number) are dropped
    from the visible output, and every leading digit is replaced with ``"X"``.
    If the string is empty or contains fewer than 4 digits, the original
    string is returned unchanged.

    When *do_mask* is ``False``, the value is returned as-is — callers can
    pass through ``do_mask`` unconditionally instead of guarding every call
    with an ``if do_mask else`` ternary.
    """
    if not s or not do_mask:
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 4:
        return s
    return "X" * (len(digits) - 4) + digits[-4:]


def mask_name(name: str) -> str:
    """Mask a person name, keeping only the first character of each part visible.

    Each part of the name (split by whitespace) that is longer than 2 characters
    is reduced to its first character followed by ``"XXX"``. Parts that are 1-2
    characters long are kept as-is. This prevents the original name length from
    being exposed while still providing enough information for the payer to
    verify the recipient's identity.

    Uses ``"X"`` (not ``"*"``) as the mask character so the result is safe to
    embed in Markdown content without being interpreted as formatting syntax.

    Examples:
        >>> mask_name("John Tan")
        'JXXX TXXX'
        >>> mask_name("Chan Shi Hui Jacqueline")
        'CXXX SXXX HXXX JXXX'
        >>> mask_name("Bob")
        'BXXX'
        >>> mask_name("Ab")
        'Ab'
        >>> mask_name("")
        ''
    """
    parts = name.strip().split()
    masked = [part if len(part) <= 1 else part[0] + "XXX" for part in parts]
    return " ".join(masked)


class WordDict(TypedDict):
    """A pdfplumber word dict subset with ``text``, ``x0``, ``x1``, ``top``."""
    text: str
    x0: float
    x1: float
    top: float


class PDFPage(Protocol):
    """Minimal pdfplumber page interface used across parsers.

    Declares the two extraction methods the parsers rely on. A real
    pdfplumber page supports both, so a single shared Protocol keeps the
    package's PDF dependency in one place.
    """

    def extract_text(self) -> str | None: ...

    def extract_words(
        self, *, use_text_flow: bool = ..., keep_blank_chars: bool = ...
    ) -> list[WordDict]: ...


class PDF(Protocol):
    """Minimal PDF interface expected by the parsers."""

    @property
    def pages(self) -> Sequence[PDFPage]: ...

    def close(self) -> None: ...


def group_lines(words: list[WordDict], y_tol: float = 3) -> list[list[WordDict]]:
    """Cluster extracted words into visual lines by their ``"top"`` coordinate.

    ``words`` is a list of pdfplumber word dicts (each with ``text``, ``x0``,
    ``x1``, ``top``). Returns a list of lines, where each line is the list of
    words on that line sorted left-to-right by ``x0``.
    """
    if not words:
        return []
    words = sorted(words, key=lambda w: w["top"])
    lines: list[list[WordDict]] = []
    cur: list[WordDict] = [words[0]]
    cur_top: float = words[0]["top"]
    for w in words[1:]:
        if abs(w["top"] - cur_top) <= y_tol:
            cur.append(w)
        else:
            lines.append(sorted(cur, key=lambda w: w["x0"]))
            cur, cur_top = [w], w["top"]
    if cur:
        lines.append(sorted(cur, key=lambda w: w["x0"]))
    return lines


# ---------------------------------------------------------------------------
# Person-name masking helpers
# ---------------------------------------------------------------------------

# CJK unified ideographs range (excludes punctuation, symbols, kana)
_CJK_RE: re.Pattern[str] = re.compile(r"(?<![一-鿿])([一-鿿]{2,3})(?![一-鿿])")

# Keywords that indicate a line is a bank/payment reference, not a person name.
_BANK_KEYWORDS: set[str] = {
    "PAYNOW", "FAST", "NETS", "GIRO", "MEPS", "DBS", "POSB", "OCBC", "UOB",
    "ICBC", "I-BANK", "IBANK", "MBK", "MBK-", "REF", "REFERENCE", "TXN",
    "TRANSFER", "PAYMENT", "SALARY", "INWARD", "OUTWARD", "CREDIT", "DEBIT", "OTHR", "OTHER",
    "BILL", "TOP-UP", "TOPUP", "EZ-LINK", "EZLINK", "SIMPLYGO",
    "REDEMPTION", "REBATE", "CASHBACK", "FEE", "CHARGE", "INTEREST",
    "WITHDRAWAL", "DEPOSIT", "DIVIDEND", "BONUS", "ADVICE",
}

# Keywords that are structural indicators (kept as-is in token-by-token masking).
_STRUCTURAL_KEYWORDS: set[str] = {
    "FROM", "TO", "VIA", "FOR",
}

# Keywords that suggest a line is a business/merchant, not an individual.
_BUSINESS_KEYWORDS: set[str] = {
    "PTE", "LTD", "LLP", "CORP", "CORPORATION", "INC", "LLC", "BHD", "BERHAD",
    "SERVICES", "TRADING", "HOLDINGS", "ENTERPRISE", "ENTERPRISES",
    "INTERNATIONAL", "ASIA", "SINGAPORE", "GLOBAL", "GROUP",
    "MANAGEMENT", "INVESTMENT", "CAPITAL", "PARTNERS", "ASSOCIATES",
    "SOLUTIONS", "TECHNOLOGIES", "CONSULTING", "ENGINEERING",
    "DEVELOPMENT", "SYSTEMS", "NETWORK", "DIGITAL", "ONLINE",
    "MARKETING", "INSURANCE", "FINANCIAL", "BANK",
    "SCHOOL", "COLLEGE", "UNIVERSITY", "HOSPITAL", "MEDICAL",
    "RESTAURANT", "CAFE", "HOTEL", "RESORT", "CLUB",
    "PROPERTY", "REALTY", "CONSTRUCTION", "LOGISTICS", "SHIPPING",
    "MINIMART", "SUPERMARKET", "PHARMACY", "CLINIC", "DENTAL",
    "TELECOM", "COMMUNICATIONS", "MEDIA", "STUDIO", "PRODUCTIONS",
    "BAKERY", "FOOD", "BEVERAGE", "CATERING", "WHOLESALE",
    "RETAIL", "HARDWARE", "ELECTRONICS", "SOFTWARE", "AUTOMOBILE",
    "MOTOR", "TRAVEL", "TOURS", "AGENCY", "SECURITIES",
    "PRIVATE", "LIMITED", "COMPANY", "BUSINESS", "CO",
    "FUND", "TRUST", "NOMINEES", "NOMINEE", "ACCOUNT",
    "SHOP", "STORE", "MALL", "PLAZA", "CENTRE", "CENTER",
    "WASH", "LAUNDRY", "CLEANING", "MAINTENANCE", "REPAIR",
    "AIRLINES", "AIRWAYS", "AVIATION", "EXPRESS", "COURIER",
    "HEALTHCARE", "WELLNESS", "BEAUTY", "SALON", "SPA",
    "EDUCATION", "TRAINING", "INSTITUTE", "ACADEMY", "LEARNING",
    "DESIGN", "CREATIVE", "PRINTING", "PUBLISHING", "ADVERTISING",
    "ENERGY", "POWER", "WATER", "GAS", "ELECTRIC",
}


def mask_chinese_name(name: str) -> str:
    """Mask a Chinese person name, keeping only the first character visible.

    Uses a fixed ``"XX"`` mask regardless of the original name length, so the
    output does not reveal whether the original was a 2-character or
    3-character name. This is consistent with :func:`mask_name`, which also
    uses a fixed ``"XXX"`` mask for English name parts.

    Examples:
        >>> mask_chinese_name("张三")
        '张XX'
        >>> mask_chinese_name("李小明")
        '李XX'
        >>> mask_chinese_name("王")
        '王'
        >>> mask_chinese_name("")
        ''
    """
    if len(name) <= 1:
        return name
    return name[0] + "XX"


def _mask_chinese_names_in_text(text: str) -> str:
    """Detect and mask 2-3 character CJK person names within *text*."""
    return _CJK_RE.sub(lambda m: mask_chinese_name(m.group(1)), text)


def _is_business_line(line: str) -> bool:
    """True if *line* appears to be a business/merchant name (UEN safe)."""
    upper = line.upper()
    words = set(upper.split())
    return bool(words & _BUSINESS_KEYWORDS)


def _is_token_keyword(token: str) -> bool:
    """Check if a token is (or contains) a known structural or bank keyword.

    Handles compound tokens like ``"PAYNOW-FAST"`` or ``"MBK-123"`` by
    splitting on ``-`` and ``/`` and checking each component.
    """
    upper = token.upper()
    if upper in _BANK_KEYWORDS or upper in _STRUCTURAL_KEYWORDS:
        return True
    # Handle compound tokens like "PAYNOW-FAST" or "MBK-123"
    for sep in ("-", "/"):
        if sep in upper:
            if any(
                p in _BANK_KEYWORDS | _STRUCTURAL_KEYWORDS
                for p in upper.split(sep)
            ):
                return True
    return False


_NUMERIC_ID_RE: re.Pattern[str] = re.compile(r"\b\d{4,}\b")

# Singapore NRIC / FIN — standard 9-char format: prefix letter (S/T/F/G/M) +
# 7 digits + checksum letter. Also supports the 8-char short form without the
# trailing checksum letter (some systems drop it).
# Fully replaced with ``[NRIC]`` to avoid partial credential exposure (NRIC
# numbers may be used as authentication factors).
_NRIC_FULL_RE: re.Pattern[str] = re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE)
_NRIC_SHORT_RE: re.Pattern[str] = re.compile(r"\b[STFGM]\d{7}\b", re.IGNORECASE)


def sanitize_description(desc: str) -> str:
    """Mask sensitive identifiers in description text.

    Controlled by the ``do_mask`` flag; when masking is off, this function is
    skipped entirely:

    - 4+ consecutive digits → only last 4 visible (via :func:`mask_id`)
    - Singapore NRIC/FIN (e.g. ``S9378424C`` or ``S9378424``) → fully replaced
      with ``[NRIC]``
    - Parentheses spacing → normalised (PDF extraction artifact fix)
    """
    if not desc:
        return desc
    desc = _normalize_parentheses(desc)
    desc = _NUMERIC_ID_RE.sub(lambda m: mask_id(m.group()), desc)
    desc = _NRIC_FULL_RE.sub("[NRIC]", desc)
    desc = _NRIC_SHORT_RE.sub("[NRIC]", desc)
    return desc


def _normalize_parentheses(text: str) -> str:
    """Insert spaces around parentheses that touch alphabetic characters.

    Fixes PDF extraction artifacts where spaces around ``(`` / ``)`` are lost,
    e.g. ``"SOON(XU"`` → ``"SOON ( XU"``, ``"LEE)"`` → ``"LEE )"``.

    Only acts when the adjacent character is an ASCII letter — avoids messing
    with numeric expressions like ``"(123.45)"`` or currency notations.
    """
    # Space before '(' when preceded by a letter
    text = re.sub(r'(?<=[A-Za-z])(?=\()', ' ', text)
    # Space after '(' when followed by a letter
    text = re.sub(r'(?<=\()(?=[A-Za-z])', ' ', text)
    # Space before ')' when preceded by a letter
    text = re.sub(r'(?<=[A-Za-z])(?=\))', ' ', text)
    # Space after ')' when followed by a letter
    text = re.sub(r'(?<=\))(?=[A-Za-z])', ' ', text)
    return text


def mask_names_in_description(desc: str) -> str:
    """Context-aware person-name masking for transaction descriptions.

    Only masks descriptions that contain ``"FAST"`` (the FAST payment protocol).
    Non-FAST transactions (credit card, GIRO, salary, etc.) are returned as-is
    to preserve merchant and reference information that would otherwise be lost.

    For FAST transactions, English names (via :func:`mask_name`) and Chinese
    names (via :func:`mask_chinese_name`) are masked on a per-token basis,
    while structural keywords (``PAYNOW``, ``FAST``, ``OTHR``, ``FROM``,
    ``TO``, etc.) and known business indicators are preserved.
    """
    if not desc:
        return desc

    # Only mask FAST payment transactions. Non-FAST transactions (credit card
    # swipes, GIRO, salary credits, bill payments, etc.) are returned as-is
    # so that merchant and reference information is preserved.
    if "FAST" not in desc:
        return desc

    lines = desc.split("\n")
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue

        # Lines containing "UEN" as a token → skip masking entirely.
        if "UEN" in (token.upper() for token in stripped.split()):
            result.append(stripped)
            continue

        # Lines that look like a business name → skip masking entirely.
        if _is_business_line(stripped):
            result.append(stripped)
            continue

        # Token-by-token masking: preserve structural/bank keywords,
        # mask everything else as a person name.
        tokens = stripped.split()
        masked_tokens: list[str] = []
        for token in tokens:
            if _is_token_keyword(token):
                masked_tokens.append(token)
            elif _CJK_RE.search(token):
                masked_tokens.append(_mask_chinese_names_in_text(token))
            elif not token.isalpha():
                # Keep tokens with any non-alphabet character (digits, symbols, etc.)
                masked_tokens.append(token)
            else:
                masked_tokens.append(mask_name(token))
        result.append(" ".join(masked_tokens))

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Fixed-deposit interest verification helpers
# ---------------------------------------------------------------------------

def parse_fd_rate(rate: str | None, *, assume_pct: bool = False) -> float | None:
    """Parse an FD interest-rate string into a decimal fraction.

    Handles ``"1.25%"`` (÷100), ``"0.050000"`` (decimal), and a bare value
    ``> 1`` with no ``%`` is treated as a percentage (covers stray ``"1.25"``).

    Set ``assume_pct=True`` when the source column is already a percentage
    number without a ``%`` sign (e.g. DBS's ``Interest Rate (% p.a.)`` column),
    so that a bare ``"0.050000"`` becomes ``0.0005`` (0.05%) rather than being
    misread as an already-decimal 5% rate.

    Returns ``None`` if unparseable/empty.
    """
    if not rate:
        return None
    s = rate.strip()
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    val = float(m.group())
    if pct:
        return val / 100.0
    if assume_pct:
        return val / 100.0
    # Backwards-compatible default: a bare value > 1 is a percentage.
    if val > 1:
        return val / 100.0
    return val


def _parse_fd_date(s: str | None) -> date | None:
    """Best-effort parse of an FD date string to a ``date`` (``None`` on failure)."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# Day-count basis (days per year) used by each institution for FD simple interest.
# Most Singapore retail banks compute FD interest on an actual/365 basis, but ICBC
# (a Chinese bank) uses actual/360. Such differences are convention, not data errors,
# so verification and display must follow the institution's basis.
FD_DAY_COUNT_BY_INSTITUTION: dict[str, int] = {
    "ICBC": 360,
}


def _fd_day_count(institution: str | None) -> int:
    """Resolve the day-count basis (days/year) for *institution*.

    Returns 360 for ICBC-family institutions and 365 otherwise. Matching is
    token-based (split on non-alphanumerics), so variants such as ``"ICBC_SG"``
    resolve to the ``"ICBC"`` entry without being listed explicitly, while an
    unrelated name merely containing ``"ICBC"`` as a substring (e.g. ``"XICBC"``)
    does not match.
    """
    key = (institution or "").strip().upper()
    if key in FD_DAY_COUNT_BY_INSTITUTION:
        return FD_DAY_COUNT_BY_INSTITUTION[key]
    for token in re.split(r"[^A-Z0-9]+", key):
        if token in FD_DAY_COUNT_BY_INSTITUTION:
            return FD_DAY_COUNT_BY_INSTITUTION[token]
    return 365


def format_fd_period(
    value_date: str | None,
    maturity_date: str | None,
    institution: str | None = None,
    day_count: int | None = None,
) -> str:
    """Render the FD term as an actual/<basis> day-count string, e.g. ``"181/365"``.

    The basis defaults to 365 but follows the institution's convention when known
    (ICBC uses 360). ``day_count`` overrides the institution lookup when provided.

    Returns ``"—"`` when the dates are missing, invalid, or inverted.
    """
    basis = day_count if (isinstance(day_count, int) and day_count > 0) else _fd_day_count(institution)
    vd = _parse_fd_date(value_date)
    mat = _parse_fd_date(maturity_date)
    if vd is None or mat is None or mat < vd:
        return "—"
    return f"{(mat - vd).days}/{basis}"


def verify_fd_interest(
    principal: float,
    interest_rate: str | float | None,
    value_date: str | None,
    maturity_date: str | None,
    interest_amount: float | None,
    *,
    tol: float = 0.01,
    institution: str | None = None,
    day_count: int | None = None,
) -> str | None:
    """Return a warning string if ``principal × rate × period ≠ interest_amount``.

    Skips (returns ``None``) when ``interest_amount`` is ``None`` (e.g. OCBC),
    the rate is unparseable, or the dates are missing/invalid/non-positive.
    Period (years) is ``(maturity - value).days / basis`` where ``basis`` follows
    the institution's day-count convention (actual/365 by default; actual/360 for
    ICBC). ``day_count`` overrides the institution lookup when provided.

    ``interest_rate`` may be either an already-decimal value (``0.025``) or a
    raw string (``"2.5%"``); a decimal is passed through directly.
    """
    if interest_amount is None:
        return None
    if isinstance(interest_rate, (int, float)):
        rate = float(interest_rate)
    else:
        rate = parse_fd_rate(interest_rate)
    if rate is None:
        return None
    vd = _parse_fd_date(value_date)
    mat = _parse_fd_date(maturity_date)
    if vd is None or mat is None or mat <= vd:
        return None
    basis = day_count if (isinstance(day_count, int) and day_count > 0) else _fd_day_count(institution)
    period_years = (mat - vd).days / basis
    expected = principal * rate * period_years
    if abs(interest_amount - expected) > tol:
        return (
            f"FD interest mismatch: principal {principal:,.2f} × rate {rate:.6f} "
            f"× period {period_years:.6f}y (actual/{basis}d) = {expected:,.2f}, "
            f"but stated interest amount is {interest_amount:,.2f} "
            f"(diff={abs(interest_amount - expected):.2f})"
        )
    return None
