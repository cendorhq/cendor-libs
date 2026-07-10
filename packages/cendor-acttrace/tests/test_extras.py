"""Opt-in extras (roadmap phase 3): locale gov-ID packs, entropy detector, and the NER adapter.

All are strictly opt-in — the default install stays pure-regex and offline, and nothing here is a
hard dependency. No network in any test.
"""

import pytest
from cendor.acttrace import (
    DETECTORS,
    enable_entropy_detector,
    enable_locale_pack,
    ner_available,
    ner_redactor,
    scan,
)
from cendor.acttrace.detectors import _verhoeff
from cendor.acttrace.packs import _nino_valid, _shannon_entropy


@pytest.fixture
def restore_registry():
    # Snapshot the global registry so an opt-in pack/detector doesn't leak into other tests.
    original = list(DETECTORS)
    yield
    DETECTORS[:] = original


# --------------------------------------------------------------------------- off by default


def test_extras_are_off_by_default():
    # A clean install detects none of the opt-in categories.
    assert scan("ni AB123456C and aadhaar 2341 2341 2346") == []
    assert all(d.category != "high_entropy_secret" for d in DETECTORS)
    assert not any(d.category in {"uk_nino", "in_aadhaar"} for d in DETECTORS)


# --------------------------------------------------------------------------- locale packs


def test_enable_locale_pack_adds_detectors(restore_registry):
    added = enable_locale_pack("uk", "in")
    assert set(added) == {"uk_nino", "in_aadhaar"}
    assert [f.category for f in scan("ni AB123456C")] == ["uk_nino"]
    assert [f.category for f in scan("aadhaar 2341 2341 2346")] == ["in_aadhaar"]


def test_enable_locale_pack_is_idempotent(restore_registry):
    enable_locale_pack("in")
    before = len(DETECTORS)
    assert enable_locale_pack("in") == []  # nothing added the second time
    assert len(DETECTORS) == before


def test_enable_locale_pack_unknown_code_rejected(restore_registry):
    with pytest.raises(ValueError, match="unknown locale pack"):
        enable_locale_pack("zz")


@pytest.mark.parametrize(
    "aadhaar,ok",
    [("234123412346", True), ("234123412345", False), ("2341 2341 2346", True)],
)
def test_aadhaar_verhoeff_checksum(aadhaar, ok):
    assert _verhoeff(aadhaar) is ok


@pytest.mark.parametrize(
    "nino,ok",
    [
        ("AB123456C", True),
        ("DA123456C", False),  # first letter D is disallowed
        ("BG123456C", False),  # BG is a disallowed prefix
        ("AO123456C", False),  # second letter O is disallowed
    ],
)
def test_nino_prefix_validator(nino, ok):
    assert _nino_valid(nino) is ok


def test_aadhaar_validator_gates_bad_checksums(restore_registry):
    enable_locale_pack("in")
    assert scan("aadhaar 234123412345") == []  # invalid Verhoeff -> no finding


# --------------------------------------------------------------------------- entropy detector


def test_entropy_detector_flags_high_entropy_tokens(restore_registry):
    enable_entropy_detector(min_length=24, min_entropy=3.5)
    hits = [f.category for f in scan("secret dGhpcyBpcyBhIHJhbmRvbXNlY3JldDEyMzQ1Njc4OTBhYg")]
    assert hits == ["high_entropy_secret"]


def test_entropy_detector_ignores_low_entropy_and_short(restore_registry):
    enable_entropy_detector(min_length=24, min_entropy=3.5)
    assert scan("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") == []  # long but ~zero entropy
    assert scan("abc123") == []  # too short


def test_entropy_detector_is_retunable(restore_registry):
    enable_entropy_detector(min_length=24)
    enable_entropy_detector(min_length=8, min_entropy=2.0)  # re-tune in place, no duplicate
    assert sum(d.category == "high_entropy_secret" for d in DETECTORS) == 1


def test_shannon_entropy_basics():
    assert _shannon_entropy("") == 0.0
    assert _shannon_entropy("aaaa") == 0.0  # single symbol -> zero entropy
    assert _shannon_entropy("ab") == pytest.approx(1.0)  # two equiprobable symbols -> 1 bit


# --------------------------------------------------------------------------- NER adapter


def test_ner_available_reflects_optional_extra():
    assert isinstance(ner_available(), bool)


@pytest.mark.skipif(ner_available(), reason="the [ner] extra is installed")
def test_ner_redactor_raises_clear_error_when_extra_absent():
    with pytest.raises(ImportError, match=r"cendor-acttrace\[ner\]"):
        ner_redactor()


def test_ner_available_false_without_a_spacy_model(monkeypatch):
    # M5: the [ner] extra installs Presidio + spaCy but NOT a language model. Even with Presidio
    # importable, ner_available() must report False when no model is loadable.
    from cendor.acttrace import ner as ner_mod

    monkeypatch.setattr(ner_mod, "_presidio_importable", lambda: True)
    monkeypatch.setattr(ner_mod, "_spacy_model_available", lambda model: False)
    assert ner_available() is False


def test_ner_redactor_raises_model_hint_when_model_missing(monkeypatch):
    # M5: Presidio present but no spaCy model -> a clear RuntimeError with the download hint,
    # NOT Presidio's pip/spacy auto-download subprocess (which SystemExits in a pip-less venv).
    from cendor.acttrace import ner as ner_mod

    monkeypatch.setattr(ner_mod, "_presidio_importable", lambda: True)
    monkeypatch.setattr(ner_mod, "_spacy_model_available", lambda model: False)
    with pytest.raises(RuntimeError, match=r"spacy download en_core_web_sm"):
        ner_redactor()


@pytest.mark.skipif(not ner_available(), reason="requires the [ner] extra + a spaCy model")
def test_ner_redactor_scrubs_a_name_when_installed():
    redactor = ner_redactor()
    out = redactor({"note": "call Alice Smith about the invoice"})
    assert "Alice Smith" not in str(out)
