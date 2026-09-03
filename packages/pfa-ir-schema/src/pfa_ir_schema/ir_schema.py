"""IR (Intermediate Representation) Schema for bank statement data.

Defines the structured data types that all bank-parser extractors produce,
independent of any source PDF layout. The schema is versioned via ``ir_version``
so downstream consumers can detect compatibility.

All dataclasses provide ``to_dict()`` / ``from_dict()`` for JSON serialisation.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import cast

from .account_type import AccountType
from .common import parse_fd_rate


# Arbitrary JSON value — used for free-form ``extras`` / reconciliation data and
# the dict-based (de)serialisation helpers. Replaces ``Any`` so the package stays
# free of explicit ``Any`` under ``reportExplicitAny``.
type JSONValue = (
    dict[str, "JSONValue"] | list["JSONValue"] | str | int | float | bool | None
)


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
    # Standalone classification tags (e.g. "salary"), independent of any
    # linkage. Distinct from ``link_labels`` which describes relationships
    # between transactions.
    tags: list[str] = field(default_factory=list)

    # === Relationship (links to twin/related transactions) ===
    # Relationship labels describing how this transaction links to others
    # (see pfa_ir_schema.relations). Accompanies ``linked_txn_ids``.
    link_labels: list[str] = field(default_factory=list)

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
    extras: dict[str, JSONValue] | None = None

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
    extras: dict[str, JSONValue] | None = None

    def __post_init__(self) -> None:
        self.account_type = AccountType.normalize(self.account_type).value


@dataclass
class ParsedStatement:
    """Top-level IR container — the output of any Extractor.to_ir()."""

    ir_version: str = "2026.5"
    parsed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    parser: ParserInfo = field(default_factory=lambda: ParserInfo(name="", version=""))
    source_file: str = ""
    statement_meta: StatementMeta = field(default_factory=StatementMeta)
    accounts: list[Account] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Phase 3 extension fields (all Optional, default=None, omitted from JSON when None)
    investment_holdings: list[InvestmentHolding] | None = None
    reconciliation: dict[str, JSONValue] | None = None
    extras: dict[str, JSONValue] | None = None
    credit_card_summary: CreditCardSummary | None = None

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize this statement to a JSON string."""
        return to_json(self, indent=indent)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _dataclass_to_dict(obj: object) -> JSONValue:
    """Recursively convert dataclass instance(s) to plain dicts.

    ``None`` values are omitted from output to keep JSON compact and
    backwards-compatible.
    """
    if isinstance(obj, list):
        return [_dataclass_to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        result: dict[str, JSONValue] = {}
        obj_dict: dict[str, object] = getattr(obj, "__dict__")
        for k, v in obj_dict.items():
            if v is not None:
                result[k] = _dataclass_to_dict(v)
        return result
    return cast(JSONValue, obj)


def to_json(statement: ParsedStatement, *, indent: int = 2) -> str:
    """Serialize a ParsedStatement to a JSON string."""
    return json.dumps(_dataclass_to_dict(statement), indent=indent, ensure_ascii=False)


def _transaction_from_dict(td: dict[str, JSONValue]) -> Transaction:
    """Build a single ``Transaction`` from its dict representation."""
    debug_data = cast("dict[str, JSONValue] | None", td.get("_debug"))
    return Transaction(
        txn_id=cast(str, td.get("txn_id", "")),
        posted_date=cast(str, td.get("posted_date", "")),
        value_date=cast("str | None", td.get("value_date")),
        amount=cast(float, td.get("amount", 0.0)),
        currency=cast(str, td.get("currency", "")),
        interest_amount=cast("float | None", td.get("interest_amount")),
        description=cast(str, td.get("description", "")),
        tags=cast("list[str]", td.get("tags", [])),
        link_labels=cast("list[str]", td.get("link_labels", [])),
        is_reversal=cast(bool, td.get("is_reversal", False)),
        is_internal_transfer=cast(bool, td.get("is_internal_transfer", False)),
        linked_txn_ids=cast("list[str]", td.get("linked_txn_ids") or []),
        balance_after=cast("float | None", td.get("balance_after")),
        extras=cast("dict[str, JSONValue] | None", td.get("extras")),
        _debug=(
            DebugInfo(
                raw_pdf_text=cast("str | None", debug_data.get("raw_pdf_text")),
                classification_rules=cast("list[str]", debug_data.get("classification_rules", [])),
            )
            if debug_data else None
        ),
    )


def _account_from_dict(ad: dict[str, JSONValue]) -> Account:
    """Build a single ``Account`` (with nested transactions) from its dict."""
    return Account(
        name=cast(str, ad.get("name", "")),
        account_no=cast(str, ad.get("account_no", "")),
        account_type=cast(str, ad.get("account_type", "unknown")),
        currency=cast(str, ad.get("currency", "")),
        account_holder=cast("str | None", ad.get("account_holder")),
        institution=cast("str | None", ad.get("institution")),
        opening_balance=cast("float | None", ad.get("opening_balance")),
        closing_balance=cast("float | None", ad.get("closing_balance")),
        transactions=cast(
            "list[Transaction]",
            [_transaction_from_dict(cast("dict[str, JSONValue]", t)) for t in cast("list[JSONValue]", ad.get("transactions", []))],
        ),
        fd_records=(
            cast(
                "list[FixedDepositRecord]",
                [_fd_record_from_dict(cast("dict[str, JSONValue]", r)) for r in cast("list[JSONValue]", ad["fd_records"])],
            )
            if ad.get("fd_records") is not None else None
        ),
        investment_holdings=(
            cast(
                "list[InvestmentHolding]",
                [_investment_holding_from_dict(cast("dict[str, JSONValue]", r)) for r in cast("list[JSONValue]", ad["investment_holdings"])],
            )
            if ad.get("investment_holdings") is not None else None
        ),
        extras=cast("dict[str, JSONValue] | None", ad.get("extras")),
    )


def _fd_record_from_dict(rd: dict[str, JSONValue]) -> FixedDepositRecord:
    """Build a single ``FixedDepositRecord`` from its dict representation."""
    raw = cast("str | None", rd.get("raw_interest_rate"))
    rate = cast("float | None", rd.get("interest_rate"))
    if rate is None and raw is not None:
        rate = parse_fd_rate(raw)
    return FixedDepositRecord(
        deposit_no=cast(str, rd.get("deposit_no", "")),
        value_date=cast("str | None", rd.get("value_date")),
        maturity_date=cast("str | None", rd.get("maturity_date")),
        interest_rate=rate,
        raw_interest_rate=raw,
        interest_amount=cast("float | None", rd.get("interest_amount")),
        principal=cast(float, rd.get("principal", 0.0)),
        currency=cast(str, rd.get("currency", "")),
    )


def _investment_holding_from_dict(hd: dict[str, JSONValue]) -> InvestmentHolding:
    """Build a single ``InvestmentHolding`` from its dict representation."""
    return InvestmentHolding(
        name=cast(str, hd.get("name", "")),
        units=cast(str, hd.get("units", "")),
        currency=cast(str, hd.get("currency", "")),
        unit_price=cast(str, hd.get("unit_price", "")),
        valuation=cast(str, hd.get("valuation", "")),
        cost=cast(str, hd.get("cost", "")),
        unrealised_pl=cast(str, hd.get("unrealised_pl", "")),
    )


def from_dict(data: dict[str, JSONValue]) -> ParsedStatement:
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

    parser_data = cast("dict[str, JSONValue]", data.get("parser", {}))
    meta_data = cast("dict[str, JSONValue]", data.get("statement_meta", {}))

    accounts = cast(
        "list[Account]",
        [_account_from_dict(cast("dict[str, JSONValue]", a)) for a in cast("list[JSONValue]", data.get("accounts", []))],
    )

    credit_card_summary = cast("dict[str, JSONValue] | None", data.get("credit_card_summary"))
    investment_holdings_raw = cast("list[JSONValue] | None", data.get("investment_holdings"))

    return ParsedStatement(
        ir_version=cast(str, data.get("ir_version", "2026.4")),
        parsed_at=cast(str, data.get("parsed_at", "")),
        parser=ParserInfo(
            name=cast(str, parser_data.get("name", "")),
            version=cast(str, parser_data.get("version", "")),
        ),
        source_file=cast(str, data.get("source_file", "")),
        statement_meta=StatementMeta(
            institution=cast(str, meta_data.get("institution") or ""),
            account_holder=cast("str | None", meta_data.get("account_holder")),
            period_from=cast("str | None", meta_data.get("period_from")),
            period_to=cast("str | None", meta_data.get("period_to")),
            functional_currency=cast(str, meta_data.get("functional_currency", "")),
        ),
        accounts=accounts,
        warnings=cast("list[str]", data.get("warnings", [])),
        # Phase 3 extension fields (default to None when absent)
        investment_holdings=(
            cast(
                "list[InvestmentHolding]",
                [
                    InvestmentHolding(
                        name=cast(str, rd.get("name", "")),
                        units=cast(str, rd.get("units", "")),
                        currency=cast(str, rd.get("currency", "")),
                        unit_price=cast(str, rd.get("unit_price", "")),
                        valuation=cast(str, rd.get("valuation", "")),
                        cost=cast(str, rd.get("cost", "")),
                        unrealised_pl=cast(str, rd.get("unrealised_pl", "")),
                    )
                    for r in cast("list[JSONValue]", data["investment_holdings"])
                    for rd in (cast("dict[str, JSONValue]", r),)
                ],
            )
            if investment_holdings_raw is not None else None
        ),
        reconciliation=cast("dict[str, JSONValue] | None", data.get("reconciliation")),
        extras=cast("dict[str, JSONValue] | None", data.get("extras")),
        credit_card_summary=(
            CreditCardSummary(
                payment_due_date=cast("str | None", credit_card_summary.get("payment_due_date")),
                credit_limit=cast("str | None", credit_card_summary.get("credit_limit")),
                available_credit=cast("str | None", credit_card_summary.get("available_credit")),
                minimum_due=cast("str | None", credit_card_summary.get("minimum_due")),
                previous_balance=cast("str | None", credit_card_summary.get("previous_balance")),
                total_amount_due=cast("str | None", credit_card_summary.get("total_amount_due")),
            )
            if credit_card_summary else None
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
