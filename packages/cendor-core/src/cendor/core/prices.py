"""Offline-first price registry: bundled snapshot + optional refresh. docs/core.md §7.

A dated ``prices.json`` ships in the wheel, so cost estimation works with no network. ``refresh()``
optionally pulls rates from a *static* JSON file (GitHub raw / CDN) **or** a built-in live
``source`` adapter — each an unauthenticated HTTPS GET, never a running service — and falls back
silently to the bundled snapshot if it can't.

The direct model labs (OpenAI / Anthropic) expose **no pricing API**; their model-list endpoints
carry ids only. What does exist, and what the built-in sources are:

* two **cloud catalogs** that publish their own billing meters keyless — Azure Retail Prices
  (``azure``) and the AWS Bedrock public price files (``aws``). These are first-party facts.
* three **aggregators / gateways** — ``modelsdev`` (MIT, the widest catalog), ``litellm`` (MIT) and
  ``openrouter`` / ``vercel`` (gateway *resale* prices — see their notes).
* the **cendor-prices feed** (the default): one dated, per-row-provenanced table reconciled from all
  of the above by ``cendorhq/cendor-prices``, so a bare ``refresh()`` gets first-party rates without
  fetching five sources.

Nothing here is a guess. A model no source prices stays absent, ``estimate()`` raises
:class:`UnknownModelError`, and tokenguard warns — an honest gap beats a confident wrong number.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from .types import Money


class UnknownModelError(KeyError):
    """Raised when a model id is not present in the price table."""


class MissingRateError(UnknownModelError):
    """Raised when a model IS in the table but its rates cannot price a call.

    A **subclass of** :class:`UnknownModelError` (and therefore of ``KeyError``) on purpose: every
    caller that already handles "I cannot price this" keeps working unchanged — ``instrument()``,
    ``otel``, the LangChain handler and ``tokenguard`` all catch ``KeyError`` and fall back to an
    honest ``None``/warn-once. Catch this specific type only when you want to tell *"no such model"*
    apart from *"known model, unusable rate"*.

    Raised for a rate a **table** left absent, or an input rate a table states as ``0`` — both are
    indistinguishable from "we do not know", and :func:`estimate` returning ``$0.00`` for them
    reports a fabricated cost as a *fact* while a USD budget cap silently never binds. A rate
    **you** registered is never second-guessed: ``prices.register("llama3", {"input": 0})``
    prices a local model at zero because you said so.
    """

    def __str__(self) -> str:  # KeyError repr()s its arg, which mangles a sentence into "'…'"
        return str(self.args[0]) if self.args else ""


def _missing_rate(model: str, key: str, why: str) -> MissingRateError:
    """Build the actionable error: it names the fix, in the caller's own code, both call shapes."""
    return MissingRateError(
        f"the price table {why} for {model!r}, so this call cannot be priced. An absent or zero "
        f"rate is indistinguishable from 'we do not know': pricing it as $0.00 would report a "
        f"fabricated cost as a fact, and a USD budget cap would silently never bind on it.\n"
        f"Set the rate yourself:\n"
        f"    prices.register_model_price({model!r}, input=..., output=..., per='1M')\n"
        f"    prices.register({model!r}, {{'input': ..., 'output': ...}})   # per-token\n"
        f"If this model genuinely bills nothing for {key}, say so explicitly — an explicit "
        f"{key}=0 is honoured, an absent one is not."
    )


class PriceRefreshError(RuntimeError):
    """Raised by ``refresh(..., required=True)`` when the fetch/parse/map failed.

    ``refresh()`` is contractually never-raise: it returns ``False`` and leaves the last-good table
    active. Pass ``required=True`` when running on stale rates would be worse than not running —
    then a failure is loud. Never the default. docs/core.md §7.
    """


_table: dict | None = None
_source: str = "bundled"  # "bundled" | "refreshed" | "loaded" — coarse provenance (back-compat)
#: Finer: "bundled" | "feed" | "litellm" | "openrouter" | "azure" | "aws" | "modelsdev" | "vercel"
#: | "custom" | "default"
_source_name: str = "bundled"
_source_url: str | None = None
_table_lock = threading.Lock()  # guards the lazy load + refresh() swap of the module-global table
#: Programmatic registrations (see :func:`_register`) — re-applied on top of every loaded or
#: refreshed table, so a ``refresh()`` never drops them.
_registered: dict[str, dict] = {}

#: Default table used by ``refresh()`` when no URL or source is given: the **cendor-prices feed** —
#: a dated, per-row-provenanced ``prices/1`` table rebuilt daily behind validation gates and served
#: by GitHub Pages. Cendor operates no server for this; it is a static file on GitHub's CDN, and a
#: Cendor outage cannot exist to break your cost estimation. Override by passing ``url=`` /
#: ``source=`` or reassigning this. Must resolve to a *public* static JSON.
#:
#: ⚠️ It is a **Pages** URL, not ``raw.githubusercontent``. The builder repo is private — the source,
#: the curation policy and the run history are internal — while a data-only ``gh-pages`` branch
#: publishes the file itself, keyless. Pages also serves it as ``application/json`` rather than
#: raw's ``text/plain``. Do not "correct" this back to a raw URL: that one needs auth and 404s.
SNAPSHOT_URL: str = "https://cendorhq.github.io/cendor-prices/prices.json"

#: Community-maintained, near-daily-updated cross-provider price table (broadest coverage).
LITELLM_URL: str = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
#: OpenRouter's public model catalog — per-token pricing as JSON strings, no auth. Gateway
#: **resale** prices: what OpenRouter charges you, not what the lab charges.
OPENROUTER_URL: str = "https://openrouter.ai/api/v1/models"
#: Vercel AI Gateway's public model catalog — same shape and the same resale caveat as OpenRouter.
#: Base rates only; its tiered (long-context) and service-tier prices are out of scope.
VERCEL_URL: str = "https://ai-gateway.vercel.sh/v1/models"
#: models.dev — MIT, the widest keyless catalog found (177 providers / 5,935 models on 2026-08-01),
#: per-**1M** rates with a per-row ``last_updated``.
MODELSDEV_URL: str = "https://models.dev/api.json"

#: Azure Retail Prices API endpoint and version. See :func:`azure_url`.
AZURE_API: str = "https://prices.azure.com/api/retail/prices"
AZURE_API_VERSION: str = "2023-01-01-preview"
#: Region whose meters ``refresh(source="azure")`` reads. eastus2 carries the largest Foundry
#: catalog (1,526 meters on 2026-08-01). Override with ``refresh(source="azure", region=...)``.
AZURE_DEFAULT_REGION: str = "eastus2"
#: AWS Price List public files — the Bedrock offers, and the region whose file ``aws`` reads.
AWS_PRICING_HOST: str = "https://pricing.us-east-1.amazonaws.com"
#: ⚠️ **Both** offer codes are required. Measured 2026-08-01: ``AmazonBedrock`` alone carries only
#: Claude 2.0/2.1/3-Haiku/3-Sonnet/Instant — ``Claude Sonnet 4`` and ``Claude Sonnet 4.5`` exist
#: **only** in ``AmazonBedrockService``, so a single-offer client silently misses every current
#: Claude rate.
AWS_OFFERS: tuple[str, ...] = ("AmazonBedrock", "AmazonBedrockService")
AWS_DEFAULT_REGION: str = "us-east-1"


def azure_url(region: str = AZURE_DEFAULT_REGION) -> str:
    """The Azure Retail Prices query ``refresh(source="azure")`` issues, for one region.

    ⚠️ **Percent-encoded on purpose.** The readable form (``&$filter=serviceName eq 'Foundry
    Models'``) carries raw spaces and ``urllib.request.urlopen`` refuses it outright
    (``InvalidURL: URL can't contain control characters``). Because :func:`refresh` swallows every
    exception, that surfaced as a plain ``False`` — indistinguishable from being offline — so
    ``refresh(source="azure")`` had never once worked in Python (measured 2026-07-31; the TS twin
    was unaffected because ``fetch`` encodes for us). Keep it encoded in both languages so the two
    URLs stay byte-identical and comparable.

    ⚠️ **The region term is not an optimisation.** Measured 2026-08-01: with a region, this query is
    1,526 meters over 2 pages in 0.7 s; without one it is **≥25,000 rows and still paging after
    28.5 s** — not something a library may do inside one ``refresh()``.

    ⚠️ ``serviceName eq 'Foundry Models'`` replaced the pre-rename ``productName eq 'Azure OpenAI'``.
    The old filter still returns rows, which is why nothing looked broken — it just saw 462 of the
    1,526 and **no GPT-5, DeepSeek, Grok, Mistral, Llama, Phi, Kimi, Qwen or Cohere meter at all**.
    """
    import urllib.parse

    # `quote`, not `urlencode`: urlencode is form encoding and writes a space as `+`, which Azure
    # accepts but which makes this URL differ from the TypeScript twin's `encodeURIComponent` form
    # byte for byte. Two mappers that cannot be diffed is how the original Azure defect survived.
    f = urllib.parse.quote(
        f"serviceName eq 'Foundry Models' and armRegionName eq '{region}'", safe=""
    )
    return (
        f"{AZURE_API}?api-version={AZURE_API_VERSION}&{urllib.parse.quote('$filter', safe='')}={f}"
    )


#: The URL ``refresh(source="azure")`` uses by default. Kept as a module constant for back-compat
#: with anything that read it; :func:`azure_url` is the region-aware form.
AZURE_URL: str = azure_url()
#: The AWS region index the ``aws`` source resolves before fetching a region file.
AWS_URL: str = f"{AWS_PRICING_HOST}/offers/v1.0/aws/{{offer}}/current/region_index.json"

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
                table = _loads(_bundled_text())
                if _registered:  # re-apply programmatic registrations (see _register)
                    table.setdefault("models", {}).update(_registered)
                _table = table
    return _table


def register(model: str, rates: dict) -> None:
    """Register (or override) one model's **per-token** rates — survives ``refresh()``.

    Use this when a model id is absent from the bundled snapshot (an Azure/Foundry **deployment**
    name, a fine-tune, a Bedrock marketplace id, a local model), so its calls are costed and USD
    ``budget(...)`` caps can bind on it. Parity with ``@cendor/core``'s ``prices.register``; for
    rates quoted per 1M/1K tokens use :func:`register_model_price` instead.

    ``rates`` uses per-**token** values with the snapshot's keys (``input`` / ``output`` /
    ``cached`` / ``cache_write``); values are coerced to ``Decimal`` via ``str``, so pass
    ``Decimal``/``str``/``int`` and never a float you care about. The registration is applied to
    the active table immediately and re-applied after every ``refresh()`` table swap, so a refresh
    never drops it; it overrides a snapshot entry with the same id. ``_reset()`` (tests) clears
    registrations.

    ```python
    from decimal import Decimal
    from cendor.core import prices

    prices.register("my-deployment", {"input": Decimal("0.0000025"), "output": "0.00001"})
    prices.estimate("my-deployment", 1000, output_tokens=500)   # -> Money("0.0075", "USD")
    ```

    Args:
        model: The exact model id calls report (deployment name / Hub id / local id).
        rates: Per-**token** USD rates — ``input`` (required), ``output``, ``cached``,
            ``cache_write``.
    """
    entry = {k: Decimal(str(v)) for k, v in rates.items()}
    table = _ensure_loaded()
    with _table_lock:
        _registered[str(model)] = entry
        table.setdefault("models", {})[str(model)] = entry


#: Accepted ``per=`` units for :func:`register_model_price` → tokens per price unit.
_PER: dict[str, Decimal] = {
    "1M": Decimal(1_000_000),
    "1K": Decimal(1000),
    "token": Decimal(1),
}


def register_model_price(
    model: str,
    *,
    input: float | str | Decimal,  # noqa: A002 - mirrors the published SDK helper's signature
    output: float | str | Decimal = 0,
    cached: float | str | Decimal | None = None,
    cache_write: float | str | Decimal | None = None,
    per: str = "1M",
) -> dict[str, Decimal]:
    """Register a model's rates quoted **per 1M tokens** (the unit price lists use).

    The unit-converting convenience over :func:`register`: rates are divided by ``per`` and stored
    as exact per-token ``Decimal``, so ``LLMCall.cost`` is non-zero for the model and USD budgets
    enforce against it. Registrations survive :func:`refresh`.

    ``cendor.sdk.register_model_price`` is a thin re-export of this function — a **libraries-door**
    user needs only ``cendor-core``, not the SDK distribution.

    ```python
    from cendor.core import prices

    prices.register_model_price("my-deployment", input=2.50, output=10.00)  # USD per 1M tokens
    ```

    Args:
        model: The exact model id calls report (deployment name / Hub id / local id).
        input: Input (prompt) price, in units of ``per``.
        output: Output (completion) price.
        cached: Optional cache-read price (defaults to the input rate when absent).
        cache_write: Optional cache-write price (Anthropic-style).
        per: Unit the prices are expressed in — ``"1M"`` (default), ``"1K"``, or ``"token"``.

    Returns:
        The stored per-token rate dict.

    Raises:
        ValueError: If ``per`` is not one of ``"1M"`` / ``"1K"`` / ``"token"``.
    """
    if per not in _PER:
        raise ValueError(f"per must be one of {sorted(_PER)}, got {per!r}")
    divisor = _PER[per]
    rates: dict[str, Decimal] = {
        "input": Decimal(str(input)) / divisor,
        "output": Decimal(str(output)) / divisor,
    }
    if cached is not None:
        rates["cached"] = Decimal(str(cached)) / divisor
    if cache_write is not None:
        rates["cache_write"] = Decimal(str(cache_write)) / divisor
    register(model, rates)
    return rates


def register_deployment(deployment: str, *, like: str) -> dict[str, Decimal]:
    """Price a **deployment name** by copying the rates of the base model it serves.

    On Microsoft Foundry (formerly Azure AI Foundry) the id a call reports is the *deployment*
    name you chose
    (``prod-gpt4o-eastus``), not a model id — so it is absent from every price table, its cost is
    ``None``, and a USD ``budget(...)`` silently never binds. You already know which model it
    serves; this says so once:

    ```python
    from cendor.core import prices

    prices.register_deployment("prod-gpt4o-eastus", like="gpt-4o")
    prices.estimate("prod-gpt4o-eastus", 1000, output_tokens=500)   # priced like gpt-4o
    ```

    This is an **explicit** mapping you supply — deliberately not the automatic ``-preview`` /
    ``-latest`` alias guessing that was considered and rejected (a confidently wrong price is worse
    than an honest ``None``). Nothing is inferred from the deployment's name.

    **Copy-at-registration, not a live alias.** ``like``'s rates are read *now* and stored as
    ``deployment``'s own registration, exactly as if you had called :func:`register` with them. Two
    consequences worth knowing, both deliberate:

    * a later :func:`refresh` that reprices ``like`` does **not** reprice ``deployment`` — call this
      again to pick the new rates up. (The alternative, a live alias, would make a deployment's cost
      depend on whether a base model still exists in whatever table was last fetched, and would have
      to invent an answer when it doesn't.)
    * like every registration, it **survives** ``refresh()`` and overrides a snapshot entry with the
      same id.

    ``like`` goes through the same lookup reduction as a real call, so a dated or Bedrock-decorated
    base id works (``like="gpt-4o-2024-08-06"``, ``like="us.anthropic.claude-sonnet-4-6-…-v1:0"``).

    Args:
        deployment: The exact id calls report — your Azure/Foundry deployment name.
        like: A model id already in the price table whose rates the deployment should use.

    Returns:
        The stored per-token rate dict (a copy — mutating it does not change the table).

    Raises:
        UnknownModelError: If ``like`` is not in the active table.
        MissingRateError: If ``like``'s entry cannot price a call — no ``input`` rate, a
            table-stated zero ``input``, or no ``output``. Copying an unpriceable entry onto one
            would reproduce the exact silence this function exists to remove, so it fails at
            registration rather than on the first call.
    """
    # Copy EVERY rate key, not an enumerated few: a base entry may carry a key this function has
    # never heard of (a future rate category, or a hand-written `register()` dict), and dropping it
    # would silently under-price the deployment. `Decimal` is immutable, so sharing values is safe.
    rates = dict(_rates(like))  # raises UnknownModelError — never register a silent nothing
    _priceable(rates, like)  # ...and never register rates that cannot price a call
    register(deployment, rates)
    return rates


def _register(model: str, rates: dict) -> None:
    """Deprecated private alias of :func:`register`, kept for the pre-1.15 contractual write hook.

    ``cendor.sdk.register_model_price`` wrote through this name before ``register`` was public
    (core 1.6 → 1.14). Retained so an older SDK pinned against an older core keeps working; new
    code calls :func:`register`.
    """
    register(model, rates)


# Wire-level id decorations stripped at LOOKUP time (the table keys stay bare). Alpha-only dotted
# prefixes cover Bedrock vendor/region namespaces (`anthropic.`, `us.anthropic.`) without touching
# in-name dots like `gpt-4.1` / `gemini-2.5-pro` (those have digits adjacent to the dot).
_PROVIDER_PREFIX_RE = re.compile(r"^(?:[a-z]+\.)+")
_BEDROCK_VERSION_RE = re.compile(r"-v\d+(?::\d+)?$")  # trailing `-v1:0` / `-v2`
_DATE_SUFFIX_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")  # `-20260115` / `-2025-11-13`


def _lookup_id(mid: str) -> str:
    """Reduce a wire-level model id to a bare table key, e.g.
    ``us.anthropic.claude-sonnet-4-6-20260115-v1:0`` → ``claude-sonnet-4-6`` and
    ``gpt-5.1-2025-11-13`` → ``gpt-5.1``. Applied only when the exact id misses the table."""
    s = _normalize_model_id(mid)
    s = _PROVIDER_PREFIX_RE.sub("", s)
    s = _BEDROCK_VERSION_RE.sub("", s)
    s = _DATE_SUFFIX_RE.sub("", s)
    return _ALIASES.get(s, s)


def _rates(model: str) -> dict:
    models = _ensure_loaded().get("models", {})
    r = models.get(model)
    if r is None:
        r = models.get(_lookup_id(model))  # Bedrock/dated/prefixed ids price like their base model
    if r is None:
        raise UnknownModelError(model)
    return r


def _is_registered(model: str) -> bool:
    """Did *you* write these rates with :func:`register`, rather than a table supplying them?

    The distinction is the whole reason a zero can be legal: the spec already says a user
    registration outranks any table, so ``register("llama3", {"input": 0, "output": 0})`` is a
    person stating a fact, while a ``0`` arriving inside a fetched table is a parser having lost
    one. Checked against the same two ids :func:`_rates` resolves.
    """
    return model in _registered or _lookup_id(model) in _registered


def _priceable(r: dict, model: str) -> None:
    """Refuse rates that cannot price a call, instead of quietly treating the gap as free.

    Applied whenever :func:`estimate` looks a model up — **not** only when the call happens to carry
    output tokens. A table that cannot price this model cannot price it, and finding that out on the
    first output-bearing call rather than the first call is exactly the kind of late, partial signal
    this rule exists to remove.

    Symmetric across the two rate keys that have no defined fallback (``cached`` and ``cache_write``
    do have one, stated in the spec, so their absence is a default and not a gap).
    """
    if "input" not in r:
        raise _missing_rate(model, "input", "has no INPUT rate")
    if Decimal(str(r["input"])) <= 0 and not _is_registered(model):
        raise _missing_rate(model, "input", "states a zero INPUT rate")
    if "output" not in r:
        raise _missing_rate(model, "output", "has no OUTPUT rate")


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

    The model must be **priceable**, not merely present: a rate the table leaves absent, or an
    input rate the table states as ``0``, raises :class:`MissingRateError` (a subclass of
    :class:`UnknownModelError`, so existing ``except KeyError`` handlers are unaffected). Unknown is
    not free — see the class docstring. State the rate with :func:`register_model_price`, or state
    an explicit ``output=0`` if the model really does bill nothing for output.

    Args:
        model: Model id; must exist in the table (else :class:`UnknownModelError`) and carry usable
            rates (else :class:`MissingRateError`).
        input_tokens: Billed input tokens (inclusive of ``cached_tokens``).
        output_tokens: Billed output tokens.
        cached_tokens: Cache-read tokens, a subset of ``input_tokens``; priced at the model's
            ``cached`` rate if present, else at the input rate.
        cache_write_tokens: Cache-*write* tokens (Anthropic ``cache_creation_input_tokens``), a
            *separate* category from ``input_tokens``; priced at the model's ``cache_write`` rate if
            present, else ~1.25× the input rate.

    Returns:
        The estimated :class:`~cendor.core.types.Money` cost in USD.

    ``output_tokens`` (and the cache args) are **positional** here — a documented divergence from
    the TypeScript port, where they ride an options object (``prices.estimate(model, n,
    {outputTokens})``).

    ```python
    from cendor.core import prices
    cost = prices.estimate("gpt-4o", 1200, output_tokens=300)   # -> Money("...", "USD")
    ```
    """
    r = _rates(model)
    _priceable(r, model)  # an absent or zero-in-a-table rate is UNKNOWN, never free
    cached = min(max(cached_tokens, 0), input_tokens)  # cached ⊆ input; clamp defensively
    input_rate = Decimal(str(r["input"]))
    cached_rate = Decimal(str(r["cached"])) if "cached" in r else input_rate
    write_rate = (
        Decimal(str(r["cache_write"])) if "cache_write" in r else input_rate * Decimal("1.25")
    )
    amount = (
        input_rate * (input_tokens - cached)
        + Decimal(str(r["output"])) * output_tokens
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
    """``"bundled"`` | ``"refreshed"`` | ``"loaded"`` — where the active table came from."""
    return _source


def source_name() -> str:
    """Finer provenance of the active table: ``"bundled"`` | ``"feed"`` | ``"azure"`` | ``"aws"``
    | ``"modelsdev"`` | ``"litellm"`` | ``"openrouter"`` | ``"vercel"`` | ``"custom"``.

    ``"feed"`` is a bare ``refresh()`` — the cendor-prices table. Use :func:`explain` for the
    per-row story."""
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
    """LiteLLM ``model_prices_and_context_window.json``: ``{id: {input_cost_per_token, ...}}``.

    ⚠️ litellm keys the same model many times, once per host — ``claude-3-5-haiku`` arrives as
    ``anthropic.claude-3-5-haiku-…`` ($0.80/$4, the lab rate), ``vertex_ai/claude-3-5-haiku``
    ($1/$5, Vertex's) and ``replicate/anthropic/claude-3.5-haiku``. A **bare** key names the model;
    a namespaced one names a host's listing of it, and must never overwrite the bare one (measured
    2026-08-01 — it published Vertex's price as Anthropic's). See :func:`_is_host_id`.
    """
    out: dict[str, dict] = {}
    bare: set[str] = set()
    for mid, rec in raw.items():
        if not isinstance(rec, dict) or "input_cost_per_token" not in rec:
            continue  # skips non-model entries like "sample_spec"
        if rec["input_cost_per_token"] is None:
            continue
        rates: dict[str, Decimal] = {"input": _dec(rec["input_cost_per_token"])}
        if rates["input"] <= 0:
            continue  # a $0 input rate makes estimate() report $0.00 as a fact and a cap never bind
        if rec.get("output_cost_per_token") is not None:
            rates["output"] = _dec(rec["output_cost_per_token"])
        if rec.get("cache_read_input_token_cost") is not None:
            rates["cached"] = _dec(rec["cache_read_input_token_cost"])
        if rec.get("cache_creation_input_token_cost") is not None:
            rates["cache_write"] = _dec(rec["cache_creation_input_token_cost"])
        key = _normalize_model_id(mid)
        if key in bare:
            continue
        if not _is_host_id(mid):
            bare.add(key)
        out[key] = rates
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


def _map_modelsdev(raw: dict) -> dict:
    """models.dev ``api.json``: ``{provider: {models: {id: {cost: {...}}}}}``, rates per **1M**.

    ⚠️ The payload is **provider → models**, and the same model id appears under many providers at
    different prices: measured 2026-08-01, ``gpt-5.1`` appears 11 times between $1.07 and $1.25 per
    MTok, and the providers with the most rows are all resellers (nano-gpt 617, kilo 346,
    openrouter 335, vercel 312). "Last one wins" would hand you a random reseller's resale price as
    the model's rate, so :data:`_MODELSDEV_PROVIDERS` is an allowlist with a fixed precedence, not a
    tidy-up filter.
    """
    out: dict[str, dict] = {}
    latest: str | None = None
    bare: set[str] = set()
    million = Decimal(1_000_000)
    for pid in reversed(_MODELSDEV_PROVIDERS):  # walk in reverse so the top of the list wins
        prov = raw.get(pid)
        if not isinstance(prov, dict):
            continue
        for mid, rec in (prov.get("models") or {}).items():
            cost = rec.get("cost") if isinstance(rec, dict) else None
            if not isinstance(cost, dict) or cost.get("input") is None:
                continue
            rates: dict[str, Decimal] = {}
            for src, dst in (
                ("input", "input"),
                ("output", "output"),
                ("cache_read", "cached"),
                ("cache_write", "cache_write"),
            ):
                if cost.get(src) is not None:
                    rates[dst] = _dec(cost[src]) / million
            if "input" not in rates or rates["input"] <= 0:
                continue
            key = _normalize_model_id(str(mid))
            # A host listing must never overwrite a direct naming (see :func:`_is_host_id`).
            # ⚠️ The ``and _is_host_id(...)`` is LOAD-BEARING. Without it this guard INVERTED the
            # precedence list above: when two allowlisted providers both key a model BARE, the
            # reverse walk writes the lower-precedence one first, it claims ``bare``, and the
            # higher-precedence one is skipped. Measured 2026-08-02 on the live payload —
            # ``refresh(source="modelsdev")`` returned azure's **$1/$6 deployment** price for
            # ``gpt-5.6-luna`` instead of OpenAI's own **$0.2/$1.2**. Four rows were affected, every
            # one of them a host's listing displacing the lab's:
            #   gpt-5.6-luna $1/$6 -> $0.2/$1.2 · gpt-5.6-terra $2.5/$15 -> $2/$12
            #   deepseek-v4-pro $1.74/$3.48 -> $0.435/$0.87
            #   deepseek-v4-flash $0.19/$0.51 -> $0.14/$0.28
            # The two rules collide only when both ids are bare; precedence decides that case.
            # (``_map_litellm`` keeps the plain guard on purpose — its payload is a flat dict with
            # no precedence order to appeal to, so "the first bare id wins" is the only rule there.)
            if key in bare and _is_host_id(str(mid)):
                continue
            if not _is_host_id(str(mid)):
                bare.add(key)
            out[key] = rates
            lu = str(rec.get("last_updated") or "")[:10]
            if len(lu) == 10 and (latest is None or lu > latest):
                latest = lu
    result: dict = {"models": out}
    if latest is not None:
        result["_updated"] = latest
    return result


def _map_vercel(raw: dict) -> dict:
    """Vercel AI Gateway ``/v1/models``: ``{"data": [{id, type, pricing: {input, output, ...}}]}``.

    Per-token rates as JSON **strings**. Filtered to ``type == "language"``. Base rates only — the
    catalog also carries ``input_tiers`` / ``service_tiers``, which are out of scope. Like
    OpenRouter these are **gateway resale** prices: what Vercel charges you, not what the lab does.
    No catalog-wide date ⇒ undatable, never stamped "today".
    """
    out: dict[str, dict] = {}
    for rec in raw.get("data", []):
        if not isinstance(rec, dict) or rec.get("type") != "language":
            continue
        pricing = rec.get("pricing") or {}
        if pricing.get("input") is None:
            continue
        rates: dict[str, Decimal] = {}
        for src, dst in (
            ("input", "input"),
            ("output", "output"),
            ("input_cache_read", "cached"),
            ("input_cache_write", "cache_write"),
        ):
            v = pricing.get(src)
            if v is not None and _dec(v) > 0:
                rates[dst] = _dec(v)
        if "input" not in rates:
            continue
        out[_normalize_model_id(str(rec.get("id", "")))] = rates
    return {"models": out}


# --------------------------------------------------------------------------------- azure (Foundry)

#: Azure writes one direction seven ways. ⚠️ **``opt`` means OUTPUT** — 141 rows on 2026-08-01, and
#: the pre-fix parser looked only for ``outp``/``output``, so every GPT-5.x family would have had an
#: input rate and no output rate. Proven by price: ``GPT 5.1 inp Gl`` 1.25/1M vs ``GPT 5.1 opt Gl``
#: 10.0/1M = GPT-5.1's published $1.25/$10.
_AZ_DIRECTION = {
    "inp": "input", "inpt": "input", "input": "input", "in": "input",
    "outp": "output", "outpt": "output", "output": "output", "out": "output", "opt": "output",
}  # fmt: skip
_AZ_CACHE_READ = {"cd", "cchd", "ccchd", "cached", "cache"}
_AZ_CACHE_WRITE = {"wr"}
#: Meters that are not a plain on-demand per-token inference rate: a different product, a different
#: SLA, or not per-token at all. Pricing them as the base rate would be wrong, not approximate.
#: (``l`` alone is the long-context tier — ``4.3 Inp Glbl L`` is 2x ``4.3 Inp Glbl``.)
_AZ_NOT_INFERENCE = {
    "batch", "ft", "finetuned", "training", "trng", "hosting", "pp", "ptu", "provisioned",
    "grader", "grdr", "img", "image", "aud", "audio", "rt", "realtime", "tts", "trscb", "tcrb",
    "transcribe", "ocr", "doc", "video", "speech", "shortco", "longco", "reservation",
    "embedding", "l",
}  # fmt: skip
_AZ_TIER = {
    "gl", "glbl", "global", "dz", "dzone", "datazone", "dzn", "regnl", "regional", "rgnl", "regn",
    "std", "zone", "data", "mn",
}  # fmt: skip
_AZ_PRODUCT_SKIP = {
    "Azure OpenAI Media", "Azure BFL Flux Models", "Managed Compute",
    "Azure AI Foundry Provisioned Throughput Reservation", "Azure OpenAI PP FT GPT4s",
    "Azure OpenAI Embedding",
}  # fmt: skip
#: ``productName`` → family root. A sku alone is ambiguous: ``4.3 Inp Glbl`` under *Azure Grok
#: Models* is ``grok-4.3`` and ``V4 Pro Inp glbl`` under *Azure Deepseek Models* is
#: ``deepseek-v4-pro``. ⚠️ Applied only when the parsed head does not already start with the root —
#: prefixing unconditionally turned ``o1``/``o3``/``o4-mini`` into ``gpt-o1``/``gpt-o3``/
#: ``gpt-o4-mini``, a regression against the pre-fix mapper. *Azure OpenAI* and *Azure OpenAI
#: Reasoning* carry full ids already, so they get no root.
_AZ_FAMILY_ROOT = {
    "Azure OpenAI GPT5": "gpt",
    "Azure Grok Models": "grok",
    "Azure Deepseek Models": "deepseek",
    "Azure Kimi": "kimi",
    "Azure Llama Models": "llama",
    "Azure Mistral Models": "mistral",
    "Qwen models": "qwen",
    "Azure Phi Models": "phi",
    "MAI Models": "mai",
    "Azure OpenAI OSS Models": "gpt-oss",
}


def _azure_unit_divisor(unit_of_measure: str) -> Decimal:
    """Tokens per price unit for an Azure ``unitOfMeasure`` (``"1K"`` / ``"1M"`` / ``"1000"``).

    ⚠️ Read it **per row**: measured 2026-08-01, eastus2 mixes 905 ``1K`` meters with 479 ``1M``
    ones in the same response.
    """
    u = (unit_of_measure or "").upper().replace(" ", "")
    if "1M" in u or "1000000" in u:
        return Decimal(1_000_000)
    return Decimal(1000)  # default + "1K"/"1000"


def _map_azure(raw: dict) -> dict:
    """Azure Retail Prices ``{"Items": [{skuName, productName, retailPrice, unitOfMeasure, ...}]}``.

    Parses the model + direction + cache role out of ``skuName``, drops every meter that is not a
    plain on-demand per-token rate, converts per-1K/1M to per-token ``Decimal``, and keeps the
    cheapest surviving rate per key (which resolves the Global / Data Zone / Regional tiers to
    Global). Imperfect SKU→id mapping is expected and documented: Microsoft's meter names are prose,
    so a few rows land under an id nothing will ever look up. Those are inert, not wrong.
    """
    by_model: dict[str, dict[str, Decimal]] = {}
    latest: str | None = None  # the newest source effectiveStartDate seen (real provenance date)
    for item in raw.get("Items", []):
        if item.get("productName") in _AZ_PRODUCT_SKIP:
            continue
        # Real Retail rows always carry a `type`; treat a missing one as Consumption so a hand-
        # written fixture stays valid. Only `Reservation` rows (6 of 1,526 in eastus2) are excluded
        # — a committed capacity price is not a per-call rate.
        if str(item.get("type") or "Consumption") != "Consumption":
            continue
        unit = str(item.get("unitOfMeasure", "")).strip().upper()
        if unit not in ("1K", "1M"):
            continue  # 1/Hour, 1 Second, 100, … are not token meters
        price = item.get("retailPrice")
        if price is None or _dec(price) <= 0:
            continue
        words = [w for w in re.split(r"[\s\-_]+", str(item.get("skuName", "")).lower()) if w]
        if any(w in _AZ_NOT_INFERENCE for w in words):
            continue

        direction: str | None = None
        cached = write = False
        head: list[str] = []
        for w in words:
            if w in _AZ_DIRECTION:
                direction = _AZ_DIRECTION[w]
                continue
            if w in _AZ_CACHE_READ:
                cached = True
                continue
            if w in _AZ_CACHE_WRITE:
                write = True
                continue
            if w in _AZ_TIER:
                continue
            if direction is None:
                head.append(w)
        if direction is None:
            continue
        if cached and direction == "input":
            key = "cache_write" if write else "cached"
        elif write:
            continue  # a cache-write row we cannot place — skip rather than guess
        else:
            key = direction

        while head and head[-1].isdigit() and len(head[-1]) in (3, 4, 8):
            head.pop()  # trailing bare snapshot-date token: "gpt 4o 1120" -> "gpt 4o"
        if not head:
            continue
        mid = "-".join(head)
        root = _AZ_FAMILY_ROOT.get(str(item.get("productName")))
        if root and not mid.startswith(root):
            mid = f"{root}-{mid}"
        mid = _normalize_model_id(mid)

        per_token = _dec(price) / _azure_unit_divisor(str(item.get("unitOfMeasure", "")))
        rates = by_model.setdefault(mid, {})
        if key not in rates or per_token < rates[key]:
            rates[key] = per_token

        eff = str(item.get("effectiveStartDate", ""))[:10]  # "2024-01-01T..." -> "2024-01-01"
        if len(eff) == 10 and (latest is None or eff > latest):
            latest = eff
    out = {mid: r for mid, r in by_model.items() if "input" in r}
    # Carry Azure's real effectiveStartDate when present (its genuine provenance), else undatable —
    # never fake "today", which would make a stale refresh look fresh to is_stale().
    result: dict = {"models": out}
    if latest is not None:
        result["_updated"] = latest
    return result


# ------------------------------------------------------------------------------------ aws (Bedrock)

#: ⚠️ usagetype fragments marking a different SLA or commitment — never the on-demand base rate.
#: Measured 2026-08-01: ``Claude Sonnet 4`` carries ``inferenceType: "Input tokens"`` on **both**
#: ``…-input-tokens-cross-region-global`` ($3/MTok) and ``…-input-tokens-cross-region-global-batch``
#: ($1.50/MTok), so a plain cheapest-wins over ``inferenceType`` publishes the batch price as the
#: standard one.
_AWS_NOT_ON_DEMAND = ("batch", "long-context", "reserved", "priority", "flex", "provisioned")
_AWS_UNITS = {
    "1k tokens": Decimal(1000),
    "1k token": Decimal(1000),
    "1m tokens": Decimal(1_000_000),
    "1m token": Decimal(1_000_000),
}


def _aws_rate_key(usagetype: object, inference_type: object) -> str | None:
    """Which rate a Bedrock price dimension is, from the ``usagetype`` first.

    ⚠️ ``inferenceType`` is not sufficient: the cache-write row carries ``inferenceType: null`` and
    only the usagetype says ``cache-write-input-token-count``.
    """
    u = str(usagetype or "").lower()
    if "cache-read" in u:
        return "cached"
    if "cache-write" in u:
        return "cache_write"
    if "input-token" in u:
        return "input"
    if "output-token" in u:
        return "output"
    it = str(inference_type or "").strip().lower()
    if it == "prompt cache read input tokens":
        return "cached"
    if it == "prompt cache write input tokens":
        return "cache_write"
    if it in ("input tokens", "text input tokens", "text input token"):
        return "input"
    if it in ("output tokens", "text output tokens", "text output token"):
        return "output"
    return None


def _aws_model_key(name: str) -> str:
    """Normalise an AWS ``attributes.model`` to the shape core's own lookup reduction produces.

    AWS names a model two ways in the same file: a **display name** with spaces
    (``Claude Sonnet 4.5``, ``Llama 3.3 70B``) and a **wire-ish id** with none (``gpt-oss-120b``,
    ``xai.grok-4.3``). A display name becomes what :func:`_lookup_id` yields from a Bedrock wire id
    (``us.anthropic.claude-sonnet-4-5-…-v1:0`` → ``claude-sonnet-4-5``): lowercase, ``N.M`` →
    ``N-M``, whitespace → ``-``. A wire-ish id only loses its dotted vendor prefix.

    Honest limit: a wire id carrying a suffix the display name lacks (``llama3-3-70b-instruct``)
    will not match — and is never guessed at.
    """
    s = name.strip()
    if " " not in s:
        return re.sub(r"^(?:[a-z0-9]+\.)+", "", s.lower())
    s = re.sub(r"(?<=\d)\.(?=\d)", "-", s.lower())
    return re.sub(r"[\s_]+", "-", s)


def _map_aws(raw: dict) -> dict:
    """AWS Bedrock price files, as fetched by :func:`_fetch_aws` → ``{"offers": [file, ...]}``.

    Rates live at ``terms.OnDemand[sku][*].priceDimensions[*].pricePerUnit.USD`` with a **per
    dimension** ``unit``; ``publicationDate`` is the table's real as-of date.
    """
    by_model: dict[str, dict[str, Decimal]] = {}
    published: str | None = None
    for data in raw.get("offers", []):
        p = str(data.get("publicationDate", ""))[:10]
        if len(p) == 10 and (published is None or p > published):
            published = p
        on_demand = (data.get("terms") or {}).get("OnDemand") or {}
        for sku, product in (data.get("products") or {}).items():
            attrs = product.get("attributes") or {}
            model = attrs.get("model")
            if not model:
                continue
            usagetype = str(attrs.get("usagetype") or "").lower()
            if any(frag in usagetype for frag in _AWS_NOT_ON_DEMAND):
                continue
            key = _aws_rate_key(attrs.get("usagetype"), attrs.get("inferenceType"))
            if key is None:
                continue
            for term in (on_demand.get(sku) or {}).values():
                for pd in (term.get("priceDimensions") or {}).values():
                    divisor = _AWS_UNITS.get(str(pd.get("unit", "")).strip().lower())
                    if divisor is None:
                        continue  # image / hour / TPM-Hour — not a token rate
                    usd = (pd.get("pricePerUnit") or {}).get("USD")
                    if usd is None:
                        continue
                    value = _dec(usd) / divisor
                    if value <= 0:
                        continue
                    mid = _aws_model_key(str(model))
                    rates = by_model.setdefault(mid, {})
                    if key not in rates or value < rates[key]:
                        rates[key] = value
    out = {mid: r for mid, r in by_model.items() if "input" in r}
    result: dict = {"models": out}
    if published is not None:
        result["_updated"] = published
    return result


# ------------------------------------------------------------------------------- source registry


def _get(url: str, timeout: float) -> dict:
    """One unauthenticated HTTPS GET → parsed JSON with ``Decimal`` numbers.

    ⚠️ Never gates on the HTTP status or the content-type, because neither is a signal here.
    Measured 2026-08-01: Azure answers a wrong ``$filter`` with **200 + ``{"Items": []}``**,
    models.dev answers a wrong path with **200 + ``text/html``**, Vercel answers a wrong path with
    **404 + valid JSON**, AWS serves its *good* index files as ``application/octet-stream``, and
    raw.githubusercontent serves the feed as ``text/plain``. Parse, then check shape — which is what
    :func:`refresh`'s ``data.get("models")`` truthiness test does.
    """
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "cendor-core/prices"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - http(s) only
        return _loads(resp.read().decode("utf-8"))


def _fetch_simple(url: str, timeout: float, region: str | None) -> dict:
    return _get(url, timeout)


def _fetch_azure(url: str, timeout: float, region: str | None) -> dict:
    """Paginate the Retail Prices API. Two pages for one region; capped so a filter that somehow
    matches everything cannot turn one ``refresh()`` into an unbounded crawl."""
    items: list = []
    next_url: str | None = url
    pages = 0
    while next_url and pages < 10:
        payload = _get(next_url, timeout)
        items.extend(payload.get("Items") or [])
        pages += 1
        next_url = payload.get("NextPageLink")
    return {"Items": items}


def _fetch_aws(url: str, timeout: float, region: str | None) -> dict:
    """Resolve each offer's region index, then fetch that region's file. Both offers, always."""
    reg = region or AWS_DEFAULT_REGION
    offers = []
    for offer in AWS_OFFERS:
        index = _get(
            f"{AWS_PRICING_HOST}/offers/v1.0/aws/{offer}/current/region_index.json", timeout
        )
        entry = (index.get("regions") or {}).get(reg)
        if not entry:
            continue  # a region one offer does not publish is not a failure of the other
        href = str(entry.get("currentVersionUrl") or "")
        if not href:
            continue
        offers.append(_get(href if href.startswith("http") else AWS_PRICING_HOST + href, timeout))
    return {"offers": offers}


@dataclass(frozen=True)
class _Source:
    """A built-in ``refresh(source=...)`` adapter: how to reach it, and how to map its reply."""

    url: str | Callable[[str | None], str]
    mapper: Callable[[dict], dict]
    fetch: Callable[[str, float, str | None], dict] = _fetch_simple
    #: Accepts ``refresh(..., region=...)``. The others ignore it rather than pretending.
    regional: bool = False

    def url_for(self, region: str | None) -> str:
        return self.url(region) if callable(self.url) else self.url


#: models.dev providers we accept, in precedence order (earlier wins a collision). See
#: :func:`_map_modelsdev` for why an allowlist is load-bearing here.
_MODELSDEV_PROVIDERS = (
    "openai", "anthropic", "google", "google-vertex", "xai", "deepseek", "mistral", "meta",
    "alibaba", "moonshotai", "cohere", "amazon-bedrock", "azure", "groq", "fireworks-ai",
    "huggingface",
)  # fmt: skip


def _is_host_id(mid: str) -> bool:
    """Is this source id namespaced to a **host** rather than naming the model itself?

    ⚠️ Measured 2026-08-01, and it published a wrong number before it was caught. litellm reaches
    ``claude-3-5-haiku`` through ``vertex_ai/claude-3-5-haiku`` — **Vertex's $1/$5**, not
    Anthropic's **$0.80/$4**. Stripping the namespace collapses a host's listing onto the bare id.
    A direct naming outranks a host listing; the host case is what ``register_model_price`` /
    ``register_deployment`` exist for, and core's lookup reduction still matches a Bedrock/Vertex
    *wire* id onto the bare row at call time.
    """
    s = mid.strip().lower()
    return "/" in s or bool(re.match(r"^(?:[a-z][a-z0-9_-]*\.)+[a-z]", s))


#: Built-in live sources, all unauthenticated HTTPS GET → JSON. ``azure`` and ``aws`` are the
#: providers' own billing catalogs (first-party facts); ``modelsdev`` and ``litellm`` are MIT
#: aggregators; ``openrouter`` and ``vercel`` are gateways quoting their own **resale** prices.
_SOURCES: dict[str, _Source] = {
    "litellm": _Source(LITELLM_URL, _map_litellm),
    "openrouter": _Source(OPENROUTER_URL, _map_openrouter),
    "modelsdev": _Source(MODELSDEV_URL, _map_modelsdev),
    "vercel": _Source(VERCEL_URL, _map_vercel),
    "azure": _Source(
        lambda region: azure_url(region or AZURE_DEFAULT_REGION),
        _map_azure,
        _fetch_azure,
        regional=True,
    ),
    "aws": _Source(AWS_URL, _map_aws, _fetch_aws, regional=True),
}


def sources() -> list[str]:
    """Names of the built-in live price sources accepted by ``refresh(source=...)``.

    ``["aws", "azure", "litellm", "modelsdev", "openrouter", "vercel"]``. ``azure`` and ``aws``
    additionally accept ``region=``.

    ```python
    from cendor.core import prices
    prices.refresh(source="aws", region="eu-west-1")
    ```
    """
    return sorted(_SOURCES)


def refresh(
    url: str | None = None,
    *,
    source: str | None = None,
    mapper: Callable[[dict], dict] | None = None,
    timeout: float = 5.0,
    region: str | None = None,
    required: bool = False,
) -> bool:
    """Replace the table from a live source or static JSON URL. Never raises; offline-safe.

    With no arguments this fetches the **cendor-prices feed** (:data:`SNAPSHOT_URL`) — a dated,
    per-row-provenanced table reconciled from the cloud catalogs and the MIT aggregators.

    Args:
        url: A static JSON URL in *our* schema (``{"models": {...}}``). Defaults to
            :data:`SNAPSHOT_URL` when neither ``url`` nor ``source`` is given.
        source: A built-in adapter name — one of :func:`sources` (``"azure"``, ``"aws"``,
            ``"modelsdev"``, ``"litellm"``, ``"openrouter"``, ``"vercel"``). Takes precedence over
            ``url``; selects that source's URL + mapper.
        mapper: A custom ``raw_json -> {"models": {...}}`` callable (overrides the source's mapper);
            use it to map any other source onto our schema.
        timeout: Per-request timeout in seconds.
        region: Cloud region for the ``azure`` / ``aws`` sources (default ``eastus2`` /
            ``us-east-1``). Ignored by the others.
        required: ``True`` raises :class:`PriceRefreshError` instead of returning ``False``. Use it
            when running on stale rates would be worse than not running at all. **Never the
            default** — ``refresh()`` is contractually never-raise so a library import can't take an
            app down when a CDN blips.

    Returns:
        ``True`` if the table was updated, ``False`` if the fetch/parse/map failed (the bundled or
        last-good snapshot stays active — a failure never reverts anything). The fetched table lives
        in memory only; nothing is persisted unless you call :func:`save`. docs/core.md §7.

    ```python
    from cendor.core import prices

    prices.refresh()                                  # the cendor-prices feed
    prices.refresh(source="aws", region="eu-west-1")  # Bedrock's own price file
    prices.refresh(required=True)                     # raise instead of degrading silently
    ```
    """
    global _table, _source, _source_name, _source_url
    adapter: Callable[[dict], dict] | None
    # Annotated: without it mypy infers the *function* type of `_fetch_simple` rather than the
    # protocol, and the reassignment below is then an error.
    fetch: Callable[[str, float, str | None], dict] = _fetch_simple
    if source is not None:
        entry = _SOURCES.get(source)
        if entry is None:
            if required:
                raise PriceRefreshError(
                    f"unknown price source {source!r}; expected one of {sources()}"
                )
            return False  # unknown source name -> no-op, keep current table
        target = entry.url_for(region)
        adapter = mapper or entry.mapper
        fetch = entry.fetch
        name = source
    else:
        target = url or SNAPSHOT_URL
        adapter = mapper
        name = "custom" if url else "feed"
    if not target or not target.lower().startswith(("http://", "https://")):
        if required:
            raise PriceRefreshError(f"price source must be an http(s) URL, got {target!r}")
        return False  # fetch static JSON over http(s) only — never file://, ftp://, etc.
    try:
        raw = fetch(target, timeout, region)
        data = adapter(raw) if adapter is not None else raw
        if adapter is not None and isinstance(data, dict):
            # A MAPPED source only. These adapters are the deliberate twins of the cendor-prices
            # builder's, and the feed already applies this rule (`zero.mjs`) before publishing —
            # so a row our own mapper cannot price should be absent here for the same reason it
            # is absent there. A pass-through `refresh(url=…)` is a TABLE, not a mapper: every row a
            # user's own table states and let `estimate()` refuse the unpriceable ones by name.
            _drop_unpriceable(data.get("models") or {})
        if isinstance(data, dict) and data.get("models"):
            _install(data, "refreshed", name, target)
            return True
        detail = "the source returned no models (a wrong filter or a changed shape answers 200)"
    except Exception as exc:  # noqa: BLE001 - the never-raise contract; re-raised only if required
        if required:
            raise PriceRefreshError(f"price refresh from {target!r} failed: {exc}") from exc
        return False
    if required:
        raise PriceRefreshError(f"price refresh from {target!r} failed: {detail}")
    return False


def _drop_unpriceable(models: dict) -> list[str]:
    """Drop rows a mapped source produced that cannot price a call. In place; returns the ids.

    The library mirror of ``cendor-prices``' ``dropZeroInput`` + ``dropMissingOutput``. Measured
    2026-08-02 against the live payloads: ``refresh(source="litellm")`` produced **10** rows with no
    output rate — including ``gpt-image-1``, which OpenAI bills at $40 per 1M output tokens, so
    ``estimate("gpt-image-1", 1M, 1M)`` answered **$5.00** where the truth is **$45.00** — and
    ``refresh(source="azure")`` produced one (``fw-deepseek-v4-pro-ch``).

    A model no source can price is honestly **absent**, which is the plain ``UnknownModelError`` a
    caller already handles, rather than a half-priced row that survives to under-report money. An
    output rate a source explicitly states as ``0`` is kept: embeddings really do have one.
    """
    dropped = [
        mid
        for mid, r in models.items()
        if not isinstance(r, dict)
        or r.get("input") is None
        or Decimal(str(r["input"])) <= 0
        or r.get("output") is None
    ]
    for mid in dropped:
        del models[mid]
    return dropped


def _coerce_rates(models: dict) -> None:
    """Force every rate in a swapped-in table to a ``Decimal``, in place.

    A **pass-through** ``refresh(url)`` — no mapper, the caller pointing at any ``prices/1`` JSON —
    hands the parsed rate objects straight to :func:`estimate`. ``json.loads(parse_float=Decimal)``
    turns a JSON *number* into a ``Decimal`` but leaves a JSON *string* a string, so a table that
    quotes its rates (a perfectly reasonable authoring choice) would otherwise reach the arithmetic
    as text. ``estimate`` already coerces defensively on every read; this does it once at the swap
    so ``explain()`` also hands callers real ``Decimal``s. (Measured 2026-08-01: the TypeScript twin
    had no such per-read coercion and threw ``inputRate.times is not a function``.)
    """
    for rates in models.values():
        if not isinstance(rates, dict):
            continue
        for k, v in list(rates.items()):
            if not isinstance(v, Decimal):
                try:
                    rates[k] = Decimal(str(v))
                except Exception:  # noqa: BLE001 - a non-numeric rate is dropped, never guessed
                    del rates[k]


def _install(data: dict, kind: str, name: str, url: str | None) -> None:
    """Publish a new table atomically for concurrent ``estimate()`` readers."""
    global _table, _source, _source_name, _source_url
    _coerce_rates(data.get("models") or {})
    with _table_lock:
        if _registered:  # programmatic registrations survive every table swap (see register)
            data.setdefault("models", {}).update(_registered)
        _table = data
        _source = kind
        _source_name = name
        _source_url = url


# ------------------------------------------------------------------------- explain / save / load


@dataclass(frozen=True)
class PriceExplanation:
    """Where one model's rates came from — the answer to *"why is my cost that number?"*.

    Returned by :func:`explain`. Field names are snake_case here and camelCase in
    ``@cendor/core``'s ``prices.explain`` (the same documented divergence as ``snapshot_date`` /
    ``snapshotDate``).
    """

    model: str
    """The id you asked about, verbatim."""
    resolved: str | None
    """The table key that answered, or ``None`` if nothing did."""
    how: Literal["exact", "normalized", "registered", "unpriced"]
    """``"registered"`` — your own ``register*`` call is in effect (it overrides every table).
    ``"exact"`` — the id is a table key. ``"normalized"`` — a wire-level id was reduced to its base
    (``us.anthropic.claude-…-v1:0`` → ``claude-sonnet-4-6``). ``"unpriced"`` — no rate exists, and
    :func:`estimate` would raise."""
    rates: dict[str, Decimal] | None
    """Per-token USD rates, or ``None`` when unpriced."""
    registered: bool
    """A user registration is in effect for this id."""
    source_name: str
    """Provenance of the whole table: ``"bundled"`` | ``"feed"`` | ``"azure"`` | …"""
    source_url: str | None
    table_origin: str
    """``"bundled"`` | ``"refreshed"`` | ``"loaded"``."""
    snapshot_date: str | None
    age_days: int | None
    row_source: str | None = None
    """Per-row provenance from the feed's ``_provenance`` map: which source this rate came from."""
    row_asof: str | None = None
    """That source's own as-of date for this rate — not the day it was fetched."""
    notes: tuple[str, ...] = field(default_factory=tuple)
    """Honest caveats that apply to this answer (resale pricing, staleness, …)."""

    def summary(self) -> str:
        """One human-readable line, for a log or a CLI."""
        if self.rates is None:
            return f"{self.model}: no price in the {self.source_name} table — cost will be None"
        rates = " ".join(f"{k}={v}" for k, v in sorted(self.rates.items()))
        via = "" if self.resolved == self.model else f" (via {self.resolved})"
        prov = f"{self.row_source or self.source_name}"
        asof = self.row_asof or self.snapshot_date or "undated"
        return f"{self.model}{via}: {rates} — {self.how}, from {prov} as of {asof}"


#: Sources whose numbers are what a **gateway** charges for reselling a model, not what the lab
#: charges. Surfaced by :func:`explain` rather than buried in the docs.
_RESALE_SOURCES = {"openrouter", "vercel"}


def explain(model: str) -> PriceExplanation:
    """Explain where ``model``'s rates come from: the resolved id, the rates, and the provenance.

    The visibility half of *"if the live price is wrong, the user can overwrite it"*: an override
    already wins (:func:`register`), and this shows you whether one is in effect, which table
    answered, which source that row came from, and how old it is.

    ```python
    from cendor.core import prices

    prices.refresh()
    print(prices.explain("gpt-4o").summary())
    # gpt-4o: cached=1.25E-6 input=2.5E-6 output=1E-5 — exact, from azure as of 2026-07-01
    ```

    Args:
        model: The id a call reports (a deployment name, a Bedrock wire id, anything).

    Returns:
        A :class:`PriceExplanation`. Never raises — an unpriced model is an answer, not an error.
    """
    table = _ensure_loaded()
    models = table.get("models", {})
    mid = str(model)
    resolved: str | None
    how: Literal["exact", "normalized", "registered", "unpriced"]
    if mid in models:
        resolved, how = mid, "exact"
    else:
        reduced = _lookup_id(mid)
        resolved, how = (reduced, "normalized") if reduced in models else (None, "unpriced")
    registered = resolved is not None and resolved in _registered
    if registered:
        how = "registered"
    rates = dict(models[resolved]) if resolved is not None else None
    prov = table.get("_provenance") or {}
    row = prov.get(resolved) if resolved is not None else None
    notes: list[str] = []
    if registered:
        notes.append(
            "a register()/register_model_price()/register_deployment() call overrides "
            "every table for this id, including after a refresh()"
        )
    if _source_name in _RESALE_SOURCES:
        notes.append(
            f"{_source_name} publishes gateway RESALE prices — what the gateway charges "
            "you, which may differ from the model lab's own rate"
        )
    age = age_days()
    if age is not None and age > 45:
        notes.append(f"this table is {age} days old; call refresh() for current rates")
    if snapshot_date() is None:
        notes.append(
            "this source publishes no as-of date, so staleness cannot be measured "
            "(is_stale() reports False, which means unknown, not fresh)"
        )
    if how == "unpriced":
        notes.append(
            "estimate() raises UnknownModelError and tokenguard records $0 — register a "
            "rate with prices.register_model_price(...) or prices.register_deployment(...)"
        )
    return PriceExplanation(
        model=mid,
        resolved=resolved,
        how=how,
        rates=rates,
        registered=registered,
        source_name=_source_name,
        source_url=_source_url,
        table_origin=_source,
        snapshot_date=snapshot_date(),
        age_days=age,
        row_source=(row or {}).get("src") if isinstance(row, dict) else None,
        row_asof=(row or {}).get("asof") if isinstance(row, dict) else None,
        notes=tuple(notes),
    )


def save(path: str) -> str:
    """Write the **active** table to ``path`` so a later process can :func:`load` it. Opt-in.

    ``refresh()`` is in-memory only, per process: a short-lived or serverless worker starts at the
    bundled snapshot every time and must fetch again. This is the explicit escape hatch — a path
    *you* choose, written when *you* ask. There is deliberately **no implicit cache**: a library
    quietly writing price files is a side effect, and a hidden cache is exactly how prices go
    *invisibly* stale.

    Provenance rides along, so ``explain()`` and ``age_days()`` stay honest after a ``load()`` —
    the saved file records the original source and its ``_updated``, never the moment you saved.

    ```python
    from cendor.core import prices

    prices.refresh()
    prices.save(".cache/cendor-prices.json")     # in your deploy step
    # ... a later process:
    prices.load(".cache/cendor-prices.json")     # no network
    ```

    Args:
        path: Destination file. Parent directories are created.

    Returns:
        The path written.

    Raises:
        OSError: If the file cannot be written. Unlike :func:`refresh`, this one is explicit, so it
            reports failure the normal way.
    """
    from pathlib import Path

    table = _ensure_loaded()
    payload = {
        "_note": "Saved by cendor.core.prices.save(). Restore with prices.load(path).",
        "_schema": "prices/1",
        "_saved": {"source_name": _source_name, "source_url": _source_url, "origin": _source},
        # `format(d, "f")`, not `str(d)`: str renders 1.23e-7 in scientific notation. Both
        # round-trip exactly, but a plain decimal literal is what the price-dataset spec and the
        # cendor-prices feed use, so a saved file is diffable against them.
        "models": {
            k: {kk: format(_dec(vv), "f") for kk, vv in v.items()}
            for k, v in table.get("models", {}).items()
        },
    }
    if table.get("_updated"):
        payload["_updated"] = table["_updated"]
    if table.get("_provenance"):
        payload["_provenance"] = table["_provenance"]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return str(p)


def load(path: str) -> bool:
    """Load a table previously written by :func:`save`. Opt-in, explicit, no network.

    Registrations are re-applied on top exactly as they are after a ``refresh()``, and the file's
    recorded source name / URL / ``_updated`` are restored so :func:`explain` and :func:`age_days`
    describe where the rates *came from*, not where they were read from. ``source()`` then reports
    ``"loaded"``.

    ```python
    from cendor.core import prices
    if not prices.load(".cache/cendor-prices.json"):
        prices.refresh()      # no saved table yet, or it was unreadable
    ```

    Args:
        path: A file written by :func:`save` (or any ``prices/1`` JSON).

    Returns:
        ``True`` if the table was replaced, ``False`` if the file was missing, unreadable or empty
        — the same never-raise, keep-the-last-good contract as :func:`refresh`.
    """
    from pathlib import Path

    try:
        data = _loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict) or not data.get("models"):
        return False
    # `save()` writes rates as strings (a plain decimal literal, diffable against the feed);
    # `_install`'s `_coerce_rates` turns them back into Decimals.
    saved = data.get("_saved") or {}
    _install(data, "loaded", str(saved.get("source_name") or "custom"), saved.get("source_url"))
    return True


def _reset() -> None:
    """Test helper: drop the loaded table (and registrations) so the bundled snapshot reloads."""
    global _table, _source, _source_name, _source_url
    _table = None
    _source = "bundled"
    _source_name = "bundled"
    _source_url = None
    _registered.clear()


def __getattr__(name: str) -> object:  # PEP 562 — teach the common wrong guess
    """Turn a near-miss price-registration name into a helpful error, not a bare AttributeError.

    ``register`` and ``register_model_price`` are **real functions** here since core 1.15 (parity
    with ``@cendor/core``'s ``prices.register``), so they never reach this hook. What still misses
    is the *counter* confusion and a couple of plausible spellings: ``cendor.core.tokens.register``
    registers a token counter, not a price.
    """
    if name in ("register_price", "registerModelPrice", "add_price", "set_price"):
        raise AttributeError(
            f"cendor.core.prices has no {name!r}. Register a price with "
            "cendor.core.prices.register_model_price(model, input=..., output=..., per='1M') "
            "(per-1M rates) or cendor.core.prices.register(model, {'input': ..., 'output': ...}) "
            "(per-token); cendor.core.tokens.register(fam, counter) registers a token counter, "
            "not a price."
        )
    if name in ("cache", "cache_path", "persist", "save_to_disk", "load_from_disk"):
        raise AttributeError(
            f"cendor.core.prices has no {name!r}. refresh() is in-memory only and never writes a "
            "hidden cache; persistence is explicit — cendor.core.prices.save(path) then "
            "cendor.core.prices.load(path) in the next process."
        )
    if name in ("refresh_async", "arefresh"):
        raise AttributeError(
            f"cendor.core.prices has no {name!r}. Python's refresh() is SYNCHRONOUS (urllib); the "
            "async form is the TypeScript one, `await prices.refresh()` in @cendor/core. Call it "
            "once at startup, or from a thread if you must not block the loop."
        )
    if name in ("why", "describe", "provenance", "source_of"):
        raise AttributeError(
            f"cendor.core.prices has no {name!r}. To see where a model's rates came from, use "
            "cendor.core.prices.explain(model) -> PriceExplanation (.rates, .row_source, "
            ".row_asof, .registered, .summary())."
        )
    if name in ("register_alias", "alias", "map_deployment", "registerDeployment"):
        raise AttributeError(
            f"cendor.core.prices has no {name!r}. To price an Azure/Foundry deployment name like "
            "the model it serves, use "
            "cendor.core.prices.register_deployment(deployment, like='gpt-4o')."
        )
    raise AttributeError(f"module 'cendor.core.prices' has no attribute {name!r}")
