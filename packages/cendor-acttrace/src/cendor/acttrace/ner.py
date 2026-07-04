"""Optional NER-backed redaction for names/addresses — the ``[ner]`` extra.

Regex can't reliably catch free-text **names** and **addresses**; that needs a model. This adapter
plugs `Microsoft Presidio <https://microsoft.github.io/presidio/>`_ in as a ``redactor=`` for
``AuditLog`` (or a standalone scrubber), **only** when the optional extra is installed::

    pip install "cendor-acttrace[ner]"

It is strictly opt-in — the default install stays pure-regex, offline, and dependency-light. When
the extra is absent, :func:`ner_available` returns ``False`` and :func:`ner_redactor` raises a
clear, actionable :class:`ImportError` (it never silently degrades or reaches the network — Presidio
runs locally).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .detectors import REDACTED

#: Presidio entity types redacted by default (free-text PII the regex detectors can't catch).
DEFAULT_NER_ENTITIES = ("PERSON", "LOCATION", "NRP", "DATE_TIME")

_INSTALL_HINT = "NER redaction needs the optional extra: pip install 'cendor-acttrace[ner]'"


def ner_available() -> bool:
    """``True`` if the ``[ner]`` backend (Presidio) is importable in this environment."""
    try:
        import presidio_analyzer  # noqa: F401
        import presidio_anonymizer  # noqa: F401
    except ImportError:
        return False
    return True


def ner_redactor(
    entities: tuple[str, ...] = DEFAULT_NER_ENTITIES,
    language: str = "en",
    compose: Callable[[Any], Any] | None = None,
) -> Callable[[Any], Any]:
    """Build a ``payload -> payload`` redactor that scrubs NER entities from strings.

    Pass the result to ``AuditLog(redactor=…)`` (a custom redactor owns its own flagging, so the
    built-in policy auto-flag does not also run), or call it directly. Walks dicts/lists like the
    built-in scrubber and replaces each detected entity span with ``<redacted>``.

    Args:
        entities: Presidio entity types to redact (defaults to names/locations/dates).
        language: Analyzer language code.
        compose: An optional inner redactor to run **first** (e.g. :data:`default_redactor` to also
            scrub the regex categories); its output is then passed through NER.

    Raises:
        ImportError: if the ``[ner]`` extra is not installed.
    """
    if not ner_available():
        raise ImportError(_INSTALL_HINT)

    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig

    analyzer = AnalyzerEngine()
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
