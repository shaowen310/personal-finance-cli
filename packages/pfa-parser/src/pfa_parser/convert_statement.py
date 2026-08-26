#!/usr/bin/env python3
"""Convert a Singapore bank statement (PDF or IR JSON) to Markdown.

**Pipeline:**
  PDF:    ``detect → extractor.to_ir() → postprocess → write IR JSON → renderer(ir) → write MD``
  IR JSON: ``load IR JSON → postprocess → renderer(ir) → write MD``

For PDF input, the script auto-detects the source bank and statement family,
then dispatches to the appropriate extractor and IR→MD renderer. Masking
(account numbers via ``mask_id`` + descriptions via ``sanitize_description`` +
``mask_names_in_description``) is applied at **render time**, so the IR stores
unmasked raw data.

For IR JSON input (``.json``), the script skips PDF extraction and directly
renders the IR to Markdown using the same renderers.

If ``--ir-only`` is passed, the script does **not** render Markdown:
- For PDF input: only writes the IR JSON, skips Markdown.
- For IR JSON input: loads & validates the JSON, then exits (no output written).

Usage:
  python convert_statement.py <input.pdf|input.ir.json> [output.md] [--no-mask] [--ir-only]

If output.md is omitted, it is derived from the input filename in the same
directory. All masking is enabled by default (account numbers, NRIC, person
names in transaction descriptions); pass ``--no-mask`` to disable all masking.
"""
import re
import sys
from pathlib import Path

import pdfplumber
from pdfplumber.pdf import PDF as PDFType

from pfa_ir_schema import from_json as ir_from_json, ParsedStatement

from .postprocess import postprocess_statement

from .renderers.markdown import MD_RENDERER_REGISTRY


def get_renderer(bank: str, family: str):
    """Return the IR→MD renderer function for ``(bank, family)``, or None."""
    return MD_RENDERER_REGISTRY.get((bank, family))

# ----------------------------------------------------------------------------
# Parser name → (bank, family) mapping
# Maps the ``parser.name`` stored in IR JSON to the renderer registry key.
# ----------------------------------------------------------------------------

PARSER_NAME_TO_BANK_FAMILY: dict[str, tuple[str, str]] = {
    "dbs_sg": ("dbs", "consolidated"),
    "dbs_txn_2013": ("dbs", "txn_2013"),
    "icbc_sg": ("icbc", "consolidated"),
    "ocbc_consolidated": ("ocbc", "consolidated"),
    "ocbc_card": ("ocbc", "card"),
    "uob_txn": ("uob", "txn"),
    "uob_one": ("uob", "one"),
    "uob_portfolio": ("uob", "portfolio"),
}


# ----------------------------------------------------------------------------
# Auto-detection (PDF only)
# ----------------------------------------------------------------------------

def detect_type(pdf: PDFType) -> tuple[str, str]:
    """Return a (bank, family) tuple.

    ``bank`` is one of: "dbs", "uob", "icbc", "ocbc".

    The family value is bank-scoped -- the same label may recur across banks
    (e.g. "consolidated" is used by both DBS and OCBC), so the full
    (bank, family) key is what selects an extractor/renderer:

      dbs:    "consolidated"                  (DBS/POSB consolidated statement)
      icbc:   "consolidated"                  (ICBC bilingual multi-currency statement)
      ocbc:   "consolidated" | "card"         (OCBC consolidated | OCBC credit card)
      uob:    "txn" | "one" | "portfolio"
              (UOB single-account txn | UOB One multi-account | UOB portfolio summary)
    """
    full_text: str = ""
    for page in pdf.pages:
        full_text += "\n" + (page.extract_text() or "")

    # UOB detection — uobgroup.com email in the "Contact Us" card.
    uob = detect_uob(pdf)
    if uob is not None:
        return uob

    # ICBC detection
    if "Statement Date 结单日期" in full_text:
        return ("icbc", "consolidated")

    # DBS detection — rotated "DBS … POSB" banner down the left margin of page 0.
    dbs = detect_dbs(pdf)
    if dbs is not None:
        return dbs

    # OCBC detection — "OCBC Bank" wordmark in the upper-right of page 1.
    ocbc = detect_ocbc(pdf)
    if ocbc is not None:
        return ocbc

    return ("unknown", "unknown")


def detect_dbs(pdf: PDFType) -> tuple[str, str] | None:
    """Return ``("dbs", family)`` if the PDF is a DBS/POSB statement.

    Two distinct DBS layouts are recognised:

    * ``txn_2013`` — older single-account savings statements (e.g. the
      DBS REMIX eSAVINGS PLUS format) that print a non-rotated ``DBS Bank
      Ltd`` letterhead, an ``As at <date>`` period line, and a transaction
      table with separate WITHDRAWAL / DEPOSIT / BALANCE columns. These have
      no rotated left-margin banner.
    * ``consolidated`` — the current consolidated DBS/POSB statement, which
      prints a vertical (90°-rotated) banner down the left edge of every page
      (the ``DBS Bank Ltd … POSB …`` strip). Because the text is rotated,
      pdfplumber yields each word character-reversed (e.g. ``DBS`` -> ``SBD``,
      ``POSB`` -> ``BSOP``).
    """
    full_text = ""
    for page in pdf.pages:
        full_text += "\n" + (page.extract_text() or "")

    # Legacy single-account savings statement (no rotated banner).
    legacy_markers = ("DBS Bank Ltd", "REMIX", "eSAVINGS", "As at")
    if all(marker in full_text for marker in ("DBS Bank Ltd", "As at")) and any(
        marker in full_text for marker in ("REMIX", "eSAVINGS")
    ):
        return ("dbs", "txn_2013")

    # Current consolidated statement — rotated left-margin banner.
    page = pdf.pages[0]
    left_words = {w["text"] for w in page.extract_words() if w["x0"] < 25}
    if "SBD" in left_words and "BSOP" in left_words:
        return ("dbs", "consolidated")
    return None


def detect_ocbc(pdf: PDFType) -> tuple[str, str] | None:
    """Return ``("ocbc", family)`` if page 1 carries the OCBC Bank wordmark in
    its upper-right corner, otherwise ``None``.

    The wordmark (``OCBC Bank`` beside the Chulia Street address) sits in the
    top-right quadrant of page 1 for both OCBC bank and credit-card statements,
    and no other supported bank prints ``OCBC Bank`` there — so this is a
    precise, bank-level signal that pre-empts the weaker table-header heuristics.

    Family is then decided from page-1 content:
      * ``card`` — credit-card statements expose ``PAYMENT DUE`` /
        ``CREDIT LIMIT`` on page 1 (e.g. the payment-due-date and
        credit-limit summary block).
      * ``consolidated`` — everything else (consolidated / savings account statements).
    """
    page = pdf.pages[0]
    w, h = page.width, page.height

    # Upper-right region: right half, top 15% of page height.
    region = page.crop((0.5 * w, 0, w, 0.15 * h))
    region_text = (region.extract_text() or "").lower()
    if "ocbc bank" not in region_text:
        return None

    full_page = (page.extract_text() or "").lower()
    if "payment due" in full_page and "credit limit" in full_page:
        return ("ocbc", "card")
    return ("ocbc", "consolidated")


def detect_uob(pdf: PDFType) -> tuple[str, str] | None:
    """Return ``("uob", family)`` if the statement is a UOB statement, else ``None``.

    UOB prints a "Contact Us" card (typically on the last page) that includes a
    UOB Group email address whose domain is always ``uobgroup.com``. No other
    supported bank emits that domain, so it is a precise, bank-level signal. We
    match any email of the form ``<local>@uobgroup.com`` (case-insensitive)
    across the whole document.

    Family is then decided from the document content:
      * ``portfolio`` — statements with no ``Account Transaction Details`` block
        (a portfolio summary rather than an account transaction listing).
      * ``one`` — multi-account statements that head each transaction section
        with the ``One Account`` label.
      * ``txn`` — everything else (single-account transaction-style statements).
    """
    full_text = ""
    for page in pdf.pages:
        full_text += "\n" + (page.extract_text() or "")

    if re.search(r"[A-Za-z0-9._%+-]+@uobgroup\.com", full_text, re.I) is None:
        return None

    # Transaction-style statement vs. portfolio summary.
    if "Account Transaction Details" not in full_text:
        return ("uob", "portfolio")
    # UOB One: account section is headed by a "One Account" label.
    if _uob_has_one_account(full_text):
        return ("uob", "one")
    return ("uob", "txn")


def _uob_has_one_account(full_text: str) -> bool:
    """Return True if the statement is a UOB One (multi-account) statement.

    A UOB One statement heads each ``Account Transaction Details`` section with
    the account label ``One Account`` immediately before the dashed account
    number (e.g. ``One Account 123-456-789-0``). Single-account transaction
    statements use a different product label (e.g. ``UOB Stash Account``) and
    never print ``One Account``, so this keyword reliably distinguishes the two
    transaction-style families.
    """
    lines = full_text.splitlines()
    acc_re = re.compile(r"\b\d{3}-\d{3}-\d{3}-\d{1,3}\b")
    for i, line in enumerate(lines):
        if "One Account" not in line:
            continue
        # Account number on the same line, or within the next 2 lines.
        for j in range(i, min(i + 3, len(lines))):
            if acc_re.search(lines[j]):
                return True
    return False


# ----------------------------------------------------------------------------
# Shared render helper (used by both PDF and IR JSON paths)
# ----------------------------------------------------------------------------

def render_ir_to_md(ir: ParsedStatement, out_path: Path, *, do_mask: bool = True) -> str:
    """Render a ``ParsedStatement`` to Markdown and write to *out_path*.

    Returns the Markdown string (also written to disk).
    """
    bank, family = PARSER_NAME_TO_BANK_FAMILY.get(ir.parser.name, ("unknown", "unknown"))
    if bank == "unknown":
        print(f"Error: No renderer found for parser '{ir.parser.name}' — " +
              f"unknown parser name. Supported names: {', '.join(sorted(PARSER_NAME_TO_BANK_FAMILY))}")
        sys.exit(1)

    renderer = get_renderer(bank, family)
    if renderer is None:
        print(f"Error: No renderer registered for ({bank}, {family})")
        sys.exit(1)

    md = renderer(ir, do_mask=do_mask)
    _ = out_path.write_text(md, encoding="utf-8")
    print(f"Wrote: {out_path}")
    return md


# ----------------------------------------------------------------------------
# Main entry — unified IR→MD pipeline
# ----------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python convert_statement.py <input.pdf|input.ir.json> [output.md] [--no-mask] [--ir-only]")
        sys.exit(1)

    in_path = Path(sys.argv[1])
    if not in_path.exists():
        print(f"Input not found: {in_path}")
        sys.exit(1)

    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else in_path.with_suffix(".md")
    do_mask = "--no-mask" not in sys.argv
    ir_only = "--ir-only" in sys.argv

    # ------------------------------------------------------------------
    # Branch: IR JSON input → load & render directly
    # ------------------------------------------------------------------
    if in_path.suffix.lower() == ".json":
        json_str = in_path.read_text(encoding="utf-8")
        ir = ir_from_json(json_str)
        ir = postprocess_statement(ir)
        print(f"Loaded IR: {in_path}  ({sum(len(a.transactions) for a in ir.accounts)} txns, parser: {ir.parser.name})")
        if ir_only:
            return  # validate only, do nothing
        _ = render_ir_to_md(ir, out_path, do_mask=do_mask)
        return

    # ------------------------------------------------------------------
    # Branch: PDF input → detect → extract → write IR → render MD
    # ------------------------------------------------------------------
    with pdfplumber.open(str(in_path)) as pdf:
        bank, family = detect_type(pdf)

    if bank == "unknown":
        print("Error: This bank statement type is not supported yet.")
        print("The script could not identify the statement as DBS, UOB, ICBC, or OCBC.")
        print("Please update the skill with detection rules for this statement format.")
        sys.exit(1)

    from .extractors.registry import get_extractor

    ir_cls = get_extractor(bank, family)
    renderer = get_renderer(bank, family)

    if ir_cls is None or renderer is None:
        print(f"Error: No extractor/renderer registered for ({bank}, {family})")
        sys.exit(1)

    extractor = ir_cls()
    ir = extractor.to_ir(in_path)  # includes postprocessing

    # Write IR JSON (unmasked raw data)
    ir_path = out_path.with_suffix(".ir.json")
    _ = ir_path.write_text(ir.to_json(), encoding="utf-8")
    print(f"Wrote IR: {ir_path}  ({sum(len(a.transactions) for a in ir.accounts)} txns)")

    # Render Markdown (masking applied at render time)
    if not ir_only:
        _ = render_ir_to_md(ir, out_path, do_mask=do_mask)

    # Summary
    # Human-readable label for the detected statement type, keyed by (bank, family)
    # — the same key used by the extractor/renderer registries.
    STATEMENT_LABELS: dict[tuple[str, str], str] = {
        ("dbs", "consolidated"): "DBS consolidated statement",
        ("icbc", "consolidated"): "ICBC bank account statement",
        ("ocbc", "consolidated"): "OCBC consolidated statement",
        ("ocbc", "card"): "OCBC credit card",
        ("uob", "txn"): "UOB transaction-style",
        ("uob", "one"): "UOB One multi-account",
        ("uob", "portfolio"): "UOB portfolio summary",
    }
    label = STATEMENT_LABELS.get((bank, family), f"{bank}/{family}")

    print(f"Statement type: {label}")
    print(f"Records: {sum(len(a.transactions) for a in ir.accounts)}")


if __name__ == "__main__":
    main()
