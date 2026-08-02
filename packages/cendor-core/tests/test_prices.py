"""Prices: exact Decimal estimates from the bundled snapshot + offline refresh fallback."""

import re
from decimal import Decimal

import pytest
from cendor.core import prices
from cendor.core.types import Money


@pytest.fixture(autouse=True)
def _fresh_table():
    prices._reset()
    yield
    prices._reset()


def test_estimate_is_exact_decimal_money():
    # 0.000005*1200 + 0.000025*300 = 0.006 + 0.0075 = 0.0135
    cost = prices.estimate("claude-opus-4-8", input_tokens=1200, output_tokens=300)
    assert isinstance(cost, Money)
    assert cost.amount == Decimal("0.0135")


def test_estimate_includes_cached_rate():
    # cached ⊆ input, so the 200 cached tokens are billed once at the cached rate, not on top of
    # the input rate: 0.0000025*(1000-200) + 0.00001*500 + 0.00000125*200
    #               = 0.002 + 0.005 + 0.00025 = 0.00725
    cost = prices.estimate("gpt-4o", 1000, 500, cached_tokens=200)
    assert cost.amount == Decimal("0.00725")


def test_estimate_cached_not_double_charged():
    # Regression for the money bug: cached tokens must not be billed at input_rate AND cached_rate.
    # gpt-4o, 1000 input / 200 cached, no output: correct = 0.0000025*800 + 0.00000125*200 = 0.00225
    cost = prices.estimate("gpt-4o", 1000, cached_tokens=200)
    assert cost.amount == Decimal("0.00225")


def test_estimate_cached_never_exceeds_uncached_cost():
    # Property: when a cached rate exists and is below the input rate, quoting cached tokens can
    # only lower (never raise) the estimate vs. treating them as ordinary input.
    with_cache = prices.estimate("gpt-4o", 1000, 500, cached_tokens=200)
    without = prices.estimate("gpt-4o", 1000, 500, cached_tokens=0)
    assert with_cache.amount <= without.amount
    assert with_cache.amount < without.amount  # strict, since gpt-4o's cached rate < input rate


def test_estimate_cached_clamped_to_input():
    # Defensive clamp: cached tokens can never exceed input tokens, so a bogus cached count that
    # overshoots input is capped (the whole input is billed at the cached rate, nothing negative).
    clamped = prices.estimate("gpt-4o", 1000, cached_tokens=5000)
    all_cached = prices.estimate("gpt-4o", 1000, cached_tokens=1000)
    assert clamped.amount == all_cached.amount
    assert clamped.amount == Decimal("0.00000125") * 1000


def test_estimate_cached_without_published_rate_bills_input_rate():
    # A model with no `cached` rate must fall back to the input rate for cache reads — i.e. the
    # cached count makes no difference — never underbilling to zero for the cached portion.
    prices._table = {
        "_updated": "2026-06-26",
        "models": {"nocache": {"input": Decimal("0.000002"), "output": Decimal("0.000008")}},
    }
    with_cache = prices.estimate("nocache", 1000, 100, cached_tokens=300)
    without = prices.estimate("nocache", 1000, 100, cached_tokens=0)
    assert (
        with_cache.amount
        == without.amount
        == Decimal("0.000002") * 1000 + Decimal("0.000008") * 100
    )


def test_unknown_model_raises():
    with pytest.raises(prices.UnknownModelError):
        prices.estimate("does-not-exist", 100)


# ------------------------------------------------------- unknown is not zero (prices/1, 2026-08-02)
#
# `prices/1` used to read an absent `output` as 0. Right for an embedding, wrong for a chat model
# whose rate never parsed — and the two are indistinguishable downstream, so `estimate()` reported a
# fabricated $0.00 as a FACT and a USD cap under-counted by the whole output side. Measured on the
# shipped 1.19.2: after `refresh(source="litellm")`, `estimate("gpt-image-1", 1M, 1M)` returned
# $5.00 where OpenAI's own rates make it $45.00.


def _table(models):
    """Install a table exactly as a pass-through `refresh(url=…)` would."""
    prices._install({"_updated": "2026-08-02", "models": models}, "refreshed", "custom", "http://x")


def test_a_missing_output_rate_is_unknown_not_free():
    _table({"chatty": {"input": Decimal("0.000005")}})
    with pytest.raises(prices.MissingRateError) as ei:
        prices.estimate("chatty", 1_000_000, 1_000_000)
    msg = str(ei.value)
    assert "no OUTPUT rate" in msg
    assert "register_model_price" in msg, "the error must name the fix, in the caller's own code"
    assert "output=0 is honoured" in msg


def test_the_refusal_does_not_wait_for_an_output_bearing_call():
    """D2: refuse whenever the model is priced, not only when output tokens happen to be present.

    A table that cannot price this model cannot price it. Finding that out on the first
    output-bearing call rather than the first call is a late, partial signal.
    """
    _table({"chatty": {"input": Decimal("0.000005")}})
    with pytest.raises(prices.MissingRateError):
        prices.estimate("chatty", 1_000_000)  # no output tokens at all


def test_missing_rate_error_is_catchable_as_unknown_model_and_keyerror():
    """Every existing handler keeps working: instrument/otel/langchain/tokenguard catch KeyError."""
    _table({"chatty": {"input": Decimal("0.000005")}})
    assert issubclass(prices.MissingRateError, prices.UnknownModelError)
    assert issubclass(prices.MissingRateError, KeyError)
    with pytest.raises(prices.UnknownModelError):
        prices.estimate("chatty", 10)
    with pytest.raises(KeyError):
        prices.estimate("chatty", 10)


def test_an_explicit_zero_output_rate_is_honoured_forever():
    """NEGATIVE CONTROL for the rule above: a STATED zero is a real embedding price, not a gap.
    18 rows in the bundled snapshot depend on this."""
    _table({"embedder": {"input": Decimal("0.00000002"), "output": Decimal(0)}})
    assert prices.estimate("embedder", 1000, 1000).amount == Decimal("0.00002")
    # ...and the shipped snapshot's real embeddings keep pricing.
    prices._reset()
    assert prices.estimate("text-embedding-3-small", 1000, 0).amount == Decimal("0.00002")


def test_a_table_zero_input_rate_is_refused_but_a_registered_one_is_honoured():
    """D5. A zero that arrived in a TABLE is a parser having lost a rate; a zero YOU registered is a
    person stating a fact. The spec already says a user registration outranks any table, and
    1.19.0 documented `register("llama3", {"input": 0, "output": 0})` as pricing one free."""
    _table({"llama3": {"input": Decimal(0), "output": Decimal(0)}})
    with pytest.raises(prices.MissingRateError, match="zero INPUT rate"):
        prices.estimate("llama3", 1000, 500)

    prices.register("llama3", {"input": 0, "output": 0})
    assert prices.estimate("llama3", 1000, 500).amount == Decimal(0)


def test_a_missing_input_key_raises_the_typed_error_not_a_bare_keyerror():
    _table({"headless": {"output": Decimal("0.00001")}})
    with pytest.raises(prices.MissingRateError, match="no INPUT rate"):
        prices.estimate("headless", 1000)


def test_register_model_price_is_the_documented_escape_and_it_works():
    _table({"gpt-image-1": {"input": Decimal("0.000005")}})
    with pytest.raises(prices.MissingRateError):
        prices.estimate("gpt-image-1", 1_000_000, 1_000_000)
    # OpenAI's published rates: $5/1M text in, $40/1M image out.
    prices.register_model_price("gpt-image-1", input=5, output=40, per="1M")
    assert prices.estimate("gpt-image-1", 1_000_000, 1_000_000).amount == Decimal(45)


def test_register_deployment_refuses_an_unpriceable_base():
    """Copying an unpriceable row onto a deployment reproduces the exact silence the function
    exists to remove, so it fails at registration rather than on the first call."""
    _table({"half-priced": {"input": Decimal("0.000005")}})
    with pytest.raises(prices.MissingRateError):
        prices.register_deployment("prod-eastus", like="half-priced")
    assert "prod-eastus" not in prices.models()


def test_a_mapped_source_drops_a_row_it_cannot_price(monkeypatch):
    """D4 — the library mirror of the feed's `dropMissingOutput`. Measured 2026-08-02 against the
    live payload: `refresh(source="litellm")` produced 10 such rows, `gpt-image-1` among them."""
    raw = """
    {"gpt-4o":      {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001},
     "gpt-image-1": {"input_cost_per_token": 0.000005}}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="litellm") is True
    assert "gpt-4o" in prices.models()
    assert "gpt-image-1" not in prices.models(), (
        "an unpriceable mapped row is absent, not half-priced"
    )
    with pytest.raises(prices.UnknownModelError):  # the plain one — the model is simply not there
        prices.estimate("gpt-image-1", 1000, 500)


def test_a_pass_through_table_keeps_every_row_and_estimate_refuses(monkeypatch):
    """The other half of D4: a `refresh(url=…)` is a TABLE, not a mapper. We do not quietly discard
    rows from a table the user chose — `estimate()` refuses the unpriceable ones, by name."""
    _install_fetch(monkeypatch, '{"models": {"chatty": {"input": 0.000005}}}')
    assert prices.refresh(url="https://example.test/p.json") is True
    assert "chatty" in prices.models()
    with pytest.raises(prices.MissingRateError):
        prices.estimate("chatty", 1000, 500)


def test_every_model_the_bundled_snapshot_lists_can_actually_be_priced():
    """The invariant the whole rule exists to protect. If this ever fails, the generated snapshot
    published a row no caller can use — which is what shipped in <= 1.19.1."""
    unpriceable = []
    for mid in prices.models():
        try:
            prices.estimate(mid, 1000, 1000)
        except prices.UnknownModelError:
            unpriceable.append(mid)
    assert unpriceable == [], f"{len(unpriceable)} rows cannot be priced: {unpriceable[:10]}"


def test_lookup_normalizes_wire_level_ids():
    # Bedrock modelId (vendor + region prefixes, -vN:0 suffix) prices like the base model.
    base = prices.estimate("claude-sonnet-4-6", 1000, 500)
    assert prices.estimate("anthropic.claude-sonnet-4-6-v1:0", 1000, 500) == base
    assert prices.estimate("us.anthropic.claude-sonnet-4-6-20260115-v1:0", 1000, 500) == base
    # Anthropic dated ids and OpenAI dated snapshots also resolve.
    assert prices.estimate("claude-sonnet-4-6-20260115", 1000, 500) == base
    assert prices.estimate("gpt-5.1-2025-11-13", 1000, 500) == prices.estimate("gpt-5.1", 1000, 500)
    # Normalization never invents a price: decorated unknowns still raise.
    with pytest.raises(prices.UnknownModelError):
        prices.estimate("us.anthropic.claude-nonexistent-v1:0", 100)


def test_bundled_snapshot_metadata():
    assert prices.source() == "bundled"
    assert prices.source_name() == "bundled"
    assert prices.source_url() is None
    assert "claude-opus-4-8" in prices.models()
    # The snapshot is GENERATED from the cendor-prices feed by `scripts/sync_prices.py`, so its date
    # moves on every regeneration. Asserting a literal here would turn every refresh into a red
    # test and teach the next maintainer to edit the assertion rather than look at the data. Assert
    # the CONTRACT instead: it is datable, parseable, and not from the future.
    d = prices.snapshot_date()
    assert d is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d), d
    age = prices.age_days()
    assert age is not None and age >= 0, f"snapshot dated in the future: {d}"
    # And it is a real generated table, not the old hand-fed 44-row one.
    assert len(prices.models()) > 400


def test_o1_family_is_priced():
    # family() claims "o1"; the snapshot must carry it (and o1-mini/o3-mini) so it's priceable.
    for mid in ("o1", "o1-mini", "o3-mini"):
        assert mid in prices.models()
    # o1: 0.000015*1000 + 0.00006*500 = 0.015 + 0.03 = 0.045
    assert prices.estimate("o1", 1000, 500).amount == Decimal("0.045")


def test_cache_write_priced_at_explicit_rate():
    # claude-opus-4-8 has an explicit cache_write rate (0.00000625). cache_write is a separate
    # category (not part of input): 0.000005*1000 + 0.00000625*200 = 0.005 + 0.00125 = 0.00625
    cost = prices.estimate("claude-opus-4-8", 1000, cache_write_tokens=200)
    assert cost.amount == Decimal("0.00625")


def test_cache_write_defaults_to_1_25x_input_when_no_rate():
    # gpt-4o has no cache_write rate -> ~1.25x input: 1.25 * 0.0000025 = 0.000003125 per token.
    cost = prices.estimate("gpt-4o", 1000, cache_write_tokens=100)
    # 0.0000025*1000 + 0.000003125*100 = 0.0025 + 0.0003125 = 0.0028125
    assert cost.amount == Decimal("0.0028125")


def test_refresh_uses_default_snapshot_url(monkeypatch):
    import contextlib
    import io
    import json

    seen = {}
    payload = json.dumps(
        {"_updated": "2099-02-02", "models": {"gpt-4o": {"input": 0.002, "output": 0}}}
    )

    @contextlib.contextmanager
    def fake_urlopen(url, timeout=5.0):
        seen["url"] = getattr(url, "full_url", url)
        yield io.BytesIO(payload.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert prices.refresh() is True  # no url -> uses SNAPSHOT_URL
    assert seen["url"] == prices.SNAPSHOT_URL
    assert prices.source() == "refreshed"


def test_refresh_falls_back_silently_when_offline(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert prices.refresh() is False  # default URL unreachable -> keep bundled
    assert prices.source() == "bundled"
    # bundled table still usable
    assert prices.estimate("gpt-4o", 1000).amount == Decimal("0.0025")


def test_table_rates_are_decimal_not_float():
    # Money rule: per-token rates are exact Decimals from the JSON text, never via float.
    rate = prices._ensure_loaded()["models"]["gpt-4o"]["input"]
    assert isinstance(rate, Decimal)


def test_refresh_rejects_non_http_scheme(tmp_path):
    # refresh fetches static JSON over http(s) only — a file:// URL must not load a local file.
    p = tmp_path / "evil.json"
    p.write_text('{"models": {"evil": {"input": 0.0, "output": 0.0}}}', encoding="utf-8")
    assert prices.refresh(url=p.as_uri()) is False  # as_uri() -> file://...
    assert "evil" not in prices.models()
    assert prices.source() == "bundled"


def test_refresh_preserves_high_precision_rate(monkeypatch):
    import contextlib
    import io

    # A rate with more precision than a float holds must survive refresh exactly (Decimal).
    payload = '{"models": {"hp": {"input": 0.000000123456789012345678, "output": 0}}}'

    @contextlib.contextmanager
    def fake_urlopen(url, timeout=5.0):
        yield io.BytesIO(payload.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert prices.refresh("https://example.com/p.json") is True
    expected = Decimal("0.000000123456789012345678") * 1_000_000
    assert prices.estimate("hp", 1_000_000, 0).amount == expected


def test_refresh_updates_from_static_json(monkeypatch):
    import contextlib
    import io
    import json

    table = {"_updated": "2099-01-01", "models": {"gpt-4o": {"input": 0.001, "output": 0}}}
    payload = json.dumps(table)

    @contextlib.contextmanager
    def fake_urlopen(url, timeout=5.0):
        yield io.BytesIO(payload.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert prices.refresh("https://example.com/prices.json") is True
    assert prices.source() == "refreshed"
    assert prices.estimate("gpt-4o", 1000).amount == Decimal("1")


# --------------------------------------------------------------------------- live-source adapters


def _install_fetch(monkeypatch, raw_text: str):
    """Monkeypatch urlopen to serve a fixed raw body, capturing the URL it was called with."""
    import contextlib
    import io

    seen = {}

    @contextlib.contextmanager
    def fake_urlopen(url, timeout=5.0):
        seen["url"] = getattr(url, "full_url", url)
        yield io.BytesIO(raw_text.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_sources_lists_builtin_adapters():
    assert prices.sources() == ["aws", "azure", "litellm", "modelsdev", "openrouter", "vercel"]


def test_normalize_model_id_strips_provider_prefix():
    assert prices._normalize_model_id("openai/gpt-4o") == "gpt-4o"
    assert prices._normalize_model_id("GPT-4o") == "gpt-4o"


def test_refresh_litellm_maps_per_token_decimals(monkeypatch):
    raw = """
    {
      "sample_spec": {"note": "not a model"},
      "gpt-4o": {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001,
                 "cache_read_input_token_cost": 0.00000125},
      "claude-sonnet-4-6": {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000015}
    }
    """
    seen = _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="litellm") is True
    assert seen["url"] == prices.LITELLM_URL
    assert prices.source() == "refreshed"
    assert prices.source_name() == "litellm"
    assert "sample_spec" not in prices.models()  # non-model entry dropped
    # cached ⊆ input: 0.0000025*(1000-200) + 0.00001*500 + 0.00000125*200 = 0.00725
    assert prices.estimate("gpt-4o", 1000, 500, cached_tokens=200).amount == Decimal("0.00725")


def test_refresh_source_leaves_table_undatable(monkeypatch):
    # A live source with no global as-of date must NOT stamp _updated as today (that would make a
    # stale refresh look fresh). It's undatable -> snapshot_date() None, is_stale() False.
    raw = '{"gpt-4o": {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001}}'
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="litellm") is True
    assert prices.snapshot_date() is None
    assert prices.age_days() is None
    assert prices.is_stale() is False  # undatable is never "stale"


def test_refresh_azure_carries_effective_date(monkeypatch):
    raw = """
    {"Items": [
      {"skuName": "gpt 4o 1120 Inp Global", "retailPrice": 0.0025, "unitOfMeasure": "1K",
       "effectiveStartDate": "2025-03-01T00:00:00Z"},
      {"skuName": "gpt 4o 1120 Outp Global", "retailPrice": 0.01, "unitOfMeasure": "1K",
       "effectiveStartDate": "2025-05-15T00:00:00Z"}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="azure") is True
    assert prices.snapshot_date() == "2025-05-15"  # the newest source effectiveStartDate, not today


def test_refresh_openrouter_maps_string_prices_and_strips_prefix(monkeypatch):
    raw = """
    {"data": [
      {"id": "openai/gpt-4o", "pricing": {"prompt": "0.0000025", "completion": "0.00001",
                                          "input_cache_read": "0.00000125"}},
      {"id": "meta/llama3", "pricing": {"prompt": "0", "completion": "0"}}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="openrouter") is True
    assert prices.source_name() == "openrouter"
    assert "gpt-4o" in prices.models()  # prefix stripped
    assert prices.estimate("gpt-4o", 1000, 500).amount == Decimal("0.0075")


def test_refresh_openrouter_drops_the_string_zero_free_tier(monkeypatch):
    """The exact shape that carried the bug, pinned offline so it cannot come back.

    ``BUG-openrouter-source-publishes-zero-input-rates.md`` (closed 2026-08-02): every other mapper
    dropped a zero input rate, but ``_map_openrouter``'s guard was ``pricing.prompt is None`` and
    OpenRouter serves its free tier as the **string** ``"0"``, which is not ``None``. 17 models
    priced 1M input tokens at ``$0.00`` *as a fact*, so a USD ``budget(...)`` cap never bound on
    them. The row is now dropped by ``_drop_unpriceable`` and the model is honestly absent.

    ⚠️ Asserting "no row prices at $0" would go **vacuously green** the day OpenRouter stops
    listing free models. The fixture carries the zero rows itself, so the DROP is what is pinned.

    ``-1`` is OpenRouter's own sentinel for a dynamically-routed model (``openrouter/auto`` and
    four siblings, measured live 2026-08-02): the price is not known until a model is chosen, which
    is the model-router case that is never priceable. It is dropped by the same ``<= 0`` rule.
    """
    raw = """
    {"data": [
      {"id": "openai/gpt-4o", "pricing": {"prompt": "0.0000025", "completion": "0.00001"}},
      {"id": "meta/llama3:free", "pricing": {"prompt": "0", "completion": "0"}},
      {"id": "google/lyria-3-pro-preview", "pricing": {"prompt": "0", "completion": "0.000002"}},
      {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="openrouter") is True
    assert prices.models() == ["gpt-4o"]
    for absent in ("llama3:free", "lyria-3-pro-preview", "auto"):
        with pytest.raises(prices.UnknownModelError):
            prices.estimate(absent, 1_000_000)


def test_refresh_azure_parses_sku_and_converts_per_1k(monkeypatch):
    raw = """
    {"Items": [
      {"skuName": "gpt 4o 1120 Inp Global", "retailPrice": 0.0025, "unitOfMeasure": "1K",
       "productName": "Azure OpenAI"},
      {"skuName": "gpt 4o 1120 Outp Global", "retailPrice": 0.01, "unitOfMeasure": "1K",
       "productName": "Azure OpenAI"}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="azure") is True
    assert prices.source_name() == "azure"
    # 0.0025/1K -> 0.0000025 per token (input); 0.01/1K -> 0.00001 per token (output)
    assert prices.estimate("gpt-4o", 1000, 500).amount == Decimal("0.0075")


def test_refresh_unknown_source_is_noop(monkeypatch):
    # an unknown source name must not fetch or change the active table
    called = {"hit": False}

    def boom(*a, **k):
        called["hit"] = True
        raise AssertionError("should not fetch")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert prices.refresh(source="nope") is False
    assert called["hit"] is False
    assert prices.source() == "bundled"


def test_refresh_custom_mapper(monkeypatch):
    _install_fetch(monkeypatch, '{"weird": {"in": 0.001}}')

    def mapper(raw):
        return {"models": {k: {"input": v["in"], "output": 0} for k, v in raw.items()}}

    assert prices.refresh("https://example.com/x.json", mapper=mapper) is True
    assert prices.estimate("weird", 1000).amount == Decimal("1")


def test_refresh_empty_mapping_falls_back(monkeypatch):
    # a payload that maps to zero models is treated as a failed refresh (keep last-good table)
    _install_fetch(monkeypatch, '{"data": []}')
    assert prices.refresh(source="openrouter") is False
    assert prices.source() == "bundled"
    assert "gpt-4o" in prices.models()


def test_age_days_and_is_stale(monkeypatch):
    from datetime import date

    raw = '{"_updated": "2026-06-01", "models": {"gpt-4o": {"input": 0.001, "output": 0}}}'
    _install_fetch(monkeypatch, raw)
    assert prices.refresh("https://example.com/p.json") is True
    assert prices.age_days(today=date(2026, 6, 26)) == 25

    raw_old = '{"_updated": "2000-01-01", "models": {"gpt-4o": {"input": 0.001, "output": 0}}}'
    _install_fetch(monkeypatch, raw_old)
    assert prices.refresh("https://example.com/p.json") is True
    assert prices.is_stale(max_age_days=30) is True


# --- _register: the contractual programmatic write hook (1.6.0) ---------------------------------


def test_register_writes_rate_and_estimate_uses_it():
    prices._register("my-fine-tune", {"input": Decimal("0.000001"), "output": Decimal("0.000002")})
    assert "my-fine-tune" in prices.models()
    # 0.000001*1000 + 0.000002*500 = 0.001 + 0.001 = 0.002
    assert prices.estimate("my-fine-tune", 1000, 500).amount == Decimal("0.002")


def test_register_survives_refresh(monkeypatch):
    import contextlib
    import io
    import json

    prices._register("my-fine-tune", {"input": Decimal("0.000001"), "output": Decimal("0")})
    payload = json.dumps(
        {"_updated": "2099-02-02", "models": {"gpt-4o": {"input": 0.002, "output": 0}}}
    )

    @contextlib.contextmanager
    def fake_urlopen(url, timeout=5.0):
        yield io.BytesIO(payload.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert prices.refresh() is True
    # The refreshed table swapped in — but the registration is re-applied, not dropped.
    assert prices.estimate("my-fine-tune", 1000).amount == Decimal("0.001")
    assert prices.estimate("gpt-4o", 1000).amount == Decimal("2")  # refreshed rate active


def test_register_coerces_via_str_and_overrides_snapshot():
    # Snapshot gpt-4o input is 0.0000025; a registration overrides it. str-coercion, no float noise.
    prices._register("gpt-4o", {"input": "0.00001", "output": 0})
    rate = prices._ensure_loaded()["models"]["gpt-4o"]["input"]
    assert isinstance(rate, Decimal) and rate == Decimal("0.00001")


def test_reset_clears_registrations():
    prices._register("my-fine-tune", {"input": Decimal("0.000001")})
    prices._reset()
    assert "my-fine-tune" not in prices.models()


def test_embedding_models_priced_in_snapshot():
    # Embedding rows back the new instrument() embeddings capture (USD budgets bind on embed calls).
    # text-embedding-3-small: $0.02/1M -> 0.00000002/token; 1000 tokens = 0.00002.
    assert prices.estimate("text-embedding-3-small", 1000).amount == Decimal("0.00002")
    assert prices.estimate("text-embedding-3-large", 1000).amount == Decimal("0.00013")
    assert prices.estimate("text-embedding-ada-002", 1000).amount == Decimal("0.0001")


# --- register / register_model_price: the PUBLIC registration API (1.15.0, D3) -------------------
#
# Before 1.15 `cendor.core.prices` deliberately had no public `register` and a PEP 562 __getattr__
# pointed callers at `cendor.sdk.register_model_price` — which meant a **libraries-door** user had
# to install the SDK distribution to price one deployment. Parity with `@cendor/core`'s
# `prices.register` (prices.ts) closes that.


def test_register_is_public_and_survives_refresh(monkeypatch):
    import contextlib
    import io
    import json

    prices.register("my-deployment", {"input": Decimal("0.0000025"), "output": "0.00001"})
    assert "my-deployment" in prices.models()
    # 0.0000025*1000 + 0.00001*500 = 0.0025 + 0.005 = 0.0075
    assert prices.estimate("my-deployment", 1000, 500).amount == Decimal("0.0075")

    payload = json.dumps(
        {"_updated": "2099-02-02", "models": {"gpt-4o": {"input": 1, "output": 0}}}
    )

    @contextlib.contextmanager
    def fake_urlopen(url, timeout=5.0):
        yield io.BytesIO(payload.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert prices.refresh() is True
    assert prices.estimate("my-deployment", 1000).amount == Decimal("0.0025")  # not dropped


def test_register_model_price_converts_per_1m_by_default():
    rates = prices.register_model_price("my-deployment", input=2.50, output=10.00)
    assert rates["input"] == Decimal("2.50") / Decimal(1_000_000)
    # 1M in + 1M out at $2.50 / $10.00 -> $12.50 exactly, no float noise.
    assert prices.estimate("my-deployment", 1_000_000, 1_000_000).amount == Decimal("12.50")


def test_register_model_price_honours_1k_and_token_units():
    prices.register_model_price("per-1k", input="1", per="1K")
    prices.register_model_price("per-token", input="0.001", per="token")
    assert prices.estimate("per-1k", 1000).amount == Decimal("1")
    assert prices.estimate("per-token", 1000).amount == Decimal("1")


def test_register_model_price_rejects_an_unknown_unit():
    with pytest.raises(ValueError, match="per must be one of"):
        prices.register_model_price("x", input=1, per="1B")
    assert "x" not in prices.models()  # nothing registered on the failure path


def test_register_model_price_carries_cached_and_cache_write():
    prices.register_model_price("cachey", input=10, output=0, cached=1, cache_write=12.5)
    # 1M input, 200k of it cached: 10*0.8 + 1*0.2 = 8.2 ; plus 100k cache-write at 12.5 -> 1.25
    got = prices.estimate("cachey", 1_000_000, 0, cached_tokens=200_000, cache_write_tokens=100_000)
    assert got.amount == Decimal("9.45")


def test_sdk_era_private_alias_still_writes():
    # An older `cendor-sdk` pinned against an older core calls `prices._register`. Keep it working.
    # The `output` is STATED (0) rather than omitted: since 1.20.0 an absent rate means unknown, and
    # this is exactly the one-line migration the error message asks any author to make.
    prices._register("legacy-hook", {"input": Decimal("0.000001"), "output": 0})
    assert prices.estimate("legacy-hook", 1000).amount == Decimal("0.001")


def test_getattr_still_teaches_a_near_miss_but_no_longer_denies_register():
    # Negative control for the PEP 562 hook: the real functions must NOT reach it...
    assert callable(prices.register) and callable(prices.register_model_price)
    # ...while a plausible wrong spelling still gets a pointer naming both real entry points.
    with pytest.raises(AttributeError) as ei:
        prices.set_price  # noqa: B018 - attribute access is the assertion
    msg = str(ei.value)
    assert "register_model_price" in msg and "prices.register(" in msg
    with pytest.raises(AttributeError, match="has no attribute 'nope'"):
        prices.nope  # noqa: B018


# --- source URLs must be fetchable by the stdlib (D5-1 regression) -------------------------------


def test_builtin_source_urls_are_urllib_safe():
    """`refresh(source="azure")` had never worked: AZURE_URL carried raw spaces in its `$filter`,
    `urllib.request.urlopen` rejects those outright (`InvalidURL: URL can't contain control
    characters`), and `refresh()` swallows every exception — so the failure looked exactly like
    being offline. Measured 2026-07-31; the TS twin was fine because `fetch` encodes for us.

    This is the negative control: with the pre-fix URL, `Request(...)` raises here.
    """
    import http.client
    import urllib.request

    from cendor.core.prices import _SOURCES

    for name, entry in _SOURCES.items():
        url = entry.url_for(None)
        assert " " not in url, f"{name} source URL carries a raw space: {url}"
        urllib.request.Request(url)  # constructs == urlopen will accept the URL
    # Teeth: the exact shape that shipped is rejected by the stdlib *before* any socket work
    # (`_validate_path` runs inside `putrequest`), so this control needs no network. The host is
    # `.invalid` so it could not resolve even if the order ever changed.
    with pytest.raises(http.client.InvalidURL, match="control characters"):
        urllib.request.urlopen(  # noqa: S310 - https only; raises on the raw space, never connects
            "https://cendor.invalid/x?$filter=productName eq 'Azure OpenAI'", timeout=0.001
        )


# ======================================================================== live-pricing wave (W2)
#
# Every case below is anchored to something MEASURED on 2026-08-01 against the real endpoints; the
# comments name the measurement so a future edit knows what it would be undoing. Raw traces:
# cendorhq `plan/evidence-live-pricing-2026-08-01/`.


def _install_multi(monkeypatch, bodies: dict[str, str]):
    """Serve a different body per URL substring, and record every URL fetched (in order)."""
    import contextlib
    import io

    seen: list[str] = []

    @contextlib.contextmanager
    def fake_urlopen(url, timeout=5.0):
        full = getattr(url, "full_url", url)
        seen.append(full)
        for needle, body in bodies.items():
            if needle in full:
                yield io.BytesIO(body.encode("utf-8"))
                return
        raise AssertionError(f"unexpected fetch: {full}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


# ----------------------------------------------------------------------------- the azure rewrite


def test_azure_url_is_the_foundry_filter_with_a_region():
    """The pre-rename `productName eq 'Azure OpenAI'` filter still RETURNS ROWS — which is why the
    coverage loss was invisible — but sees 462 of eastus2's 1,526 meters and no GPT-5 at all."""
    url = prices.azure_url()
    assert "serviceName" in url and "Foundry%20Models" in url
    assert "productName" not in url
    assert "armRegionName" in url and "eastus2" in url
    assert " " not in url  # urllib refuses a raw space (the 2026-07-31 defect)
    assert "westeurope" in prices.azure_url("westeurope")


def test_azure_region_is_mandatory_not_cosmetic(monkeypatch):
    """Measured: unregioned, the same query is >=25,000 rows and still paging after 28.5 s. The
    region term is what makes one refresh() bounded, so it must reach the wire."""
    seen = _install_multi(monkeypatch, {"prices.azure.com": '{"Items": []}'})
    prices.refresh(source="azure", region="swedencentral")
    assert "swedencentral" in seen[0]


def test_azure_opt_means_output(monkeypatch):
    """141 rows on 2026-08-01 spell output `opt`. The pre-fix parser looked only for
    `outp`/`output`, so every GPT-5.x family had an input rate and NO output rate. Proven by price:
    GPT-5.1 is published at $1.25 in / $10 out."""
    raw = """
    {"Items": [
      {"skuName": "GPT 5.1 inp Gl", "retailPrice": 1.25, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "GPT 5.1 opt Gl", "retailPrice": 10.0, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="azure") is True
    # 1.25/1M in + 10/1M out
    assert prices.estimate("gpt-5.1", 1_000_000, 1_000_000).amount == Decimal("11.25")


def test_azure_skips_batch_and_fine_tune_and_long_context_meters(monkeypatch):
    """A batch meter is half price and a long-context meter is double. Cheapest-wins across all of
    them would publish the batch rate as the standard one."""
    raw = """
    {"Items": [
      {"skuName": "GPT 5.1 inp Gl", "retailPrice": 1.25, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "GPT 5.1 opt Gl", "retailPrice": 10.0, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "GPT 5.1 Batch inp Gl", "retailPrice": 0.625, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "GPT 5.1 inp Gl L", "retailPrice": 0.5, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "4.1 ft training Dz", "retailPrice": 0.0275, "unitOfMeasure": "1K",
       "productName": "Azure OpenAI", "type": "Consumption"}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="azure") is True
    assert prices.estimate("gpt-5.1", 1_000_000).amount == Decimal("1.25")


def test_azure_unit_is_read_per_row(monkeypatch):
    """eastus2 mixes 905 `1K` meters with 479 `1M` ones in one response."""
    raw = """
    {"Items": [
      {"skuName": "gpt-4o-0806-Inp-glbl", "retailPrice": 0.0025, "unitOfMeasure": "1K",
       "productName": "Azure OpenAI", "type": "Consumption"},
      {"skuName": "gpt-4o-0806-Outp-glbl", "retailPrice": 0.01, "unitOfMeasure": "1K",
       "productName": "Azure OpenAI", "type": "Consumption"},
      {"skuName": "GPT 5 Inpt Glbl", "retailPrice": 1.25, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "GPT 5 Outp Glbl", "retailPrice": 10.0, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    # Assert the source actually took: before 1.20.0 these two rows carried no output rate, and once
    # such a row is dropped `refresh` returns False — leaving the BUNDLED snapshot active, whose
    # gpt-4o happens to be $2.50/1M too. The test would then have passed without the mapper running.
    assert prices.refresh(source="azure") is True
    assert prices.source_name() == "azure"
    assert prices.estimate("gpt-4o", 1_000_000).amount == Decimal("2.5")
    assert prices.estimate("gpt-5", 1_000_000).amount == Decimal("1.25")


def test_azure_family_root_does_not_mangle_o1_o3(monkeypatch):
    """A bare `4.3` under *Azure Grok Models* is grok-4.3; a bare `V4 Pro` under *Azure Deepseek
    Models* is deepseek-v4-pro. Applying the root unconditionally turned `o1`/`o3`/`o4-mini` into
    `gpt-o1`/`gpt-o3`/`gpt-o4-mini` — a regression against the pre-fix mapper."""
    raw = """
    {"Items": [
      {"skuName": "o3 Inp glbl", "retailPrice": 0.002, "unitOfMeasure": "1K",
       "productName": "Azure OpenAI Reasoning", "type": "Consumption"},
      {"skuName": "o3 Outp glbl", "retailPrice": 0.008, "unitOfMeasure": "1K",
       "productName": "Azure OpenAI Reasoning", "type": "Consumption"},
      {"skuName": "4.3 Inp Glbl", "retailPrice": 0.00125, "unitOfMeasure": "1K",
       "productName": "Azure Grok Models", "type": "Consumption"},
      {"skuName": "4.3 Outp Glbl", "retailPrice": 0.00625, "unitOfMeasure": "1K",
       "productName": "Azure Grok Models", "type": "Consumption"},
      {"skuName": "V4 Pro Inp glbl", "retailPrice": 0.00174, "unitOfMeasure": "1K",
       "productName": "Azure Deepseek Models", "type": "Consumption"},
      {"skuName": "V4 Pro Outp glbl", "retailPrice": 0.00696, "unitOfMeasure": "1K",
       "productName": "Azure Deepseek Models", "type": "Consumption"},
      {"skuName": "GPT 5.2 pro inp Gl", "retailPrice": 21.0, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "GPT 5.2 pro opt Gl", "retailPrice": 84.0, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="azure") is True  # a dropped row would leave the bundled table
    known = prices.models()
    assert "o3" in known and "gpt-o3" not in known
    assert "grok-4.3" in known
    assert "deepseek-v4-pro" in known
    assert "gpt-5.2-pro" in known


def test_azure_maps_cache_read(monkeypatch):
    raw = """
    {"Items": [
      {"skuName": "GPT 5.1 inp Gl", "retailPrice": 1.25, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "GPT 5.1 opt Gl", "retailPrice": 10.0, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"},
      {"skuName": "GPT 5.1 cd inp Gl", "retailPrice": 0.125, "unitOfMeasure": "1M",
       "productName": "Azure OpenAI GPT5", "type": "Consumption"}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="azure") is True  # a dropped row would leave the bundled table
    # all 1M tokens cached: billed at the cached rate, once
    assert prices.estimate("gpt-5.1", 1_000_000, cached_tokens=1_000_000).amount == Decimal("0.125")


def test_azure_paginates(monkeypatch):
    """1,526 meters arrive over 2 pages. Reading page 1 only is what the shipped source did."""
    page1 = (
        '{"Items": [{"skuName": "gpt 4o Inp glbl", "retailPrice": 0.0025,'
        ' "unitOfMeasure": "1K", "productName": "Azure OpenAI", "type": "Consumption"}],'
        ' "NextPageLink": "https://prices.azure.com/next-page"}'
    )
    page2 = (
        '{"Items": [{"skuName": "gpt 4o Outp glbl", "retailPrice": 0.01,'
        ' "unitOfMeasure": "1K", "productName": "Azure OpenAI", "type": "Consumption"}]}'
    )
    seen = _install_multi(monkeypatch, {"next-page": page2, "%24filter": page1})
    assert prices.refresh(source="azure") is True
    assert len(seen) == 2, seen
    assert prices.estimate("gpt-4o", 1000, 500).amount == Decimal("0.0075")  # both pages mapped


def test_azure_wrong_filter_answers_200_with_zero_items(monkeypatch):
    """NEGATIVE CONTROL. Measured: a wrong `serviceName` returns HTTP 200 and an empty Items list —
    the status is meaningless. An empty map must leave the last-good table alone."""
    _install_fetch(monkeypatch, '{"Items": []}')
    assert prices.refresh(source="azure") is False
    assert prices.source() == "bundled"
    assert prices.estimate("gpt-4o", 1000).amount == Decimal("0.0025")


# ----------------------------------------------------------------------------------- the aws source

_AWS_INDEX = (
    '{"regions": {"us-east-1": {"currentVersionUrl": "/offers/v1.0/aws/X/2/us-east-1/index.json"}}}'
)


def _aws_file(products, terms, published="2026-07-29T23:58:47Z", with_output=True):
    """Build an AWS offer file. ``with_output`` mirrors the real ones — for every *input-tokens*
    product it appends the matching *output-tokens* product and term at 4x the rate.

    Not cosmetic. Since ``cendor-core`` 1.20.0 a mapped row with no output rate is dropped as
    unpriceable, so an input-only fixture maps to an EMPTY table, ``refresh()`` returns ``False``
    and the bundled snapshot stays active — several of these tests would then have gone on passing
    while asserting against the bundled rates instead of the mapper's. Pass ``with_output=False``
    to exercise that drop on purpose.
    """
    import json as _json

    if with_output:
        products = dict(products)
        terms = dict(terms)
        for sku, p in list(products.items()):
            attrs = p.get("attributes", {})
            if attrs.get("inferenceType") != "Input tokens":
                continue  # cache rows carry a null inferenceType; only the usagetype names them
            rate = terms[sku]["t"]["priceDimensions"]["d"]["pricePerUnit"]["USD"]
            products[f"{sku}o"] = {
                "attributes": {
                    **attrs,
                    "inferenceType": "Output tokens",
                    "usagetype": attrs["usagetype"].replace("input-tokens", "output-tokens"),
                }
            }
            terms[f"{sku}o"] = {
                "t": {
                    "priceDimensions": {
                        "d": {
                            "unit": terms[sku]["t"]["priceDimensions"]["d"]["unit"],
                            "pricePerUnit": {"USD": str(Decimal(rate) * 4)},
                        }
                    }
                }
            }
    return _json.dumps(
        {"publicationDate": published, "products": products, "terms": {"OnDemand": terms}}
    )


def test_aws_unions_both_offer_codes(monkeypatch):
    """MEASURED, and the single most important AWS fact: `AmazonBedrock` alone carries only
    Claude 2.0/2.1/3-Haiku/3-Sonnet/Instant. `Claude Sonnet 4` and `Claude Sonnet 4.5` live ONLY in
    `AmazonBedrockService`. A single-offer client silently misses every current Claude rate."""
    import contextlib
    import io

    main = _aws_file(
        {
            "A": {
                "attributes": {
                    "model": "Claude 2.1",
                    "inferenceType": "Input tokens",
                    "usagetype": "USE1-Claude2.1-input-tokens",
                }
            }
        },
        {
            "A": {
                "t": {
                    "priceDimensions": {
                        "d": {"unit": "1K tokens", "pricePerUnit": {"USD": "0.008"}}
                    }
                }
            }
        },
    )
    service = _aws_file(
        {
            "B": {
                "attributes": {
                    "model": "Claude Sonnet 4",
                    "inferenceType": "Input tokens",
                    "usagetype": "USE1-Claude4Sonnet-input-tokens-cross-region-global",
                }
            }
        },
        {
            "B": {
                "t": {
                    "priceDimensions": {
                        "d": {"unit": "1K tokens", "pricePerUnit": {"USD": "0.003"}}
                    }
                }
            }
        },
    )
    calls = {"n": 0}

    @contextlib.contextmanager
    def fake(url, timeout=5.0):
        full = getattr(url, "full_url", url)
        if "current/region_index.json" in full:
            yield io.BytesIO(_AWS_INDEX.encode())
            return
        calls["n"] += 1
        yield io.BytesIO((main if calls["n"] == 1 else service).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert prices.refresh(source="aws") is True
    assert prices.source_name() == "aws"
    known = prices.models()
    assert "claude-2-1" in known
    assert "claude-sonnet-4" in known, "the union is what makes the CURRENT Claude rate visible"
    assert calls["n"] == 2, "both offer codes must be fetched, every time"
    assert prices.snapshot_date() == "2026-07-29"  # publicationDate, not today


def test_aws_batch_usagetype_never_becomes_the_base_rate(monkeypatch):
    """MEASURED: `Claude Sonnet 4` carries `inferenceType: "Input tokens"` on BOTH
    `...-cross-region-global` ($3/MTok) and `...-cross-region-global-batch` ($1.50/MTok).
    Cheapest-wins over `inferenceType` publishes the batch price as the standard one — the
    prototype did exactly that. `usagetype` is the discriminator."""
    f = _aws_file(
        {
            "A": {
                "attributes": {
                    "model": "Claude Sonnet 4",
                    "inferenceType": "Input tokens",
                    "usagetype": "USE1-Claude4Sonnet-input-tokens-cross-region-global",
                }
            },
            "B": {
                "attributes": {
                    "model": "Claude Sonnet 4",
                    "inferenceType": "Input tokens",
                    "usagetype": "USE1-Claude4Sonnet-input-tokens-cross-region-global-batch",
                }
            },
        },
        {
            "A": {
                "t": {
                    "priceDimensions": {
                        "d": {"unit": "1K tokens", "pricePerUnit": {"USD": "0.003"}}
                    }
                }
            },
            "B": {
                "t": {
                    "priceDimensions": {
                        "d": {"unit": "1K tokens", "pricePerUnit": {"USD": "0.0015"}}
                    }
                }
            },
        },
    )
    _install_multi(
        monkeypatch, {"current/region_index.json": _AWS_INDEX, "us-east-1/index.json": f}
    )
    assert prices.refresh(source="aws") is True
    assert prices.estimate("claude-sonnet-4", 1_000_000).amount == Decimal("3")


def test_aws_cache_rows_carry_a_null_inference_type(monkeypatch):
    """MEASURED: the cache row's `inferenceType` is null; only the usagetype says what it is."""
    f = _aws_file(
        {
            "A": {
                "attributes": {
                    "model": "Claude Sonnet 4",
                    "inferenceType": "Input tokens",
                    "usagetype": "USE1-Claude4Sonnet-input-tokens-cross-region-global",
                }
            },
            "C": {
                "attributes": {
                    "model": "Claude Sonnet 4",
                    "inferenceType": None,
                    "usagetype": (
                        "USE1-Claude4Sonnet-cache-read-input-token-count-cross-region-global"
                    ),
                }
            },
        },
        {
            "A": {
                "t": {
                    "priceDimensions": {
                        "d": {"unit": "1K tokens", "pricePerUnit": {"USD": "0.003"}}
                    }
                }
            },
            "C": {
                "t": {
                    "priceDimensions": {
                        "d": {"unit": "1K tokens", "pricePerUnit": {"USD": "0.0003"}}
                    }
                }
            },
        },
    )
    _install_multi(
        monkeypatch, {"current/region_index.json": _AWS_INDEX, "us-east-1/index.json": f}
    )
    prices.refresh(source="aws")
    assert prices.estimate("claude-sonnet-4", 1000, cached_tokens=1000).amount == Decimal("0.0003")


def test_aws_ignores_non_token_units(monkeypatch):
    """`1K TPM Hour` (a reserved-throughput commitment) and `image` are not per-token rates."""
    f = _aws_file(
        {
            "A": {
                "attributes": {
                    "model": "Nova Canvas",
                    "inferenceType": "T2I 1024 Standard",
                    "usagetype": "USE1-NovaCanvas-input-tokens",
                }
            }
        },
        {
            "A": {
                "t": {"priceDimensions": {"d": {"unit": "image", "pricePerUnit": {"USD": "0.04"}}}}
            }
        },
    )
    _install_multi(
        monkeypatch, {"current/region_index.json": _AWS_INDEX, "us-east-1/index.json": f}
    )
    assert prices.refresh(source="aws") is False  # nothing mappable -> keep the bundled table
    assert prices.source() == "bundled"


def test_aws_model_key_normalisation():
    """AWS mixes display names with wire ids in the same file."""
    k = prices._aws_model_key
    assert k("Claude Sonnet 4.5") == "claude-sonnet-4-5"  # what _lookup_id yields from a wire id
    assert k("Llama 3.3 70B") == "llama-3-3-70b"
    assert k("gpt-oss-120b") == "gpt-oss-120b"
    assert k("xai.grok-4.3") == "grok-4.3"
    assert k("google.gemma-4-31b") == "gemma-4-31b"


def test_aws_region_reaches_the_wire(monkeypatch):
    idx = '{"regions": {"eu-west-1": {"currentVersionUrl": "https://p/eu.json"}}}'
    f = _aws_file(
        {
            "A": {
                "attributes": {
                    "model": "Claude Sonnet 4",
                    "inferenceType": "Input tokens",
                    "usagetype": "USE1-Claude4Sonnet-input-tokens",
                }
            }
        },
        {
            "A": {
                "t": {
                    "priceDimensions": {
                        "d": {"unit": "1K tokens", "pricePerUnit": {"USD": "0.003"}}
                    }
                }
            }
        },
    )
    seen = _install_multi(monkeypatch, {"region_index.json": idx, "eu.json": f})
    assert prices.refresh(source="aws", region="eu-west-1") is True
    assert any("eu.json" in u for u in seen)


# ------------------------------------------------------------------------------ modelsdev / vercel


def test_modelsdev_allowlist_keeps_a_reseller_from_outranking_the_lab(monkeypatch):
    """MEASURED: `gpt-5.1` appears under 11 models.dev providers between $1.07 and $1.25 per MTok,
    and the providers with the most rows are all resellers (nano-gpt 617, kilo 346, openrouter
    335). "Last one wins" hands you a random reseller's resale price as the model's rate."""
    raw = """
    {
      "nano-gpt": {"models": {"gpt-5.1": {"cost": {"input": 9, "output": 9}}}},
      "opencode": {"models": {"gpt-5.1": {"cost": {"input": 1.07, "output": 8.5}}}},
      "openai":   {"models": {"gpt-5.1": {"cost": {"input": 1.25, "output": 10},
                                          "last_updated": "2026-07-20"}}}
    }
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="modelsdev") is True
    assert prices.source_name() == "modelsdev"
    assert prices.estimate("gpt-5.1", 1_000_000).amount == Decimal("1.25")
    assert prices.snapshot_date() == "2026-07-20"  # per-row last_updated, real provenance


def test_modelsdev_lab_beats_a_host_when_both_key_the_model_bare(monkeypatch):
    """⚠️ THE PRECEDENCE INVERSION, measured 2026-08-02 on the live payload.

    The ``bare`` guard was a plain ``if key in bare: continue``, which inverted the allowlist
    whenever two ALLOWLISTED providers both used a bare id: the reverse walk writes the
    lower-precedence provider first, it claims the key, and the higher-precedence one is skipped.
    So ``refresh(source="modelsdev")`` returned **azure's $1/$6 deployment price** for
    ``gpt-5.6-luna`` where OpenAI's own listing says **$0.2/$1.2**. Four rows were affected, every
    one of them a host's listing displacing the lab's.

    Caught in ``cendor-prices`` by G6 (>2x day-over-day swing) rather than by any library test,
    because nothing offline had two allowlisted providers keying one model bare.
    """
    raw = """
    {
      "azure":  {"models": {"gpt-5.6-luna": {"cost": {"input": 1,   "output": 6}}}},
      "openai": {"models": {"gpt-5.6-luna": {"cost": {"input": 0.2, "output": 1.2}}}}
    }
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="modelsdev") is True
    # openai is index 0 in the allowlist, azure index 12 — the LAB rate must win.
    assert prices.estimate("gpt-5.6-luna", 1_000_000).amount == Decimal("0.2")
    assert prices.estimate("gpt-5.6-luna", 0, 1_000_000).amount == Decimal("1.2")


def test_modelsdev_a_host_namespaced_id_still_never_overwrites_a_bare_one(monkeypatch):
    """The rule the guard actually exists for, unchanged: a namespaced id names a HOST's listing,
    and must not displace a direct naming even when its provider outranks the bare one's."""
    raw = """
    {
      "azure":  {"models": {"gpt-5.9": {"cost": {"input": 9, "output": 9}}}},
      "openai": {"models": {"azure/gpt-5.9": {"cost": {"input": 1, "output": 1}}}}
    }
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="modelsdev") is True
    assert prices.estimate("gpt-5.9", 1_000_000).amount == Decimal("9")


def test_modelsdev_ignores_providers_outside_the_allowlist(monkeypatch):
    _install_fetch(monkeypatch, '{"nano-gpt": {"models": {"only-here": {"cost": {"input": 1}}}}}')
    assert prices.refresh(source="modelsdev") is False  # nothing allowlisted -> no swap
    assert prices.source() == "bundled"


def test_modelsdev_converts_per_1m_exactly(monkeypatch):
    raw = (
        '{"openai": {"models": {"gpt-4o": {"cost": '
        '{"input": 2.5, "output": 10, "cache_read": 1.25}}}}}'
    )
    _install_fetch(monkeypatch, raw)
    prices.refresh(source="modelsdev")
    assert prices.estimate("gpt-4o", 1000, 500, cached_tokens=200).amount == Decimal("0.00725")


def test_vercel_maps_string_rates_and_language_models_only(monkeypatch):
    raw = """
    {"data": [
      {"id": "openai/gpt-4o", "type": "language",
       "pricing": {"input": "0.0000025", "output": "0.00001", "input_cache_read": "0.00000125"}},
      {"id": "openai/dall-e-3", "type": "image", "pricing": {"input": "0.04"}}
    ]}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh(source="vercel") is True
    assert prices.source_name() == "vercel"
    assert "gpt-4o" in prices.models() and "dall-e-3" not in prices.models()
    assert prices.snapshot_date() is None  # no catalog-wide date -> undatable, never faked as today


def test_litellm_host_key_never_overwrites_the_bare_one(monkeypatch):
    """MEASURED, and it published a wrong number in the feed before it was caught: litellm keys
    `claude-3-5-haiku` under several hosts. `vertex_ai/claude-3-5-haiku` is VERTEX's $1/$5, not
    Anthropic's $0.80/$4, and stripping the namespace collapsed it onto the bare id."""
    raw = """
    {
      "claude-3-5-haiku": {"input_cost_per_token": 0.0000008, "output_cost_per_token": 0.000004},
      "vertex_ai/claude-3-5-haiku": {"input_cost_per_token": 0.000001,
                                     "output_cost_per_token": 0.000005},
      "heroku/claude-3-5-haiku": {"input_cost_per_token": 0.000002,
                                  "output_cost_per_token": 0.00001}
    }
    """
    _install_fetch(monkeypatch, raw)
    prices.refresh(source="litellm")
    assert prices.estimate("claude-3-5-haiku", 1_000_000).amount == Decimal("0.8")


def test_litellm_drops_a_zero_input_rate(monkeypatch):
    """A $0 input rate makes estimate() report $0.00 as a FACT and a USD cap silently never bind —
    the failure this whole wave removes. Absent, plus a warning, is the honest answer."""
    raw = """
    {"free-model": {"input_cost_per_token": 0, "output_cost_per_token": 0},
     "gpt-4o": {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001}}
    """
    _install_fetch(monkeypatch, raw)
    prices.refresh(source="litellm")
    assert "free-model" not in prices.models()
    with pytest.raises(prices.UnknownModelError):
        prices.estimate("free-model", 1000)


# ------------------------------------------------------------------------------ refresh(required)


def test_refresh_required_raises_instead_of_returning_false(monkeypatch):
    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert prices.refresh() is False  # the default contract is unchanged: never raises
    with pytest.raises(prices.PriceRefreshError, match="failed"):
        prices.refresh(required=True)
    assert prices.source() == "bundled"  # and it still did not revert anything


def test_refresh_required_raises_on_an_empty_result(monkeypatch):
    """A 200 that maps to nothing is the Azure-wrong-filter shape. `required=True` must not read it
    as success just because the socket worked."""
    _install_fetch(monkeypatch, '{"Items": []}')
    with pytest.raises(prices.PriceRefreshError, match="no models"):
        prices.refresh(source="azure", required=True)


def test_refresh_required_raises_on_an_unknown_source():
    with pytest.raises(prices.PriceRefreshError, match="unknown price source"):
        prices.refresh(source="nope", required=True)


def test_default_refresh_targets_the_cendor_prices_feed(monkeypatch):
    seen = _install_multi(
        monkeypatch, {"cendor-prices": '{"_updated": "2026-08-01", "models": {"x": {"input": 1}}}'}
    )
    assert prices.refresh() is True
    assert seen[0] == prices.SNAPSHOT_URL
    assert "cendor-prices" in prices.SNAPSHOT_URL
    # A PAGES url, not raw.githubusercontent: the builder repo is private, so the raw URL needs
    # auth and 404s. A data-only gh-pages branch publishes the file itself, keyless.
    assert prices.SNAPSHOT_URL.startswith("https://cendorhq.github.io/")
    assert "raw.githubusercontent" not in prices.SNAPSHOT_URL
    assert prices.source_name() == "feed"


# ------------------------------------------------------------------------------------------ explain


def test_explain_reports_exact_normalized_registered_and_unpriced():
    assert prices.explain("gpt-4o").how == "exact"
    # a Bedrock wire id reduces to its base row
    e = prices.explain("us.anthropic.claude-sonnet-4-6-20260115-v1:0")
    assert e.how == "normalized" and e.resolved == "claude-sonnet-4-6"
    assert prices.explain("no-such-model").how == "unpriced"
    prices.register_model_price("my-deployment", input=2.5, output=10)
    e = prices.explain("my-deployment")
    assert e.how == "registered" and e.registered is True
    assert any("overrides every table" in n for n in e.notes)


def test_explain_surfaces_per_row_provenance(monkeypatch):
    """The feed carries `_provenance` as a PARALLEL top-level map, so rate objects stay pure
    numbers and a `prices/1` reader that ignores unknown keys is unaffected."""
    raw = """
    {"_updated": "2026-08-01",
     "models": {"gpt-4o": {"input": 0.0000025, "output": 0.00001}},
     "_provenance": {"gpt-4o": {"src": "azure", "asof": "2026-07-01"}}}
    """
    _install_fetch(monkeypatch, raw)
    prices.refresh()
    e = prices.explain("gpt-4o")
    assert e.row_source == "azure"
    assert e.row_asof == "2026-07-01"
    assert e.source_name == "feed"
    assert e.table_origin == "refreshed"
    assert "azure as of 2026-07-01" in e.summary()
    # and no provenance string ever reaches a Decimal coercion
    assert prices.estimate("gpt-4o", 1000).amount == Decimal("0.0025")


def test_explain_flags_a_resale_source(monkeypatch):
    raw = (
        '{"data": [{"id": "gpt-4o", "type": "language",'
        ' "pricing": {"input": "0.000003", "output": "0.000012"}}]}'
    )
    _install_fetch(monkeypatch, raw)
    prices.refresh(source="vercel")
    assert any("RESALE" in n for n in prices.explain("gpt-4o").notes)


def test_explain_flags_an_undatable_table(monkeypatch):
    _install_fetch(
        monkeypatch,
        '{"gpt-4o": {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001}}',
    )
    prices.refresh(source="litellm")
    assert any("no as-of date" in n for n in prices.explain("gpt-4o").notes)


def test_explain_never_raises_on_anything():
    for weird in ("", "  ", "a/b/c", "us.anthropic.", "gpt-4o-2026-01-01"):
        assert prices.explain(weird).model == weird


# ---------------------------------------------------------------------------------- save() / load()


def test_save_load_round_trips_rates_and_provenance(tmp_path, monkeypatch):
    raw = """
    {"_updated": "2026-08-01",
     "models": {"gpt-4o": {"input": 0.0000025, "output": 0.00001}},
     "_provenance": {"gpt-4o": {"src": "azure", "asof": "2026-07-01"}}}
    """
    _install_fetch(monkeypatch, raw)
    prices.refresh()
    path = prices.save(str(tmp_path / "c" / "prices.json"))

    prices._reset()
    assert prices.source() == "bundled"
    assert prices.load(path) is True
    assert prices.source() == "loaded"
    assert prices.source_name() == "feed", "the ORIGINAL source travels, not 'a file'"
    assert prices.snapshot_date() == "2026-08-01", "age_days() must describe the data, not the read"
    assert prices.estimate("gpt-4o", 1000).amount == Decimal("0.0025")
    e = prices.explain("gpt-4o")
    assert (e.row_source, e.row_asof, e.table_origin) == ("azure", "2026-07-01", "loaded")


def test_save_writes_exact_decimals_not_floats(tmp_path, monkeypatch):
    _install_fetch(
        monkeypatch,
        '{"models": {"m": {"input": 0.000000123456789012345, "output": 0}}}',
    )
    prices.refresh()
    path = prices.save(str(tmp_path / "p.json"))
    assert "0.000000123456789012345" in (tmp_path / "p.json").read_text(encoding="utf-8")
    prices._reset()
    prices.load(path)
    assert prices.estimate("m", 1_000_000).amount == Decimal("0.123456789012345")


def test_load_re_applies_registrations_exactly_as_refresh_does(tmp_path, monkeypatch):
    _install_fetch(monkeypatch, '{"models": {"gpt-4o": {"input": 0.0000025}}}')
    prices.refresh()
    path = prices.save(str(tmp_path / "p.json"))
    prices._reset()
    prices.register_model_price("mine", input=2.5, output=10)
    assert prices.load(path) is True
    assert prices.estimate("mine", 1_000_000).amount == Decimal("2.5")  # survived the swap


def test_load_of_a_missing_or_junk_file_keeps_the_last_good_table(tmp_path):
    assert prices.load(str(tmp_path / "nope.json")) is False
    assert prices.source() == "bundled"
    junk = tmp_path / "junk.json"
    junk.write_text("not json at all", encoding="utf-8")
    assert prices.load(str(junk)) is False
    empty = tmp_path / "empty.json"
    empty.write_text('{"models": {}}', encoding="utf-8")
    assert prices.load(str(empty)) is False
    assert prices.estimate("gpt-4o", 1000).amount == Decimal("0.0025")


def test_there_is_no_implicit_cache():
    """`refresh()` is in-memory only, per process. A library that quietly writes price files is a
    side effect, and a hidden cache is how prices go INVISIBLY stale."""
    with pytest.raises(AttributeError, match=r"save\(path\)"):
        _ = prices.cache
    with pytest.raises(AttributeError, match=r"save\(path\)"):
        _ = prices.persist


def test_teach_hook_points_at_explain():
    with pytest.raises(AttributeError, match="explain"):
        _ = prices.why
    with pytest.raises(AttributeError, match="SYNCHRONOUS"):
        _ = prices.refresh_async


def test_pass_through_string_rates_are_coerced_at_the_swap(monkeypatch):
    """MEASURED 2026-08-01 by the live cross-language trace, not by any offline test: every unit
    fixture used numeric rates, so nothing exercised a quoted one. A pass-through `refresh(url)`
    hands the parsed rate objects straight to `estimate()`, and `parse_float=Decimal` leaves a JSON
    *string* a string. `estimate` coerces on every read so Python survived it; the TS twin threw.
    Both now coerce once at the swap, so `explain()` also hands callers real Decimals."""
    raw = """
    {"_updated": "2026-08-01",
     "models": {"gpt-4o": {"input": "0.0000025", "output": "0.00001", "cached": "0.00000125"}}}
    """
    _install_fetch(monkeypatch, raw)
    assert prices.refresh() is True
    assert prices.estimate("gpt-4o", 1000, 500, cached_tokens=200).amount == Decimal("0.00725")
    rates = prices.explain("gpt-4o").rates
    assert rates is not None
    assert all(isinstance(v, Decimal) for v in rates.values())


def test_the_feed_schema_is_number_literals_and_stays_exact(monkeypatch):
    """`prices/1` specifies JSON number literals; the cendor-prices feed emits them, and
    `parse_float=Decimal` reads the token text verbatim, so a long decimal survives to the last
    digit rather than going through a float."""
    _install_fetch(
        monkeypatch,
        '{"models": {"m": {"input": 0.000000123456789012345, "output": 0}}}',
    )
    prices.refresh()
    assert prices.estimate("m", 1_000_000).amount == Decimal("0.123456789012345")
