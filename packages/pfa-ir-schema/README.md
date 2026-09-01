# pfa-ir-schema

Shared **IR (Intermediate Representation) schema** data models for the
`personal-finance-cli` monorepo.

This package defines the structured data types that all bank-statement
extractors produce and all downstream consumers read — independent of any
source PDF layout. It is the contract between `pfa-parser` (which extracts
data) and the consolidation and analysis packages (which
consume it). It has **no third-party dependencies** and is the leaf of the
dependency graph.

## What it provides

- `ParsedStatement` — the top-level IR object (`ir_version`, `statement_meta`,
  `accounts[]`, `warnings[]`).
- `Account`, `Transaction`, `StatementMeta`, `ParserInfo` — per-account and
  per-transaction records.
- `CreditCardSummary`, `FixedDepositRecord`, `InvestmentHolding`, `DebugInfo` —
  optional statement sections.
- `AccountType`, `VALID_ACCOUNT_TYPES` — account-type enumeration.
- `from_json` / `to_json` / `from_dict` / `generate_txn_id` — (de)serialization
  helpers and stable transaction-id generation.
- `common` — masking & sanitization utilities shared by renderers:
  `mask_id`, `mask_name`, `mask_chinese_name`, `mask_names_in_description`,
  `sanitize_description`, `is_bank_num`, `parse_fd_rate`,
  `format_fd_period`.

  > FD-interest *verification* (`verify_fd_interest`) lives in the
  > `pfa-ir-verifier` package, not here — see that package's docs.

## Install

```bash
pip install -e packages/pfa-ir-schema
```

Requires Python >= 3.12. No runtime dependencies.

## Usage

```python
from pfa_ir_schema import ParsedStatement, from_json, to_json

# Load a previously written IR JSON file
ir: ParsedStatement = from_json(open("statement.ir.json", encoding="utf-8").read())

print(ir.statement_meta.bank, ir.statement_meta.family)
print(ir.accounts[0].account_type)
print(len(ir.accounts[0].transactions), "transactions")

# Re-serialize (e.g. after downstream edits)
json_str: str = to_json(ir)
```

### Masking helpers (used at render time by `pfa-parser`)

```python
from pfa_ir_schema import mask_id, mask_names_in_description

mask_id("123-456-789-0")                 # -> "***-***-***-0"
mask_names_in_description("PAYNOW TO TAN WEI MING 1234")
```

Masking is applied in the **renderer**, not in the IR — the IR always stores
the unmasked raw data so downstream consumers can choose whether to mask.

## Package layout

```
pfa-ir-schema/
├── pyproject.toml          # hatchling build, no runtime deps
└── src/
    └── pfa_ir_schema/
        ├── __init__.py     # public API re-exports
        ├── account_type.py # AccountType enum
        ├── ir_schema.py    # ParsedStatement / Account / Transaction / ... + JSON (de)serialization
        └── common.py       # masking, sanitization, validation
```

## License

MIT © 2026 Zhou Shaowen
