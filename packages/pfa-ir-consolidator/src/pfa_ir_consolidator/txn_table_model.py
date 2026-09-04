"""txn_table_model.py — flatten a ParsedStatement into the categorizer interchange.

Keeps ``consolidated.ir.json`` as the authoritative ``ParsedStatement`` (no data
duplication) while projecting it into the de-identified ``TxnTableModel``
structure: ``export_model.py`` serializes it for the transaction-categorization
stage (consumed by ``pfa-analysis``) and ``merge_categories.py`` merges the
categorizer output back through it.

Masking is intentionally NOT applied here — descriptions/account numbers stay
raw so the categorizer can match inter-bank transfers by destination account
number; masking happens only at report-rendering time (``pfa-analysis``).

FX rates (``DEFAULT_FX_RATES``, sourced from the ``pfa-fx`` package) are
**not embedded in the statement data**; they are sourced externally (e.g.
historical mid-market rates) and used to estimate SGD-equivalent values.
Override by passing a custom ``fx_rates`` dict on construction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from pfa_fx import DEFAULT_FX_RATES

# Account types that carry FD/investment records instead of spend transactions.
_NON_TXN_ACCOUNTS = ("fixed_deposit", "unit_trust")


@dataclass
class TxnRow:
    date: str
    bank: str
    account: str            # raw account_no (masking applied downstream)
    description: str        # raw description (masking applied downstream)
    withdrawal: float | None
    deposit: float | None
    balance_after: float | None
    net_deposits: float | None = None  # running net (deposit - withdrawal) within a currency table
    txn_id: str = ""
    currency: str = ""
    category: str = ""      # set from txn.extras["category"] when present (merge-back)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "date": self.date,
            "bank": self.bank,
            "account": self.account,
            "description": self.description,
            "withdrawal": self.withdrawal,
            "deposit": self.deposit,
            "balance_after": self.balance_after,
            "txn_id": self.txn_id,
            "currency": self.currency,
        }
        if self.net_deposits is not None:
            d["net_deposits"] = self.net_deposits
        if self.category:
            d["category"] = self.category
        return d


@dataclass
class CurrencyTable:
    currency: str
    rows: list[TxnRow] = field(default_factory=list)

    @property
    def total_withdrawal(self) -> float:
        return sum((r.withdrawal or 0.0) for r in self.rows)

    @property
    def total_deposit(self) -> float:
        return sum((r.deposit or 0.0) for r in self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {"currency": self.currency, "rows": [r.to_dict() for r in self.rows]}


@dataclass
class TxnTableModel:
    ir_version: str
    sources: list[dict[str, Any]]
    institutions: list[str]
    period_from: str | None
    period_to: str | None
    net_sgd: float
    per_ccy_balances: dict[str, float]
    fx_rates: dict[str, float]       # currency → SGD per 1 unit (SGD = 1.0)
    txn_tables_by_type: dict[str, list[CurrencyTable]]
    accounts: list[Any]      # original accounts, projected (PII-stripped) in to_dict()
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the de-identified interchange JSON for categorizers.

        ``accounts`` is **projected** (never the raw ``ir_schema.Account``): only
        ``account_no``, ``account_type``, ``currency``, ``balance``, ``fd_records``
        and ``investment_holdings`` are emitted — ``account_holder`` PII is dropped.
        Raw ``account_no`` / ``deposit_no`` are intentionally kept so a separate
        project can match inter-bank transfers by destination account number.
        """
        accounts = [
            {
                "account_no": a.account_no,
                "account_type": a.account_type,
                "currency": a.currency,
                "balance": a.closing_balance,
                "fd_records": [
                    {"deposit_no": fd.deposit_no} for fd in (a.fd_records or [])
                ],
                "investment_holdings": [
                    asdict(h) for h in (a.investment_holdings or [])
                ],
            }
            for a in self.accounts
        ]
        return {
            "ir_version": self.ir_version,
            "institutions": self.institutions,
            "period_from": self.period_from,
            "period_to": self.period_to,
            "txn_tables_by_type": {
                atype: [ct.to_dict() for ct in tables]
                for atype, tables in self.txn_tables_by_type.items()
            },
            "accounts": accounts,
        }


def _account_institution(acc: Any) -> str:
    """Resolve the owning institution from the account's institution field."""
    return acc.institution or ""


def _skip_in_consolidated_table(txn: Any) -> bool:
    """Internal lines excluded from the consolidated combined transaction table.

    OCBC labels internet-banking transfers as ``PAYMENT BY INTERNET``; these carry
    a negative (debit) amount and are internal/posted summaries that shouldn't
    appear in the consolidated table (nor in its totals / running net).

    Transactions flagged ``is_internal_transfer`` (linked to another txn via
    transfer detection) are also skipped to avoid double-counting.
    """
    if txn.is_internal_transfer:
        return True
    return txn.amount < 0 and "PAYMENT BY INTERNET" in (txn.description or "").upper()


def build_txn_table_model(stmt: Any, fx_rates: dict[str, float] | None = None) -> TxnTableModel:
    meta = stmt.statement_meta
    consolidation = (stmt.extras or {}).get("consolidation", {})
    sources = consolidation.get("sources", [])
    institutions = sorted({s.get("institution") for s in sources if s.get("institution")})

    effective_fx = {**DEFAULT_FX_RATES, **(fx_rates or {})}

    net_sgd = 0.0
    for a in stmt.accounts:
        if a.closing_balance is not None and a.currency:
            rate = effective_fx.get(a.currency)
            if rate is not None:
                net_sgd += a.closing_balance * rate
    per_ccy: dict[str, float] = {}
    for a in stmt.accounts:
        if a.closing_balance is not None and a.currency:
            per_ccy[a.currency] = per_ccy.get(a.currency, 0.0) + a.closing_balance

    by_type_ccy: dict[tuple[str, str], CurrencyTable] = {}
    for acc in stmt.accounts:
        atype = acc.account_type
        if atype in _NON_TXN_ACCOUNTS:
            continue
        bank = _account_institution(acc)
        for t in acc.transactions:
            if _skip_in_consolidated_table(t):
                continue
            wd = abs(t.amount) if t.amount < 0 else None
            dp = t.amount if t.amount > 0 else None
            ct = by_type_ccy.setdefault(
                (atype, t.currency), CurrencyTable(currency=t.currency)
            )
            ct.rows.append(
                TxnRow(
                    date=t.posted_date,
                    bank=bank,
                    account=acc.account_no,
                    description=t.description,
                    withdrawal=wd,
                    deposit=dp,
                    balance_after=t.balance_after,
                    txn_id=t.txn_id,
                    currency=t.currency,
                    category=(t.extras or {}).get("category", "") if t.extras else "",
                )
            )

    for ct in by_type_ccy.values():
        ct.rows.sort(key=lambda r: (r.date, r.txn_id))
        # Running net deposits (deposit - withdrawal) across the currency table.
        # The per-account balance_after is meaningless once rows from multiple
        # accounts are interleaved, so the consolidated view uses this instead.
        running = 0.0
        for r in ct.rows:
            running += (r.deposit or 0.0) - (r.withdrawal or 0.0)
            r.net_deposits = running

    txn_tables_by_type: dict[str, list[CurrencyTable]] = {}
    for (atype, _ccy), ct in by_type_ccy.items():
        txn_tables_by_type.setdefault(atype, []).append(ct)
    for tables in txn_tables_by_type.values():
        tables.sort(key=lambda ct: ct.currency)

    return TxnTableModel(
        ir_version=stmt.ir_version,
        sources=sources,
        institutions=institutions,
        period_from=meta.period_from,
        period_to=meta.period_to,
        net_sgd=net_sgd,
        per_ccy_balances=per_ccy,
        fx_rates=effective_fx,
        txn_tables_by_type=txn_tables_by_type,
        accounts=stmt.accounts,
        warnings=stmt.warnings,
    )
