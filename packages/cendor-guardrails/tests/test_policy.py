"""Config-as-data: load_policy() builds guardrails from a JSON/dict document and stamps every
decision with the policy hash + version. No network. docs/guardrails.md."""

from __future__ import annotations

import json

import pytest
from cendor.guardrails import GuardrailTripped, LoadedPolicy, apply, evaluate, load_policy

_DOC = {
    "version": "2026-07-09",
    "guardrails": [
        {
            "rule": "keyword_deny",
            "args": {"words": ["forbidden"]},
            "stage": "input",
            "action": "block",
        },
        {
            "rule": "regex_rule",
            "args": {"pattern": r"\d{3}-\d{2}-\d{4}"},
            "action": "redact",
            "stage": "input",
        },
        {"rule": "length_bounds", "args": {"max_chars": 20}, "stage": "input", "action": "flag"},
    ],
}


def test_load_policy_from_dict_builds_guardrails():
    policy = load_policy(_DOC)
    assert isinstance(policy, LoadedPolicy)
    assert len(policy) == 3
    assert policy.policy_version == "2026-07-09"
    assert policy.policy_hash.startswith("sha256:")


def test_loaded_policy_is_usable_as_a_guardrail_list():
    policy = load_policy(_DOC)
    with pytest.raises(GuardrailTripped):
        apply(policy, "input", "this is forbidden")


def test_policy_hash_and_version_stamped_on_every_decision(decisions):
    policy = load_policy(_DOC)
    apply(policy, "input", "x" * 40)  # trips length_bounds (flag → continues)
    assert decisions
    d = decisions[-1]
    assert d.metadata["policy_hash"] == policy.policy_hash
    assert d.metadata["policy_version"] == "2026-07-09"


def test_hash_is_stable_and_content_addressed():
    # same content, different key order / whitespace → identical hash
    reordered = {"guardrails": _DOC["guardrails"], "version": _DOC["version"]}
    assert load_policy(_DOC).policy_hash == load_policy(reordered).policy_hash
    # a changed rule → a different hash
    changed = json.loads(json.dumps(_DOC))
    changed["guardrails"][0]["args"]["words"] = ["other"]
    assert load_policy(changed).policy_hash != load_policy(_DOC).policy_hash


def test_load_policy_from_json_file(tmp_path):
    path = tmp_path / "guardrails.json"
    path.write_text(json.dumps(_DOC), encoding="utf-8")
    policy = load_policy(path)
    assert len(policy) == 3 and policy.policy_version == "2026-07-09"


def test_redact_rule_from_policy_applies():
    policy = load_policy(_DOC)
    payload, decisions = evaluate(policy, "input", "my ssn is 123-45-6789")
    assert "[redacted]" in payload
    assert any(d.action == "redact" for d in decisions)


def test_unknown_rule_raises_clear_error():
    with pytest.raises(ValueError, match="unknown or non-declarative rule"):
        load_policy({"guardrails": [{"rule": "llm_judge"}]})


def test_missing_guardrails_list_raises():
    with pytest.raises(ValueError, match="guardrails"):
        load_policy({"version": "1"})


def test_bad_rule_args_raise_value_error():
    with pytest.raises(ValueError, match="bad arguments"):
        load_policy({"guardrails": [{"rule": "length_bounds", "args": {}}]})  # needs a bound


try:
    import yaml as _yaml  # noqa: F401

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


@pytest.mark.skipif(
    _HAS_YAML, reason="PyYAML installed — the missing-extra path can't be exercised"
)
def test_yaml_without_extra_raises_actionable_error(tmp_path):
    # A clear, actionable ImportError naming the extra when PyYAML is absent
    path = tmp_path / "guardrails.yaml"
    path.write_text("version: '1'\nguardrails: []\n", encoding="utf-8")
    with pytest.raises(ImportError, match=r"cendor-guardrails\[yaml\]"):
        load_policy(path)


@pytest.mark.skipif(not _HAS_YAML, reason="PyYAML not installed")
def test_yaml_policy_loads_when_extra_present(tmp_path):
    path = tmp_path / "guardrails.yaml"
    path.write_text(
        "version: '2026-07-09'\nguardrails:\n  - rule: keyword_deny\n    args: {words: [nope]}\n",
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert len(policy) == 1 and policy.policy_version == "2026-07-09"
