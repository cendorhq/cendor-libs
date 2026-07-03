"""Auto-populated, hash-chained, tamper-evident audit log. Offline; mock clients only."""

import json
from types import SimpleNamespace

import pytest
from cendor.acttrace import AuditLog, verify
from cendor.core import bus, instrument


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


def _client():
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_auto_populates_from_instrumented_calls(tmp_path):
    log = AuditLog(system="loan_triage", risk_tier="high", path=str(tmp_path / "audit.jsonl"))
    try:
        client = _client()
        with log.decision(input={"amount": 5000}, actor="agent") as d:
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "x"}]
            )
            d.record(model="gpt-4o", prompt_id="triage@v3")
            d.human_oversight(reviewer="ops@bank", action="approved", note="manual check")
    finally:
        log.detach()

    types = [e.type for e in log.entries]
    assert "decision" in types
    assert "llm_call" in types  # captured with zero per-call wiring
    assert "human_oversight" in types
    # the llm_call carries cost + is tagged with the active decision
    llm = next(e for e in log.entries if e.type == "llm_call")
    assert llm.payload["cost"] is not None
    assert llm.payload["decision_id"] is not None


def test_chain_verifies_and_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        with log.decision(input="app") as d:
            d.human_oversight(reviewer="r", action="approved")
    finally:
        log.detach()

    ok, detail = verify(str(path))
    assert ok, detail

    # Tamper: edit a payload in the middle of the chain.
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["payload"]["actor"] = "HACKED"
    lines[1] = json.dumps(row)
    path.write_text("\n".join(lines), encoding="utf-8")

    ok, detail = verify(str(path))
    assert not ok and "tampered" in detail


def test_context_assembly_auto_captured(tmp_path):
    # contextkit emits an AssemblyReport on the bus; acttrace records it without importing it.
    from cendor.contextkit import Block, Context

    log = AuditLog(system="s", path=str(tmp_path / "a.jsonl"))
    try:
        with log.decision(input="q"):
            ctx = Context(budget_tokens=1000, model="gpt-4o")
            ctx.add(Block("system", priority=10, role="system"))
            ctx.assemble()
    finally:
        log.detach()
    assert any(e.type == "context_assembly" for e in log.entries)


def test_export_with_control_mapping(tmp_path):
    log = AuditLog(system="s", risk_tier="high")
    try:
        with log.decision(input="x") as d:
            d.human_oversight(reviewer="r", action="approved")
    finally:
        log.detach()
    out = tmp_path / "evidence.jsonl"
    log.export(str(out), framework="eu_ai_act")

    rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert "_meta" in rows[0]
    assert "not legal advice" in rows[0]["_meta"]["disclaimer"].lower()
    oversight = next(r for r in rows if r.get("type") == "human_oversight")
    assert oversight["controls"] == ["Art.14 human oversight", "Art.26(5) deployer oversight"]
    # exported evidence still verifies
    ok, _ = verify(str(out))
    assert ok


def test_nist_rmf_export_annotates_and_summarizes(tmp_path):
    from cendor.acttrace import frameworks

    assert set(frameworks()) >= {"eu_ai_act", "nist_rmf"}

    log = AuditLog(system="s", risk_tier="high")
    try:
        with log.decision(input="x") as d:
            d.human_oversight(reviewer="r", action="approved")
    finally:
        log.detach()
    out = tmp_path / "evidence.jsonl"
    log.export(str(out), framework="nist_rmf")

    rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    meta = rows[0]["_meta"]
    assert meta["framework"] == "nist_rmf"
    assert "MANAGE-2.1" in meta["controls_covered"]  # from the human_oversight event
    oversight = next(r for r in rows if r.get("type") == "human_oversight")
    assert oversight["controls"] == ["MANAGE-2.1"]
    ok, _ = verify(str(out))  # exported pack still verifies
    assert ok


def test_iso_42001_and_gdpr_frameworks_annotate(tmp_path):
    from cendor.acttrace import frameworks

    assert set(frameworks()) == {"eu_ai_act", "gdpr", "iso_42001", "nist_rmf"}

    log = AuditLog(system="s", risk_tier="high")
    try:
        with log.decision(input="x") as d:
            d.human_oversight(reviewer="r", action="approved")
    finally:
        log.detach()

    iso = tmp_path / "iso.jsonl"
    log.export(str(iso), framework="iso_42001")
    rows = [json.loads(ln) for ln in iso.read_text(encoding="utf-8").splitlines()]
    assert "A.6.2.8 event logs" in rows[0]["_meta"]["controls_covered"]
    oversight = next(r for r in rows if r.get("type") == "human_oversight")
    assert oversight["controls"] == ["A.9.2 responsible use", "A.9.4 intended use"]
    assert verify(str(iso))[0] is True

    gdpr = tmp_path / "gdpr.jsonl"
    log.export(str(gdpr), framework="gdpr")
    grows = [json.loads(ln) for ln in gdpr.read_text(encoding="utf-8").splitlines()]
    decision = next(r for r in grows if r.get("type") == "decision")
    assert "Art.22 automated decision-making" in decision["controls"]
    assert verify(str(gdpr))[0] is True


def test_flag_records_policy_event_and_tags_decision(tmp_path):
    path = tmp_path / "f.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        log.flag("pii detected", action="redacted", data="email")  # no decision open
        with log.decision(input="x") as d:
            d.flag("out of scope", action="blocked", severity="critical")
            did = d.id
    finally:
        log.detach()

    flags = [e for e in log.entries if e.type == "policy_flag"]
    assert len(flags) == 2
    assert flags[0].payload["decision_id"] is None and flags[0].payload["action"] == "redacted"
    assert flags[1].payload["decision_id"] == did and flags[1].payload["action"] == "blocked"
    assert verify(str(path))[0] is True  # flags are in the tamper-evident chain


def test_preflight_guard_blocks_call_and_records_flag(tmp_path):
    # End-to-end: a guard refuses disallowed input AND the refusal is recorded — closing the gap
    # that a blocked (raised) pre-flight call never emits on the bus, so it isn't auto-audited.
    from cendor.core import instrument
    from cendor.core.instrument import MISS, add_interceptor, remove_interceptor
    from cendor.core.types import LLMCall

    path = tmp_path / "guard.jsonl"
    log = AuditLog(system="s", path=str(path))
    calls = {"n": 0}

    class Completions:
        def create(self, **kwargs):
            calls["n"] += 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    class PolicyViolation(Exception): ...

    def guard(call):
        if isinstance(call, LLMCall):
            text = " ".join(
                m["content"] for m in call.messages if isinstance(m.get("content"), str)
            )
            if "ssn" in text.lower():
                # record the refusal BEFORE raising
                log.flag(
                    "special-category data in prompt",
                    action="blocked",
                    severity="critical",
                    data="ssn pattern",
                )
                raise PolicyViolation("must not send special-category data to the model")
        return MISS

    add_interceptor(guard)
    try:
        with pytest.raises(PolicyViolation):
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "my ssn is x"}]
            )
    finally:
        remove_interceptor(guard)
        log.detach()

    assert calls["n"] == 0  # the model call was blocked before it ran
    flags = [e for e in log.entries if e.type == "policy_flag"]
    assert len(flags) == 1 and flags[0].payload["action"] == "blocked"
    assert not any(e.type == "llm_call" for e in log.entries)  # blocked call left no llm_call entry
    assert verify(str(path))[0] is True  # the refusal is in the tamper-evident record


def test_policy_flag_control_mapping(tmp_path):
    log = AuditLog(system="s")
    try:
        log.flag("special-category data", action="blocked")
    finally:
        log.detach()
    out = tmp_path / "e.jsonl"
    log.export(str(out), framework="gdpr")
    rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    flag = next(r for r in rows if r.get("type") == "policy_flag")
    assert "Art.9 special-category data" in flag["controls"]
    assert "Art.9 special-category data" in rows[0]["_meta"]["controls_covered"]


def test_export_unknown_framework_rejected(tmp_path):
    log = AuditLog(system="s")
    log.detach()
    with pytest.raises(ValueError):
        log.export(str(tmp_path / "x.jsonl"), framework="iso_9000")


def test_payload_redaction_on_by_default(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="s", path=str(path))  # redact=True default
    try:
        with log.decision(
            input={"user_email": "alice@example.com", "api_key": "sk-ABCDEFGH12345678"}
        ):
            pass
    finally:
        log.detach()

    decision = next(e for e in log.entries if e.type == "decision")
    blob = json.dumps(decision.payload)
    assert "alice@example.com" not in blob and "sk-ABCDEFGH12345678" not in blob
    assert "<redacted>" in blob
    assert verify(str(path))[0] is True  # chain consistent over the redacted payloads

    # decision_id (32-hex) is NOT clobbered by redaction
    assert decision.payload["decision_id"] and decision.payload["decision_id"] != "<redacted>"


def test_modern_secret_formats_are_redacted_and_flagged(tmp_path):
    # The hyphen in sk-ant-…/sk-proj-… broke the old \bsk-[A-Za-z0-9]{8,} run; AWS/Google/JWT
    # weren't covered at all. All must now be scrubbed and their categories flagged.
    secrets = {
        "anthropic": "sk-ant-api03-ABCDEFGH12345678",
        "openai_proj": "sk-proj-ABCDEFGH12345678",
        "aws": "AKIA" + "A" * 16,
        "google": "AIza" + "b" * 35,
        "jwt": "eyJ" + "a" * 15 + "." + "b" * 15 + "." + "c" * 15,
    }
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        with log.decision(input=secrets):
            pass
    finally:
        log.detach()

    blob = json.dumps(next(e for e in log.entries if e.type == "decision").payload)
    for raw in secrets.values():
        assert raw not in blob  # every modern format scrubbed
    assert "<redacted>" in blob
    assert verify(str(path))[0] is True  # chain consistent over the redacted payloads

    # The auto policy_flag records which categories were removed.
    flag = next(e for e in log.entries if e.type == "policy_flag")
    cats = set(flag.payload["data"])
    assert {"api_key", "aws_key", "google_api_key", "jwt"} <= cats


def test_plain_hyphenated_text_is_not_redacted(tmp_path):
    # False-positive guard: prefix-anchored patterns must leave ordinary hyphenated prose alone.
    sentence = "a well-known best-practice for multi-region fail-over in us-east-1"
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        with log.decision(input={"note": sentence}):
            pass
    finally:
        log.detach()
    payload = next(e for e in log.entries if e.type == "decision").payload
    assert payload["input"]["note"] == sentence  # untouched
    assert not any(e.type == "policy_flag" for e in log.entries)  # nothing flagged


def test_redaction_can_be_disabled(tmp_path):
    log = AuditLog(system="s", path=str(tmp_path / "a.jsonl"), redact=False)
    try:
        with log.decision(input={"user_email": "bob@example.com"}):
            pass
    finally:
        log.detach()
    decision = next(e for e in log.entries if e.type == "decision")
    assert decision.payload["input"]["user_email"] == "bob@example.com"  # kept verbatim


def test_custom_redactor_scrubs_domain_specific_pii(tmp_path):
    from cendor.acttrace import default_redactor

    def scrub(obj):  # mask account numbers, then apply the built-in email/key redaction
        obj = default_redactor(obj)
        if isinstance(obj, str):
            return obj.replace("ACCT-9988", "<account>")
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj

    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="bank", path=str(path), redactor=scrub)
    try:
        with log.decision(input={"note": "wire from ACCT-9988 by carol@example.com"}):
            pass
    finally:
        log.detach()

    blob = json.dumps(next(e for e in log.entries if e.type == "decision").payload)
    assert "ACCT-9988" not in blob and "carol@example.com" not in blob  # custom + composed default
    assert "<account>" in blob and "<redacted>" in blob
    assert verify(str(path))[0] is True  # chain consistent over the scrubbed payloads


def test_signed_records_verify_with_key(tmp_path):
    path = tmp_path / "signed.jsonl"
    log = AuditLog(system="loan", risk_tier="high", path=str(path), signing_key="s3cret")
    try:
        with log.decision(input="app") as d:
            d.human_oversight(reviewer="r", action="approved")
    finally:
        log.detach()

    assert all(e.sig for e in log.entries)  # every entry is signed
    assert verify(str(path), key="s3cret")[0] is True  # right key verifies
    ok, detail = verify(str(path), key="wrong-key")  # wrong key fails on signature
    assert ok is False and "signature" in detail
    assert verify(str(path))[0] is True  # no key -> chain-only check still passes


def test_unsigned_log_fails_when_key_required(tmp_path):
    path = tmp_path / "plain.jsonl"
    log = AuditLog(system="s", path=str(path))  # no signing_key
    log.detach()
    assert verify(str(path))[0] is True  # chain-only OK
    ok, detail = verify(str(path), key="expected")  # demanding signatures that aren't there
    assert ok is False and "signature" in detail


def test_verify_detects_tail_truncation_of_exported_pack(tmp_path):
    # A hash chain alone can't catch trailing entries being dropped; the _meta header (head + count)
    # written by export() lets verify() catch it.
    log = AuditLog(system="s", risk_tier="high")
    try:
        for i in range(3):
            with log.decision(input=f"app {i}") as d:
                d.human_oversight(reviewer="r", action="approved" if i < 2 else "REJECTED")
    finally:
        log.detach()
    pack = tmp_path / "pack.jsonl"
    log.export(str(pack), framework="eu_ai_act")
    assert verify(str(pack))[0] is True  # full pack verifies (chain + _meta completeness)

    lines = [ln for ln in pack.read_text(encoding="utf-8").split("\n") if ln.strip()]
    pack.write_text("\n".join(lines[:-3]) + "\n", encoding="utf-8")  # drop the trailing entries
    ok, detail = verify(str(pack))
    assert ok is False and "incomplete" in detail  # truncation caught against _meta


def test_verify_expected_head_catches_raw_log_truncation(tmp_path):
    path = tmp_path / "raw.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        for i in range(3):
            with log.decision(input=f"x{i}"):
                pass
    finally:
        log.detach()
    head = log.head  # captured out-of-band for later completeness checks
    assert verify(str(path), expected_head=head)[0] is True

    lines = [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]
    path.write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")  # truncate the tail
    assert verify(str(path), expected_head=head)[0] is False  # expected head no longer reached
    assert verify(str(path))[0] is True  # back-compat: no expected_head -> chain-only still passes


def test_context_manager_auto_detaches():
    bus._reset()
    with AuditLog(system="cm") as log:
        assert log.head  # usable inside the block
    assert bus._subscribers == []  # detached on exit — no leaked subscription


def test_redaction_auto_emits_policy_flag(tmp_path):
    # The redactor scrubs PII from a decision's input AND records that it did — closing the gap
    # where PII was removed silently with no trace in the chain.
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        with log.decision(input={"note": "reach me at alice@example.com"}):
            pass
    finally:
        log.detach()

    flags = [e for e in log.entries if e.type == "policy_flag"]
    assert len(flags) == 1
    assert flags[0].payload["action"] == "redacted"
    assert flags[0].payload["data"] == ["email"]
    assert flags[0].payload["auto"] is True
    # the underlying decision input was actually scrubbed
    decision = next(e for e in log.entries if e.type == "decision")
    assert "alice@example.com" not in json.dumps(decision.payload)
    assert verify(str(path))[0] is True  # the auto-flag is in the tamper-evident chain


def test_redaction_auto_flag_links_tool_call_to_decision(tmp_path):
    from cendor.core.instrument import instrument_tool

    log = AuditLog(system="s", path=str(tmp_path / "a.jsonl"))
    try:

        @instrument_tool("notify")
        def notify(to):
            return "sent"

        with log.decision(input="go") as d:
            notify("carol@example.com")  # email in tool arguments -> redacted + flagged
            did = d.id
    finally:
        log.detach()

    flags = [e for e in log.entries if e.type == "policy_flag"]
    assert len(flags) == 1
    assert flags[0].payload["data"] == ["email"]
    assert flags[0].payload["decision_id"] == did  # tagged to the active decision


def test_no_auto_flag_without_pii_or_when_disabled(tmp_path):
    # clean content -> no flag
    log = AuditLog(system="s", path=str(tmp_path / "clean.jsonl"))
    try:
        with log.decision(input={"q": "how do refunds work?"}):
            pass
    finally:
        log.detach()
    assert not any(e.type == "policy_flag" for e in log.entries)

    # flag_on_redact=False -> redaction still happens, but no flag
    log2 = AuditLog(system="s", path=str(tmp_path / "off.jsonl"), flag_on_redact=False)
    try:
        with log2.decision(input={"e": "dave@example.com"}):
            pass
    finally:
        log2.detach()
    assert not any(e.type == "policy_flag" for e in log2.entries)
    assert "dave@example.com" not in json.dumps(
        next(e for e in log2.entries if e.type == "decision").payload
    )  # still scrubbed


def test_custom_redactor_does_not_auto_flag(tmp_path):
    # a custom redactor owns its own flagging semantics; we don't second-guess it
    from cendor.acttrace import default_redactor

    def scrub(obj):  # a distinct callable (not the built-in _redact) that composes the default
        return default_redactor(obj)

    log = AuditLog(system="s", path=str(tmp_path / "a.jsonl"), redactor=scrub)
    try:
        with log.decision(input={"e": "erin@example.com"}):
            pass
    finally:
        log.detach()
    assert not any(e.type == "policy_flag" for e in log.entries)


def test_export_meta_summary_counts(tmp_path):
    log = AuditLog(system="s", risk_tier="high")
    try:
        with log.decision(input="x") as d:
            d.human_oversight(reviewer="r", action="approved")
            d.flag("out of scope", action="blocked", severity="critical")
    finally:
        log.detach()
    out = tmp_path / "e.jsonl"
    log.export(str(out), framework="eu_ai_act")
    summary = json.loads(out.read_text(encoding="utf-8").splitlines()[0])["_meta"]["summary"]
    assert summary["decisions"] == 1
    assert summary["human_oversight"] == 1
    assert summary["policy_flags"] == 1
    assert summary["flags_by_action"] == {"blocked": 1}
    assert summary["flags_by_severity"] == {"critical": 1}


def test_decision_flag_returns_entry_and_normalizes(tmp_path):
    from cendor.acttrace import AuditEntry

    log = AuditLog(system="s")
    try:
        with log.decision(input="x") as d:
            entry = d.flag("nope", action="BLOCKED", severity="Critical")
    finally:
        log.detach()
    assert isinstance(entry, AuditEntry)
    assert entry.payload["action"] == "blocked"  # normalized to lowercase
    assert entry.payload["severity"] == "critical"


def test_cli_verify(tmp_path, capsys):
    from cendor.acttrace.cli import main

    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="s", path=str(path))
    log.detach()
    assert main(["verify", str(path)]) == 0

    path.write_text(
        path.read_text(encoding="utf-8").replace('"system"', '"SYSTEM"'), encoding="utf-8"
    )
    assert main(["verify", str(path)]) == 1


def _signed_pack(tmp_path, key="s3cret", n=3):
    """A signed exported evidence pack (with a signed _meta header). Returns its path + AuditLog."""
    src = tmp_path / "src.jsonl"
    log = AuditLog(system="loan", risk_tier="high", path=str(src), signing_key=key)
    try:
        for i in range(n):
            with log.decision(input=f"app {i}") as d:
                d.human_oversight(reviewer="r", action="approved")
    finally:
        log.detach()
    pack = tmp_path / "pack.jsonl"
    log.export(str(pack), framework="eu_ai_act")
    return pack, log


def test_signed_pack_meta_is_verifiable(tmp_path):
    pack, _ = _signed_pack(tmp_path)
    ok, detail = verify(str(pack), key="s3cret")
    assert ok is True
    assert "metadata signature verified" in detail  # the _meta header itself is authenticated


def test_forged_meta_truncation_fails_even_with_key(tmp_path):
    # The core Phase 0.3 hole: drop trailing entries AND rewrite _meta head_hash/entries to match.
    # Without a signed _meta this passes verify() even with --key; with it, the stale sig is caught.
    pack, _ = _signed_pack(tmp_path)
    rows = [json.loads(ln) for ln in pack.read_text(encoding="utf-8").splitlines() if ln.strip()]
    meta, entries = rows[0], rows[1:]
    kept = entries[:-3]  # forge: drop the last 3 entries
    meta["_meta"]["head_hash"] = kept[-1]["hash"]  # rewrite completeness fields to fit the forgery
    meta["_meta"]["entries"] = len(kept)
    forged = [meta, *kept]
    pack.write_text("\n".join(json.dumps(r) for r in forged) + "\n", encoding="utf-8")

    ok, detail = verify(str(pack), key="s3cret")
    assert ok is False and "forged _meta" in detail  # header signature no longer matches


def test_stripped_meta_signature_fails_with_key(tmp_path):
    # Stripping the _meta.sig from a signed pack must not silently downgrade to "trust the header".
    pack, _ = _signed_pack(tmp_path)
    rows = [json.loads(ln) for ln in pack.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows[0]["_meta"].pop("sig", None)
    pack.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    ok, detail = verify(str(pack), key="s3cret")
    assert ok is False and "signature" in detail


def test_entry_swap_reordering_is_detected(tmp_path):
    # Reordering two entries breaks the prev_hash link — claimed by the README, now tested.
    path = tmp_path / "raw.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        for i in range(4):
            with log.decision(input=f"x{i}"):
                pass
    finally:
        log.detach()
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lines[1], lines[2] = lines[2], lines[1]  # swap two adjacent entries
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, detail = verify(str(path))
    assert ok is False and ("broken link" in detail or "tampered" in detail)


def test_verify_missing_file_returns_false_not_raises(tmp_path):
    ok, detail = verify(str(tmp_path / "does-not-exist.jsonl"))
    assert ok is False and "cannot read" in detail


def test_verify_corrupt_json_returns_false_not_raises(tmp_path):
    path = tmp_path / "corrupt.jsonl"
    path.write_text("{not valid json\n", encoding="utf-8")
    ok, detail = verify(str(path))
    assert ok is False and "corrupt" in detail


def test_cli_missing_file_exits_nonzero_cleanly(tmp_path):
    from cendor.acttrace.cli import main

    # Must exit non-zero without a traceback (verify() no longer raises on a missing file).
    assert main(["verify", str(tmp_path / "nope.jsonl")]) == 1


def test_no_key_detail_flags_unauthenticated_meta(tmp_path):
    # Without a key, completeness rests on the unauthenticated in-file _meta — detail must say so.
    pack, _ = _signed_pack(tmp_path)
    ok, detail = verify(str(pack))
    assert ok is True
    assert "unauthenticated" in detail and "expected_head" in detail


def test_concurrent_emits_keep_chain_intact(tmp_path):
    # Concurrent bus emits must not corrupt the hash chain (head/entries/file append are locked).
    import threading
    from decimal import Decimal

    from cendor.core.types import LLMCall, Money, Usage

    path = tmp_path / "concurrent.jsonl"
    log = AuditLog(system="s", path=str(path))
    threads_n, per = 6, 30

    def worker(w: int) -> None:
        for i in range(per):
            bus.emit(
                LLMCall(
                    id=f"{w}-{i}",
                    provider="openai",
                    model="gpt-4o",
                    messages=[],
                    usage=Usage(1, 1),
                    cost=Money(Decimal("0")),
                )
            )

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.detach()

    expected = 1 + threads_n * per  # audit_open + every emitted llm_call
    assert len(log.entries) == expected
    ok, detail = verify(str(path), expect_entries=expected)
    assert ok, detail  # chain links and count are intact despite concurrent appends
