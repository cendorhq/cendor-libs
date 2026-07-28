"""``guard()`` enforcement (roadmap phase 2): block + flag on core's interceptor seam. No network.

The guard *enforces* (raise to block / proceed) on ``core``'s seam; ``acttrace`` *records* the
decision as a tamper-evident ``policy_flag``. These stay separate by design.
"""

from types import SimpleNamespace

import pytest
from cendor.acttrace import AuditLog, Policy, PolicyViolation, guard, verify
from cendor.core import bus, instrument
from cendor.core.instrument import MISS, add_interceptor, instrument_tool, remove_interceptor
from cendor.core.types import LLMCall


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


def _client(calls):
    class Completions:
        def create(self, **kwargs):
            calls["n"] += 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def _msgs(text):
    return [{"role": "user", "content": text}]


def test_guard_blocks_disallowed_call_and_records_flag(tmp_path):
    # End-to-end: a blocked call never runs, the refusal is a policy_flag(blocked), chain verifies.
    path = tmp_path / "g.jsonl"
    log = AuditLog(system="s", path=str(path))
    calls = {"n": 0}
    client = _client(calls)
    g = guard(Policy.pci(), audit=log)  # financial -> block

    add_interceptor(g)
    try:
        with pytest.raises(PolicyViolation) as exc:
            client.chat.completions.create(
                model="gpt-4o", messages=_msgs("card 4111 1111 1111 1111")
            )
    finally:
        remove_interceptor(g)
        log.detach()

    assert calls["n"] == 0  # blocked before it ran
    assert [f.category for f in exc.value.findings] == ["credit_card"]
    flags = [e for e in log.entries if e.type == "policy_flag"]
    assert len(flags) == 1 and flags[0].payload["action"] == "blocked"
    assert flags[0].payload["data"] == ["credit_card"]
    assert not any(e.type == "llm_call" for e in log.entries)  # blocked call left no llm_call
    assert verify(str(path))[0] is True


def test_guard_flag_action_proceeds_and_records(tmp_path):
    log = AuditLog(system="s", path=str(tmp_path / "f.jsonl"))
    calls = {"n": 0}
    client = _client(calls)
    g = guard(Policy.default(), audit=log)  # credit_card -> flag under default

    add_interceptor(g)
    try:
        client.chat.completions.create(model="gpt-4o", messages=_msgs("card 4111 1111 1111 1111"))
    finally:
        remove_interceptor(g)
        log.detach()

    assert calls["n"] == 1  # flag proceeds untouched
    flags = [e for e in log.entries if e.type == "policy_flag"]
    assert any(
        f.payload["action"] == "flagged" and f.payload["data"] == ["credit_card"] for f in flags
    )
    assert any(e.type == "llm_call" for e in log.entries)  # the call did run


def test_guard_redact_before_send_scrubs_the_provider_payload(tmp_path):
    # Phase 2b: a redact-action category is scrubbed from what the *provider* receives (via core's
    # Reroute(messages=…)), and recorded as action="redacted". Redaction is now a real pre-send
    # control, not just record-only.
    received = {}

    class Completions:
        def create(self, **kwargs):
            received["messages"] = kwargs.get("messages")
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    log = AuditLog(system="s", path=str(tmp_path / "r.jsonl"))
    g = guard(Policy.default(), audit=log)  # email -> redact under default

    add_interceptor(g)
    try:
        client.chat.completions.create(
            model="gpt-4o", messages=_msgs("mail me at alice@example.com")
        )
    finally:
        remove_interceptor(g)
        log.detach()

    # the provider received the scrubbed content — the email never left the process
    assert "alice@example.com" not in str(received["messages"])
    assert "<redacted>" in str(received["messages"])
    flags = [e for e in log.entries if e.type == "policy_flag"]
    redacted = next(f for f in flags if f.payload["data"] == ["email"])
    assert redacted.payload["action"] == "redacted"


# --------------------------------------------------------------- Gemini `contents` back-map


def _gemini_client(received):
    """A google-genai-shaped client that records the ``contents`` the SDK would have been handed."""

    class Models:
        def generate_content(self, **kwargs):
            received["contents"] = kwargs.get("contents")
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1)
            )

    return instrument(SimpleNamespace(models=Models()))


def test_guard_redact_before_send_keeps_a_gemini_string_contents_a_string():
    # core's `_extract_request` normalizes a non-list `contents` into one canonical {role, content}
    # message so interceptors see every provider alike. Writing that message object straight back
    # onto `contents` is what google-genai rejects (it takes a str | Content | Part), so a redacted
    # Gemini call could not proceed at all — the redaction fired and then the request raised.
    received = {}
    client = _gemini_client(received)
    g = guard(Policy.default())  # email -> redact under default

    add_interceptor(g)
    try:
        client.models.generate_content(
            model="gemini-2.0-flash", contents="repeat back: mail me at alice@example.com"
        )
    finally:
        remove_interceptor(g)

    assert "content" not in str(received["contents"]), "a message list was written into `contents`"
    assert isinstance(received["contents"], str), "a string `contents` came back as a non-string"
    assert "alice@example.com" not in received["contents"]  # redaction actually applied
    assert "<redacted>" in received["contents"]


def test_guard_redact_before_send_keeps_a_gemini_contents_list_in_content_shape():
    # The case that was NOT broken: an already-Gemini-shaped `contents` list is passed through
    # `_extract_request` untouched and the guard's scrub preserves its structure. Pinned so a future
    # change to the back-map can't regress it.
    received = {}
    client = _gemini_client(received)
    g = guard(Policy.default())

    add_interceptor(g)
    try:
        client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[{"role": "user", "parts": [{"text": "mail me at alice@example.com"}]}],
        )
    finally:
        remove_interceptor(g)

    contents = received["contents"]
    assert isinstance(contents, list)
    assert contents[0]["role"] == "user"
    assert isinstance(contents[0].get("parts"), list), "the Content shape lost its `parts`"
    assert "<redacted>" in contents[0]["parts"][0]["text"]  # redaction actually applied
    assert "alice@example.com" not in str(contents)
    assert '"content"' not in str(contents).replace("'", '"'), "an OpenAI message key leaked in"


def test_guard_without_audit_still_enforces():
    calls = {"n": 0}
    client = _client(calls)
    g = guard(Policy.strict())  # no audit; secrets -> block

    add_interceptor(g)
    try:
        with pytest.raises(PolicyViolation):
            client.chat.completions.create(
                model="gpt-4o", messages=_msgs("key sk-ant-api03-ABCDEFGH12345678")
            )
    finally:
        remove_interceptor(g)
    assert calls["n"] == 0  # enforced even with nothing to record on


def test_guard_clean_call_proceeds_untouched():
    calls = {"n": 0}
    client = _client(calls)
    g = guard(Policy.strict())

    add_interceptor(g)
    try:
        client.chat.completions.create(model="gpt-4o", messages=_msgs("how do refunds work?"))
    finally:
        remove_interceptor(g)
    assert calls["n"] == 1  # nothing detected -> MISS -> call runs


def test_guard_custom_on_block_exception_class():
    class Blocked(Exception): ...

    calls = {"n": 0}
    client = _client(calls)
    g = guard(Policy.pci(), on_block=Blocked)

    add_interceptor(g)
    try:
        with pytest.raises(Blocked):
            client.chat.completions.create(
                model="gpt-4o", messages=_msgs("card 4111 1111 1111 1111")
            )
    finally:
        remove_interceptor(g)
    assert calls["n"] == 0


def test_guard_custom_on_block_factory_gets_findings():
    seen = {}

    def make_exc(findings):
        seen["cats"] = [f.category for f in findings]
        return RuntimeError("blocked by factory")

    calls = {"n": 0}
    client = _client(calls)
    g = guard(Policy.pci(), on_block=make_exc)

    add_interceptor(g)
    try:
        with pytest.raises(RuntimeError):
            client.chat.completions.create(
                model="gpt-4o", messages=_msgs("card 4111 1111 1111 1111")
            )
    finally:
        remove_interceptor(g)
    assert seen["cats"] == ["credit_card"]


def test_guard_blocks_tool_call_arguments(tmp_path):
    log = AuditLog(system="s", path=str(tmp_path / "t.jsonl"))
    ran = {"n": 0}
    g = guard(Policy.pci(), audit=log)

    @instrument_tool("charge")
    def charge(card):
        ran["n"] += 1
        return "charged"

    add_interceptor(g)
    try:
        with pytest.raises(PolicyViolation):
            charge("4111 1111 1111 1111")
    finally:
        remove_interceptor(g)
        log.detach()

    assert ran["n"] == 0  # the tool body never executed
    flags = [e for e in log.entries if e.type == "policy_flag"]
    assert flags and flags[0].payload["action"] == "blocked"
    assert verify(str(tmp_path / "t.jsonl"))[0] is True


def test_guard_redact_on_tool_arguments_is_record_only(tmp_path):
    # Tools have no message-rewrite seam (Reroute is for model calls), so a redact-action category
    # in tool arguments is recorded and the tool still runs — block is the pre-send control there.
    log = AuditLog(system="s", path=str(tmp_path / "tr.jsonl"))
    ran = {"n": 0}
    g = guard(Policy.default(), audit=log)  # email -> redact under default

    @instrument_tool("notify")
    def notify(to):
        ran["n"] += 1
        return "sent"

    add_interceptor(g)
    try:
        notify("alice@example.com")
    finally:
        remove_interceptor(g)
        log.detach()

    assert ran["n"] == 1  # the tool ran (record-only)
    flags = [e for e in log.entries if e.type == "policy_flag"]
    # the guard's own record-only flag (the AuditLog also auto-flags the emitted tool_call itself)
    note = next(f for f in flags if "tool arguments unchanged" in f.payload.get("reason", ""))
    assert note.payload["action"] == "flagged" and note.payload["data"] == ["email"]


def test_guard_default_policy_never_blocks():
    # Policy.default() has no block actions — the guard only flags/redacts, never raises.
    calls = {"n": 0}
    client = _client(calls)
    g = guard(Policy.default())

    add_interceptor(g)
    try:
        client.chat.completions.create(
            model="gpt-4o",
            messages=_msgs("key sk-ant-api03-ABCDEFGH12345678 and card 4111 1111 1111 1111"),
        )
    finally:
        remove_interceptor(g)
    assert calls["n"] == 1  # proceeds


def test_guard_ignores_non_call_events():
    g = guard(Policy.strict())
    assert g(object()) is MISS  # not an LLMCall/ToolCall -> MISS
    assert g(LLMCall(id="x", provider="openai", model="m", messages=[])) is MISS  # empty -> MISS


# --- dual-shape guard (1.5.0): scope form + resolve_findings ------------------------------------


def test_guard_scope_form_installs_and_removes_exactly_once():
    # `with guard(...):` installs the interceptor on core's seam on enter, removes it on exit.
    from cendor.core.instrument import _interceptors

    calls = {"n": 0}
    client = _client(calls)
    g = guard(Policy.pci())  # financial -> block

    base = list(_interceptors)
    with g as installed:
        assert installed is g  # the CM yields the interceptor itself (dual shape, same object)
        assert sum(1 for i in _interceptors if i is g) == 1
        with pytest.raises(PolicyViolation):
            client.chat.completions.create(
                model="gpt-4o", messages=_msgs("card 4111 1111 1111 1111")
            )
    assert all(i is not g for i in _interceptors)
    assert list(_interceptors) == base
    assert calls["n"] == 0  # blocked while scoped

    # enforcement is really gone after exit
    client.chat.completions.create(model="gpt-4o", messages=_msgs("card 4111 1111 1111 1111"))
    assert calls["n"] == 1


def test_guard_scope_form_removes_on_exception():
    from cendor.core.instrument import _interceptors

    g = guard(Policy.strict())
    with pytest.raises(RuntimeError):
        with g:
            assert sum(1 for i in _interceptors if i is g) == 1
            raise RuntimeError("boom")
    assert all(i is not g for i in _interceptors)


def test_guard_raw_interceptor_form_unchanged():
    # The returned object is still a plain callable for add_interceptor (shape pin).
    g = guard(Policy.strict())
    assert callable(g)
    assert g(object()) is MISS  # callable without installation, exactly as before


def test_resolve_findings_uses_resolved_actions():
    from cendor.acttrace import resolve_findings, scan

    findings = scan(
        "email bob@acme.com card 4111 1111 1111 1111 key sk-ant-api03-ABCDEFGH12345678",
        Policy.gdpr(),
    )
    groups = resolve_findings(findings)
    assert {f.category for f in groups["redact"]} >= {"email", "credit_card"}
    assert not groups["block"]  # gdpr redacts financial, blocks only special_category/credential


def test_resolve_findings_reresolves_under_another_policy():
    from cendor.acttrace import resolve_findings, scan

    # scan wide under default (never blocks), enforce under pci (financial -> block)
    findings = scan("card 4111 1111 1111 1111", Policy.default())
    assert all(f.action != "block" for f in findings)
    groups = resolve_findings(findings, Policy.pci())
    assert [f.category for f in groups["block"]] == ["credit_card"]
    assert groups["block"][0].action == "block"  # the finding is re-stamped


def test_resolve_findings_unknown_action_falls_back_to_flag():
    from dataclasses import replace

    from cendor.acttrace import Finding, resolve_findings

    weird = replace(
        Finding(category="email", group="pii", severity="warning", action="flag", count=1),
        action="quarantine",
    )
    groups = resolve_findings([weird])
    assert [f.category for f in groups["flag"]] == ["email"]
