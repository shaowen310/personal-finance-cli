"""fx_rates.py — on-demand FX rate retrieval with on-disk caching.

Fetches historical mid-market FX rates on demand and caches them per
``(provider, date)`` so that repeat renders (or re-runs) reuse the same numbers
without re-hitting the network.

Design notes
------------
* **No third-party dependencies** — uses only the stdlib (``urllib``), so this
  works in the minimal environment the consolidation skill targets (no
  pdfplumber / requests / etc.).
* Rates are stored as **SGD per 1 unit** of foreign currency, matching the shape
  used by ``render_model.DEFAULT_FX_RATES`` (e.g. ``1 USD = 1.2912 SGD``).
* The default provider (Frankfurter) returns ``1 SGD = X <CCY>``; the provider
  layer inverts that to SGD-per-unit so the cache always holds the canonical
  shape regardless of which provider is plugged in.
* Any network / parse failure degrades gracefully to the previous cache, then to
  the hardcoded ``DEFAULT_FX_RATES`` fallback — the render never crashes because
  FX could not be fetched.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# render_model has no heavy dependencies, so importing it here is safe and keeps
# the fallback rates in sync with what the renderer already knows about.
try:  # pragma: no cover - import shim for standalone vs package usage
    from render_model import DEFAULT_FX_RATES
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from render_model import DEFAULT_FX_RATES  # noqa: E402


# Default currencies we always want rates for, on top of whatever the statement
# actually contains. Mirrors render_model.DEFAULT_FX_RATES.
DEFAULT_WATCH_SYMBOLS: list[str] = ["USD", "JPY", "CNY"]

# Default cache location: bank-ir-consolidate/cache/
DEFAULT_CACHE_DIR: Path = Path(__file__).resolve().parent.parent / "cache"

# How many calendar days back we walk when a date is not a trading day (weekend
# or holiday that the provider 404s on).
MAX_BACKOFF_DAYS: int = 5

# Base currency for all conversions.
BASE_CCY: str = "SGD"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _normalize_date(as_of: str | None) -> str:
    """Return an ISO ``YYYY-MM-DD`` for *as_of* (defaulting to today)."""
    if as_of is None:
        return _today_iso()
    s = str(as_of).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    s = s[:10]
    # Validate; fall back to a lenient parse if something odd slipped in.
    try:
        _ = _date.fromisoformat(s)
    except ValueError:
        s = datetime.fromisoformat(s).date().isoformat()
    return s


def _ordered_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# Provider interface (pluggable)
# ---------------------------------------------------------------------------
@runtime_checkable
class FXProvider(Protocol):
    """A pluggable FX rate source.

    ``fetch`` must return ``{symbol: sgd_per_1_unit}`` for the given date and
    symbols (i.e. how many SGD one unit of *symbol* is worth). Providers are
    responsible for any base-currency inversion so callers always receive
    SGD-per-unit.
    """

    name: str

    def fetch(
        self, date: str, symbols: list[str], *, timeout: float = 10.0
    ) -> dict[str, float]:
        ...


class FrankfurterProvider:
    """Free, keyless ECB historical rates (https://frankfurter.dev).

    Supports historical dates and returns ``1 SGD = X <CCY>``; we invert to
    SGD-per-unit so the cache holds the canonical shape.
    """

    name: str = "frankfurter"
    base: str = "SGD"
    endpoint: str = "https://api.frankfurter.dev/v1/{date}"

    def fetch(
        self, date: str, symbols: list[str], *, timeout: float = 10.0
    ) -> dict[str, float]:
        if not symbols:
            return {}
        syms = ",".join(sorted({s.upper() for s in symbols}))
        url = f"{self.endpoint.format(date=date)}?base={self.base}&symbols={syms}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "bank-ir-consolidate/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted HTTPS API
            payload = json.loads(resp.read().decode("utf-8"))
        raw = payload.get("rates", {})
        out: dict[str, float] = {}
        for sym in symbols:
            val = raw.get(sym)
            if val:
                out[sym] = 1.0 / float(val)
        if not out:
            raise ValueError(
                f"Frankfurter returned no usable rates for {symbols} on {date}"
            )
        return out


# Registry of built-in providers — extend here (or via get_provider) to add more.
PROVIDERS: dict[str, type[FXProvider]] = {
    "frankfurter": FrankfurterProvider,
}


def get_provider(name: str = "frankfurter") -> FXProvider:
    """Resolve a provider instance by name (pluggable entry point)."""
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown FX provider {name!r}. Available: {', '.join(sorted(PROVIDERS))}"
        )
    return cls()


# ---------------------------------------------------------------------------
# Result + cache helpers
# ---------------------------------------------------------------------------
@dataclass
class FXResult:
    """Outcome of an FX lookup."""

    rates: dict[str, float]               # SGD per 1 unit (always includes SGD: 1.0)
    source: str                           # "live" | "cached" | "fallback" | "none"
    provider: str
    as_of: str                            # effective (trading-day) date used
    fetched_at: str                       # ISO timestamp of the retrieval
    symbols_requested: list[str] = field(default_factory=list)
    requested_as_of: str | None = None
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    note: str = ""


def _cache_path(cache_dir: Path, provider_name: str, as_of: str) -> Path:
    return Path(cache_dir) / f"fx_{provider_name}_{as_of}.json"


def _load_cache(cache_dir: Any, provider_name: str, as_of: str) -> dict[str, Any] | None:
    if not cache_dir:
        return None
    p = _cache_path(Path(cache_dir), provider_name, as_of)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("provider") != provider_name:
        return None
    return data


def _save_cache(
    cache_dir: Any,
    provider_name: str,
    as_of: str,
    effective: str,
    rates: dict[str, float],
) -> None:
    if not cache_dir:
        return
    try:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        data = {
            "provider": provider_name,
            "base": BASE_CCY,
            "requested_as_of": as_of,
            "as_of": effective,
            "fetched_at": _now_iso(),
            "rates_sgd_per_unit": rates,
        }
        _ = _cache_path(cd, provider_name, as_of).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - caching is best-effort
        pass


def _fetch_with_backoff(
    provider: FXProvider,
    as_of: str,
    symbols: list[str],
    timeout: float,
) -> tuple[str, dict[str, float]]:
    """Fetch rates, stepping back up to MAX_BACKOFF_DAYS for non-trading days.

    Returns ``(effective_date, rates)``. Raises if no trading day in range
    succeeds (e.g. network down, 400 invalid symbol, persistent 404).
    """
    d = _date.fromisoformat(as_of)
    last_err: Exception | None = None
    for i in range(MAX_BACKOFF_DAYS):
        cur = (d - timedelta(days=i)).isoformat()
        try:
            rates = provider.fetch(cur, symbols, timeout=timeout)
        except urllib.error.HTTPError as e:  # 404 on a weekend/holiday -> step back
            if e.code == 404:
                last_err = e
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ValueError):
            # Network/parse/unusable-data failures can't be fixed by stepping back.
            raise
        return cur, rates
    raise ValueError(
        f"Could not fetch FX rates for {as_of} (tried {MAX_BACKOFF_DAYS} days): {last_err}"
    )


def _build_rates(
    foreign: list[str],
    resolved: dict[str, float],
    fallback: dict[str, float],
) -> tuple[dict[str, float], list[str], list[str]]:
    """Merge SGD base + resolved + fallback; report symbols still missing.

    CNH (offshore yuan) typically tracks CNY closely.  When a dedicated CNH
    rate is unavailable, the CNY rate is reused automatically.
    """
    rates: dict[str, float] = {BASE_CCY: 1.0}
    missing: list[str] = []
    warnings: list[str] = []
    for sym in foreign:
        if sym in resolved:
            rates[sym] = resolved[sym]
        elif sym in fallback:
            rates[sym] = fallback[sym]
        else:
            missing.append(sym)

    # ── CNH → CNY fallback ───────────────────────────────────────────────
    if "CNH" in missing and "CNY" in rates:
        rates["CNH"] = rates["CNY"]
        missing.remove("CNH")
        warnings.append(
            f"CNH rate not available from provider; using CNY rate "+
            f"({rates['CNY']:,.4f} SGD per 1 unit). CNH ↔ CNY typically "+
            f"track closely but may diverge intraday."
        )

    return rates, missing, warnings


def collect_currencies(stmt: Any) -> list[str]:
    """Return the sorted set of non-SGD currencies present in a statement."""
    ccy: set[str] = set()
    for acc in getattr(stmt, "accounts", []) or []:
        c = getattr(acc, "currency", None)
        if c and c != BASE_CCY:
            ccy.add(c)
    return sorted(ccy)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_fx_rates(
    *,
    as_of: str | None = None,
    symbols: list[str] | None = None,
    provider: FXProvider | None = None,
    cache_dir: Any = None,
    offline: bool = False,
    force_refresh: bool = False,
    timeout: float = 10.0,
    fallback_rates: dict[str, float] | None = None,
) -> FXResult:
    """Retrieve SGD-per-unit FX rates for *symbols* as of *as_of*.

    Resolution order: cache (unless ``force_refresh``) → live provider →
    previous cache → ``fallback_rates`` (default ``DEFAULT_FX_RATES``). The
    ``source`` field records which tier supplied the result.
    """
    prov = provider or FrankfurterProvider()
    cd = cache_dir or DEFAULT_CACHE_DIR
    fallback = fallback_rates or dict(DEFAULT_FX_RATES)

    as_of_norm = _normalize_date(as_of)
    foreign = _ordered_unique([s for s in (symbols or []) if s and s != BASE_CCY])

    if not foreign:
        cached = None if force_refresh else _load_cache(cd, prov.name, as_of_norm)
        src = "cached" if cached is not None else ("fallback" if offline else "none")
        return FXResult(
            rates={BASE_CCY: 1.0},
            source=src,
            provider=prov.name,
            as_of=as_of_norm,
            fetched_at=(cached or {}).get("fetched_at", _now_iso()),
            symbols_requested=[],
            requested_as_of=as_of_norm,
            missing=[],
            note="No foreign currencies requested; SGD only.",
        )

    # 1) Cache (unless refreshing).
    cached: dict[str, Any] | None = None
    if not force_refresh:
        cached = _load_cache(cd, prov.name, as_of_norm)
        if cached is not None and all(s in cached.get("rates_sgd_per_unit", {}) for s in foreign):
            rates, missing, fx_warnings = _build_rates(foreign, cached["rates_sgd_per_unit"], fallback)
            return FXResult(
                rates=rates,
                source="cached",
                provider=prov.name,
                as_of=cached.get("as_of", as_of_norm),
                fetched_at=cached.get("fetched_at", ""),
                symbols_requested=foreign,
                requested_as_of=as_of_norm,
                missing=missing,
                warnings=fx_warnings,
                note=f"Using cached rates ({prov.name}) for {as_of_norm}.",
            )

    # 2) Offline: no live fetch — fall back to cache, else to hardcoded defaults.
    if offline:
        if cached is not None:
            rates, missing, fx_warnings = _build_rates(foreign, cached["rates_sgd_per_unit"], fallback)
            return FXResult(
                rates=rates,
                source="cached",
                provider=prov.name,
                as_of=cached.get("as_of", as_of_norm),
                fetched_at=cached.get("fetched_at", ""),
                symbols_requested=foreign,
                requested_as_of=as_of_norm,
                missing=missing,
                warnings=fx_warnings,
                note="Offline mode: using cached rates.",
            )
        rates, missing, fx_warnings = _build_rates(foreign, {}, fallback)
        return FXResult(
            rates=rates,
            source="fallback",
            provider=prov.name,
            as_of=as_of_norm,
            fetched_at=_now_iso(),
            symbols_requested=foreign,
            requested_as_of=as_of_norm,
            missing=missing,
            warnings=fx_warnings,
            note="Offline mode: no cache available, using hardcoded fallback rates.",
        )

    # 3) Live fetch (with trading-day backoff).
    try:
        effective, live_rates = _fetch_with_backoff(prov, as_of_norm, foreign, timeout)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        if cached is not None:
            rates, missing, fx_warnings = _build_rates(foreign, cached["rates_sgd_per_unit"], fallback)
            return FXResult(
                rates=rates,
                source="cached",
                provider=prov.name,
                as_of=cached.get("as_of", as_of_norm),
                fetched_at=cached.get("fetched_at", ""),
                symbols_requested=foreign,
                requested_as_of=as_of_norm,
                missing=missing,
                warnings=fx_warnings,
                note=f"Live fetch failed ({e}); using cached rates.",
            )
        rates, missing, fx_warnings = _build_rates(foreign, {}, fallback)
        return FXResult(
            rates=rates,
            source="fallback",
            provider=prov.name,
            as_of=as_of_norm,
            fetched_at=_now_iso(),
            symbols_requested=foreign,
            requested_as_of=as_of_norm,
            missing=missing,
            warnings=fx_warnings,
            note=f"Live fetch failed ({e}); using hardcoded fallback rates.",
        )

    # Merge live rates over any existing cached rates, then persist.
    merged = {**(cached or {}).get("rates_sgd_per_unit", {}), **live_rates}
    _save_cache(cd, prov.name, as_of_norm, effective, merged)
    rates, missing, fx_warnings = _build_rates(foreign, merged, fallback)
    return FXResult(
        rates=rates,
        source="live",
        provider=prov.name,
        as_of=effective,
        fetched_at=_now_iso(),
        symbols_requested=foreign,
        requested_as_of=as_of_norm,
        missing=missing,
        warnings=fx_warnings,
        note=f"Fetched live rates ({prov.name}) as of {effective}.",
    )


if __name__ == "__main__":  # pragma: no cover - manual demo
    import argparse

    _ap = argparse.ArgumentParser(description="Fetch & cache FX rates (demo).")
    _ = _ap.add_argument("--fx-date", default=None, help="As-of date YYYY-MM-DD")
    _ = _ap.add_argument("--symbols", default=",".join(DEFAULT_WATCH_SYMBOLS),
                         help="Comma-separated currency symbols")
    _ = _ap.add_argument("--provider", default="frankfurter")
    _ = _ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    _ = _ap.add_argument("--offline", action="store_true")
    _ = _ap.add_argument("--force-refresh", action="store_true")
    _a = _ap.parse_args()

    _syms = [s.strip().upper() for s in _a.symbols.split(",") if s.strip()]
    _res = get_fx_rates(
        as_of=_a.fx_date,
        symbols=_syms,
        provider=get_provider(_a.provider),
        cache_dir=_a.cache_dir,
        offline=_a.offline,
        force_refresh=_a.force_refresh,
    )
    print(json.dumps({
        "source": _res.source,
        "provider": _res.provider,
        "as_of": _res.as_of,
        "fetched_at": _res.fetched_at,
        "rates_sgd_per_unit": _res.rates,
        "missing": _res.missing,
        "note": _res.note,
    }, indent=2, ensure_ascii=False))
