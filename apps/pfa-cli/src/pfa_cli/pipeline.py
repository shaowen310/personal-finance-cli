"""Workflow pipeline: orchestrates parse → ir → categorize → analyze → report."""

from pathlib import Path
from typing import Any


def run_pipeline(
    input_path: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the full personal finance analysis pipeline.

    Steps:
        1. Parse bank statements via pfa-parser / sg-bank-pdf-parser
        2. Consolidate IR data via pfa-ir-consolidator
        3. Categorize transactions via pfa-categorize
        4. Analyze and generate reports via pfa-analysis

    Returns a dict with paths to all output files.
    """
    output_dir = output_dir or Path.cwd() / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # TODO: Implement full pipeline orchestration
    results: dict[str, Any] = {"output_dir": str(output_dir)}
    return results
