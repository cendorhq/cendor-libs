"""Offline-first price registry: bundled snapshot + optional refresh. docs/core.md §7.

A dated ``prices.json`` ships in the wheel, so cost estimation works with no network. ``refresh()``
optionally pulls rates from a *static* JSON file (GitHub raw / CDN) **or** a built-in live
``source`` adapter (``litellm`` / ``openrouter`` / ``azure``) — each an unauthenticated HTTPS GET,
never a running service — and falls back silently to the bundled snapshot if it can't.

The direct model labs (OpenAI / Anthropic) expose no pricing API; their model-list endpoints carry
IDs only. The built-in sources are a community aggregator (LiteLLM), a gateway (OpenRouter) and a
cloud catalog (Azure Retail Prices), all of which *do* publish per-model rates as plain JSON.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from .types import Money


class UnknownModelError(KeyError):
    """Raised when a model id is not present in the price table."""


_table: dict | None = None
_source: str = "bundled"  # "bundled" | "refreshed" — coarse provenance (back-compat)
_source_name: str = "bundled"  # finer: "bundled" | "litellm" | "openrouter" | "azure" | "custom"
_source_url: str | None = None
_table_lock = threading.Lock()  # guards the lazy load + refresh() swap of the module-global table

#: Default static snapshot location used by ``refresh()`` when no URL or source is given. Points at
#: the bundled ``prices.json`` on the repo's main branch; override by passing ``url=`` / ``source=``
#: or reassigning this. Must resolve to a *public* static JSON (no auth, no running service).
SNAPSHOT_URL: str = (
    "https://raw.githubusercontent.com/cendorhq/cendor-libs/main/"
    "packages/cendor-core/src/cendor/core/prices.json"
)

#: Community-maintained, near-daily-updated cross-provider price table (broadest coverage).
LITELLM_URL: str = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
#: OpenRouter's public model catalog — per-token pricing as JSON strings, no auth.
OPENROUTER_URL: str = "https://openrouter.ai/api/v1/models"
#: Azure Retail Prices API filtered to Azure OpenAI meters — public, no auth.
AZURE_URL: str = (
    "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview"
    "&$filter=productName eq 'Azure OpenAI'"
)

#: Optional explicit id aliases applied after prefix-stripping (extend as needed).
_ALIASES: dict[str, str] = {}


def _bundled_text() -> str:
    try:
        from importlib.resources import files

        return (files("cendor.core") / "prices.json").read_text(encoding="utf-8")
    except Exception:
        from pathlib import Path

        return (Path(__file__).with_name("prices.json")).read_text(encoding="utf-8")


def _loads(text: str) -> dict:
    """Parse a price table, decoding numbers as ``Decimal`` so rates never round-trip through
    ``float`` (this is money — see the project's Decimal-only rule)."""
    return json.loads(text, parse_float=Decimal)


def _ensure_loaded() -> dict:
    global _table
    if _table is None:
        with _table_lock:  # double-checked: only one thread loads the bundled snapshot
            if _table is None:
                _table = _loads(_bundled_text())
    return _table


def _rates(model: str) -> dict:
    models = _ensure_loaded().get("models", {})
    if model not in models:
        raise UnknownModelError(model)
    return models[model]


def estimate(
    model: str,
    input_tokens: int,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Money:
    """Estimate the cost of a call from the price snapshot, as exact ``Decimal`` ``Money``.

    ``cached_tokens`` is a *subset of* ``input_tokens`` (the documented :class:`~cendor.core.types.
    Usage` convention — see :func:`cendor.core.instrument._extract_usage`), so the cached portion is
    billed **once**: ``input_rate*(input − cached) + cached_rate*cached``. When the model has no
    published ``cached`` rate, cache-read tokens fall back to the full input rate (no discount),
    which reduces to ``input_rate*input`` — never a double charge.

    Args:
        model: Model id; must exist in the table (else :class:`UnknownModelError`).
        input_tokens: Billed input tokens (inclusive of ``cached_tokens``).
        output_tokens: Billed output tokens.
        cached_tokens: Cache-read tokens, a subset of ``input_tokens``; priced at the model's
            ``cached`` rate if present, else at the input rate.
        cache_write_tokens: Cache-*write* tokens (Anthropic ``cache_creation_input_tokens``), a
            *separate* category from ``input_tokens``; priced at the model's ``cache_write`` rate if
            present, else ~1.25× the input rate.

    Returns:
        The estimated :class:`~cendor.core.types.Money` cost in USD.
    """
    r = _rates(model)
    cached = min(max(cached_tokens, 0), input_tokens)  # cached ⊆ input; clamp defensively
    input_rate = Decimal(str(r["input"]))
    cached_rate = Decimal(str(r["cached"])) if "cached" in r else input_rate
    write_rate = (
        Decimal(str(r["cache_write"])) if "cache_write" in r else input_rate * Decimal("1.25")
    )
    amount = (
        input_rate * (input_tokens - cached)
        + Decimal(str(r.get("output", 0))) * output_tokens
        + cached_rate * cached
        + write_rate * max(cache_write_tokens, 0)
    )
    return Money(amount)


def models() -> list[str]:
    """Sorted list of model ids known to the current price table."""
    return sorted(_ensure_loaded().get("models", {}))


def snapshot_date() -> str | None:
    """The ``_updated`` date of the loaded snapshot, so callers can surface its age."""
    return _ensure_loaded().get("_updated")


def source() -> str:
    """``"bundled"`` or ``"refreshed"`` — where the active table came from."""
    return _source


def source_name() -> str:
    """Finer provenance of the active table: ``"bundled"`` | ``"litellm"`` | ``"openrouter"`` |
    ``"azure"`` | ``"custom"`` | ``"default"``."""
    return _source_name


def source_url() -> str | None:
    """The URL the active table was fetched from, or ``None`` if it's the bundled snapshot."""
    return _source_url


def age_days(today: date | None = None) -> int | None:
    """Age of the active table in days (today − ``_updated``), or ``None`` if undatable.

    Lets callers surface staleness — e.g. warn before trusting an old offline snapshot.
    """
    d = snapshot_date()
    if not d:
        return None
    try:
        y, m, dd = (int(x) for x in d.split("-"))
        ref = today or date.today()
        return (ref - date(y, m, dd)).days
    except Exception:
        return None


def is_stale(max_age_days: int = 30) -> bool:
    """``True`` if the table is older than ``max_age_days`` (an undatable table is never stale)."""
    a = age_days()
    return a is not None and a > max_age_days


# --------------------------------------------------------------------------- live-source adapters
#
# Each adapter maps a source's raw JSON (already Decimal-parsed by ``_loads``) into our schema:
#   {"_updated": "YYYY-MM-DD", "models": {id: {"input": Decimal, "output": Decimal, "cached"?}}}
# Rates are coerced through ``Decimal(str(...))`` because some sources (OpenRouter) ship rates as
# JSON *strings*, not numbers, so ``parse_float`` doesn't touch them.


def _normalize_model_id(mid: str) -> str:
    """Align a source's model id with our bare keys: drop a provider prefix (``openai/gpt-4o`` →
    ``gpt-4o``), lowercase, then apply any explicit alias. The main reconciliation seam between
    differently-namespaced sources."""
    s = mid.strip()
    if "/" in s:
        s = s.split("/", 1)[1]
    s = s.lower()
    return _ALIASES.get(s, s)


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _map_litellm(raw: dict) -> dict:
    """LiteLLM ``model_prices_and_context_window.json``: ``{id: {input_cost_per_token, ...}}``."""
    out: dict[str, dict] = {}
    for mid, rec in raw.items():
        if not isinstance(rec, dict) or "input_cost_per_token" not in rec:
            continue  # skips non-model entries like "sample_spec"
        rates: dict[str, Decimal] = {"input": _dec(rec["input_cost_per_token"])}
        if rec.get("output_cost_per_token") is not None:
            rates["output"] = _dec(rec["output_cost_per_token"])
        if rec.get("cache_read_input_token_cost") is not None:
            rates["cached"] = _dec(rec["cache_read_input_token_cost"])
        out[_normalize_model_id(mid)] = rates
    # No global "as-of" date in the LiteLLM payload — omit `_updated` (undatable, not falsely
    # "today"), so is_stale() reports unknown instead of defeating itself. Provenance: source_name.
    return {"models": out}


def _map_openrouter(raw: dict) -> dict:
    """OpenRouter ``/api/v1/models``: ``{"data": [{"id", "pricing": {prompt, completion}}]}``."""
    out: dict[str, dict] = {}
    for rec in raw.get("data", []):
        mid = rec.get("id")
        pricing = rec.get("pricing") or {}
        if not mid or pricing.get("prompt") is None:
            continue
        rates: dict[str, Decimal] = {"input": _dec(pricing["prompt"])}
        if pricing.get("completion") is not None:
            rates["output"] = _dec(pricing["completion"])
        cached = pricing.get("input_cache_read")
        if cached is not None and _dec(cached) > 0:
            rates["cached"] = _dec(cached)
        out[_normalize_model_id(mid)] = rates
    # OpenRouter's catalog carries no global date — omit `_updated` (undatable, not fake "today").
    return {"models": out}


def _azure_unit_divisor(unit_of_measure: str) -> Decimal:
    """Tokens per price unit for an Azure ``unitOfMeasure`` (``"1K"`` / ``"1M"`` / ``"1000"``)."""
    u = (unit_of_measure or "").upper().replace(" ", "")
    if "1M" in u or "1000000" in u:
        return Decimal(1_000_000)
    return Decimal(1000)  # default + "1K"/"1000"


def _map_azure(raw: dict) -> dict:
    """Azure Retail Prices ``{"Items": [{skuName, retailPrice, unitOfMeasure, ...}]}``.

    Parses the model + direction (input/output) + deployment tier out of ``skuName`` (e.g.
    ``"gpt 4o 1120 Inp Global"``), keeps the cheapest (Global) tier, and converts the per-1K/1M
    retail price to a per-token ``Decimal``. Best-effort over the first page; narrow the ``$filter``
    for full coverage (the Retail API paginates). Imperfect SKU→id mapping is expected.
    """
    by_model: dict[str, dict[str, Decimal]] = {}
    latest: str | None = None  # the newest source effectiveStartDate seen (real provenance date)
    for item in raw.get("Items", []):
        eff = str(item.get("effectiveStartDate", ""))[:10]  # "2024-01-01T..." -> "2024-01-01"
        if len(eff) == 10 and (latest is None or eff > latest):
            latest = eff
        sku = str(item.get("skuName", ""))
        low = sku.lower()
        if "input" in low or " inp" in low or low.endswith("inp"):
            direction = "input"
        elif "output" in low or "outp" in low:
            direction = "output"
        else:
            continue
        if "global" not in low and "regional" in low + " " + str(item.get("meterName", "")).lower():
            continue  # prefer the Global (cheapest) tier when tiers are distinguishable
        price = item.get("retailPrice")
        if price is None:
            continue
        per_token = _dec(price) / _azure_unit_divisor(str(item.get("unitOfMeasure", "")))
        # model id = sku up to the direction keyword, sans tier/version noise
        head = low
        for cut in (" inp", " input", " outp", " output"):
            if cut in head:
                head = head.split(cut, 1)[0]
                break
        words = head.strip().split()
        while words and words[-1].isdigit() and len(words[-1]) in (3, 4):
            words.pop()  # drop a trailing snapshot-date token, e.g. "gpt 4o 1120" -> "gpt 4o"
        mid = _normalize_model_id("-".join(words))
        rates = by_model.setdefault(mid, {})
        # keep the lowest seen rate per direction (cheapest tier/region)
        if direction not in rates or per_token < rates[direction]:
            rates[direction] = per_token
    out = {mid: r for mid, r in by_model.items() if "input" in r}
    # Carry Azure's real effectiveStartDate when present (its genuine provenance), else undatable —
    # never fake "today", which would make a stale refresh look fresh to is_stale().
    result: dict = {"models": out}
    if latest is not None:
        result["_updated"] = latest
    return result


#: Built-in live sources: name -> (url, mapper). All unauthenticated HTTPS GET → JSON.
_SOURCES: dict[str, tuple[str, Callable[[dict], dict]]] = {
    "litellm": (LITELLM_URL, _map_litellm),
    "openrouter": (OPENROUTER_URL, _map_openrouter),
    "azure": (AZURE_URL, _map_azure),
}


def sources() -> list[str]:
    """Names of the built-in live price sources accepted by ``refresh(source=...)``."""
    return sorted(_SOURCES)


def refresh(
    url: str | None = None,
    *,
    source: str | None = None,
    mapper: Callable[[dict], dict] | None = None,
    timeout: float = 5.0,
) -> bool:
    """Replace the table from a live source or static JSON URL. Never raises; offline-safe.

    Args:
        url: A static JSON URL in *our* schema (``{"models": {...}}``). Defaults to
            :data:`SNAPSHOT_URL` when neither ``url`` nor ``source`` is given.
        source: A built-in adapter name — one of :func:`sources` (``"litellm"`` / ``"openrouter"``
            / ``"azure"``). Takes precedence over ``url``; selects that source's URL + mapper.
        mapper: A custom ``raw_json -> {"models": {...}}`` callable (overrides the source's mapper);
            use it to map any other source onto our schema.
        timeout: Per-request timeout in seconds.

    Returns:
        ``True`` if the table was updated, ``False`` if the fetch/parse/map failed (the bundled or
        last-good snapshot stays active). The fetched table lives in memory only — nothing is
        persisted. docs/core.md §7.
    """
    global _table, _source, _source_name, _source_url
    adapter: Callable[[dict], dict] | None
    if source is not None:
        if source not in _SOURCES:
            return False  # unknown source name -> no-op, keep current table
        target, builtin_mapper = _SOURCES[source]
        adapter = mapper or builtin_mapper
        name = source
    else:
        target = url or SNAPSHOT_URL
        adapter = mapper
        name = "custom" if url else "default"
    if not target or not target.lower().startswith(("http://", "https://")):
        return False  # fetch static JSON over http(s) only — never file://, ftp://, etc.
    try:
        import urllib.request

        with urllib.request.urlopen(target, timeout=timeout) as resp:  # noqa: S310 - http(s) only
            raw = _loads(resp.read().decode("utf-8"))
        data = adapter(raw) if adapter is not None else raw
        if isinstance(data, dict) and data.get("models"):
            with _table_lock:  # publish the new table atomically for concurrent estimate() readers
                _table = data
                _source = "refreshed"
                _source_name = name
                _source_url = target
            return True
    except Exception:
        return False
    return False


def _reset() -> None:
    """Test helper: drop the loaded table so the bundled snapshot reloads."""
    global _table, _source, _source_name, _source_url
    _table = None
    _source = "bundled"
    _source_name = "bundled"
    _source_url = None
