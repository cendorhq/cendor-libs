"""Golden token counts: known input -> expected count per model family. No network.

OpenAI counts are forced onto the heuristic path (tiktoken absent / patched off) so the
goldens are deterministic regardless of whether tiktoken happens to be installed.
"""

import pytest
from cendor.core import tokens


@pytest.fixture(autouse=True)
def _no_tiktoken(monkeypatch):
    # Force the offline heuristic so goldens are stable whether or not tiktoken is installed.
    monkeypatch.setattr(tokens, "_tiktoken_encoding", lambda model: None)
    monkeypatch.setattr(tokens, "_o200k", lambda: None)
    yield


def test_family_detection():
    assert tokens.family("gpt-4o") == "openai"
    assert tokens.family("o3-mini") == "openai"
    assert tokens.family("claude-opus-4-8") == "anthropic"
    assert tokens.family("mistral-large") == "default"


def test_family_detection_covers_oseries_and_finetunes():
    # H2: a new o-series id and an OpenAI fine-tune must resolve to the openai family (not default).
    assert tokens.family("o5-mini") == "openai"
    assert tokens.family("o1") == "openai"
    assert tokens.family("ft:gpt-4o:acme::abc123") == "openai"
    assert tokens.family("ft:gpt-4o-2024-08-06:acme::xyz") == "openai"
    # non-OpenAI open/hosted models stay "default" (but now count via the o200k proxy, see below).
    assert tokens.family("llama-3.1-70b") == "default"
    assert tokens.family("deepseek-chat") == "default"
    # "ollama"/"olmo" must not be mistaken for an o-series id (no digit after the leading "o").
    assert tokens.family("ollama-thing") == "default"
    assert tokens.family("olmo-7b") == "default"


def test_golden_text_counts():
    # openai (no tiktoken) @4.0 chars/token: ceil(11/4) = 3.
    assert tokens.count("hello world", "gpt-4o") == 3
    # anthropic uses the blended subword estimator (deterministic).
    assert tokens.count("hello world", "claude-opus-4-8") == 3
    assert tokens.count("", "gpt-4o") == 0


def test_anthropic_estimator_is_structure_aware():
    # The blended subword estimator counts punctuation/code-dense text higher than a flat chars/N
    # divisor would — closer to how BPE tokenizers actually behave. Compared against the OpenAI
    # char heuristic (openai has no tiktoken under the fixture); mistral/default now share the
    # subword estimator, so the contrast is with the char-divisor path, not `default`.
    dense = "def f(x): return x+1;"
    assert tokens.count(dense, "claude-opus-4-8") > tokens.count(dense, "gpt-4o")
    # deterministic
    assert tokens.count(dense, "claude-opus-4-8") == tokens.count(dense, "claude-sonnet-4-6")


def test_golden_message_counts_include_overhead():
    # priming(3) + per-message overhead(4) + content(3) = 10
    msgs = [{"role": "user", "content": "hello world"}]
    assert tokens.count(msgs, "gpt-4o") == 10


def test_multimodal_content_blocks_sum_text():
    content = [{"type": "text", "text": "hello world"}, {"type": "image"}]
    msgs = [{"role": "user", "content": content}]
    assert tokens.count(msgs, "gpt-4o") == 10


def test_register_overrides_family():
    tokens.register("default", lambda t, m: 42)
    try:
        assert tokens.count("anything", "some-unknown-model") == 42
    finally:
        tokens._counters.clear()


def test_method_reports_offline_paths():
    # The fixture forces "no tokenizer installed", so every family is the heuristic fallback.
    assert tokens.method("gpt-4o") == "heuristic"
    assert tokens.method("claude-opus-4-8") == "heuristic"
    assert tokens.is_exact("gpt-4o") is False


def test_method_reflects_active_tokenizer(monkeypatch):
    # Fake the tokenizers so the path logic is tested without requiring the optional dep.
    monkeypatch.setattr(
        tokens, "_tiktoken_encoding", lambda m: object() if tokens.family(m) == "openai" else None
    )
    monkeypatch.setattr(tokens, "_o200k", lambda: object())
    # A known OpenAI id has a model-native tiktoken encoding -> exact; an unknown one silently
    # falls back to o200k -> honestly reported as bpe-estimate, not exact.
    monkeypatch.setattr(tokens, "_openai_encoding_is_native", lambda m: m == "gpt-4o")
    assert tokens.method("gpt-4o") == "exact"
    assert tokens.is_exact("gpt-4o") is True
    assert tokens.method("gpt-9-future") == "bpe-estimate"  # unknown OpenAI id -> o200k fallback
    assert tokens.is_exact("gpt-9-future") is False
    assert tokens.method("claude-opus-4-8") == "bpe-estimate"
    assert tokens.method("gemini-2.0-flash") == "bpe-estimate"


def test_method_registered_counter_wins():
    tokens.register("default", lambda t, m: 1)
    try:
        assert tokens.method("some-unknown-model") == "registered"
    finally:
        tokens._counters.clear()


def test_count_uses_o200k_estimate_for_non_openai_when_available(monkeypatch):
    class _Enc:
        def encode(self, text):
            return [0] * 7  # pretend o200k tokenizes this to 7 tokens

    monkeypatch.setattr(tokens, "_o200k", lambda: _Enc())
    # Claude and every non-OpenAI/hosted "default" model now route through the o200k BPE proxy
    # instead of the char/subword heuristic (H2) — so they count identically.
    assert tokens.count("some sample text", "claude-opus-4-8") == 7
    assert tokens.count("some sample text", "llama-3.1-70b") == 7
    assert tokens.count("some sample text", "mistral-large") == 7
    assert tokens.count("some sample text", "deepseek-chat") == 7


def test_hosted_open_models_report_bpe_estimate(monkeypatch):
    # H2: llama/mistral/deepseek/qwen (family "default") route through the o200k BPE proxy — no
    # longer the +12% char heuristic. `method` must honestly report bpe-estimate.
    monkeypatch.setattr(tokens, "_o200k", lambda: object())
    for m in ("llama-3.1-70b", "mistral-large", "deepseek-chat", "qwen2.5-72b"):
        assert tokens.method(m) == "bpe-estimate", m


def test_new_oseries_and_finetunes_route_correctly(monkeypatch):
    # H2: a new o-series id (openai family, no model-native encoding) falls back to the o200k
    # proxy (bpe-estimate); an OpenAI fine-tune maps to its base model's native encoding (exact).
    monkeypatch.setattr(
        tokens, "_tiktoken_encoding", lambda m: object() if tokens.family(m) == "openai" else None
    )
    monkeypatch.setattr(tokens, "_o200k", lambda: object())
    monkeypatch.setattr(
        tokens, "_openai_encoding_is_native", lambda m: tokens._base_model(m) == "gpt-4o"
    )
    assert tokens.method("o5-mini") == "bpe-estimate"
    assert tokens.method("ft:gpt-4o:acme::abc") == "exact"
    assert tokens.is_exact("ft:gpt-4o:acme::abc") is True
