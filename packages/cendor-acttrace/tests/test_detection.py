"""Detection engine + policy (roadmap phase 1). Offline, deterministic; no network.

Covers: golden detections per category, a false-positive corpus that must yield zero hits,
validator unit tests (Luhn / IBAN mod-97 / Verhoeff / ABA / SSN / BIC), policy resolution across
presets, the pure ``scan``/``redact`` surface, ``AuditLog(policy=…)`` auto-flagging, the custom
detector registry, and framework control augmentation for category-tagged flags.
"""

import json

import pytest
from cendor.acttrace import (
    DETECTORS,
    AuditLog,
    Detector,
    Finding,
    Policy,
    default_redactor,
    redact,
    register_detector,
    scan,
    verify,
)
from cendor.acttrace.detectors import (
    _aba_valid,
    _bic_valid,
    _c,
    _iban_mod97,
    _luhn,
    _scrub,
    _ssn_valid,
    _verhoeff,
)
from cendor.core import bus


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


# --------------------------------------------------------------------------- validators


@pytest.mark.parametrize(
    "number,ok",
    [
        ("4111111111111111", True),  # Visa test number
        ("4111 1111 1111 1111", True),  # separators tolerated
        ("5500005555555559", True),  # Mastercard test number
        ("4111111111111112", False),  # one digit off — fails Luhn
        ("1234567890123456", False),
        ("123", False),  # too short
    ],
)
def test_luhn(number, ok):
    assert _luhn(number) is ok


@pytest.mark.parametrize(
    "iban,ok",
    [
        ("GB82WEST12345698765432", True),
        ("GB82 WEST 1234 5698 7654 32", True),  # spaced form
        ("DE89370400440532013000", True),
        ("GB00WEST12345698765432", False),  # bad check digits
        ("XX00", False),  # too short / malformed
    ],
)
def test_iban_mod97(iban, ok):
    assert _iban_mod97(iban) is ok


@pytest.mark.parametrize(
    "number,ok",
    [
        ("2363", True),  # 236 + Verhoeff check digit 3
        ("1428570", True),  # documented valid Verhoeff number
        ("2364", False),
        ("1428571", False),
    ],
)
def test_verhoeff(number, ok):
    assert _verhoeff(number) is ok


@pytest.mark.parametrize(
    "number,ok",
    [
        ("021000021", True),  # a real ABA routing number (checksum valid)
        ("123456789", False),  # fails the ABA weighting
        ("12345678", False),  # wrong length
    ],
)
def test_aba(number, ok):
    assert _aba_valid(number) is ok


@pytest.mark.parametrize(
    "ssn,ok",
    [
        ("123-45-6789", True),
        ("666-45-6789", False),  # area 666 is invalid
        ("000-45-6789", False),  # area 000 is invalid
        ("900-45-6789", False),  # area 900+ is invalid
        ("123-00-6789", False),  # group 00 is invalid
        ("123-45-0000", False),  # serial 0000 is invalid
    ],
)
def test_ssn_valid(ssn, ok):
    assert _ssn_valid(ssn) is ok


def test_bic_requires_valid_country_code():
    assert _bic_valid("DEUTDEFF") is True  # DE = Germany
    assert _bic_valid("DEUTDEFF500") is True  # 11-char with branch
    assert _bic_valid("ABCDZZFF") is False  # ZZ is not an ISO country code
    assert _bic_valid("SHORT") is False


# --------------------------------------------------------------------------- golden detections


@pytest.mark.parametrize(
    "text,category,group",
    [
        ("reach me at alice@example.com", "email", "pii"),
        ("key sk-ant-api03-ABCDEFGH12345678", "api_key", "secret"),
        ("aws AKIA" + "A" * 16, "aws_key", "secret"),
        ("google AIza" + "b" * 35, "google_api_key", "secret"),
        ("gh ghp_" + "a" * 36, "github_token", "secret"),
        ("slack xoxb-123456789012-abcdef", "slack_token", "secret"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key", "secret"),
        ("the password is hunter2!", "password", "credential"),
        ("card 4111 1111 1111 1111 on file", "credit_card", "financial"),
        ("iban GB82WEST12345698765432 please", "iban", "financial"),
        ("routing 021000021 for wire", "us_routing", "financial"),
        ("bic DEUTDEFF for transfer", "swift_bic", "financial"),
        ("ssn 123-45-6789 on record", "us_ssn", "gov_id"),
        ("call 415-555-2671 tomorrow", "phone", "pii"),
        ("call +14155552671 now", "phone", "pii"),
        ("host at 192.168.1.1 up", "ipv4", "pii"),
        ("addr 2001:0db8:85a3::8a2e:0370:7334 up", "ipv6", "pii"),
        ("nic 01:23:45:67:89:ab reset", "mac_address", "pii"),
        ("patient diagnosis recorded", "special_category", "special_category"),
    ],
)
def test_golden_detection_per_category(text, category, group):
    findings = scan(text)
    cats = {f.category: f for f in findings}
    assert category in cats, f"{category!r} not detected in {text!r} (got {sorted(cats)})"
    assert cats[category].group == group
    assert cats[category].count >= 1


def test_scan_returns_findings_with_resolved_action_and_counts():
    findings = scan("email a@b.com and again c@d.com")
    assert findings == [Finding("email", "pii", "warning", "redact", 2)]


# --------------------------------------------------------------------------- false positives

# Real-world strings that look risky but must NOT match: logs, code, UUIDs, git SHAs, ISO
# timestamps, hyphenated prose, semver, order/tracking ids, and validator-failing candidates.
FALSE_POSITIVE_CORPUS = [
    "INFO 2026-07-04T12:34:56.123456Z request_id=abc123 latency=123ms status=200",
    "commit 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b merged to main",
    "550e8400-e29b-41d4-a716-446655440000",  # a UUID
    "order ORD-2024-0001 shipped; tracking 1Z999AA10123456784",
    "a well-known best-practice for multi-region fail-over in us-east-1",
    "version v1.2.3 released; also 10.20.30 and build 2026",
    "color #3af5c2 padding 0 0 0 0 margin: 10px auto",
    "def multiply(x): return x * 2  # a multiply-by-two helper",
    "price was $1,234.56 and quantity 4 units in cart",
    "the 9-digit id 123456789 failed ABA; card 4111111111111112 fails luhn",
    "sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "timestamp 12:34:56 on date 2026-07-04 in year 2026",
    "GET /api/v2/users?id=42&sort=name HTTP/1.1 200",
    "hex dump 0xDEADBEEF and 0xCAFEBABE at offset 16",
    "kubernetes pod nginx-7d8b49c9f4-xk2lm restarted 3 times",
]


@pytest.mark.parametrize("line", FALSE_POSITIVE_CORPUS)
def test_false_positive_corpus_is_clean(line):
    assert scan(line) == [], f"unexpected finding(s) in {line!r}: {scan(line)}"


def test_validators_gate_loose_matches():
    # The regex matches the shape, but the checksum/format validator rejects the value → no finding.
    assert scan("card 4111111111111112") == []  # fails Luhn
    assert scan("iban GB00WEST12345698765432") == []  # fails mod-97
    assert scan("routing 123456789") == []  # fails ABA


# --------------------------------------------------------------------------- policy resolution


def test_policy_default_matches_legacy_behaviour():
    p = Policy.default()
    assert p.action_for("email", "pii") == "redact"
    assert p.action_for("api_key", "secret") == "redact"
    assert p.action_for("credit_card", "financial") == "flag"  # rest -> flag
    assert p.action_for("phone", "pii") == "flag"


def test_policy_presets():
    assert Policy.gdpr().action_for("special_category", "special_category") == "block"
    assert Policy.gdpr().action_for("phone", "pii") == "redact"
    assert Policy.pci().action_for("credit_card", "financial") == "block"
    assert Policy.strict().action_for("api_key", "secret") == "block"
    assert Policy.strict().action_for("phone", "pii") == "redact"  # rest -> redact


def test_policy_specificity_category_over_group_over_default():
    p = Policy(actions={"financial": "flag", "credit_card": "block"}, default="allow")
    assert p.action_for("credit_card", "financial") == "block"  # category wins
    assert p.action_for("iban", "financial") == "flag"  # group wins over default
    assert p.action_for("email", "pii") == "allow"  # default


# --------------------------------------------------------------------------- pure scan / redact


def test_redact_scrubs_only_redact_and_block_actions():
    obj = {"e": "a@b.com", "card": "4111 1111 1111 1111"}
    cleaned, findings = redact(obj, Policy.default())
    assert cleaned["e"] == "<redacted>"  # email -> redact
    assert cleaned["card"] == "4111 1111 1111 1111"  # card -> flag (left in place)
    cats = {f.category: f.action for f in findings}
    assert cats == {"email": "redact", "credit_card": "flag"}


def test_redact_pci_blocks_and_scrubs_card():
    cleaned, findings = redact({"card": "4111111111111111"}, Policy.pci())
    assert cleaned["card"] == "<redacted>"
    assert any(f.category == "credit_card" and f.action == "block" for f in findings)


def test_default_redactor_byte_identical_for_original_six():
    # The original six categories must scrub exactly as before (single source of truth = registry).
    sample = {
        "email": "reach me at alice@example.com",
        "api_key": "sk-ant-api03-ABCDEFGH12345678",
        "aws": "AKIA" + "A" * 16,
        "google": "AIza" + "b" * 35,
        "jwt": "eyJ" + "a" * 15 + "." + "b" * 15 + "." + "c" * 15,
        "bearer": "Bearer abc.def-123",
    }
    out = default_redactor(sample)
    blob = json.dumps(out)
    for raw in sample.values():
        assert raw not in blob
    assert blob.count("<redacted>") == 6  # one per original-six field, byte-for-byte


def test_scrub_leaves_hashes_uuids_and_ids_untouched():
    ids = {
        "uuid": "550e8400-e29b-41d4-a716-446655440000",
        "sha": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
        "hex64": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }
    cleaned, findings = redact(ids, Policy.strict())  # even the strictest policy
    assert cleaned == ids and findings == []


# --------------------------------------------------------------------------- AuditLog(policy=)


def test_auditlog_default_policy_redacts_secrets_flags_the_rest(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(system="s", path=str(path))  # Policy.default()
    try:
        with log.decision(input={"e": "a@b.com", "card": "4111 1111 1111 1111"}):
            pass
    finally:
        log.detach()

    flags = {f.payload["action"]: f for f in log.entries if f.type == "policy_flag"}
    assert set(flags) == {"redacted", "flagged"}
    assert flags["redacted"].payload["data"] == ["email"]
    assert flags["flagged"].payload["data"] == ["credit_card"]
    assert flags["flagged"].payload["severity"] == "critical"
    decision = next(e for e in log.entries if e.type == "decision")
    blob = json.dumps(decision.payload)
    assert "a@b.com" not in blob  # email scrubbed (redact)
    assert "4111 1111 1111 1111" in blob  # card left in place (flag)
    assert verify(str(path))[0] is True


def test_auditlog_gdpr_policy_blocks_special_category(tmp_path):
    path = tmp_path / "g.jsonl"
    log = AuditLog(system="s", path=str(path), policy=Policy.gdpr())
    try:
        with log.decision(input={"note": "patient diagnosis pending; ping a@b.com"}):
            pass
    finally:
        log.detach()

    flags = {f.payload["action"]: f for f in log.entries if f.type == "policy_flag"}
    assert flags["blocked"].payload["data"] == ["special_category"]
    assert flags["redacted"].payload["data"] == ["email"]
    decision = next(e for e in log.entries if e.type == "decision")
    assert "diagnosis" not in json.dumps(decision.payload)  # block scrubs for record safety
    assert verify(str(path))[0] is True


def test_auditlog_policy_implies_scanning_even_when_redact_false(tmp_path):
    # An explicit policy turns detection on regardless of the redact flag.
    log = AuditLog(system="s", path=str(tmp_path / "p.jsonl"), redact=False, policy=Policy.pci())
    try:
        with log.decision(input={"card": "4111111111111111"}):
            pass
    finally:
        log.detach()
    flags = [e for e in log.entries if e.type == "policy_flag"]
    assert len(flags) == 1 and flags[0].payload["action"] == "blocked"


# --------------------------------------------------------------------------- custom registry


def test_register_detector_is_picked_up_by_scan_and_redact():
    original = list(DETECTORS)
    try:
        register_detector(Detector("employee_id", "gov_id", "warning", _c(r"\bEMP-\d{5}\b")))
        findings = scan("ticket for EMP-12345 opened")
        assert any(f.category == "employee_id" for f in findings)
        # under a policy that redacts gov_id, the custom detector is scrubbed too
        cleaned, _ = redact("EMP-12345", Policy(actions={"gov_id": "redact"}))
        assert cleaned == "<redacted>"
    finally:
        DETECTORS[:] = original  # restore the global registry for other tests


def test_scrub_applies_registry_order_deterministically():
    # A bearer token that embeds a JWT: JWT runs before bearer (registry order), same as before.
    text = "Authorization: Bearer eyJ" + "a" * 15 + "." + "b" * 15 + "." + "c" * 15
    out = _scrub(text, {"jwt", "bearer_token"})
    assert "eyJ" not in out and "<redacted>" in out


# --------------------------------------------------------------------------- control mapping


def test_category_tagged_flag_maps_to_specific_controls(tmp_path):
    log = AuditLog(system="s", policy=Policy.gdpr())
    try:
        with log.decision(input={"note": "patient diagnosis pending"}):
            pass
    finally:
        log.detach()
    out = tmp_path / "e.jsonl"
    log.export(str(out), framework="gdpr")
    rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    flag = next(r for r in rows if r.get("type") == "policy_flag")
    # base policy_flag controls PLUS the special-category-specific Art.9 pointer
    assert "Art.9 special-category data" in flag["controls"]
    assert "Art.9 special-category data" in rows[0]["_meta"]["controls_covered"]
