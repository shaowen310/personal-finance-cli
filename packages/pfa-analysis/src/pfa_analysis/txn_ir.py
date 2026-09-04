"""Shared IR JSON parser for categorization tools.

Parses consolidated.ir.json (from bank-ir-consolidate) into a unified
``TxnIR`` dataclass consumed by the categorization pipeline.

Schema aligned with pfa_ir_schema.ir_schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from pfa_ir_schema import from_json


class TxnRowDict(TypedDict):
    """One flattened transaction row (IR-native keys, ``posted_date`` → ``date``)."""

    txn_id: str
    date: str
    description: str
    amount: float
    currency: str
    balance_after: float | None
    bank: str
    account: str
    category_hint: str | None
    tags: list[str]
    link_labels: list[str]
    is_internal_transfer: bool


class AccountDict(TypedDict):
    """Projected account metadata (no account-holder PII)."""

    account_no: str
    account_type: str
    institution: str
    currency: str


class IrMeta(TypedDict):
    """Statement-level metadata."""

    ir_version: str
    institutions: list[str]
    period_from: str
    period_to: str


@dataclass
class TxnIR:
    """Parsed intermediate representation of bank transactions.

    Attributes:
        txns_raw: Flat list of all transaction row dicts (across all accounts).
            Each row uses IR-native keys: ``txn_id``, ``date`` (mapped from
            ``posted_date``), ``description``, ``amount`` (signed float),
            ``currency``, ``balance_after``, ``bank`` (from account's
            ``institution``), ``account`` (from ``account_no``).
        accounts_raw: Original ``accounts`` array from the IR JSON.
        meta: ``ir_version``, ``institutions`` (sorted list),
            ``period_from``, ``period_to``.
        account_types: ``account_no -> account_type`` mapping
    """

    txns_raw: list[TxnRowDict]
    accounts_raw: list[AccountDict]
    meta: IrMeta
    account_types: dict[str, str]


def parse_ir(path: Path) -> TxnIR:
    """Parse a consolidated IR JSON file via the official schema.

    Iterates ``accounts[].transactions[]``, flattening all rows and
    attaching per-account metadata (``institution``, ``account_no``) to
    each row.  ``posted_date`` is normalised to ``date``.
    """
    statement = from_json(path.read_text(encoding="utf-8"))

    txns_raw: list[TxnRowDict] = []
    account_types: dict[str, str] = {}
    institutions: set[str] = set()

    for acct in statement.accounts:
        account_no = acct.account_no
        acct_type = acct.account_type
        institution = acct.institution or ""

        account_types[account_no] = acct_type
        if institution:
            institutions.add(institution)

        for txn in acct.transactions:
            txn_extras = txn.extras or {}
            # ``category_hint`` is an optional free-form string written by the parser.
            hint = cast("str | None", txn_extras.get("category_hint"))
            txns_raw.append(
                {
                    "txn_id": txn.txn_id,
                    "date": txn.posted_date,
                    "description": txn.description,
                    "amount": txn.amount,
                    "currency": txn.currency,
                    "balance_after": txn.balance_after,
                    "bank": institution,
                    "account": account_no,
                    "category_hint": hint,
                    "tags": list(txn.tags),
                    "link_labels": list(txn.link_labels),
                    "is_internal_transfer": txn.is_internal_transfer,
                }
            )

    return TxnIR(
        txns_raw=txns_raw,
        accounts_raw=[
            {
                "account_no": acct.account_no,
                "account_type": acct.account_type,
                "institution": acct.institution or "",
                "currency": acct.currency,
            }
            for acct in statement.accounts
        ],
        meta={
            "ir_version": statement.ir_version,
            "institutions": sorted(institutions),
            "period_from": statement.statement_meta.period_from or "",
            "period_to": statement.statement_meta.period_to or "",
        },
        account_types=account_types,
    )
