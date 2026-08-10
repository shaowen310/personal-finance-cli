"""Shared date-parsing utilities for the PFA CLI and test pipeline.

Both ``pfa analyze`` and ``tests/run_full_pipeline.py`` use the same
``YYYYMMDD`` / ``YYYYMM`` date format, with ``YYYYMM`` expanding to a
full month (start-of-month for start dates, end-of-month for end dates).
"""

import calendar


def parse_start_date(raw: str | None) -> str | None:
    """Normalize a start-date string to ``YYYY-MM-DD``.

    Accepted formats:
      - ``YYYYMMDD`` → exact date (e.g. ``20260801``)
      - ``YYYYMM``   → first day of the month (e.g. ``202608`` → ``2026-08-01``)

    Returns ``None`` when *raw* is empty/None.
    Raises ``ValueError`` on invalid input.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) == 6 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-01"

    raise ValueError(f"Invalid start date '{raw}'. Expected YYYYMMDD or YYYYMM.")


def parse_end_date(raw: str | None) -> str | None:
    """Normalize an end-date string to ``YYYY-MM-DD``.

    Accepted formats:
      - ``YYYYMMDD`` → exact date (e.g. ``20260810``)
      - ``YYYYMM``   → last day of the month (e.g. ``202608`` → ``2026-08-31``)

    Returns ``None`` when *raw* is empty/None.
    Raises ``ValueError`` on invalid input.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) == 6 and raw.isdigit():
        year = int(raw[:4])
        month = int(raw[4:6])
        last_day = calendar.monthrange(year, month)[1]
        return f"{raw[:4]}-{raw[4:6]}-{last_day:02d}"

    raise ValueError(f"Invalid end date '{raw}'. Expected YYYYMMDD or YYYYMM.")


def parse_month(raw: str | None) -> tuple[str, str] | None:
    """Parse a month value into ``(start, end)`` in ``YYYY-MM-DD`` format.

    Accepted formats:
      - ``YYYYMMDD`` → exact date, start=end
      - ``YYYYMM``   → whole month, start=first day, end=last day

    Returns ``None`` when *raw* is empty/None.
    Raises ``ValueError`` on invalid input.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    if len(raw) == 8 and raw.isdigit():
        date = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        return (date, date)
    if len(raw) == 6 and raw.isdigit():
        year = int(raw[:4])
        month = int(raw[4:6])
        last_day = calendar.monthrange(year, month)[1]
        start = f"{raw[:4]}-{raw[4:6]}-01"
        end = f"{raw[:4]}-{raw[4:6]}-{last_day:02d}"
        return (start, end)

    raise ValueError(f"Invalid month '{raw}'. Expected YYYYMMDD or YYYYMM.")
