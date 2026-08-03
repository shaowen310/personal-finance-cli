"""Abstract base class for bank-statement extractors.

Every supported bank/family pair should subclass ``BaseExtractor`` and
implement ``supports()`` and ``to_ir()``.

``EXTRACTOR_REGISTRY`` maps ``(bank, family)`` tuples (as returned by
``detect_type()``) to extractor class references so that
``convert_statement.py`` can auto-route without manual dispatch.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

import pdfplumber

from pfa_ir_schema import ParsedStatement
from pfa_ir_schema.common import PDF
from ..ir_builder import IRBuilder


# Type aliases
BankFamily = tuple[str, str]  # (bank, family) e.g. ("dbs", "consolidated")


class BaseExtractor(ABC):
    """Abstract base for all bank-statement-to-IR extractors."""

    # Extractor metadata — override in subclasses.
    parser_name: ClassVar[str] = "unknown"
    parser_version: ClassVar[str] = "1.0"

    @classmethod
    @abstractmethod
    def supports(cls, pdf_path: Path) -> bool:
        """Return True if this extractor can handle the given PDF.

        Typically delegates to ``detect_type()`` and compares against the
        extractor's expected (bank, family) tuple.
        """
        ...

    @abstractmethod
    def to_ir(self, pdf_path: Path) -> ParsedStatement:
        """Parse the PDF and return a structured ``ParsedStatement``."""
        ...

    @classmethod
    @abstractmethod
    def bank_name(cls) -> str:
        """Human-readable bank name, e.g. "DBS/POSB"."""
        ...

    def _create_builder(self) -> IRBuilder:
        """Convenience: create an IRBuilder pre-filled with this parser's info."""
        return IRBuilder(self.parser_name, self.parser_version)

    @staticmethod
    def _open_pdf(pdf_path: Path) -> PDF:
        """Open a PDF with pdfplumber. Raises FileNotFoundError if not found."""
        return pdfplumber.open(str(pdf_path))  # pyright: ignore[reportReturnType]
