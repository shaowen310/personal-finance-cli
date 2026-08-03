"""Registry mapping (bank, family) → Extractor class and Renderer function.

``convert_statement.py`` uses this to dispatch IR extraction and Markdown
rendering without a manual if/elif chain.
"""

from __future__ import annotations

from .dbs_extractor import DBSExtractor
from .base import BaseExtractor
from .icbc_extractor import ICBCExtractor
from .ocbc_extractor import OCBCConsolidatedExtractor, OCRCCardExtractor
from .uob_extractor import UOBOneExtractor, UOBPortfolioExtractor, UOBTxnExtractor

# Lazily populated on first access to avoid circular imports.
_extractor_registry: dict[tuple[str, str], type[BaseExtractor]] | None = None


def _build_registry() -> dict[tuple[str, str], type[BaseExtractor]]:
    """Build the extractor registry (imports extractors on first call)."""
    return {
        ("dbs", "consolidated"): DBSExtractor,
        ("ocbc", "consolidated"):  OCBCConsolidatedExtractor,
        ("ocbc", "card"):        OCRCCardExtractor,
        ("icbc", "consolidated"):   ICBCExtractor,
        ("uob", "txn"):          UOBTxnExtractor,
        ("uob", "one"):          UOBOneExtractor,
        ("uob", "portfolio"):    UOBPortfolioExtractor,
    }


def get_extractor(bank: str, family: str) -> type[BaseExtractor] | None:
    """Return the extractor class for ``(bank, family)``, or None."""
    global _extractor_registry
    if _extractor_registry is None:
        _extractor_registry = _build_registry()
    return _extractor_registry.get((bank, family))
