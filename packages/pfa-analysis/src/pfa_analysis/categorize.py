"""Transaction categorization from a consolidated bank-statement IR JSON.

Classifies transactions using rule-first matching (from a YAML rules file)
with optional LLM fallback for uncategorized items.  Detects external
transfers to prevent double-counting as spend or income.

CLI (via the package entry point):
    python -m pfa_analysis categorize consolidated.ir.json -o categories.json \\
        [--rules rules.yaml] [--llm] [--model gpt-4o-mini]
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from pfa_analysis.txn_ir import AccountDict, IrMeta, parse_ir
from pfa_analysis.types import TxnRow

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# Default subcategory used when a transaction cannot be classified. Both the
# categorization step (categorize.py) and the analysis/drilldown step
# (analyze.py) reference these so the "uncategorized" label stays consistent
# across the pipeline.
UNCATEGORIZED = "Uncategorized"
EXPENSE_UNCATEGORIZED = f"Expense: {UNCATEGORIZED}"
INCOME_UNCATEGORIZED = f"Income: {UNCATEGORIZED}"




# ---------------------------------------------------------------------------
# Rules-file / LLM payload types
# ---------------------------------------------------------------------------


class RuleDict(TypedDict):
    """One categorization rule: a target ``category`` plus ``match`` keywords."""

    category: str
    match: list[str]


class CategoriesConfig(TypedDict):
    """Parsed structure of a categories.yaml rules file."""

    categories: list[str]
    rules: list[RuleDict]


class LlmBatchItem(TypedDict):
    """A single transaction serialized for the LLM classification prompt."""

    txn_id: str
    description: str
    amount: float


# Account types where positive amounts represent debits (outflows).
_CREDIT_LIKE: frozenset[str] = frozenset({"credit_card", "credit", "card"})


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_input(path: Path) -> tuple[list[TxnRow], list[AccountDict], IrMeta]:
    """Parse a bank-ir-consolidate IR JSON export.

    Returns ``(txns, accounts_raw, meta)`` where **txns** is a flat list of
    all :class:`TxnRow` from every account type, **accounts_raw** is the raw
    ``accounts`` array, and **meta** holds ``ir_version``, ``institutions``,
    ``period_from``, ``period_to``.
    """
    ir_data = parse_ir(path)

    # Build account_no → account_type lookup from raw accounts.
    acct_types: dict[str, str] = {
        a["account_no"]: a.get("account_type", "")
        for a in ir_data.accounts_raw
    }

    txns = [
        TxnRow(
            txn_id=row["txn_id"],
            date=row["date"],
            bank=row["bank"],
            account=row["account"],
            account_type=acct_types.get(row["account"], ""),
            description=row["description"],
            amount=row["amount"],
            balance_after=row.get("balance_after"),
            currency=row["currency"],
            category_hint=row.get("category_hint"),
            tags=row.get("tags"),
            is_internal_transfer=row.get("is_internal_transfer", False),
        )
        for row in ir_data.txns_raw
    ]

    return txns, ir_data.accounts_raw, ir_data.meta


# ---------------------------------------------------------------------------
# Rules loading (YAML)
# ---------------------------------------------------------------------------

DEFAULT_RULES_PATH = (
    Path(__file__).resolve().parent.parent / "references" / "categories.yaml"
)


def load_rules(path: Path) -> CategoriesConfig:
    """Load categorization rules from a YAML file.

    Expected structure::

        categories: [Income, Transfer, Groceries, ...]
        rules:
          - category: Income
            match: [SALARY, PAYROLL, ...]
          - category: Groceries
            match: [NTUC, FAIRPRICE, ...]

    Returns the parsed dict.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        print(
            "ERROR: PyYAML is required.  Install with:  pip install pyyaml",
            file=sys.stderr,
        )
        sys.exit(1)

    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Rule-based classifier
# ---------------------------------------------------------------------------


def _normalize_desc(desc: str) -> str:
    """Replace punctuation with spaces & uppercase for case/punctuation-insensitive matching."""
    return re.sub(r"[^\w\s]", " ", desc).upper().strip()


def classify_by_rules(txn: TxnRow, rules: list[RuleDict]) -> str | None:
    """Classify a single transaction using the ordered rules list.

    Each rule has ``category`` and ``match`` (list of keyword strings).
    Matching is case- and punctuation-insensitive.  First match wins.
    Returns the category name, or ``None`` if no rule matched.
    """
    nd = _normalize_desc(txn["description"])
    for rule in rules:
        category: str = rule["category"]
        keywords: list[str] = rule.get("match", [])
        for kw in keywords:
            nk = _normalize_desc(kw)
            if nk in nd:
                return category
    return None


# ---------------------------------------------------------------------------
# External transfer detection
# ---------------------------------------------------------------------------

# Regex to extract account-number-like tokens (10-20 digit sequences).
_ACCT_NO_RE = re.compile(r"\b\d{10,20}\b")

# Keywords that strongly suggest a transfer in the description.
_TRANSFER_KW = {"TRANSFER", "TRF", "GIRO", "FAST", "MEPS", "IBG", "IBFT"}

# Map IR category_hint values to user-defined categories.
# Parsers tag transactions with a hint (e.g. "fixed_deposit"); this maps
# those hints to the actual category names used in categories.yaml.
_HINT_CATEGORY_MAP: dict[str, str] = {
    "salary": "Income: Salary",
    "dividend": "Income: Dividends",
    "interest": "Income: Interest",
    "fd_interest": "Income: Interest",
    "investment": "Income: Investment",
    "groceries": "Expense: Groceries",
}


# ---------------------------------------------------------------------------
# Two-level category utilities
# ---------------------------------------------------------------------------


def _split_cat(cat: str) -> tuple[str, str]:
    """Split a ``"Class: Subtype"`` category string into ``(cls, sub)``.

    Strings without a ``":"`` separator are treated as having no class
    (``""``, whole string as ``sub``) for backward compatibility with old
    flat ``categories.json`` files.
    """
    parts = cat.split(": ", 1)
    if len(parts) == 2:
        return (parts[0], parts[1])
    return ("", cat)


def build_institution_map(txns: list[TxnRow]) -> dict[str, str]:
    """Build ``account_no -> institution`` from transaction data.

    Every TxnRow carries both ``bank`` (the institution) and ``account``
    (the raw account_no).  We collect the first observed mapping.
    """
    imap: dict[str, str] = {}
    for txn in txns:
        acct = txn["account"].strip()
        bank = txn.get("bank", "").strip()
        if acct and bank and acct not in imap:
            imap[acct] = bank
    return imap


def detect_interbank_transfers(
    txns: list[TxnRow],
    account_to_institution: dict[str, str],
) -> dict[str, str]:
    """Detect external transfers and return ``{txn_id: transfer_category}``.

    Logic
    -----
    1. For each **withdrawal**, scan ``description`` for a token matching an
       ``account_no`` whose institution ≠ the row's own ``bank``
       → ``"Transfer: External"``.
    2. For each **deposit**, if the description contains a transfer keyword
       **and** references a source account_no belonging to a different
       institution → ``"Transfer: External"``.
    3. For remaining deposits, try to pair with a detected outgoing transfer
       by matching (amount, destination account_no, within 3 days).

    Transfer categories produced by this function should **override** regular
    rule-based categories.
    """
    result: dict[str, str] = {}
    outgoing_pool: list[tuple[TxnRow, str]] = []  # (txn, destination_account_no)

    # ---- Step 1: outgoing transfers ----------------------------------------
    for txn in txns:
        if txn["amount"] >= 0:
            continue

        src_inst = account_to_institution.get(txn["account"], "")
        desc_tokens = set(_ACCT_NO_RE.findall(txn["description"]))

        for token in desc_tokens:
            dest_inst = account_to_institution.get(token, "")
            if dest_inst and dest_inst != src_inst:
                result[txn["txn_id"]] = "Transfer: External"
                outgoing_pool.append((txn, token))
                break

    # ---- Step 2: incoming transfers (keyword-based) ------------------------
    for txn in txns:
        if txn["txn_id"] in result:
            continue
        if txn["amount"] <= 0:
            continue

        nd = _normalize_desc(txn["description"])
        has_transfer_kw = any(kw in nd for kw in _TRANSFER_KW)

        if not has_transfer_kw:
            continue

        dest_inst = account_to_institution.get(txn["account"], "")
        desc_tokens = set(_ACCT_NO_RE.findall(txn["description"]))

        for token in desc_tokens:
            src_inst = account_to_institution.get(token, "")
            if src_inst and src_inst != dest_inst:
                result[txn["txn_id"]] = "Transfer: External"
                break

    # ---- Step 3: pair remaining deposits to known outgoing transfers -------
    for txn in txns:
        if txn["txn_id"] in result:
            continue
        if txn["amount"] <= 0:
            continue

        txn_date = datetime.strptime(txn["date"], "%Y-%m-%d")  # noqa: DTZ007 -- date-only arithmetic, tz irrelevant

        for out_txn, dest_acct in outgoing_pool:
            if dest_acct != txn["account"]:
                continue
            out_date = datetime.strptime(out_txn["date"], "%Y-%m-%d")  # noqa: DTZ007 -- date-only arithmetic, tz irrelevant
            delta = abs((txn_date - out_date).days)

            if (
                delta <= 3
                and out_txn["amount"] < 0
                and abs(txn["amount"] - abs(out_txn["amount"])) < 0.01
            ):
                result[txn["txn_id"]] = "Transfer: External"
                break

    return result





# ---------------------------------------------------------------------------
# LLM fallback classification
# ---------------------------------------------------------------------------


def llm_classify(
    uncategorized: list[TxnRow],
    known_categories: list[str],
    *,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, str]:
    """Batch-classify uncategorized transactions via an OpenAI-compatible API.

    Sends ``{txn_id, description, amount}`` as a batch and
    requires the model to return ``{txn_id: category}`` using *only* the
    supplied ``known_categories``.  Extra / invented txn_ids are silently
    dropped.
    """
    try:
        import requests
    except ImportError:
        print(
            "ERROR: 'requests' is required for --llm.  "
            + "Install with:  pip install requests",
            file=sys.stderr,
        )
        sys.exit(1)

    if not uncategorized:
        return {}

    api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print(
            "ERROR: OPENAI_API_KEY environment variable not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
    if not base_url:
        base_url = "https://api.openai.com/v1"

    # ---- build batch payload -----------------------------------------------
    batch_items: list[LlmBatchItem] = []
    for txn in uncategorized:
        item: LlmBatchItem = {
            "txn_id": txn["txn_id"],
            "description": txn["description"],
            "amount": txn["amount"],
        }
        batch_items.append(item)

    categories_str = ", ".join(known_categories)
    input_ids = {t["txn_id"] for t in uncategorized}

    prompt = (
        f"Classify each transaction into exactly one of these categories:\n"
        f"{categories_str}\n\n"
        f"Rules:\n"
        f"- Only use the exact category names listed above.\n"
        f"- Never invent new categories or transaction IDs.\n"
        f"- Ignore any transaction ID not in the input.\n"
        f"- Return ONLY a JSON object mapping txn_id to category, nothing else.\n\n"
        f"Transactions:\n{json.dumps(batch_items, indent=2, ensure_ascii=False)}"
    )

    # ---- call API ----------------------------------------------------------
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a precise transaction categorizer. "
                            "Return ONLY valid JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.0,
            },
            timeout=120,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 -- any LLM/HTTP failure degrades to rules-only categorization
        print(f"ERROR: LLM API call failed: {exc}", file=sys.stderr)
        return {}

    data = response.json()
    content: str = data["choices"][0]["message"]["content"]

    # ---- parse JSON response -----------------------------------------------
    content = content.strip()
    # Strip optional markdown code fences
    if content.startswith("```"):
        content = re.sub(r"^```\w*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)

    try:
        raw_result: dict[str, str] = json.loads(content)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: Failed to parse LLM response as JSON: {exc}",
            file=sys.stderr,
        )
        print(f"Raw response (first 500 chars):\n{content[:500]}", file=sys.stderr)
        return {}

    # ---- validate & filter -------------------------------------------------
    filtered: dict[str, str] = {}
    for tid, cat in raw_result.items():
        if tid not in input_ids:
            continue  # ignore invented txn_ids
        if cat not in known_categories:
            continue  # ignore unknown categories
        filtered[tid] = cat

    return filtered


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_coverage(txns: list[TxnRow], result: dict[str, str]) -> list[str]:
    """Check every input ``txn_id`` appears *exactly once* in the output.

    Returns a list of issue strings (empty = full coverage).
    """
    input_ids = {t["txn_id"] for t in txns}
    output_ids = set(result.keys())

    missing = input_ids - output_ids
    extra = output_ids - input_ids

    issues: list[str] = []
    for tid in sorted(missing):
        issues.append(f"Missing in output: {tid}")
    for tid in sorted(extra):
        issues.append(f"Extra in output (not in input): {tid}")

    return issues


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_categories(result: dict[str, str], path: Path) -> None:
    """Write ``{txn_id: category}`` as a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def print_summary(txns: list[TxnRow], result: dict[str, str], *, default_category: str = "Uncategorized") -> None:
    """Print a human-readable categorization summary to stdout."""
    cat_counts: dict[str, int] = defaultdict(int)
    for cat in result.values():
        cat_counts[cat] += 1

    # Separate transfers from regular categories for cleaner display
    transfer_cats = {
        k: v for k, v in cat_counts.items() if k.startswith("Transfer:")
    }
    regular_cats = {
        k: v for k, v in cat_counts.items() if not k.startswith("Transfer:")
    }

    n_total = len(txns)
    n_un = cat_counts.get(INCOME_UNCATEGORIZED, 0) + cat_counts.get(EXPENSE_UNCATEGORIZED, 0)
    n_categorized = n_total - n_un

    print()
    print("=" * 42)
    print("  Categorization Summary")
    print("=" * 42)

    if regular_cats:
        print("\nCategories:")
        for cat in sorted(regular_cats, key=lambda c: -regular_cats[c]):
            print(f"  {cat:30s} {regular_cats[cat]:>5d}")

    n_external = transfer_cats.get("Transfer: External", 0)
    if n_external:
        txn_map = {t["txn_id"]: t for t in txns}
        out_total = sum(
            abs(txn_map[tid]["amount"])
            for tid, cat in result.items()
            if cat == "Transfer: External" and tid in txn_map and txn_map[tid]["amount"] < 0
        )
        in_total = sum(
            txn_map[tid]["amount"]
            for tid, cat in result.items()
            if cat == "Transfer: External" and tid in txn_map and txn_map[tid]["amount"] > 0
        )
        print(f"\nExternal transfers: {n_external}")
        if out_total:
            print(f"  Total outgoing:   SGD {out_total:,.2f}")
        if in_total:
            print(f"  Total incoming:   SGD {in_total:,.2f}")

    # ---- Class-level subtotals ---------------------------------------------
    cls_counts: dict[str, int] = defaultdict(int)
    for cat in result.values():
        cls, _ = _split_cat(cat)
        if cls:
            cls_counts[cls] += 1
    if cls_counts:
        print("\nBy class:")
        for cls_name in sorted(cls_counts, key=lambda c: -cls_counts[c]):
            print(f"  {cls_name:30s} {cls_counts[cls_name]:>5d}")

    print("\n---")
    if n_un:
        print(f"  {f'{UNCATEGORIZED} (fallback):':30s} {n_un:>5d}")
    coverage = n_categorized / n_total * 100 if n_total else 0
    print(f"  {'Coverage:':30s} {coverage:>5.1f}%")
    print(f"  {'Total transactions:':30s} {n_total:>5d}")
    print()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def categorize(
    input_path: Path,
    rules_path: Path,
    *,
    use_llm: bool = False,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, str]:
    """Run the full categorization pipeline.

    Returns ``{txn_id: category}`` covering every input transaction.
    """
    # 1. Parse input ---------------------------------------------------------
    txns, _accounts_raw, _meta = parse_input(input_path)

    if not txns:
        print("WARNING: No transactions found in input.", file=sys.stderr)
        return {}

    # 2. Build institution lookup -------------------------------------------
    institution_map = build_institution_map(txns)

    # 3. Load rules ----------------------------------------------------------
    rules_data = load_rules(rules_path)
    rules: list[RuleDict] = rules_data["rules"]
    known_categories: list[str] = rules_data["categories"]

    # 4. Internal transfer pre-classification --------------------------------
    result: dict[str, str] = {}
    for txn in txns:
        if txn["is_internal_transfer"]:
            result[txn["txn_id"]] = "Transfer: Internal"

    # 5. Rule-based classification -------------------------------------------
    for txn in txns:
        if txn["txn_id"] in result:
            continue
        cat = classify_by_rules(txn, rules)
        if cat:
            result[txn["txn_id"]] = cat

    # 6. External transfer detection (overrides rule-based, not internal) ----
    transfer_cats = detect_interbank_transfers(txns, institution_map)
    for tid, cat in transfer_cats.items():
        if tid not in result:  # never override pre-classified internal transfers
            result[tid] = cat

    # 7. LLM fallback --------------------------------------------------------
    uncategorized = [t for t in txns if t["txn_id"] not in result]
    if use_llm and uncategorized:
        print(
            f"\n{len(uncategorized)} transaction(s) uncategorized after rules.  "
            + f"Calling LLM ({model}) ..."
        )
        llm_results = llm_classify(
            uncategorized,
            known_categories,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
        result.update(llm_results)
        still_un = len([t for t in txns if t["txn_id"] not in result])
        print(
            f"  LLM categorized {len(llm_results)} more; "
            + f"{still_un} still uncategorized."
        )

    # 8. Fill remaining — income/expense-aware fallback -----------------------
    for txn in txns:
        if txn["txn_id"] not in result:
            # Try category_hint as a last-resort classification
            hint = txn.get("category_hint")
            mapped = _HINT_CATEGORY_MAP.get(hint) if hint else None
            # Also check classification tags for hint matches
            tags = txn.get("tags", [])
            if not mapped and tags:
                for tag in tags:
                    mapped = _HINT_CATEGORY_MAP.get(tag)
                    if mapped:
                        break
            if mapped and mapped in known_categories:
                result[txn["txn_id"]] = mapped
                continue

            # Determine income vs expense from amount sign and account type.
            at = txn["account_type"].lower()
            is_debit = (txn["amount"] > 0) if at in _CREDIT_LIKE else (txn["amount"] < 0)
            if is_debit:
                result[txn["txn_id"]] = EXPENSE_UNCATEGORIZED
            else:
                result[txn["txn_id"]] = INCOME_UNCATEGORIZED

    return result


# ---------------------------------------------------------------------------

