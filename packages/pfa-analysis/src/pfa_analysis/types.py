"""Shared type hints for the ``pfa_analysis`` package.

Centralises the cross-module domain types (transaction rows, raw-IR JSON
shape, analysis results, dashboard/renderer views) so that ``analyze``,
``render_md``, ``dashboard``, ``categorize`` and ``report`` all import from a
single source instead of reaching into each other's modules.  Keeping these as
plain ``TypedDict`` definitions (no runtime logic) means every module can import
this one safely with no circular-import risk.
"""

from typing import NotRequired, TypedDict

# ---------------------------------------------------------------------------
# Transaction rows
# ---------------------------------------------------------------------------


class TxnRow(TypedDict):
    """A flattened transaction row as consumed across the analysis stack.

    Merges the previously-separate ``analyze.TxnRow`` (TypedDict) and
    ``categorize.TxnRow`` (dataclass) definitions: ``institution``/``bank`` come
    from the analysis view, ``category_hint``/``tags`` from the categorization
    view.  All such extras are ``NotRequired`` so either producer may omit them.
    """

    txn_id: str
    date: str
    description: str
    amount: float
    currency: str
    account_type: str
    account: str
    is_internal_transfer: bool
    balance_after: float | None
    linked_txn_ids: NotRequired[list[str]]
    link_labels: NotRequired[list[str]]
    institution: NotRequired[str]
    bank: NotRequired[str]
    category_hint: NotRequired[str | None]
    tags: NotRequired[list[str] | None]


# Backwards-compatible alias for the transaction-row type.
Txn = TxnRow


class DrilldownTxn(TypedDict):
    """A display transaction row used inside income/expense/transfer drilldowns."""

    date: str
    description: str
    amount: float
    currency: str
    bank: str
    account: str
    account_type: str
    txn_id: NotRequired[str]
    linked_txn_ids: NotRequired[list[str]]


# ---------------------------------------------------------------------------
# Raw consolidated-IR JSON shape (mirrors pfa_ir_schema; used by the legacy
# json.loads + cast path in analyze.py)
# ---------------------------------------------------------------------------


class RawTxn(TypedDict, total=False):
    """A raw ``transactions[]`` entry as parsed from ``.ir.json``."""

    posted_date: str
    value_date: str
    amount: float | str
    description: str
    is_internal_transfer: bool
    balance_after: float | str | None
    txn_id: str
    linked_txn_ids: list[str]
    link_labels: list[str]
    currency: str


class RawHolding(TypedDict, total=False):
    name: str
    currency: str
    valuation: float | str | None
    market_value: float | str | None


class RawAccount(TypedDict, total=False):
    account_no: str
    account_type: str
    currency: str
    institution: str
    closing_balance: float | str | None
    opening_balance: float | str | None
    period_to: str
    period_from: str
    transactions: list[RawTxn]
    investment_holdings: list[RawHolding]


class StatementMeta(TypedDict, total=False):
    institution: str
    account_id: str
    currency: str
    functional_currency: str
    period_from: str
    period_to: str
    opening_balance: float | str | None
    closing_balance: float | str | None


class TimeDeposit(TypedDict, total=False):
    account_no: str
    currency: str
    closing_balance: float | None
    deposit_no: str
    principal: float | str | None
    interest_amount: float | str | None


class Extras(TypedDict, total=False):
    time_deposits: list[TimeDeposit]
    unit_trusts: list[RawHolding]


class AccountSummaryEntry(TypedDict, total=False):
    account_no: str
    account_type: str
    currency: str
    institution: str
    closing_balance: float | None
    opening_balance: float | None


class RawIr(TypedDict, total=False):
    statement_meta: StatementMeta
    extras: Extras
    accounts: list[RawAccount]
    account_summary: list[AccountSummaryEntry]
    investment_holdings: list[RawHolding]
    institution: str
    source_file: str


# ---------------------------------------------------------------------------
# Analysis results
# ---------------------------------------------------------------------------


class Meta(TypedDict, total=False):
    bank: str
    account_no: str
    currency: str | None
    period_start: str | None
    period_end: str | None
    opening: float | str | None
    closing: float | str | None
    account_summary: list[AccountSummaryEntry]
    investment_holdings: list[RawHolding]
    extras: Extras
    source_file: str | None
    _consolidated: bool


class Metrics(TypedDict):
    income: float
    expense: float
    transfer_in: float
    transfer_out: float
    transfer_in_internal: float
    transfer_in_external: float
    transfer_out_internal: float
    transfer_out_external: float
    fx_conversion_in: float
    fx_conversion_out: float
    total_inflow: float
    total_outflow: float
    net_change_cash: float
    net_operating: float
    savings_rate: float
    opening: float | None
    closing: float | None
    balance_change: float | None
    reconciliation_ok: bool | None
    txn_count: int


class DrilldownRow(TypedDict):
    currency: str
    institution: str
    account_no: str
    account_type: str
    bucket: str
    native_value: float
    derivation: str
    carried_forward: bool
    period_to: str | None
    missing_early: bool
    earliest_covered: str | None
    latest_covered: str | None


class IncomeExpenseGroup(TypedDict):
    source: NotRequired[str]
    category: NotRequired[str]
    by_currency: dict[str, float]
    transactions: list[DrilldownTxn]


class TransferGroup(TypedDict):
    category: str
    by_currency: dict[str, float]
    transactions: list[DrilldownTxn]


class FxPair(TypedDict):
    date: str
    given: dict[str, str | float]
    received: dict[str, str | float]
    implied_rate: float
    fx_gl_sgd: float
    txn_ids: list[str]


class FxResult(TypedDict):
    base_currency: str
    total_sgd: float
    as_of: str
    source: str
    by_received_currency: dict[str, float]
    pairs: list[FxPair]


class AnalysisResult(TypedDict):
    meta: Meta
    metrics_by_ccy: dict[str, Metrics]
    assets: dict[str, dict[str, float]]
    drilldown: list[DrilldownRow]
    has_txns: bool
    source: str
    warnings: list[str]


# ---------------------------------------------------------------------------
# Renderer view
# ---------------------------------------------------------------------------


class CatSummaryEntry(TypedDict):
    """One row of the categorization summary table.

    ``kind`` holds the high-level bucket: "Income", "Expense", or "Transfer".
    """

    kind: str
    category: str
    count: int
