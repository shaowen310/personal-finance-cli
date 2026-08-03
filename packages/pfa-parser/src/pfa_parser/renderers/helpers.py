"""Shared helpers for IR→Markdown rendering.

Provides:
- ``md_masked_description()`` — masking wrapper applied at render time
- ``fmt()`` — empty-value formatter
"""

from __future__ import annotations

from pfa_ir_schema import sanitize_description, mask_names_in_description


def md_masked_description(description: str, *, do_mask: bool = True) -> str:
    """Apply ``sanitize_description()`` + ``mask_names_in_description()`` when masking is on.

    When *do_mask* is ``False``, the description is returned unchanged — no
    sanitization or name masking is applied.  When *do_mask* is ``True``
    (default), both layers of masking run to produce a privacy-safe output.
    """
    if do_mask:
        desc = sanitize_description(description)
        desc = mask_names_in_description(desc)
        return desc
    return description


def fmt(val: object) -> str:
    """Return ``"—"`` for empty/falsy values, else ``str(val)``."""
    return str(val) if val else "—"
