"""IR (Intermediate Representation) Schema for bank statement data.

Defines the structured data types that all bank-parser extractors produce,
independent of any source PDF layout. The schema is versioned via ``ir_version``
so downstream consumers can detect compatibility.

All dataclasses provide ``to_dict()`` / ``from_dict()`` for JSON serialisation.
"""

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from .account_type import AccountType
from .common import parse_fd_rate


# ---------------------------------------------------------------------------
# Leaf-level types
# ---------------------------------------------------------------------------

@dataclass
class ParserInfo:
    """Identifies the parser and version that produced this IR."""
    name: str          # e.g. "dbs_sg", "ocbc_sg_card"
    version: str       # e.g. "1.0"


@dataclass
class DebugInfo:
    """Optional debug data attached to a transaction.

    Only populated when the environment variable ``DEBUG=1`` is set.
    """
    raw_pdf_text: str | None = None
    classification_rules: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extension types (Phase 3 — render-time enrichment)
# ---------------------------------------------------------------------------

@dataclass
class InvestmentHolding:
    """One investment / unit trust / fund holding row.

    ``name`` / ``units`` / ``currency`` / ``unit_price`` / ``valuation`` are the
    generic columns shared by all banks. ``cost`` and ``unrealised_pl`` are
    DBS SRS-specific extras (Total Cost / Unrealised P/L) that some
    other banks' holdings rows do not carry — kept optional so the type stays
    usable for every extractor.
    """
    name: str = ""
    units: str = ""
    currency: str = ""
    unit_price: str = ""
    valuation: str = ""
    cost: str = ""            # Total Cost — optional
    unrealised_pl: str = ""   # Unrealised P/L — optional


@dataclass
class CreditCardSummary:
    """Common credit card statement summary fields (bank-agnostic).

    Each extractor maps bank-specific labels to these standardised names.
    Values are stored as strings to preserve the original PDF formatting
    (commas, decimal places, date order).  Renderers format as needed.
    """

    payment_due_date: str | None = None
    credit_limit: str | None = None
    available_credit: str | None = None
    minimum_due: str | None = None
    previous_balance: str | None = None   # "last month's balance"
    total_amount_due: str | None = None


# ---------------------------------------------------------------------------
# Core IR types
# ---------------------------------------------------------------------------

@dataclass
class StatementMeta:
    """Bill-level metadata extracted from the statement header.

    Statement-level only. Per-account identity and balances now live on the
    ``Account`` entity (see ``ParsedStatement.accounts``).
    """

    institution: str = ""          # Bank name, e.g. "DBS", "OCBC", "UOB", "ICBC"
    account_holder: str | None = None  # Statement-level account holder (may be masked)
    period_from: str | None = None  # Statement start date (ISO 8601 YYYY-MM-DD)
    period_to: str | None = None    # Statement end date (ISO 8601 YYYY-MM-DD)
    functional_currency: str = ""   # Statement's functional/reporting currency (e.g. "SGD")


@dataclass
class Transaction:
    """A single transaction record, fully denormalised."""

    # === Identifier ===
    txn_id: str = ""               # Content hash (deterministic, for dedup)

    # === Time ===
    posted_date: str = ""          # Booking date (ISO 8601 YYYY-MM-DD)
    value_date: str | None = None  # Value date (optional)

    # === Amounts ===
    amount: float = 0.0            # Signed amount in transaction currency
    currency: str = ""             # Transaction currency (ISO 4217)
    # Interest leg of an FD closure / premature withdrawal. Carried on the
    # FD-account transaction so the linker can match the funding-account credit
    # (principal + interest) against the bare principal leg. None when N/A.
    interest_amount: float | None = None

    # === Description ===
    description: str = ""

    # === Classification ===
    transfer_labels: list[str] = field(default_factory=list)

    # === Cashflow flags ===
    is_reversal: bool = False
    is_internal_transfer: bool = False  # Internal transfer only (between holder's own accounts)

    # === Relationship ===
    # IDs of transactions in other accounts that this one is the twin of
    # (e.g. a fixed-deposit principal+interest move and its funding-account
    # credit). A list because one transaction can match several — a CA
    # placement may pair with both an FD principal leg and an FD interest leg.
    linked_txn_ids: list[str] = field(default_factory=list)

    # === Balance ===
    balance_after: float | None = None

    # === Bank-specific columns (free-form, for non-standard transaction fields) ===
    extras: dict[str, Any] | None = None

    # === Debug ===
    _debug: DebugInfo | None = None


@dataclass
class FixedDepositRecord:
    """A single fixed-deposit record.

    Fixed deposits are NOT transactions — they are modelled as a dedicated
    type (not as ``Transaction`` objects) so the IR stays honest about what
    each entity is. FD-specific fields are carried directly (no ``extras`` hack).
    """

    deposit_no: str = ""              # deposit / contract number
    value_date: str | None = None     # start/deal date, ISO YYYY-MM-DD (not posted_date)
    maturity_date: str | None = None  # ISO YYYY-MM-DD, normalized from source
    interest_rate: float | None = None          # canonical actual rate, e.g. 0.025
    raw_interest_rate: str | None = None        # raw printed string, e.g. "2.5%", for display
    interest_amount: float | None = None  # renamed from interest_amt
    principal: float = 0.0            # placed principal
    currency: str = ""


@dataclass
class Account:
    """A first-class bank account with identity, balances, and its transactions.

    Replaces the old split between ``StatementMeta`` (per-account fields),
    ``account_summary`` (balance rows), and a flat top-level ``transactions``
    list. Every account now owns its identity, balances, and the transactions
    that belong to it.
    """

    name: str = ""
    account_no: str = ""                       # unmasked
    account_type: str = "unknown"              # AccountType vocabulary
    currency: str = ""
    account_holder: str | None = None          # account-level holder (if distinct)
    institution: str | None = None             # owning bank/institution (set by consolidation)

    # Account-level period (set by consolidation from source statement_meta).
    # Distinct from statement-level period_from/to when a multi-account
    # statement covers different ranges per account (e.g. credit card vs
    # current account).
    period_from: str | None = None
    period_to: str | None = None

    # Balances: opening/closing may be txn-derived or summary-derived.
    opening_balance: float | None = None
    closing_balance: float | None = None

    transactions: list[Transaction] = field(default_factory=list)

    # Fixed-deposit records (NOT transactions). Populated for accounts whose
    # account_type is FIXED_DEPOSIT; otherwise None.
    fd_records: list[FixedDepositRecord] | None = None

    # Investment holdings owned by this account (e.g. SRS / UNIT_TRUST).
    # Populated for accounts whose account_type is SRS or UNIT_TRUST;
    # otherwise None. SRS accounts are ordinary accounts (balance +
    # transactions) that additionally own a list of investment holdings;
    # UNIT_TRUST accounts (UOB portfolio) own fund holdings and carry no
    # cash balance.
    investment_holdings: list[InvestmentHolding] | None = None

    # Bank-specific data that does not fit the standard fields
    # (e.g. credit_line, FD rate, locked_amount, SRS contributions).
    extras: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.account_type = AccountType.normalize(self.account_type).value


@dataclass
class ParsedStatement:
    """Top-level IR container — the output of any Extractor.to_ir()."""

    ir_version: str = "2026.4"
    parsed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    parser: ParserInfo = field(default_factory=lambda: ParserInfo(name="", version=""))
    source_file: str = ""
    statement_meta: StatementMeta = field(default_factory=StatementMeta)
    accounts: list[Account] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Phase 3 extension fields (all Optional, default=None, omitted from JSON when None)
    investment_holdings: list[InvestmentHolding] | None = None
    reconciliation: dict[str, Any] | None = None
    extras: dict[str, Any] | None = None
    credit_card_summary: CreditCardSummary | None = None

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize this statement to a JSON string."""
        return to_json(self, indent=indent)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert dataclass instance(s) to plain dicts.

    ``None`` values are omitted from output to keep JSON compact and
    backwards-compatible.
    """
    if isinstance(obj, list):
        return [_dataclass_to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        result: dict[str, Any] = {}
        for f in fields(obj):
            val = getattr(obj, f.name)
            if val is not None:
                result[f.name] = _dataclass_to_dict(val)
        return result
    return obj


def to_json(statement: ParsedStatement, *, indent: int = 2) -> str:
    """Serialize a ParsedStatement to a JSON string."""
    return json.dumps(_dataclass_to_dict(statement), indent=indent, ensure_ascii=False)


def _transaction_from_dict(td: dict[str, Any]) -> Transaction:
    """Build a single ``Transaction`` from its dict representation."""
    return Transaction(
        txn_id=td.get("txn_id", ""),
        posted_date=td.get("posted_date", ""),
        value_date=td.get("value_date"),
        amount=td.get("amount", 0.0),
        currency=td.get("currency", ""),
        interest_amount=td.get("interest_amount"),
        description=td.get("description", ""),
        transfer_labels=td.get("transfer_labels", []),
        is_reversal=td.get("is_reversal", False),
        is_internal_transfer=td.get("is_internal_transfer", False),
        linked_txn_ids=td.get("linked_txn_ids") or [],
        balance_after=td.get("balance_after"),
        extras=td.get("extras"),
        _debug=DebugInfo(**td["_debug"]) if td.get("_debug") else None,
    )


def _account_from_dict(ad: dict[str, Any]) -> Account:
    """Build a single ``Account`` (with nested transactions) from its dict."""
    return Account(
        name=ad.get("name", ""),
        account_no=ad.get("account_no", ""),
        account_type=ad.get("account_type", "unknown"),
        currency=ad.get("currency", ""),
        account_holder=ad.get("account_holder"),
        institution=ad.get("institution"),
        opening_balance=ad.get("opening_balance"),
        closing_balance=ad.get("closing_balance"),
        transactions=[_transaction_from_dict(t) for t in ad.get("transactions", [])],
        fd_records=(
            [_fd_record_from_dict(r) for r in ad["fd_records"]]
            if ad.get("fd_records") is not None else None
        ),
        investment_holdings=(
            [_investment_holding_from_dict(r) for r in ad["investment_holdings"]]
            if ad.get("investment_holdings") is not None else None
        ),
        extras=ad.get("extras"),
    )


def _fd_record_from_dict(rd: dict[str, Any]) -> FixedDepositRecord:
    """Build a single ``FixedDepositRecord`` from its dict representation."""
    raw = rd.get("raw_interest_rate")
    rate = rd.get("interest_rate")
    if rate is None and raw is not None:
        rate = parse_fd_rate(raw)
    return FixedDepositRecord(
        deposit_no=rd.get("deposit_no", ""),
        value_date=rd.get("value_date"),
        maturity_date=rd.get("maturity_date"),
        interest_rate=rate,
        raw_interest_rate=raw,
        interest_amount=rd.get("interest_amount"),
        principal=rd.get("principal", 0.0),
        currency=rd.get("currency", ""),
    )


def _investment_holding_from_dict(hd: dict[str, Any]) -> InvestmentHolding:
    """Build a single ``InvestmentHolding`` from its dict representation."""
    return InvestmentHolding(
        name=hd.get("name", ""),
        units=hd.get("units", ""),
        currency=hd.get("currency", ""),
        unit_price=hd.get("unit_price", ""),
        valuation=hd.get("valuation", ""),
        cost=hd.get("cost", ""),
        unrealised_pl=hd.get("unrealised_pl", ""),
    )


def from_dict(data: dict[str, Any]) -> ParsedStatement:
    """Deserialize a dict (e.g. from JSON) back to a ParsedStatement.

    Requires the ``accounts`` field. IR produced by older parsers (before
    ``ir_version`` 2026.3) used a flat ``transactions`` list and no ``accounts``
    field and is no longer supported — callers must re-run extraction from the
    source PDF.
    """
    if "accounts" not in data:
        raise ValueError(
            "Unsupported IR version: the 'accounts' field is missing. This IR "+
            "was produced by an older parser (ir_version < 2026.3) and is no "+
            "longer supported. Please re-run extraction from the source PDF."
        )

    parser_data = data.get("parser", {})
    meta_data = data.get("statement_meta", {})

    accounts = [_account_from_dict(a) for a in data.get("accounts", [])]

    return ParsedStatement(
        ir_version=data.get("ir_version", "2026.4"),
        parsed_at=data.get("parsed_at", ""),
        parser=ParserInfo(**parser_data) if parser_data else ParserInfo(name="", version=""),
        source_file=data.get("source_file", ""),
        statement_meta=StatementMeta(**meta_data) if meta_data else StatementMeta(),
        accounts=accounts,
        warnings=data.get("warnings", []),
        # Phase 3 extension fields (default to None when absent)
        investment_holdings=(
            [InvestmentHolding(**r) for r in data["investment_holdings"]]
            if data.get("investment_holdings") is not None else None
        ),
        reconciliation=data.get("reconciliation"),
        extras=data.get("extras"),
        credit_card_summary=(
            CreditCardSummary(**data["credit_card_summary"])
            if data.get("credit_card_summary") else None
        ),
    )


def from_json(json_str: str) -> ParsedStatement:
    """Deserialize a JSON string to a ParsedStatement."""
    return from_dict(json.loads(json_str))


# ---------------------------------------------------------------------------
# txn_id generation
# ---------------------------------------------------------------------------

def _sanitize_for_hash(value: object) -> str:
    """Convert a value to a stable string for hashing."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return "[" + ",".join(_sanitize_for_hash(v) for v in value) + "]"
    return str(value)


def generate_txn_id(
    posted_date: str,
    amount: float,
    currency: str,
    description: str,
) -> str:
    """Generate a deterministic transaction ID from key fields.

    Uses SHA-256 of sorted key-value pairs, returning the first 16 hex chars.
    This allows downstream to identify likely duplicates without a database.
    """
    fields = {
        "posted_date": posted_date,
        "amount": amount,
        "currency": currency,
        "description": description,
    }
    raw = "|".join(f"{k}:{_sanitize_for_hash(v)}" for k, v in sorted(fields.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
