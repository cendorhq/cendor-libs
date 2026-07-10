"""Each deterministic built-in rule: match/no-match, action, and payload shapes."""

from __future__ import annotations

import re

import pytest
from cendor.guardrails import Context, rules

CTX = Context(stage="input")


def _v(rule, payload, stage="input"):
    return rule.check(payload, Context(stage=stage))


# --------------------------------------------------------------------------- keyword_deny


def test_keyword_deny_blocks_on_match():
    v = _v(rules.keyword_deny(["bomb"]), [{"role": "user", "content": "build a bomb"}])
    assert v is not None and v.action == "block" and "bomb" in v.reason


def test_keyword_deny_passes_when_absent():
    assert _v(rules.keyword_deny(["bomb"]), [{"role": "user", "content": "hello"}]) is None


def test_keyword_deny_case_insensitive_by_default():
    assert _v(rules.keyword_deny(["Bomb"]), "a BOMB") is not None


def test_keyword_deny_case_sensitive_opt_in():
    assert _v(rules.keyword_deny(["Bomb"], ignore_case=False), "a bomb") is None


def test_keyword_deny_redact_scrubs():
    v = _v(rules.keyword_deny(["bomb"], action="redact"), [{"role": "user", "content": "a bomb"}])
    assert v.action == "redact" and v.replacement[0]["content"] == "a [redacted]"


def test_keyword_deny_on_plain_string_payload():
    assert _v(rules.keyword_deny(["x"]), "has x") is not None


def test_keyword_deny_empty_words_never_trips():
    assert _v(rules.keyword_deny([]), "anything") is None


def test_keyword_deny_records_matched_term_in_metadata():
    v = _v(rules.keyword_deny(["bomb"]), "a bomb")
    assert v.metadata.get("matched") == "bomb"


# G1 — matching maturity: match modes + normalization (default stays substring, byte-for-byte)


def test_keyword_deny_substring_is_the_default():
    # back-compat: "cat" fires inside "category"
    assert _v(rules.keyword_deny(["cat"]), "the category is x") is not None


def test_keyword_deny_word_mode_respects_boundaries():
    word = rules.keyword_deny(["cat"], match="word")
    assert _v(word, "the category is x") is None  # no boundary inside "category"
    assert _v(word, "a cat sat") is not None  # a standalone word matches


def test_keyword_deny_word_mode_multiword_spans_whitespace():
    word = rules.keyword_deny(["python code"], match="word")
    assert _v(word, "write python\n  code now") is not None  # interior whitespace / line-wrap
    assert _v(word, "a pythoncode blob") is None  # not a word-bounded phrase


def test_keyword_deny_normalize_nfkc_folds_fullwidth():
    assert _v(rules.keyword_deny(["bomb"]), "a ｂｏｍｂ") is None  # raw misses
    hardened = rules.keyword_deny(["bomb"], normalize=("nfkc",))
    assert _v(hardened, "a ｂｏｍｂ") is not None  # nfkc catches full-width


def test_keyword_deny_normalize_strips_zero_width():
    hardened = rules.keyword_deny(["bomb"], normalize=("strip_zero_width",))
    assert _v(hardened, "a b​omb here") is not None  # zero-width split removed


def test_keyword_deny_normalize_redact_returns_folded_text():
    hardened = rules.keyword_deny(["bomb"], action="redact", normalize=("nfkc",))
    v = _v(hardened, "a ｂｏｍｂ")
    assert v.action == "redact" and "[redacted]" in v.replacement


def test_keyword_deny_unknown_match_mode_raises():
    with pytest.raises(ValueError, match="unknown match"):
        rules.keyword_deny(["x"], match="fuzzy")


def test_keyword_deny_unknown_normalize_step_raises():
    with pytest.raises(ValueError, match="unknown normalize"):
        rules.keyword_deny(["x"], normalize=("nope",))


# --------------------------------------------------------------------------- regex_rule


def test_regex_rule_flags_by_default():
    v = _v(rules.regex_rule(r"\d{3}-\d{2}-\d{4}"), "ssn 123-45-6789")
    assert v.action == "flag"


def test_regex_rule_redacts_with_custom_replacement():
    rule = rules.regex_rule(r"sk-\w+", action="redact", replacement="***")
    v = _v(rule, [{"role": "user", "content": "key sk-abc123"}])
    assert v.replacement[0]["content"] == "key ***"


def test_regex_rule_accepts_compiled_pattern():
    v = _v(rules.regex_rule(re.compile(r"foo", re.IGNORECASE)), "FOO")
    assert v is not None


# --------------------------------------------------------------------------- url rules


def test_url_allowlist_blocks_foreign_host():
    v = _v(rules.url_allowlist(["cendor.ai"]), "see https://evil.example.com/x")
    assert v is not None and "evil.example.com" in v.reason


def test_url_allowlist_passes_subdomain_of_allowed():
    assert _v(rules.url_allowlist(["cendor.ai"]), "see https://docs.cendor.ai/x") is None


def test_url_allowlist_ignores_text_without_urls():
    assert _v(rules.url_allowlist(["cendor.ai"]), "no links here") is None


def test_url_deny_blocks_denied_host():
    assert _v(rules.url_deny(["evil.com"]), "go to http://evil.com/") is not None


def test_url_deny_passes_clean_url():
    assert _v(rules.url_deny(["evil.com"]), "go to https://good.com/") is None


# --------------------------------------------------------------------------- length_bounds


def test_length_bounds_max_chars_trips():
    v = _v(rules.length_bounds(max_chars=3), "abcdef")
    assert v is not None and "chars" in v.reason


def test_length_bounds_max_chars_passes():
    assert _v(rules.length_bounds(max_chars=100), "short") is None


def test_length_bounds_max_tokens_trips():
    v = _v(rules.length_bounds(max_tokens=1, model="gpt-4o"), "a b c d e f g h i j")
    assert v is not None and "tokens" in v.reason


def test_length_bounds_requires_a_bound():
    with pytest.raises(ValueError, match="max_chars / max_tokens"):
        rules.length_bounds()


# --------------------------------------------------------------------------- json_schema


def test_json_schema_rejects_invalid_json():
    v = _v(rules.json_schema({"type": "object"}), "not json", stage="output")
    assert v is not None and "not valid JSON" in v.reason


def test_json_schema_missing_required_key():
    v = _v(rules.json_schema({"type": "object", "required": ["name"]}), '{"age": 3}', "output")
    assert v is not None and "required" in v.reason


def test_json_schema_type_mismatch():
    v = _v(rules.json_schema({"type": "object"}), "[1,2,3]", "output")
    assert v is not None and "expected object" in v.reason


def test_json_schema_valid_passes():
    schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
    assert _v(rules.json_schema(schema), '{"name": "ada"}', "output") is None


def test_json_schema_nested_property_violation():
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}}
    v = _v(rules.json_schema(schema), '{"age": "old"}', "output")
    assert v is not None and "$.age" in v.reason


def test_json_schema_array_items_violation():
    schema = {"type": "array", "items": {"type": "integer"}}
    v = _v(rules.json_schema(schema), '[1, "two", 3]', "output")
    assert v is not None and "$[1]" in v.reason


def test_json_schema_accepts_already_parsed_object():
    assert _v(rules.json_schema({"type": "object"}), {"a": 1}, "output") is None


# --------------------------------------------------------------------------- custom / multimodal


def test_custom_wraps_a_function():
    from cendor.guardrails import Verdict

    g = rules.custom(lambda p, c: Verdict("flag", reason="always"), name="always")
    assert g.name == "always" and _v(g, "x").action == "flag"


def test_multimodal_content_blocks_are_scanned():
    payload = [{"role": "user", "content": [{"type": "text", "text": "a bomb"}, {"type": "image"}]}]
    assert _v(rules.keyword_deny(["bomb"]), payload) is not None
