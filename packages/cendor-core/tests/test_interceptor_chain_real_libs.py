"""The D3 chain fix, proved with the SHIPPED libraries rather than hand-written interceptors.

`test_interceptor_chain_order.py` pins the contract; this file pins the thing the analysis actually
reported — that a **tokenguard** pre-flight rewrite and an **acttrace `guard()`** redact-before-send
cannot both take effect on one call. Hand-rolled interceptors can express the contract but they
cannot prove the real libraries compose, and composing is the whole product claim ("they cooperate
only through core's bus/interceptor seams").

Living in `cendor-core`'s tests would violate cardinal rule 2 if it *imported* siblings for
behaviour; it does not — it imports them the way a **user** does, to assert that core's seam
composes them. The imports are skipped if a sibling is absent, so a core-only environment passes.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.types import LLMCall

acttrace = pytest.importorskip("cendor.acttrace")
tokenguard = pytest.importorskip("cendor.tokenguard")

PII = "email bob@example.com about invoice 42"


@pytest.fixture
def client_and_wire():
    bus._reset()
    tokenguard.reset()
    wire: list[dict] = []

    def create(**kwargs):
        wire.append(dict(kwargs))
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=4),
            choices=[SimpleNamespace(message=SimpleNamespace(content="an answer"))],
        )

    client = instrument(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )
    yield client, wire
    tokenguard.reset()
    bus._reset()


ARGS = {"model": "gpt-4o", "messages": [{"role": "user", "content": PII}]}


def _sent_text(wire: list[dict]) -> str:
    return str(wire[0]["messages"][0]["content"])


#: tokenguard picks the output-cap kwarg per model family — `max_completion_tokens` on the newer
#: OpenAI ids, `max_tokens` on the older ones (measured: gpt-4o gets `max_completion_tokens`). The
#: assertion is "a cap reached the provider", not "this exact spelling did".
_CAP_KWARGS = ("max_tokens", "max_completion_tokens")


def _clamped(request: dict) -> bool:
    return any(isinstance(request.get(k), int) and request[k] > 0 for k in _CAP_KWARGS)


def test_a_clamp_and_a_guard_both_take_effect(client_and_wire):
    """The reported pairing: tokenguard's `on_exceed="clamp"` plus acttrace's redact-before-send.

    Before the fix exactly one of them reached the provider, and which one depended on registration
    order: with the clamp first the email went out **in the clear**.
    """
    client, wire = client_and_wire
    log = acttrace.AuditLog(system="t", risk_tier="low")
    try:
        with acttrace.guard(acttrace.Policy.default(), log):
            with tokenguard.budget(tokens=64, on_exceed="clamp"):
                client.chat.completions.create(**ARGS)
    finally:
        log.detach()

    sent = _sent_text(wire)
    assert "bob@example.com" not in sent, "the guard's redaction must reach the provider"
    assert _clamped(wire[0]), "the clamp must reach the provider too"


def test_the_reverse_registration_order_gives_the_same_result(client_and_wire):
    """Order-independence, with the real libraries. This is what a user cannot control."""
    client, wire = client_and_wire
    log = acttrace.AuditLog(system="t", risk_tier="low")
    try:
        with tokenguard.budget(tokens=64, on_exceed="clamp"):
            with acttrace.guard(acttrace.Policy.default(), log):
                client.chat.completions.create(**ARGS)
    finally:
        log.detach()

    sent = _sent_text(wire)
    assert "bob@example.com" not in sent
    assert _clamped(wire[0])


def test_a_downgrade_and_a_guard_both_take_effect(client_and_wire):
    """`on_exceed="downgrade"` rewrites the model; the guard rewrites the messages. Different
    fields, so composing them must produce both — not whichever registered first."""
    client, wire = client_and_wire
    log = acttrace.AuditLog(system="t", risk_tier="low")
    try:
        with acttrace.guard(acttrace.Policy.default(), log):
            with tokenguard.budget(
                usd=Decimal("0.0000001"),
                on_exceed="downgrade",
                downgrade={"gpt-4o": "gpt-4o-mini"},
            ):
                client.chat.completions.create(**ARGS)
    finally:
        log.detach()

    assert wire[0]["model"] == "gpt-4o-mini", "the downgrade must reach the provider"
    assert "bob@example.com" not in _sent_text(wire), "and so must the redaction"


def test_the_audit_chain_records_the_call_once(client_and_wire):
    """Ripple check: composing two interceptors must not double-record the governance evidence."""
    client, _wire = client_and_wire
    calls: list[LLMCall] = []
    bus.subscribe(lambda e: calls.append(e) if isinstance(e, LLMCall) else None)
    log = acttrace.AuditLog(system="t", risk_tier="low")
    try:
        with acttrace.guard(acttrace.Policy.default(), log):
            with tokenguard.budget(tokens=64, on_exceed="clamp"):
                client.chat.completions.create(**ARGS)
    finally:
        log.detach()
    assert len(calls) == 1


def test_a_budget_block_still_refuses_before_the_wire(client_and_wire):
    """The block path is not a Reroute — it raises, and must still stop everything."""
    client, wire = client_and_wire
    log = acttrace.AuditLog(system="t", risk_tier="low")
    try:
        with (
            acttrace.guard(acttrace.Policy.default(), log),
            tokenguard.budget(tokens=1, on_exceed="block"),
            pytest.raises(tokenguard.BudgetExceeded),
        ):
            client.chat.completions.create(**ARGS)
    finally:
        log.detach()
    assert wire == [], "nothing may reach the provider after a pre-flight block"


def test_a_cassette_replay_still_wins_over_a_clamp(client_and_wire, tmp_path):
    """cassette's replay is a returned *response*, so it must still short-circuit the chain — and
    the provider must never be called even with a tokenguard budget installed alongside it.

    This is the half of the old behaviour that was correct; the fix must not have loosened it."""
    cassette = pytest.importorskip("cendor.cassette")
    client, wire = client_and_wire
    tape = str(tmp_path / "chain.json")

    with cassette.using(tape):  # first pass records
        client.chat.completions.create(**ARGS)
    assert len(wire) == 1, "the recording pass really called the provider"
    wire.clear()

    with cassette.using(tape):  # second pass replays
        with tokenguard.budget(tokens=64, on_exceed="clamp"):
            client.chat.completions.create(**ARGS)
    assert wire == [], "a replayed call must not reach the provider"
