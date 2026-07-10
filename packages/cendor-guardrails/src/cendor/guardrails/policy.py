"""Config-as-data — declare a set of deterministic guardrails in a versioned file. See the docs.

:func:`load_policy` reads a JSON (stdlib) or YAML (optional ``[yaml]`` extra) document and builds
the :class:`~cendor.guardrails.decision.Guardrail` list you hand to ``Agent(guardrails=…)`` /
``install()`` / ``apply()``. The point is **evidence**: the file's content hash and its declared
version are stamped onto every :class:`~cendor.guardrails.decision.GuardrailDecision` those
guardrails emit (via :attr:`Guardrail.metadata`), so the audit chain proves *which* policy was
active when a call was gated — needing no event-shape change, and a property no rival gate has.

Only the **deterministic built-ins** are constructible from data (rules that need a callable or a
live client — ``custom`` / ``llm_judge`` / the classifiers / the hosted rails — are wired in code,
not a file). The document shape:

```yaml
version: "2026-07-09"          # your policy version (any string); recorded on every decision
guardrails:
  - rule: keyword_deny
    args: { words: ["ignore previous instructions"] }
    stage: input
    action: block
  - rule: regex_rule
    args: { pattern: "\\d{3}-\\d{2}-\\d{4}" }     # a US SSN shape
    stage: [input, output]
    action: redact
  - rule: length_bounds
    args: { max_tokens: 4000 }
```
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from .decision import ACTIONS, STAGES, Guardrail
from .rules import (
    json_schema,
    keyword_deny,
    length_bounds,
    regex_rule,
    url_allowlist,
    url_deny,
)

__all__ = ["load_policy", "LoadedPolicy", "POLICY_RULES", "policy_schema"]

#: The rules that can be built from data alone (deterministic, no callable/client argument). A file
#: policy is a *declarative* artifact — a rule needing a Python callable or a cloud client is wired
#: in code, so those are deliberately absent here.
POLICY_RULES: dict[str, Any] = {
    "keyword_deny": keyword_deny,
    "regex_rule": regex_rule,
    "url_allowlist": url_allowlist,
    "url_deny": url_deny,
    "length_bounds": length_bounds,
    "json_schema": json_schema,
}


class LoadedPolicy(list):  # list[Guardrail] with provenance
    """The guardrails from a policy file — a plain ``list[Guardrail]`` (pass it straight to
    ``Agent(guardrails=…)`` / ``install()``) that also carries the provenance stamped onto every
    decision: :attr:`policy_hash` (``"sha256:<hex>"`` of the canonical document) and
    :attr:`policy_version` (the file's ``version`` field)."""

    def __init__(self, guardrails: Any, *, policy_hash: str, policy_version: str) -> None:
        super().__init__(guardrails)
        self.policy_hash = policy_hash
        self.policy_version = policy_version


def _digest(config: Mapping[str, Any]) -> str:
    """A stable content hash of the whole document — canonical JSON, so key order / whitespace in
    the source file never change it."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read(source: str | Path, fmt: str | None) -> Mapping[str, Any]:
    path = Path(source)
    text = path.read_text(encoding="utf-8")
    resolved = fmt or ("yaml" if path.suffix.lower() in {".yaml", ".yml"} else "json")
    if resolved == "yaml":
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "load_policy() needs PyYAML to read a YAML policy: "
                "pip install 'cendor-guardrails[yaml]' (or use a .json policy)."
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, Mapping):
        raise ValueError(f"policy document must be a mapping, got {type(data).__name__}")
    return data


def _coerce_stage(stage: Any) -> Any:
    return tuple(stage) if isinstance(stage, list) else stage


@lru_cache(maxsize=1)
def policy_schema() -> dict[str, Any]:
    """The JSON Schema (Draft 2020-12) for a policy document — the same ``policy.schema.json``
    shipped in the package. Reference it from your policy file's ``$schema`` for editor
    autocomplete, or use it in your own tooling. Passing ``validate=True`` to :func:`load_policy`
    checks a document against this shape with the stdlib (no ``jsonschema`` dependency)."""
    from importlib.resources import files

    text = (files("cendor.guardrails") / "policy.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def _validate_document(config: Mapping[str, Any]) -> None:
    """A small, stdlib-only structural check of a policy document (opt-in via ``validate=True``) —
    clearer, earlier errors than letting a factory raise. Verifies the top-level shape, that each
    entry names a known declarative rule, and that ``stage`` / ``action`` are in range. It is not a
    full JSON-Schema engine; the shipped ``policy.schema.json`` is the reference for tooling."""
    if "version" in config and not isinstance(config["version"], str):
        raise ValueError("policy 'version' must be a string")
    entries = config.get("guardrails")
    if not isinstance(entries, list):
        raise ValueError("policy document must have a 'guardrails' list")
    for i, entry in enumerate(entries):
        where = f"guardrails[{i}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{where} must be a mapping, got {type(entry).__name__}")
        rule = entry.get("rule")
        if str(rule) not in POLICY_RULES:
            raise ValueError(
                f"{where}: unknown or non-declarative rule {rule!r}; "
                f"policy files support {sorted(POLICY_RULES)}"
            )
        if "args" in entry and not isinstance(entry["args"], Mapping):
            raise ValueError(f"{where}.args must be a mapping")
        if "action" in entry and entry["action"] not in ACTIONS:
            raise ValueError(f"{where}.action {entry['action']!r} must be one of {list(ACTIONS)}")
        if "stage" in entry:
            stages = entry["stage"] if isinstance(entry["stage"], list) else [entry["stage"]]
            for s in stages:
                if s not in STAGES:
                    raise ValueError(f"{where}.stage {s!r} must be one of {list(STAGES)}")


def load_policy(
    source: str | Path | Mapping[str, Any],
    *,
    format: str | None = None,
    validate: bool = False,
) -> LoadedPolicy:
    """Build a :class:`LoadedPolicy` (a ``list[Guardrail]``) from a JSON/YAML file or a parsed
    mapping.

    Args:
        source: A path to a ``.json`` / ``.yaml`` / ``.yml`` file, or a mapping already parsed.
        format: Force ``"json"`` / ``"yaml"`` instead of inferring from the file suffix. Ignored
            when ``source`` is a mapping.
        validate: When ``True``, run a stdlib structural check (:func:`policy_schema`) first, so a
            malformed document fails with a clear ``$.path`` error before any rule is built.

    Returns:
        A :class:`LoadedPolicy` — usable anywhere a guardrail list is. Every guardrail is stamped
        with ``policy_hash`` + ``policy_version`` in its
        :attr:`~cendor.guardrails.decision.Guardrail.metadata`, so each
        :class:`~cendor.guardrails.decision.GuardrailDecision` records which policy was active.

    Raises:
        ValueError: on a malformed document or an unknown/uninstantiable rule name.
        ImportError: for a YAML source without the ``[yaml]`` extra.
    """
    config: Mapping[str, Any] = source if isinstance(source, Mapping) else _read(source, format)
    if validate:
        _validate_document(config)
    entries = config.get("guardrails")
    if not isinstance(entries, list):
        raise ValueError("policy document must have a 'guardrails' list")

    policy_hash = _digest(config)
    policy_version = str(config.get("version", ""))
    stamp = {"policy_hash": policy_hash, "policy_version": policy_version}

    guardrails: list[Guardrail] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"guardrails[{i}] must be a mapping, got {type(entry).__name__}")
        rule = entry.get("rule")
        factory = POLICY_RULES.get(str(rule))
        if factory is None:
            raise ValueError(
                f"guardrails[{i}]: unknown or non-declarative rule {rule!r}; "
                f"policy files support {sorted(POLICY_RULES)} "
                "(rules needing a callable or a client are wired in code)"
            )
        kwargs: dict[str, Any] = dict(entry.get("args", {}))
        if "stage" in entry:
            kwargs["stage"] = _coerce_stage(entry["stage"])
        if "action" in entry:
            kwargs["action"] = entry["action"]
        if "name" in entry:
            kwargs["name"] = entry["name"]
        try:
            g = factory(**kwargs)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"guardrails[{i}] ({rule}): bad arguments — {exc}") from exc
        g.metadata.update(stamp)
        guardrails.append(g)

    return LoadedPolicy(guardrails, policy_hash=policy_hash, policy_version=policy_version)
