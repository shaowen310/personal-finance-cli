"""Resolve sg_bank_pdf_parser schema + masking modules without importing the
heavy CLI entrypoint (``convert_statement``).

Only the light modules needed for consolidation/rendering are loaded:
``ir_schema``, ``account_type``, ``common`` and ``renderers.helpers``. These have
no heavy third-party dependencies (pdfplumber etc.), so the consolidation skill
can run in a minimal environment.

Resolution order (mirrors ``kb-ingest._load_pptx2md``):
  1. ``--parser-dir``  (skill dir containing ``sg_bank_pdf_parser/``)
  2. sibling ``../sg-bank-pdf-parser`` (relative to this file's repo root)
  3. installed package (PyPI: ``sg_bank_pdf_parser``)
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


class ParserModules:
    """Bundle of the resolved ``sg_bank_pdf_parser`` modules."""

    ir_schema: types.ModuleType
    account_type: types.ModuleType
    common: types.ModuleType
    helpers: types.ModuleType
    postprocess: types.ModuleType

    def __init__(
        self,
        ir_schema: types.ModuleType,
        account_type: types.ModuleType,
        common: types.ModuleType,
        helpers: types.ModuleType,
        postprocess: types.ModuleType,
    ) -> None:
        self.ir_schema = ir_schema
        self.account_type = account_type
        self.common = common
        self.helpers = helpers
        self.postprocess = postprocess


def load_parser_modules(parser_dir: str | None) -> ParserModules:
    pkg_name = "sg_bank_pdf_parser"
    candidates: list[Path] = []
    if parser_dir:
        candidates.append(Path(parser_dir))
    here = Path(__file__).resolve().parent
    # bank-ir-consolidate/scripts -> bank-ir-consolidate -> repo root
    candidates.append(here.parent.parent / "sg-bank-pdf-parser")
    candidates.append(here.parent / "sg-bank-pdf-parser")

    pkg_dir: Path | None = None
    for cand in candidates:
        if (cand / pkg_name).is_dir():
            pkg_dir = cand / pkg_name
            break
        if cand.is_dir() and cand.name == pkg_name:
            pkg_dir = cand
            break

    if pkg_dir is None:
        # Last resort: an installed package. Acceptable here because it falls
        # back to the real package import (may pull convert_statement, but only
        # if the sibling directory was not found).
        try:
            return ParserModules(
                ir_schema=__import__(f"{pkg_name}.ir_schema", fromlist=["ir_schema"]),
                account_type=__import__(f"{pkg_name}.account_type", fromlist=["account_type"]),
                common=__import__(f"{pkg_name}.common", fromlist=["common"]),
                helpers=__import__(f"{pkg_name}.renderers.helpers", fromlist=["helpers"]),
                postprocess=__import__(f"{pkg_name}.postprocess", fromlist=["postprocess"]),
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "Could not locate sg_bank_pdf_parser. Use --parser-dir to point at the "+
                "sg-bank-pdf-parser skill directory, or install the package (pip install sg_bank_pdf_parser)."
            ) from exc

    def _register_pkg(name: str, path: Path) -> None:
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = [str(path)]
            pkg.__package__ = name
            sys.modules[name] = pkg

    def _load(mod_name: str, file_path: Path):
        full = f"{pkg_name}.{mod_name}"
        if full in sys.modules:
            return sys.modules[full]
        spec = importlib.util.spec_from_file_location(full, file_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load module {full} from {file_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)  # noqa: S301 - trusted local file
        return mod

    _register_pkg(pkg_name, pkg_dir)
    _register_pkg(f"{pkg_name}.renderers", pkg_dir / "renderers")

    account_type = _load("account_type", pkg_dir / "account_type.py")
    common = _load("common", pkg_dir / "common.py")
    ir_schema = _load("ir_schema", pkg_dir / "ir_schema.py")
    helpers = _load("renderers.helpers", pkg_dir / "renderers" / "helpers.py")
    postprocess = _load("postprocess", pkg_dir / "postprocess.py")
    return ParserModules(ir_schema, account_type, common, helpers, postprocess)
