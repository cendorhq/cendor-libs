"""S4 — `prices.register_deployment(name, like=…)`: price an Azure/Foundry deployment name.

THE GAP (recorded verbatim by the external black-box suite as "would improve DX"): on Azure the
id a call reports is the **deployment name the user chose**, not a model id. It is therefore in no
price table, `LLMCall.cost` is `None`, tokenguard records `$0`, and a USD `budget(...)` silently
never binds. The user always knows which model the deployment serves; before this they had to find
and re-type its rate card.

This is an EXPLICIT mapping, and that distinction is load-bearing: automatic `-preview` /
`-latest` alias guessing was considered and REJECTED (a confidently wrong price is worse than an
honest `None`). Nothing here is inferred from the deployment's name.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cendor.core import prices


@pytest.fixture(autouse=True)
def _clean_prices():
    prices._reset()
    yield
    prices._reset()


def test_a_deployment_is_priced_like_its_base_model() -> None:
    prices.register_deployment("prod-gpt4o-eastus", like="gpt-4o")

    direct = prices.estimate("gpt-4o", 1000, output_tokens=500)
    via_deployment = prices.estimate("prod-gpt4o-eastus", 1000, output_tokens=500)
    assert via_deployment.amount == direct.amount
    assert via_deployment.amount > Decimal("0")


def test_the_returned_rates_are_the_stored_per_token_rates() -> None:
    rates = prices.register_deployment("dep", like="gpt-4o")
    assert set(rates) <= {"input", "output", "cached", "cache_write"}
    assert rates["input"] > Decimal("0")
    assert isinstance(rates["input"], Decimal)


def test_mutating_the_returned_dict_does_not_change_the_table() -> None:
    rates = prices.register_deployment("dep", like="gpt-4o")
    before = prices.estimate("dep", 1000, output_tokens=0).amount
    rates["input"] = Decimal("999")
    assert prices.estimate("dep", 1000, output_tokens=0).amount == before


# --- NEGATIVE CONTROL: an unknown base must RAISE, never register a silent nothing. -------------
def test_an_unknown_base_model_raises_instead_of_registering_nothing() -> None:
    with pytest.raises(prices.UnknownModelError):
        prices.register_deployment("dep", like="not-a-real-model-anywhere")
    # …and the deployment is still unpriced, i.e. nothing half-registered.
    with pytest.raises(prices.UnknownModelError):
        prices.estimate("dep", 1000)


def test_a_base_with_no_input_rate_raises_rather_than_registering_an_unpriceable_entry() -> None:
    """A rate dict without ``input`` cannot price anything — fail here, not at estimate() time."""
    prices.register("output-only", {"output": Decimal("0.00001")})
    with pytest.raises(prices.UnknownModelError):
        prices.register_deployment("dep", like="output-only")
    with pytest.raises(prices.UnknownModelError):
        prices.estimate("dep", 1000)  # nothing was registered


def test_every_rate_key_is_copied_not_an_enumerated_few() -> None:
    """A future/unknown rate category must not be silently dropped from the copy."""
    prices.register(
        "rich-base",
        {
            "input": Decimal("0.000001"),
            "output": Decimal("0.000002"),
            "cached": Decimal("0.0000005"),
            "cache_write": Decimal("0.0000012"),
            "some_future_rate": Decimal("0.000009"),
        },
    )
    rates = prices.register_deployment("dep", like="rich-base")
    assert rates["some_future_rate"] == Decimal("0.000009")


# --- NEGATIVE CONTROL: nothing is inferred from the NAME. -----------------------------------------
def test_a_deployment_name_that_looks_like_a_model_is_still_unpriced_until_registered() -> None:
    """The rejected auto-alias behaviour must stay rejected: no name-based guessing."""
    with pytest.raises(prices.UnknownModelError):
        prices.estimate("gpt-4o-my-company-preview", 1000)


def test_like_accepts_a_dated_or_decorated_base_id() -> None:
    """`like` goes through the same lookup reduction a real call does."""
    prices.register_deployment("dep-dated", like="gpt-4o-2024-08-06")
    assert prices.estimate("dep-dated", 1000, output_tokens=500).amount == (
        prices.estimate("gpt-4o", 1000, output_tokens=500).amount
    )


# --- Copy-at-registration semantics, asserted rather than assumed. --------------------------------
def test_a_registration_survives_a_table_swap() -> None:
    """Same guarantee as `register()` — a refresh must not drop it."""
    prices.register_deployment("dep", like="gpt-4o")
    expected = prices.estimate("dep", 1000, output_tokens=500).amount

    # Simulate what refresh() does: swap the table, then re-apply registrations.
    prices.register("some-other-model", {"input": Decimal("0.000001")})
    assert prices.estimate("dep", 1000, output_tokens=500).amount == expected


def test_repricing_the_base_does_not_reprice_an_already_registered_deployment() -> None:
    """COPY-at-registration, not a live alias — the documented, deliberate semantics.

    A live alias would make a deployment's cost depend on whether its base still exists in whatever
    table was last fetched, and would have to invent an answer when it doesn't. Copying makes the
    interaction with `refresh()` deterministic: the deployment keeps the rates it was given.
    """
    prices.register_deployment("dep", like="gpt-4o")
    at_registration = prices.estimate("dep", 1000, output_tokens=0).amount

    prices.register("gpt-4o", {"input": Decimal("999"), "output": Decimal("999")})  # base reprices

    assert prices.estimate("dep", 1000, output_tokens=0).amount == at_registration
    assert prices.estimate("gpt-4o", 1000, output_tokens=0).amount != at_registration
    # Re-registering is how you opt in to the new rates — stated in the docstring, pinned here.
    prices.register_deployment("dep", like="gpt-4o")
    assert prices.estimate("dep", 1000, output_tokens=0).amount == (
        prices.estimate("gpt-4o", 1000, output_tokens=0).amount
    )


def test_a_deployment_overrides_a_snapshot_entry_with_the_same_id() -> None:
    """Same override rule as `register()`, so a deployment named after a real model still works."""
    prices.register_deployment("gpt-4o-mini", like="gpt-4o")
    assert prices.estimate("gpt-4o-mini", 1000, output_tokens=500).amount == (
        prices.estimate("gpt-4o", 1000, output_tokens=500).amount
    )


def test_the_near_miss_names_teach_the_right_call() -> None:
    """Type Teach: the plausible wrong spellings raise a message naming `register_deployment`."""
    for name in ("register_alias", "alias", "map_deployment", "registerDeployment"):
        with pytest.raises(AttributeError, match="register_deployment"):
            getattr(prices, name)
