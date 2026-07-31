"""Python regression pin: the output gate fires on a structured-output (`parse`) call.

This is the fourth negative control for the TypeScript N-6 fix (`@cendor/core` 3.3.0). There, a
response consumed through ``create(...)._thenUnwrap(...)`` — which is what openai-node's
``responses.parse`` / ``chat.completions.parse`` are built from — escaped the post-flight output
gate and delivered banned text, because the caller awaited a promise derived from the SDK's own
object rather than cendor's capture chain.

**Python cannot have that defect, and this file is the proof rather than an assumption.** Here
``responses.parse`` and ``chat.completions.parse`` POST their own requests, so each is its own
``instrument()`` target (`cendor-core` 1.14.1 / 1.14.2) and the gate sits on the one and only chain
the caller's value comes back through. Nothing about the TypeScript fix touches this path — the
point of the test is that a future change cannot quietly make Python behave like the pre-fix
TypeScript.
"""

from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.guardrails import GuardrailTripped, install, rules, uninstall

BANNED = "a forbidden answer"
CLEAN = "a fine answer"


def _openai_client(text: str):
    """An openai-shaped client whose `parse` issues its own request, like the real Python SDK."""
    posts: list[dict] = []

    def _response(**kwargs):
        posts.append(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=4),
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        )

    completions = SimpleNamespace(create=_response, parse=_response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client.posts = posts  # type: ignore[attr-defined]
    return client


@pytest.fixture
def gate():
    bus._reset()
    install([rules.keyword_deny(["forbidden"], action="block", stage="output")])
    yield
    uninstall()
    bus._reset()


ARGS = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}


def test_output_gate_blocks_a_direct_create(gate):
    client = instrument(_openai_client(BANNED))
    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(**ARGS)


def test_output_gate_blocks_a_parse_call(gate):
    """The TypeScript escape's twin — it must never be reachable here."""
    client = instrument(_openai_client(BANNED))
    with pytest.raises(GuardrailTripped):
        client.chat.completions.parse(**ARGS)


def test_clean_text_still_resolves_through_parse(gate):
    client = instrument(_openai_client(CLEAN))
    out = client.chat.completions.parse(**ARGS)
    assert out.choices[0].message.content == CLEAN


def test_parse_resolves_with_no_gate_installed():
    bus._reset()
    client = instrument(_openai_client(BANNED))
    out = client.chat.completions.parse(**ARGS)
    assert out.choices[0].message.content == BANNED  # no gate ⇒ nothing to block
    bus._reset()
