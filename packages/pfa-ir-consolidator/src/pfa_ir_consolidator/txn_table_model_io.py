"""txn_table_model_io.py — shared IO helpers for the TxnTableModel JSON interchange.

The ``bank-ir-consolidate`` skill and the standalone transaction-categorization
project exchange data as a JSON document produced by ``TxnTableModel.to_dict()``
(see ``txn_table_model.py``). These helpers keep load/save logic in one place so
the export script, the merge script, and the categorizer all agree on the wire
format.

* ``load_txn_table_model`` / ``save_txn_table_model`` — read/write the JSON export.
* ``embedded_fx_rates`` — pull the FX rates already embedded in a consolidated
  ``ParsedStatement`` (written by ``consolidate``) so re-projecting the model
  never has to hit the network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_txn_table_model(path: str | Path) -> dict[str, Any]:
    """Load a TxnTableModel JSON export into a plain dict."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_txn_table_model(
    model: dict[str, Any], path: str | Path, indent: int = 2
) -> None:
    """Write a TxnTableModel dict to *path* as pretty JSON."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    _ = out.write_text(
        json.dumps(model, indent=indent, ensure_ascii=False), encoding="utf-8"
    )


def embedded_fx_rates(ir: Any) -> dict[str, float] | None:
    """Return the SGD-per-unit FX rates embedded in a consolidated IR, if present.

    ``consolidate`` stores FX under ``extras.consolidation.fx.rates_sgd_per_unit``.
    When that block exists we can feed it straight back into
    ``build_txn_table_model`` without a network call.
    """
    extras = getattr(ir, "extras", None)
    if not isinstance(extras, dict):
        return None
    consolidation = extras.get("consolidation") or {}
    fx = consolidation.get("fx") or {}
    rates = fx.get("rates_sgd_per_unit")
    if isinstance(rates, dict) and rates:
        return dict(rates)
    return None
