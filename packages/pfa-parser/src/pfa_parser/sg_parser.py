"""Parser that integrates PDF extraction for SG bank PDF statements.

Uses auto-detection (detect_type) to pick the right extractor, then calls
to_ir() to produce the full ParsedStatement IR (serialisable to .ir.json
and consumable by the consolidation / analysis pipeline).
"""

from __future__ import annotations

from pathlib import Path
from typing import override

import pdfplumber
from pfa_ir_schema import ParsedStatement

from .base import BankStatementParser
from .convert_statement import detect_type
from .extractors.registry import get_extractor


class SGBankPDFParser(BankStatementParser):
    """Parse Singapore bank PDF statements."""

    @override
    def supports_format(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() == ".pdf"

    @override
    def parse(self, file_path: str) -> ParsedStatement:
        """Parse a PDF statement and return the full :class:`ParsedStatement` IR.

        The returned statement is the post-processed IR — suitable for
        serialising to ``.ir.json`` and feeding into the consolidation /
        analysis pipeline.
        """
        pdf_path = Path(file_path)

        # Step 1: Detect bank / statement family
        with pdfplumber.open(str(pdf_path)) as pdf:
            bank, family = detect_type(pdf)

        # Step 2: Get the matching extractor
        ExtractorCls = get_extractor(bank, family)
        if ExtractorCls is None:
            raise ValueError(
                f"No extractor found for bank={bank!r}, family={family!r}. "+
                f"File: {pdf_path.name}"
            )

        # Step 3: Produce the ParsedStatement IR (no flattening)
        return ExtractorCls().to_ir(pdf_path)
