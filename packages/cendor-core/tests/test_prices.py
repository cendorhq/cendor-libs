"""Prices: exact Decimal estimates from the bundled snapshot + offline refresh fallback."""

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
    assert prices.snapshot_date() == "2026-07-11"
    assert "claude-opus-4-8" in prices.models()


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
        seen["url"] = url
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
        seen["url"] = url
        yield io.BytesIO(raw_text.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_sources_lists_builtin_adapters():
    assert prices.sources() == ["azure", "litellm", "openrouter"]


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
