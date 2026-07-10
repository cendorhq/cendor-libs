"""Optional NER-backed redaction for names/addresses — the ``[ner]`` extra.

Regex can't reliably catch free-text **names** and **addresses**; that needs a model. This adapter
plugs `Microsoft Presidio <https://microsoft.github.io/presidio/>`_ in as a ``redactor=`` for
``AuditLog`` (or a standalone scrubber), **only** when the optional extra is installed::

    pip install "cendor-acttrace[ner]"

It is strictly opt-in — the default install stays pure-regex, offline, and dependency-light. When
the backend is absent, :func:`ner_available` returns ``False`` and :func:`ner_redactor` raises a
clear, actionable error (it never silently degrades or reaches the network — Presidio runs locally).

**Two things** are needed, and the ``[ner]`` extra pulls only the first: (1) Presidio + spaCy (the
extra), and (2) a spaCy **language model**, which is not on PyPI as a normal dependency — install it
once with ``python -m spacy download en_core_web_sm``. Without the model, :func:`ner_available`
returns ``False`` and :func:`ner_redactor` raises a :class:`RuntimeError` with that hint, instead of
letting Presidio shell out to ``pip`` to auto-download it (which hard-exits in a pip-less venv).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .detectors import REDACTED

#: Presidio entity types redacted by default (free-text PII the regex detectors can't catch).
DEFAULT_NER_ENTITIES = ("PERSON", "LOCATION", "NRP", "DATE_TIME")

#: The default spaCy model. Small (~12 MB) and enough for the default entities; swap for
#: ``en_core_web_lg`` (Presidio's recommendation) for higher accuracy.
DEFAULT_NER_MODEL = "en_core_web_sm"

_INSTALL_HINT = "NER redaction needs the optional extra: pip install 'cendor-acttrace[ner]'"
_MODEL_HINT = (
    "NER redaction needs a spaCy model that the [ner] extra does not install. Download it once: "
    "python -m spacy download {model}"
)


def _presidio_importable() -> bool:
    try:
        import presidio_analyzer  # noqa: F401
        import presidio_anonymizer  # noqa: F401
    except ImportError:
        return False
    return True


def _spacy_model_available(model: str) -> bool:
    """``True`` when ``model`` is an installed, loadable spaCy package (no download attempted)."""
    try:
        import spacy.util
    except ImportError:
        return False
    return bool(spacy.util.is_package(model))


def ner_available(model: str = DEFAULT_NER_MODEL) -> bool:
    """``True`` only when **both** the ``[ner]`` backend (Presidio) **and** a loadable spaCy
    ``model`` are present — the analyzer can't run without a language model, and the extra does not
    install one. Checking both here lets :func:`ner_redactor` fail with a clear, actionable message
    up front rather than letting Presidio hard-exit trying to auto-download the model."""
    return _presidio_importable() and _spacy_model_available(model)


def ner_redactor(
    entities: tuple[str, ...] = DEFAULT_NER_ENTITIES,
    language: str = "en",
    model: str = DEFAULT_NER_MODEL,
    compose: Callable[[Any], Any] | None = None,
) -> Callable[[Any], Any]:
    """Build a ``payload -> payload`` redactor that scrubs NER entities from strings.

    Pass the result to ``AuditLog(redactor=…)`` (a custom redactor owns its own flagging, so the
    built-in policy auto-flag does not also run), or call it directly. Walks dicts/lists like the
    built-in scrubber and replaces each detected entity span with ``<redacted>``.

    Args:
        entities: Presidio entity types to redact (defaults to names/locations/dates).
        language: Analyzer language code.
        model: The spaCy model to load (default ``en_core_web_sm``). The analyzer is built with an
            explicit ``NlpEngineProvider`` for this model, so a missing model raises the clear error
            below instead of Presidio shelling out to ``pip`` to download it.
        compose: An optional inner redactor to run **first** (e.g. :data:`default_redactor` to also
            scrub the regex categories); its output is then passed through NER.

    Raises:
        ImportError: if the ``[ner]`` extra (Presidio) is not installed.
        RuntimeError: if Presidio is installed but the spaCy ``model`` is not — with the
            ``python -m spacy download`` hint.
    """
    if not _presidio_importable():
        raise ImportError(_INSTALL_HINT)
    if not _spacy_model_available(model):
        raise RuntimeError(_MODEL_HINT.format(model=model))

    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig

    # Build the NLP engine explicitly for the (pre-verified) model, so Presidio loads it directly
    # instead of falling back to its pip/spacy auto-download path (which SystemExits in a pip-less
    # venv). See the module docstring.
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": language, "model_name": model}],
        }
    ).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=[language])
    anonymizer = AnonymizerEngine()
    wanted = list(entities)
    operators = {"DEFAULT": OperatorConfig("replace", {"new_value": REDACTED})}

    def _scrub_text(text: str) -> str:
        if not text:
            return text
        results = analyzer.analyze(text=text, entities=wanted, language=language)
        if not results:
            return text
        return anonymizer.anonymize(text=text, analyzer_results=results, operators=operators).text

    def _redact(obj: Any) -> Any:
        if compose is not None:
            obj = compose(obj)
        if isinstance(obj, str):
            return _scrub_text(obj)
        if isinstance(obj, dict):
            return {k: _redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact(v) for v in obj]
        return obj

    return _redact
