"""Offline, deterministic sensitive-data detectors for ``cendor.acttrace``.

A :class:`Detector` is a labelled regex plus an optional checksum/format **validator** that gates
loose matches (Luhn for cards, mod-97 for IBANs, Verhoeff for Aadhaar, ABA for US routing numbers,
range checks for SSNs). :data:`DETECTORS` is the single source of truth for both scanning and
redaction — :data:`cendor.acttrace.default_redactor` is rebuilt from it, so the original six
categories still scrub byte-for-byte.

Everything here is **local-first**: regex + arithmetic, no network, no model, no account. The
optional NER/ML detectors and locale gov-ID packs live behind extras (see docs/acttrace.md); the
default install stays pure-regex.

The registry is ordered with the original six categories first (``email`` → ``bearer_token``) so
redaction application order — and therefore output — is unchanged for pre-existing payloads.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

#: What a scrubbed span is replaced with. Kept identical to the original redactor's token.
REDACTED = "<redacted>"

#: Recommended detector groups. A :class:`~cendor.acttrace.Policy` maps a group (or a specific
#: category) to an action, so these strings are part of the public vocabulary.
Group = str  # "secret" | "credential" | "financial" | "gov_id" | "pii" | "special_category"
Severity = str  # "info" | "warning" | "critical"


@dataclass(frozen=True)
class Detector:
    """A single offline detector: a labelled pattern, optionally gated by a validator.

    Args:
        category: Stable, specific label recorded on findings/flags (e.g. ``"credit_card"``).
        group: Coarse family used for policy resolution (``"secret"``, ``"pii"``, ...).
        severity: Recommended seriousness — ``"info"`` | ``"warning"`` | ``"critical"``.
        pattern: Compiled regex. Every match is a *candidate*; ``validator`` decides if it counts.
        validator: Optional ``str -> bool`` gate applied to each raw match (checksum/format check).
            ``None`` means every regex match is accepted. A validator must never raise on the
            substrings its pattern produces.
    """

    category: str
    group: Group
    severity: Severity
    pattern: re.Pattern[str]
    validator: Callable[[str], bool] | None = None


# --------------------------------------------------------------------------- validators


def _digits(s: str) -> list[int]:
    return [int(c) for c in s if c.isdigit()]


def _luhn(number: str) -> bool:
    """Luhn (mod-10) check for a 13–19 digit payment card number."""
    d = _digits(number)
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(d)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _iban_mod97(iban: str) -> bool:
    """ISO 13616 IBAN check (mod-97 == 1) after the country/check-digit rearrangement."""
    s = re.sub(r"\s+", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    # A=10 ... Z=35; digits stay themselves. int(c, 36) yields exactly that mapping.
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


# Verhoeff dihedral-group tables (used by the Aadhaar locale pack; validator lives here in P1).
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff(number: str) -> bool:
    """Verhoeff checksum (used for India's Aadhaar); ``True`` when the trailing digit checks out."""
    d = _digits(number)
    c = 0
    for i, digit in enumerate(reversed(d)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][digit]]
    return c == 0


def _aba_valid(number: str) -> bool:
    """ABA routing-transit checksum for a 9-digit US routing number."""
    d = _digits(number)
    if len(d) != 9:
        return False
    checksum = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    return checksum % 10 == 0


def _ssn_valid(ssn: str) -> bool:
    """Reject structurally-invalid US SSNs (area 000/666/900-999, group 00, serial 0000)."""
    d = _digits(ssn)
    if len(d) != 9:
        return False
    area = d[0] * 100 + d[1] * 10 + d[2]
    group = d[3] * 10 + d[4]
    serial = d[5] * 1000 + d[6] * 100 + d[7] * 10 + d[8]
    if area == 0 or area == 666 or area >= 900:
        return False
    return group != 0 and serial != 0


def _phone_valid(s: str) -> bool:
    """A candidate phone number is plausible when it carries 9–15 significant digits."""
    return 9 <= len(re.sub(r"\D", "", s)) <= 15


# ISO 3166-1 alpha-2 codes, used to gate SWIFT/BIC (chars 5–6 are the country) so an arbitrary
# 8-letter uppercase token isn't mistaken for a bank code.
_ISO_ALPHA2 = frozenset(
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ "
    "BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM "
    "DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS "
    "GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN "
    "KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ "
    "MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM "
    "PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV "
    "SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI "
    "VN VU WF WS YE YT ZA ZM ZW".split()
)


def _bic_valid(s: str) -> bool:
    """Structural SWIFT/BIC check: 8 or 11 chars with a valid ISO-3166 country code at 5–6."""
    return len(s) in (8, 11) and s[4:6] in _ISO_ALPHA2


# --------------------------------------------------------------------------- registry


def _c(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


#: The built-in detectors. Ordered original-six-first so redaction output is byte-identical for
#: pre-existing payloads. ``register_detector`` appends to this list (custom detectors run last).
DETECTORS: list[Detector] = [
    # -- the original six (order preserved; email first) ----------------------------------------
    Detector("email", "pii", "warning", _c(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # openai/anthropic sk- keys incl. the hyphenated modern forms (sk-ant-…, sk-proj-…) + legacy
    Detector("api_key", "secret", "critical", _c(r"\bsk-[A-Za-z0-9_-]{8,}")),
    Detector("aws_key", "secret", "critical", _c(r"\bAKIA[0-9A-Z]{16}\b")),
    Detector("google_api_key", "secret", "critical", _c(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Detector(
        "jwt",
        "secret",
        "critical",
        _c(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    Detector("bearer_token", "secret", "critical", _c(r"\b[Bb]earer\s+[A-Za-z0-9._-]+\b")),
    # -- additional secrets --------------------------------------------------------------------
    Detector("github_token", "secret", "critical", _c(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    Detector("slack_token", "secret", "critical", _c(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    Detector(
        "private_key",
        "secret",
        "critical",
        _c(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
    # -- free-text credentials -----------------------------------------------------------------
    Detector(
        "password",
        "credential",
        "critical",
        _c(r"\b(?:password|passphrase|passwd|pwd)\b\s*(?:is|:|=)\s*\S+", re.IGNORECASE),
    ),
    # -- financial (validator-gated) -----------------------------------------------------------
    Detector("credit_card", "financial", "critical", _c(r"\b\d(?:[ -]?\d){12,18}\b"), _luhn),
    Detector("iban", "financial", "critical", _c(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), _iban_mod97),
    Detector("us_routing", "financial", "critical", _c(r"\b\d{9}\b"), _aba_valid),
    Detector(
        "swift_bic",
        "financial",
        "critical",
        _c(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b"),
        _bic_valid,
    ),
    # -- government IDs (validator-gated) ------------------------------------------------------
    Detector("us_ssn", "gov_id", "critical", _c(r"\b\d{3}-\d{2}-\d{4}\b"), _ssn_valid),
    # -- remaining PII --------------------------------------------------------------------------
    Detector(
        "phone",
        "pii",
        "warning",
        _c(
            r"(?<!\w)(?:\+?1[ .\-]?)?(?:\(\d{3}\)[ .\-]?|\d{3}[ .\-])\d{3}[ .\-]\d{4}(?!\d)"
            r"|(?<!\w)\+\d{9,15}(?!\d)"
        ),
        _phone_valid,
    ),
    Detector(
        "ipv4",
        "pii",
        "warning",
        _c(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
        ),
    ),
    Detector(
        "ipv6",
        "pii",
        "warning",
        _c(
            r"\b(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}\b"
            r"|\b(?:[A-Fa-f0-9]{1,4}:){1,7}:(?:[A-Fa-f0-9]{1,4}\b)?"
        ),
    ),
    Detector("mac_address", "pii", "warning", _c(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    # -- GDPR Art.9 special categories (best-effort keyword lexicon) ---------------------------
    Detector(
        "special_category",
        "special_category",
        "warning",
        _c(
            r"\b(?:diagnos(?:is|es|ed)?|hiv|pregnan(?:t|cy)|disab(?:led|ility)|biometric|"
            r"fingerprints?|genetic|ethnicity|religio(?:n|us))\b",
            re.IGNORECASE,
        ),
    ),
]


def register_detector(detector: Detector) -> None:
    """Add a custom :class:`Detector` to the global registry (it runs after the built-ins).

    The registry is the single source of truth: a registered detector is picked up by
    :func:`cendor.acttrace.scan`, :func:`cendor.acttrace.redact`, and — via the active policy —
    ``AuditLog``'s auto-flagging.
    """
    DETECTORS.append(detector)


def detectors() -> list[Detector]:
    """A copy of the active detector registry (built-ins plus anything registered)."""
    return list(DETECTORS)


def group_of(category: str) -> str | None:
    """The group a category belongs to per the active registry (``None`` if unknown)."""
    return next((d.group for d in DETECTORS if d.category == category), None)


# --------------------------------------------------------------------------- scan / scrub


def _scan_counts(obj: Any) -> dict[str, tuple[Detector, int]]:
    """Walk ``obj`` (str/dict/list) and count validated matches per category.

    Returns an insertion-ordered mapping ``category -> (detector, occurrences)``. Never returns
    the raw values — only counts — so callers can't accidentally chain a secret.
    """
    counts: dict[str, list[Any]] = {}

    def walk(o: Any) -> None:
        if isinstance(o, str):
            for det in DETECTORS:
                n = 0
                for match in det.pattern.finditer(o):
                    value = match.group(0)
                    if det.validator is None or det.validator(value):
                        n += 1
                if n:
                    slot = counts.get(det.category)
                    if slot is None:
                        counts[det.category] = [det, n]
                    else:
                        slot[1] += n
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    return {cat: (slot[0], slot[1]) for cat, slot in counts.items()}


def _scrub(obj: Any, categories: Iterable[str]) -> Any:
    """Return a copy of ``obj`` with every span matching a category in ``categories`` replaced.

    Applies detectors in **registry order** (original six first), so output is byte-identical to
    the historical redactor for the original categories. Validator-gated detectors only scrub the
    spans that actually validate.
    """
    wanted = set(categories)
    active = [d for d in DETECTORS if d.category in wanted]
    if not active:
        return obj

    def replacer(det: Detector) -> Callable[[re.Match[str]], str]:
        if det.validator is None:
            return lambda _m: REDACTED
        validator = det.validator
        return lambda m: REDACTED if validator(m.group(0)) else m.group(0)

    subs = [(d.pattern, replacer(d)) for d in active]

    def go(o: Any) -> Any:
        if isinstance(o, str):
            out = o
            for pattern, repl in subs:
                out = pattern.sub(repl, out)
            return out
        if isinstance(o, dict):
            return {k: go(v) for k, v in o.items()}
        if isinstance(o, list):
            return [go(v) for v in o]
        return o

    return go(obj)
