#!/usr/bin/env python3
"""render_md.py — render a consolidated (or any) IR JSON into markdown.

Reads a ``ParsedStatement`` IR JSON (typically the output of ``consolidate.py``),
builds a render-oriented ``RenderModel`` and writes a human-readable, cross-bank
markdown summary with masking applied (consistent with ``sg-bank-pdf-parser``).

Usage:
    python render_md.py consolidated.ir.json -o consolidated.md
    python render_md.py consolidated.ir.json -o consolidated.md --no-mask
"""
from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path
from typing import Any

# Allow running as a standalone script from scripts/ or the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pfa_ir_schema
from pfa_fx import (
    FXResult,
    collect_currencies,
    get_fx_rates,
    get_provider,
)

from pfa_ir_consolidator.render_model import (
    TXN_SECTION_ORDER,
    build_render_model,
)


def _money(v: float | None, currency: str = "") -> str:
    if v is None:
        return "—"
    try:
        return f"{v:,.2f} {currency}".strip()
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return str(v)


def _account_institution(acc: Any) -> str:
    """Resolve the owning institution from the account's institution field."""
    return acc.institution or "—"


def _get_balance(acc: Any) -> float:
    """Get the effective balance of an account.

    Falls back through:
    1. ``acc.closing_balance``
    2. Last transaction's ``balance_after`` (common for OCBC accounts)
    3. FD record principal (for OCBC TIME DEPOSITS-style accounts)
    4. 0.0
    """
    if acc.closing_balance is not None:
        return acc.closing_balance
    if acc.transactions:
        last_ba = None
        for t in acc.transactions:
            if t.balance_after is not None:
                last_ba = t.balance_after
        if last_ba is not None:
            return last_ba
    if acc.fd_records:
        return sum(getattr(r, "principal", 0) or 0 for r in acc.fd_records)
    return 0.0


def _sgd_equiv(balance: float, currency: str, fx_rates: dict[str, float]) -> float | None:
    """Compute SGD equivalent of *balance* in *currency* using the given FX rates."""
    rate = fx_rates.get(currency)
    if rate is None:
        return None
    return balance * rate


def _parse_money(v: str | None) -> float | None:
    """Parse a money string (commas/spaces) to float; dashes/blank -> None."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "—", "-", "n/a", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# Net Position display types: (account_type, section_heading) in display order.
_NET_POSITION_TYPES: list[tuple[str, str]] = [
    ("current", "Current Accounts"),
    ("fixed_deposit", "Fixed Deposits"),
    ("srs", "SRS"),
    ("ewallet", "E-wallets"),
    ("unknown", "Other Accounts"),
]

# FX rate display order (for the FX rate table). SGD first, then the usual
# watch-list, then any extra currencies discovered in the statement.
_FX_RATE_CCY_ORDER: list[str] = ["SGD", "USD", "JPY", "CNY"]


def _fx_note(fx_result: FXResult | None) -> str:
    """Build the provenance note line for the FX rate table."""
    if fx_result is None:
        return (
            "*Mid-market rates (source unknown). "
            "SGD per 1 unit of foreign currency.*"
        )
    src = fx_result.source
    label = {
        "live": "live (fetched)",
        "cached": "cached",
        "fallback": "fallback (hardcoded default)",
        "none": "n/a",
    }.get(src, src)
    parts = [f"*Mid-market rates (**{label}**, provider: {fx_result.provider})"]
    if fx_result.as_of:
        parts.append(f"as of {fx_result.as_of}")
    if fx_result.fetched_at:
        parts.append(f"fetched at {fx_result.fetched_at}")
    parts.append("SGD per 1 unit of foreign currency.*")
    note = " ".join(parts)
    if fx_result.missing:
        note += (
            f"\n\n_Missing rates (used hardcoded fallback for these): "
            f"{', '.join(fx_result.missing)}._"
        )
    return note


def _render_net_position(model: Any, mask_id: Any, do_mask: bool, fx_result: FXResult | None = None) -> list[str]:
    lines: list[str] = []
    lines.append("## Net Position")
    lines.append("")

    period_to = model.period_to or "—"
    lines.append(f"### FX Rate Table (as of {period_to})")
    lines.append("")
    lines.append(_fx_note(fx_result))
    lines.append("")
    lines.append("| Currency | Mid-market (1 SGD =) | SGD per 1 unit |")
    lines.append("| --- | --- | ---: |")
    # Display order: SGD, then the usual watch-list (if present), then any extra
    # currencies discovered in the statement — all driven by the live/cached rates.
    order: list[str] = ["SGD"]
    for ccy in _FX_RATE_CCY_ORDER:
        if ccy != "SGD" and ccy in model.fx_rates and ccy not in order:
            order.append(ccy)
    for ccy in sorted(model.fx_rates):
        if ccy != "SGD" and ccy not in order:
            order.append(ccy)
    # Look up inverse rates (1 SGD = X CCY) from the model's fx_rates.
    for ccy in order:
        rate = model.fx_rates.get(ccy)
        if rate is None or ccy == "SGD":
            inv = "—"
            fwd = "1.0000"
        else:
            inv = f"{1.0 / rate:.4f}"
            fwd = f"{rate:.4f}"
        lines.append(f"| {ccy} | {inv} | {fwd} |")
    lines.append("")

    # Group accounts by type.
    by_type: dict[str, list[Any]] = {}
    for a in model.accounts:
        by_type.setdefault(a.account_type, []).append(a)

    grand_total_sgd = 0.0

    for atype, heading in _NET_POSITION_TYPES:
        accounts = by_type.get(atype)
        if not accounts:
            continue

        lines.append(f"### {heading}")
        lines.append("")

        if atype == "fixed_deposit":
            lines.append(
                "| Bank | Account | Currency | Principal | Balance (SGD) |"
            )
            lines.append(
                "| --- | --- | --- | ---: | ---: |"
            )
        else:
            lines.append(
                "| Bank | Account | Currency | Balance | Balance (SGD) |"
            )
            lines.append(
                "| --- | --- | --- | ---: | ---: |"
            )

        type_total_sgd = 0.0
        zero_bal_wallets: list[str] = []

        for acc in sorted(accounts, key=lambda a: (a.institution or "", a.account_no or "")):
            bal = _get_balance(acc)

            if bal == 0:
                # Collect zero-balance wallets for a footnote instead of cluttering the table.
                inst = _account_institution(acc)
                ccy = acc.currency or "—"
                zero_bal_wallets.append(f"{inst} {ccy}")
                continue

            sgd_val = _sgd_equiv(bal, acc.currency or "SGD", model.fx_rates)
            if sgd_val is None:
                sgd_equiv_display = "—"
                row_sgd = 0.0
            else:
                sgd_equiv_display = f"{sgd_val:,.2f}"
                row_sgd = sgd_val
            acct_id = mask_id(acc.account_no, do_mask=do_mask)
            inst = _account_institution(acc)
            ccy = acc.currency or "—"
            bal_str = f"{bal:,.2f}"

            if atype == "fixed_deposit":
                lines.append(
                    f"| {inst} | {acct_id} | {ccy} | {bal_str} | {sgd_equiv_display} |"
                )
            else:
                lines.append(
                    f"| {inst} | {acct_id} | {ccy} | {bal_str} | {sgd_equiv_display} |"
                )
            type_total_sgd += row_sgd

        lines.append(
            f"| | **Subtotal** | | | **{type_total_sgd:,.2f}** |"
        )
        lines.append("")

        if zero_bal_wallets:
            grouped: dict[str, list[str]] = {}
            for entry in zero_bal_wallets:
                inst, ccy = entry.split(" ", 1)
                grouped.setdefault(inst, []).append(ccy)
            notes = []
            for inst in sorted(grouped):
                notes.append(f"{inst} ({', '.join(sorted(grouped[inst]))})")
            lines.append(f"  _Zero-balance accounts: {'; '.join(notes)}._")

        grand_total_sgd += type_total_sgd

    # --- Consolidated investment holdings ---
    inv_rows: list[tuple[str, Any]] = []
    for a in model.accounts:
        inst = _account_institution(a)
        for h in a.investment_holdings or []:
            inv_rows.append((inst, h))

    inv_total_sgd = 0.0
    if inv_rows:
        lines.append("### Investments")
        lines.append("")
        lines.append(
            "| Bank | Name | Units | Currency | Unit Price | Valuation | "+
            "Cost | Unrealised P/L | Valuation (SGD) |"
        )
        lines.append(
            "| --- | --- | ---: | --- | --- | --- | --- | --- | ---: |"
        )
        for inst, h in inv_rows:
            val_raw = _parse_money(h.valuation)
            val_sgd = ""
            if val_raw is not None and h.currency:
                rate = model.fx_rates.get(h.currency)
                if rate is not None:
                    val_sgd = f"{val_raw * rate:,.2f}"
                    inv_total_sgd += val_raw * rate
            lines.append(
                f"| {inst} | {h.name} | {h.units or '—'} | {h.currency or '—'} | "+
                f"{h.unit_price or '—'} | {h.valuation or '—'} | {h.cost or '—'} | "+
                f"{h.unrealised_pl or '—'} | {val_sgd or '—'} |"
            )
        lines.append(
            f"| | | | | | | | **Subtotal (SGD)** | **{inv_total_sgd:,.2f}** |"
        )
        lines.append("")

    # Grand total: two subtotals (accounts + investments), then combined.
    lines.append("### Grand Total")
    lines.append("")
    lines.append("| | SGD |")
    lines.append("| --- | ---: |")
    lines.append(f"| Accounts (cash & deposits) | {grand_total_sgd:,.2f} |")
    if inv_total_sgd:
        lines.append(f"| Investments | {inv_total_sgd:,.2f} |")
    lines.append(
        f"| **Total Net Position** | **{grand_total_sgd + inv_total_sgd:,.2f}** |"
    )
    lines.append("")

    return lines


def render(
    model: Any,
    helpers: types.ModuleType,
    common: types.ModuleType,
    do_mask: bool,
    fx_result: FXResult | None = None,
) -> str:
    mask_desc = helpers.md_masked_description
    mask_id = common.mask_id
    lines: list[str] = []

    # --- Header / meta (from model) ---
    lines.append("# Consolidated Bank Statement")
    lines.append("")
    lines.append(f"- **Statements consolidated**: {len(model.sources)}")
    if model.institutions:
        lines.append(f"- **Banks**: {', '.join(model.institutions)}")
    period = " → ".join(filter(None, [model.period_from or "", model.period_to or ""]))
    if period:
        lines.append(f"- **Period**: {period}")
    lines.append("")

    # --- Net Position (from model) ---
    lines.extend(_render_net_position(model, mask_id, do_mask, fx_result=fx_result))

    # --- Combined transactions: one section per account type, one table per currency ---
    def _render_ccy_section(tables: list[Any], heading: str) -> None:
        lines.append(f"## {heading} (by Currency)")
        lines.append("")
        for ct in tables:
            lines.append(f"### {ct.currency}")
            lines.append("")
            lines.append("| Date | Bank | Account | Description | Withdrawal | Deposit | Running Net |")
            lines.append("| --- | --- | --- | --- | ---: | ---: | ---: |")
            for r in ct.rows:
                acct = mask_id(r.account, do_mask=do_mask)
                desc = mask_desc(r.description, do_mask=do_mask) if do_mask else r.description
                wd = f"{r.withdrawal:,.2f}" if r.withdrawal is not None else ""
                dp = f"{r.deposit:,.2f}" if r.deposit is not None else ""
                # Running Net: running net (deposit - withdrawal) within the currency table.
                # The per-account balance_after is meaningless once rows from multiple
                # accounts are interleaved, so the consolidated view uses this instead.
                netd = _money(r.net_deposits)  # currency omitted; table is grouped by currency (### header)
                lines.append(
                    f"| {r.date} | {r.bank} | {acct} | {desc} | {wd} | {dp} | {netd} |"
                )
            lines.append(
                f"| | | | **Total** | | {ct.total_withdrawal:,.2f} | "+
                f"{ct.total_deposit:,.2f} | |"
            )
            lines.append("")

    covered: set[str] = set()
    for atype, heading in TXN_SECTION_ORDER:
        tables = model.txn_tables_by_type.get(atype)
        if tables:
            _render_ccy_section(tables, heading)
            covered.add(atype)
    # Fallback for any transaction-bearing type not in the canonical order list.
    for atype in sorted(model.txn_tables_by_type):
        if atype in covered:
            continue
        heading = f"{atype.replace('_', ' ').title()} Transactions"
        _render_ccy_section(model.txn_tables_by_type[atype], heading)

    # --- Per-bank drill-down ---
    by_inst: dict[str, list[Any]] = {}
    for a in model.accounts:
        inst = _account_institution(a)
        by_inst.setdefault(inst, []).append(a)

    for inst in sorted(by_inst):
        heading = inst if inst != "—" else "Other accounts"
        lines.append(f"## {heading}")
        lines.append("")
        for acc in by_inst[inst]:
            title = acc.name
            suffix = f"({acc.currency})"
            if acc.currency and not title.strip().endswith(suffix):
                title = f"{title} {suffix}"
            lines.append(f"### {title}")
            lines.append("")
            lines.append(f"- **Account**: {mask_id(acc.account_no, do_mask=do_mask)}")
            lines.append(f"- **Type**: {acc.account_type}")
            if acc.opening_balance is not None or acc.closing_balance is not None:
                lines.append(
                    f"- **Opening**: {_money(acc.opening_balance, acc.currency)}  "+
                    f"**Closing**: {_money(acc.closing_balance, acc.currency)}"
                )
            lines.append("")

            if acc.transactions:
                # Credit cards use a running "Running Net" (deposit - withdrawal),
                # matching the consolidated Credit Card Transactions section,
                # instead of the per-account balance_after.
                is_cc = acc.account_type == "credit_card"
                if is_cc:
                    lines.append("| Date | Description | Withdrawal | Deposit | Running Net |")
                else:
                    lines.append("| Date | Description | Withdrawal | Deposit | Balance |")
                lines.append("| --- | --- | ---: | ---: | ---: |")
                cc_running: dict[str, float] = {}
                for t in acc.transactions:
                    # Skip internal "PAYMENT BY INTERNET" lines (negative amount)
                    # unless they are linked to a credit card payment.
                    is_cc_payment = False
                    if t.amount < 0 and "PAYMENT BY INTERNET" in (t.description or "").upper():
                        if not t.is_internal_transfer:
                            continue
                        is_cc_payment = True
                    desc = mask_desc(t.description, do_mask=do_mask) if do_mask else t.description
                    if is_cc_payment:
                        desc += " 🔄"
                    if t.amount < 0:
                        wd, dp = _money(abs(t.amount), t.currency), ""
                        cc_running[t.currency] = cc_running.get(t.currency, 0.0) - abs(t.amount)
                    else:
                        wd, dp = "", _money(t.amount, t.currency)
                        cc_running[t.currency] = cc_running.get(t.currency, 0.0) + t.amount
                    if is_cc:
                        last = _money(cc_running.get(t.currency, 0.0), t.currency)
                        lines.append(
                            f"| {t.posted_date} | {desc} | {wd} | {dp} | {last} |"
                        )
                    else:
                        lines.append(
                            f"| {t.posted_date} | {desc} | {wd} | {dp} | "+
                            f"{_money(t.balance_after, t.currency)} |"
                        )
                lines.append("")

            if acc.fd_records:
                lines.append("#### Fixed Deposits")
                lines.append("")
                lines.append("| Deposit No | Value Date | Maturity | Rate | Principal | Interest |")
                lines.append("| --- | --- | --- | --- | ---: | ---: |")
                for r in acc.fd_records:
                    rate = (
                        r.raw_interest_rate
                        if r.raw_interest_rate
                        else (f"{r.interest_rate * 100:.3f}%" if r.interest_rate is not None else "—")
                    )
                    lines.append(
                        f"| {mask_id(r.deposit_no, do_mask=do_mask)} | "+
                        f"{r.value_date or '—'} | {r.maturity_date or '—'} | {rate} | "+
                        f"{_money(r.principal, r.currency)} | {_money(r.interest_amount, r.currency)} |"
                    )
                lines.append("")

            if acc.investment_holdings:
                lines.append("#### Investments")
                lines.append("")
                lines.append("| Name | Units | Currency | Unit Price | Valuation | Cost | Unrealised P/L |")
                lines.append("| --- | ---: | --- | --- | --- | --- | --- |")
                for h in acc.investment_holdings:
                    lines.append(
                        f"| {h.name} | {h.units or '—'} | {h.currency or '—'} | "+
                        f"{h.unit_price or '—'} | {h.valuation or '—'} | "+
                        f"{h.cost or '—'} | {h.unrealised_pl or '—'} |"
                    )
                lines.append("")

        lines.append("")

    # --- Warnings ---
    fx_warnings = list(getattr(fx_result, "warnings", [])) if fx_result else []
    if model.warnings or fx_warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in model.warnings:
            lines.append(f"- {w}")
        for w in fx_warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_Auto-generated by bank-ir-consolidate from consolidated IR ({model.ir_version})._")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a bank IR JSON to markdown.")
    _ = ap.add_argument("input", help="Input *.ir.json file")
    _ = ap.add_argument("-o", "--out", default="consolidated.md", help="Output markdown path")
    _ = ap.add_argument("--parser-dir", default=None, help="Path to sg-bank-pdf-parser skill dir")
    _ = ap.add_argument("--no-mask", action="store_true", help="Disable masking of IDs/names")
    # FX retrieval / caching options.
    _ = ap.add_argument(
        "--fx-date", default=None,
        help="FX as-of date (YYYY-MM-DD). Default: statement period_to, else today.",
    )
    _ = ap.add_argument(
        "--fx-provider", default="frankfurter",
        help="FX provider name (pluggable). Default: frankfurter.",
    )
    _ = ap.add_argument(
        "--fx-no-embed", action="store_true",
        help="Do not embed FX provenance into extras.consolidation.fx of the IR.",
    )
    _ = ap.add_argument(
        "--fx-embed-ir", default=None,
        help="Path to write the IR with embedded FX provenance. Default: the input IR path.",
    )
    args = ap.parse_args()

    text = Path(args.input).read_text(encoding="utf-8")
    stmt = pfa_ir_schema.from_json(text)

    # Resolve FX rates on demand: union of the default watch-list and every
    # non-SGD currency actually present in the statement's accounts.
    as_of = args.fx_date or (stmt.statement_meta.period_to if stmt.statement_meta else None)
    currencies = collect_currencies(stmt)
    fx = get_fx_rates(
        currencies,
        as_of=as_of,
        provider=get_provider(args.fx_provider),
    )

    import pfa_ir_schema.common as ir_common
    from pfa_parser.renderers import helpers as md_helpers

    md = render(
        build_render_model(stmt, fx_rates=fx.rates),
        md_helpers,
        ir_common,
        do_mask=not args.no_mask,
        fx_result=fx,
    )
    out = Path(args.out)
    _ = out.write_text(md, encoding="utf-8")
    print(f"Wrote {out} (FX: {fx.source}, as_of {fx.as_of}, {fx.provider})")

    # Embed FX provenance into the consolidated IR for full reproducibility.
    if not args.fx_no_embed:
        embed_path = Path(args.fx_embed_ir) if args.fx_embed_ir else Path(args.input)
        extras = dict(stmt.extras or {})
        cons = dict(extras.get("consolidation", {}) or {})
        cons["fx"] = {
            "provider": fx.provider,
            "source": fx.source,
            "base": "SGD",
            "requested_as_of": as_of,
            "as_of": fx.as_of,
            "fetched_at": fx.fetched_at,
            "rates_sgd_per_unit": fx.rates,
            "symbols_requested": currencies,
            "missing": fx.missing,
            "warnings": list(getattr(fx, "warnings", [])),
        }
        extras["consolidation"] = cons
        stmt.extras = extras
        _ = embed_path.write_text(stmt.to_json(indent=2), encoding="utf-8")
        print(f"Embedded FX provenance into {embed_path}")


if __name__ == "__main__":
    main()
