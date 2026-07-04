"""Opt-in detector packs — locale government IDs and a high-entropy generic-secret detector.

None of these ship in the default registry: the default install stays precision-first and
pure-regex. Enable them explicitly when you want the extra coverage (and, for entropy, accept the
extra false positives). Everything here is still **offline** — regex + local checksums/entropy, no
model, no network.

    from cendor.acttrace import enable_locale_pack, enable_entropy_detector

    enable_locale_pack("uk", "in")     # UK NINO + India Aadhaar (Verhoeff-checked)
    enable_entropy_detector()          # high-entropy tokens as generic secrets (noisy!)
"""

from __future__ import annotations

import math
from collections import Counter

from .detectors import DETECTORS, Detector, _c, _verhoeff, group_of, register_detector


def _nino_valid(s: str) -> bool:
    """UK National Insurance number prefix rules (offline structural check)."""
    s = s.replace(" ", "").upper()
    if len(s) != 9:
        return False
    first, second = s[0], s[1]
    if first in "DFIQUV" or second in "DFIOQUV":
        return False
    return s[:2] not in {"BG", "GB", "NK", "KN", "NT", "TN", "ZZ"}


#: Locale government-ID detectors, keyed by ISO country code. Registered only via
#: :func:`enable_locale_pack`. Aadhaar is Verhoeff-checked; NINO is prefix-validated.
LOCALE_PACKS: dict[str, list[Detector]] = {
    "uk": [
        Detector("uk_nino", "gov_id", "critical", _c(r"\b[A-Z]{2}\d{6}[A-D]\b"), _nino_valid),
    ],
    "in": [
        Detector(
            "in_aadhaar",
            "gov_id",
            "critical",
            _c(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b"),
            _verhoeff,
        ),
    ],
}


def enable_locale_pack(*codes: str) -> list[str]:
    """Register locale gov-ID detectors (opt-in). Idempotent; returns the categories added.

    Args:
        *codes: ISO country codes present in :data:`LOCALE_PACKS` (e.g. ``"uk"``, ``"in"``).

    Raises:
        ValueError: if a code has no bundled pack.
    """
    added: list[str] = []
    for code in codes:
        pack = LOCALE_PACKS.get(code.lower())
        if pack is None:
            raise ValueError(f"unknown locale pack {code!r}; available: {sorted(LOCALE_PACKS)}")
        for detector in pack:
            if group_of(detector.category) is None:  # not already registered → idempotent
                register_detector(detector)
                added.append(detector.category)
    return added


def _shannon_entropy(s: str) -> float:
    """Shannon entropy (bits/char) of a string."""
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def enable_entropy_detector(min_length: int = 24, min_entropy: float = 3.5) -> Detector:
    """Register a high-entropy generic-secret detector (opt-in). Idempotent (re-tunes in place).

    Catches opaque high-entropy tokens (generic API keys/secrets) the anchored detectors miss. It
    is **noisy** — hashes, base64 blobs, and long random ids also look high-entropy — which is why
    it is off by default. Category ``high_entropy_secret`` (group ``secret``).

    Args:
        min_length: Minimum token length to consider (shorter tokens are ignored).
        min_entropy: Minimum Shannon entropy (bits/char) for a token to count as a secret.
    """

    def _high_entropy(value: str) -> bool:
        return len(value) >= min_length and _shannon_entropy(value) >= min_entropy

    detector = Detector(
        "high_entropy_secret",
        "secret",
        "warning",
        _c(rf"\b[A-Za-z0-9+/=_-]{{{min_length},}}\b"),
        _high_entropy,
    )
    # Idempotent + re-tunable: drop any previous instance, then append the freshly-configured one.
    DETECTORS[:] = [d for d in DETECTORS if d.category != "high_entropy_secret"]
    register_detector(detector)
    return detector
