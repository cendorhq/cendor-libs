"""cendor.cassette — record an agent run once, replay it forever. Offline, deterministic, free.

The ``vcrpy`` of the agent era, except it captures the *whole* run: every LLM call and tool call,
in order. It cooperates through ``cendor.core`` — it never patches a client itself:

  * **record** — subscribes to the bus, capturing each ``LLMCall``/``ToolCall`` (request + the raw
    response core attaches) keyed by a normalized request hash, then writes a JSON cassette.
  * **replay** — registers a core *interceptor* that returns the recorded response by hash before
    the real call runs. Unknown call → clear failure.

Secrets/PII are redacted on record (cassettes get committed). ``semantic_match`` asserts *meaning*
for output that won't be byte-identical: a lexical default (offline, zero-dep), an optional local
embedding backend (``local_embedding_scorer``, via the ``embeddings`` extra), or a bring-your-own
``embed_fn`` (``embedding_scorer``) that wraps any provider's embeddings.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from cendor.core import add_ambient_provider, bus
from cendor.core.instrument import MISS, add_interceptor, remove_interceptor
from cendor.core.types import LLMCall, ToolCall

__all__ = [
    "use",
    "using",
    "promote",
    "drift",
    "semantic_drift",
    "semantic_match",
    "lexical_score",
    "cosine",
    "embedding_scorer",
    "local_embedding_scorer",
    "openai_embedding_scorer",
    "CassetteEntry",
    "CassetteError",
]

#: Current cassette format. v2 folds ``stream`` into the request hash and records a
#: ``response_type`` marker; v1 (no ``stream`` in the hash, no marker) is still readable on replay.
_FORMAT_VERSION = 2
_SUPPORTED_VERSIONS = (1, 2)
_drift: list[dict] = []  # divergences found by the most recent mode="rerecord" run

#: The record/replay modes accepted by :func:`use` / :func:`using`, as a type so an editor
#: autocompletes them and a typo is a type error: ``"auto"`` (record if the cassette file is
#: missing, else replay), ``"record"``/``"replay"`` (force one), ``"rerecord"`` (run live and
#: report :func:`drift` without overwriting).
Mode = Literal["auto", "record", "replay", "rerecord"]

#: Marks which record/replay context an event belongs to, so concurrent ``using()`` blocks on the
#: process-global bus don't capture each other's events. Set on context entry; asyncio tasks inherit
#: it, plain threads that start their own context get their own value. docs/cassette.md.
_active_session: ContextVar[str | None] = ContextVar("cendor_cassette_session", default=None)

#: GLR-7: reserved internal metadata key carrying the record/replay session id, stamped at call
#: initiation by :func:`_cassette_ambient`. A **top-level** metadata key (not inside
#: ``request_kwargs``), so it never reaches the provider and is invisible to the replay fingerprint
#: (``_normalized_request`` hashes only kind/provider/model/messages/stream) — every recorded
#: cassette replays byte-identically, nothing to re-record.
_SESSION_KEY = "_cendor_cassette_session"


def _cassette_ambient(event: Any) -> dict | None:
    """The ambient provider (GLR-7): stamp the active session onto an event at construction — the
    caller's synchronous frame, inside the ``using()`` scope — so the recorder can record and the
    replayer can match even a streamed call finalized after the scope exits (or drained inside a
    different session's scope). No-op outside a session."""
    session = _active_session.get()
    return {_SESSION_KEY: session} if session else None


def _session_of(event: Any) -> str | None:
    """The session an event belongs to: prefer the pre-flight stamp; fall back to the delivery-time
    contextvar (split-brain: the event was built by a second cendor-core copy)."""
    meta = getattr(event, "metadata", None)
    if isinstance(meta, dict):
        stamped = meta.get(_SESSION_KEY)
        if isinstance(stamped, str):
            return stamped
    return _active_session.get()


class CassetteError(Exception):
    """Raised on replay when a call has no matching recorded entry, or on an unreadable cassette."""


@dataclass
class CassetteEntry:
    """One recorded interaction in a run. docs/cassette.md §5."""

    seq: int
    kind: str  # "llm" | "tool"
    request_hash: str
    request: dict
    response: Any
    #: "mapping" (dict-like response, e.g. Ollama/Bedrock — replayed as a dict) or "object"
    #: (SDK-like — replayed as an attribute-accessible namespace). Preserves the caller's access
    #: style so dict-access code doesn't get a namespace (or vice versa) on replay.
    response_type: str = "object"


# --------------------------------------------------------------------------- redaction

# Kept consistent with acttrace's `_REDACTION_CATEGORIES` (plus a long-opaque-token catch-all that
# acttrace omits, since it stores hashes). Each secret pattern is prefix-anchored so a plain
# hyphenated phrase is not scrubbed.
_REDACTIONS = [
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # email
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),  # openai keys: sk-, sk-ant-…, sk-proj-…, legacy
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # bare JWT
    re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._-]+\b"),  # bearer tokens
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),  # long opaque tokens
]


def _redact(obj: Any) -> Any:
    if isinstance(obj, str):
        out = obj
        for pat in _REDACTIONS:
            out = pat.sub("<redacted>", out)
        return out
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _redactor(redact: bool | Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Resolve the ``redact`` argument to a ``obj -> obj`` scrubber applied to what's written.

    ``True`` (default) uses the built-in patterns (emails; ``sk-``/``sk-ant-``/``sk-proj-`` keys;
    AWS + Google API keys; JWTs; bearer/opaque tokens); ``False`` writes content verbatim; a
    callable is used as a custom scrubber. Redaction affects
    only the *stored* request/response — request **matching hashes the un-redacted content**, so two
    requests that differ only inside a redacted span still replay to distinct entries.
    """
    if redact is True:
        return _redact
    if redact is False:
        return lambda obj: obj
    if callable(redact):
        return redact
    raise TypeError("redact must be True, False, or a callable (obj -> obj)")


# --------------------------------------------------------------------------- serialization


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    for attr in ("model_dump", "dict", "to_dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return _to_jsonable(method())
            except Exception:  # noqa: BLE001 - best-effort serialization
                pass
    if hasattr(obj, "__dict__"):
        return _to_jsonable(vars(obj))
    return str(obj)


def _reconstruct(obj: Any) -> Any:
    """Rebuild an attribute-accessible object (SDK-like) from a jsonable response."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _reconstruct(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_reconstruct(v) for v in obj]
    return obj


def _reconstruct_response(response: Any, response_type: str) -> Any:
    """Rebuild a recorded LLM response in the caller's original access style.

    ``"mapping"`` returns the stored dict unchanged (dict-access providers like Ollama/Bedrock);
    ``"object"`` (the default, and what older v1 cassettes fall back to) rebuilds an
    attribute-accessible namespace (OpenAI/Anthropic SDK objects)."""
    if response_type == "mapping":
        return response
    return _reconstruct(response)


def _response_marker(raw: Any) -> str:
    """Whether a raw provider response is a mapping (dict access) or an SDK-like object."""
    return "mapping" if isinstance(raw, Mapping) else "object"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + ``os.replace``) so a concurrent reader
    never sees a half-written cassette and two writers can't interleave bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load_cassette(path: Path) -> dict:
    """Read + validate a cassette file. Raises :class:`CassetteError` on an unreadable file or an
    unsupported format version (instead of a blind ``KeyError``/``JSONDecodeError`` later)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CassetteError(f"cannot read cassette {path.name}: {e}") from e
    version = payload.get("version", 1)
    if version not in _SUPPORTED_VERSIONS:
        raise CassetteError(
            f"unsupported cassette format version {version!r} in {path.name}; this "
            f"cendor-cassette supports versions {_SUPPORTED_VERSIONS} — upgrade the package "
            f"or re-record the cassette"
        )
    return payload


def _default_normalizer(version: int) -> Callable[[Any], dict]:
    """The built-in normalizer, matching a cassette's format: v2 folds ``stream`` into the hash;
    v1 (legacy) omits it, so committed v1 cassettes keep replaying."""
    return functools.partial(_normalized_request, include_stream=version >= 2)


# --------------------------------------------------------------------------- hashing


def _canonical_tool_arguments(arguments: Any) -> dict:
    """Canonicalize tool arguments to the ``{"args": [...], "kwargs": {...}}`` shape core's
    ``_pre_tool`` produces live, so a promoted tool entry hashes the same as the live call.

    Accepts the already-wrapped shape (used verbatim), a bare positional ``list`` (→ ``args``), or a
    bare keyword ``dict`` (→ ``kwargs``) — the shapes a hand-written or exported trace might carry.
    """
    if isinstance(arguments, dict) and ("args" in arguments or "kwargs" in arguments):
        return {
            "args": _to_jsonable(arguments.get("args", [])),
            "kwargs": _to_jsonable(arguments.get("kwargs", {})),
        }
    if isinstance(arguments, list):
        return {"args": _to_jsonable(arguments), "kwargs": {}}
    if isinstance(arguments, dict):
        return {"args": [], "kwargs": _to_jsonable(arguments)}
    return {"args": [_to_jsonable(arguments)], "kwargs": {}}


def _normalized_request(event: Any, *, include_stream: bool = True) -> dict:
    # Un-redacted (and coerced jsonable so hashing never trips on SDK objects). Redaction is applied
    # only to what gets *written*, not to the matching key — see _redactor.
    if isinstance(event, LLMCall):
        req = {
            "kind": "llm",
            "provider": event.provider,
            "model": event.model,
            "messages": _to_jsonable(event.messages),
        }
        if include_stream:
            # A stream=True call and a stream=False call take different code paths (chunk iterator
            # vs. whole response), so they must not collide on one entry. v1 cassettes omit this.
            kwargs = (event.metadata or {}).get("request_kwargs") or {}
            req["stream"] = bool(kwargs.get("stream", False))
        return req
    return {
        "kind": "tool",
        "name": event.name,
        "arguments": _canonical_tool_arguments(event.arguments),
    }


def _hash(request: dict) -> str:
    canonical = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- record / replay


@contextmanager
def _recording(
    path: Path, normalizer: Callable[[Any], dict] | None, redactor: Callable[[Any], Any]
) -> Iterator[None]:
    norm = normalizer or _normalized_request  # record always writes the current format (v2)
    entries: list[CassetteEntry] = []
    session = uuid.uuid4().hex

    def recorder(event: Any) -> None:
        if _session_of(event) != session:
            return  # a concurrent using() block on the shared bus — not ours
        if isinstance(event, LLMCall):
            raw = event.metadata.get("response")
            response = redactor(_to_jsonable(raw))
            marker = _response_marker(raw)
            kind = "llm"
        elif isinstance(event, ToolCall):
            response = redactor(_to_jsonable(event.result))
            marker = _response_marker(event.result)
            kind = "tool"
        else:
            return
        request = norm(event)  # un-redacted: this is the matching key
        entries.append(
            CassetteEntry(len(entries), kind, _hash(request), redactor(request), response, marker)
        )

    add_ambient_provider(_cassette_ambient)  # GLR-7: stamp the session pre-emit (idempotent)
    bus.subscribe(recorder)
    token = _active_session.set(session)
    try:
        yield
    finally:
        _active_session.reset(token)
        bus.unsubscribe(recorder)
        payload = {"version": _FORMAT_VERSION, "entries": [asdict(e) for e in entries]}
        _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))


@contextmanager
def _replaying(path: Path, normalizer: Callable[[Any], dict] | None) -> Iterator[None]:
    payload = _load_cassette(path)
    norm = normalizer or _default_normalizer(payload.get("version", 1))
    by_hash: dict[str, list[Any]] = {}
    for entry in payload["entries"]:
        by_hash.setdefault(entry["request_hash"], []).append(entry)
    cursor: dict[str, int] = {}  # per-replay context, keyed by hash (not a module global)
    session = uuid.uuid4().hex

    def interceptor(event: Any) -> Any:
        if _session_of(event) != session:
            return MISS  # another replay context's call — decline so its interceptor handles it
        request = norm(event)
        h = _hash(request)
        queue = by_hash.get(h, [])
        i = cursor.get(h, 0)
        if i >= len(queue):
            raise CassetteError(
                f"no recorded response for {request.get('kind')} request (hash {h[:12]}…) "
                f"in {path.name}; re-record the cassette"
            )
        cursor[h] = i + 1
        entry = queue[i]
        if entry["kind"] == "llm":
            return _reconstruct_response(entry["response"], entry.get("response_type", "object"))
        return entry["response"]

    add_ambient_provider(_cassette_ambient)  # GLR-7: stamp the session pre-emit (idempotent)
    add_interceptor(interceptor)
    token = _active_session.set(session)
    try:
        yield
    finally:
        _active_session.reset(token)
        remove_interceptor(interceptor)


@contextmanager
def _rerecording(
    path: Path, normalizer: Callable[[Any], dict] | None, redactor: Callable[[Any], Any]
) -> Iterator[None]:
    """Run live (no replay), diffing each live response against the cassette. Never overwrites."""
    _drift.clear()
    payload: dict = (
        _load_cassette(path) if path.exists() else {"version": _FORMAT_VERSION, "entries": []}
    )
    norm = normalizer or _default_normalizer(int(payload.get("version", 1)))
    by_hash: dict[str, list[Any]] = {}
    for entry in payload["entries"]:
        by_hash.setdefault(entry["request_hash"], []).append(entry)
    cursor: dict[str, int] = {}
    session = uuid.uuid4().hex

    def recorder(event: Any) -> None:
        if _session_of(event) != session:
            return  # a concurrent context's event — not ours
        if isinstance(event, LLMCall):
            live = redactor(_to_jsonable(event.metadata.get("response")))
            kind = "llm"
        elif isinstance(event, ToolCall):
            live = redactor(_to_jsonable(event.result))
            kind = "tool"
        else:
            return
        h = _hash(norm(event))
        queue = by_hash.get(h, [])
        i = cursor.get(h, 0)
        cursor[h] = i + 1
        recorded = queue[i]["response"] if i < len(queue) else None
        if recorded != live:
            _drift.append({"request_hash": h, "kind": kind, "recorded": recorded, "live": live})

    add_ambient_provider(_cassette_ambient)  # GLR-7: stamp the session pre-emit (idempotent)
    bus.subscribe(recorder)
    token = _active_session.set(session)
    try:
        yield
    finally:
        _active_session.reset(token)
        bus.unsubscribe(recorder)


def drift() -> list[dict]:
    """Divergences from the most recent ``rerecord`` run (request_hash, kind, recorded, live)."""
    return list(_drift)


def _manager(
    path: str,
    mode: str,
    normalizer: Callable[[Any], dict] | None,
    redact: bool | Callable[[Any], Any],
) -> Any:
    """Resolve the record/replay context manager for ``path``/``mode`` (``auto`` = replay if the
    cassette exists, else record). Shared by :func:`use` and :func:`using`.

    ``normalizer`` is passed through as-is (a custom one owns the whole hash); each sub-manager
    resolves the version-aware built-in default when ``normalizer is None``."""
    file = Path(path)
    effective = mode
    if mode == "auto":
        effective = "replay" if file.exists() else "record"
    if effective == "replay":
        return _replaying(file, normalizer)
    if effective == "rerecord":
        return _rerecording(file, normalizer, _redactor(redact))
    return _recording(file, normalizer, _redactor(redact))


def use(
    path: str,
    mode: Mode = "auto",
    normalizer: Callable[[Any], dict] | None = None,
    redact: bool | Callable[[Any], Any] = True,
) -> Callable:
    """Decorator: record the wrapped run on first use, replay it thereafter. docs/cassette.md §3.

    ```python
    from cendor import cassette

    @cassette.use("tests/fixtures/refund.json")
    def test_refund_flow():
        assert my_agent.run("refund please") == "processing your refund"
    ```

    Modes: ``"auto"`` (record if the cassette file is missing, else replay), ``"record"``
    (always record), ``"replay"`` (always replay; fail on an unrecorded call), ``"rerecord"``
    (run live and report drift via :func:`drift` without overwriting the cassette).

    ``normalizer`` is a pluggable ``event -> dict`` that decides what makes two requests "the same"
    for matching (default: provider/model/messages or name/arguments). Use it to ignore volatile
    fields (e.g. a request id or a timestamp in the prompt). See :func:`using` for a ``with``-block
    form (handy in pytest fixtures).

    ``redact`` scrubs what gets *written* to the cassette (not the matching key): ``True`` (default,
    built-in secret patterns), ``False`` (write verbatim — use when a response carries long IDs the
    default would over-redact), or a custom ``obj -> obj`` scrubber.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _manager(path, mode, normalizer, redact):
                return func(*args, **kwargs)

        return wrapper

    return decorator


@contextmanager
def using(
    path: str,
    mode: Mode = "auto",
    normalizer: Callable[[Any], dict] | None = None,
    redact: bool | Callable[[Any], Any] = True,
) -> Iterator[None]:
    """Context-manager form of :func:`use` — record/replay a ``with`` block instead of a function.

    Same modes, ``normalizer``, and ``redact`` as :func:`use`; convenient in pytest fixtures:

    ```python
    from cendor import cassette

    with cassette.using("tests/fixtures/run.json"):
        result = my_agent.run("refund please")
    ```
    """
    with _manager(path, mode, normalizer, redact):
        yield


def promote(trace_path: str, to: str, redact: bool | Callable[[Any], Any] = True) -> int:
    """Convert a JSONL trace of calls into a replayable cassette. docs/cassette.md §2, §6.

    ```python
    from cendor.cassette import promote

    n = promote("run.jsonl", "tests/fixtures/run.json")   # -> number of entries written
    ```

    Each trace line is ``{"kind": "llm"|"tool", "request": {...}, "response": ...}`` (a tool may
    use ``"result"`` instead of ``"response"``); ``_meta`` and unrecognized lines are skipped.
    An ``llm`` request carries ``provider``/``model``/``messages``; a ``tool`` request carries
    ``name``/``arguments`` — the same shape live calls hash by, so the cassette replays cleanly.
    ``redact`` scrubs what's written (same as :func:`use`). Returns the number of entries written.
    """
    redactor = _redactor(redact)
    entries: list[CassetteEntry] = []
    for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if "_meta" in row:
            continue
        kind = row.get("kind")
        request = row.get("request")
        if kind not in ("llm", "tool") or not isinstance(request, dict):
            continue
        response = row.get("response", row.get("result"))
        norm = _normalized_request_from(kind, request)  # un-redacted: the matching key
        entries.append(
            CassetteEntry(len(entries), kind, _hash(norm), redactor(norm), redactor(response))
        )

    dst = Path(to)
    payload = {"version": _FORMAT_VERSION, "entries": [asdict(e) for e in entries]}
    _atomic_write(dst, json.dumps(payload, indent=2, ensure_ascii=False))
    return len(entries)


def _normalized_request_from(kind: str, request: dict) -> dict:
    """Build the same (un-redacted) normalized request :func:`_normalized_request` derives live —
    including the v2 ``stream`` flag and the canonical ``{"args","kwargs"}`` tool shape, so a
    promoted entry replays against the matching live call."""
    if kind == "llm":
        return {
            "kind": "llm",
            "provider": request.get("provider"),
            "model": request.get("model"),
            "messages": _to_jsonable(request.get("messages", [])),
            "stream": bool(request.get("stream", False)),
        }
    return {
        "kind": "tool",
        "name": request.get("name"),
        "arguments": _canonical_tool_arguments(request.get("arguments", {})),
    }


# --------------------------------------------------------------------------- semantic match

_WORD = re.compile(r"[a-z0-9']+")


def _norm(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def lexical_score(actual: str, expected: str) -> float:
    """The default offline similarity score in [0, 1]: max(sequence ratio, keyword containment)."""
    a, e = _norm(actual), _norm(expected)
    if not e:
        return 1.0
    ratio = SequenceMatcher(None, a, e).ratio()
    a_tokens, e_tokens = set(a.split()), set(e.split())
    containment = len(a_tokens & e_tokens) / len(e_tokens) if e_tokens else 1.0
    return max(ratio, containment)


def semantic_match(
    actual: str,
    expected: str,
    threshold: float = 0.6,
    scorer: Callable[[str, str], float] | None = None,
) -> bool:
    """Assert ``actual`` means roughly ``expected``. Lexical default (offline, deterministic).

    ```python
    from cendor.cassette import semantic_match

    out = my_agent.run("why was I charged?")
    assert semantic_match(out, "explains the charge")
    ```

    The default :func:`lexical_score` is **recall-oriented** (keyword containment): it matches when
    ``actual`` contains ``expected``'s words, so it tolerates extra surrounding text — but it is
    *not* meaning-aware and will accept a negation or superset (``"we will not offer a refund"``
    matches ``"offer a refund"``). For adversarial or negation-sensitive checks, pass a ``scorer``
    — a pluggable ``(actual, expected) -> float in [0, 1]`` (e.g. an embedding or LLM judge) that
    swaps the default without changing call sites.
    """
    score = (scorer or lexical_score)(actual, expected)
    return score >= threshold


# --------------------------------------------------------------------------- embedding scorers
#
# Default matching is lexical (above): offline, deterministic, zero-dependency — the right default
# for a *test* tool. For meaning-aware matching, plug a scorer into semantic_match()/drift.


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors, in [-1, 1] (0 for empty/degenerate input)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def embedding_scorer(
    embed_fn: Callable[[list[str]], list[list[float]]],
) -> Callable[[str, str], float]:
    """Build a ``semantic_match`` scorer from any embedder — the bring-your-own-model path.

    ``embed_fn(texts) -> vectors`` can wrap **any** provider or local model (OpenAI, Voyage, Cohere,
    a local model …); the returned scorer embeds both strings and returns their cosine similarity
    clamped to [0, 1]. cassette binds no model and gains no dependency. For cloud embedders, mind
    that scoring then makes a network call (non-hermetic) — prefer a local model for test runs.
    """

    def score(actual: str, expected: str) -> float:
        vecs = embed_fn([actual, expected])
        if not vecs or len(vecs) < 2:
            return 0.0
        return max(0.0, cosine(list(vecs[0]), list(vecs[1])))

    return score


def local_embedding_scorer(model: str = "minishlab/potion-base-8M") -> Callable[[str, str], float]:
    """A free, offline, deterministic embedding scorer backed by **model2vec** (static embeddings).

    Needs the optional extra: ``pip install 'cendor-cassette[embeddings]'`` (numpy-only, no
    torch). The recommended zero-config semantic backend for tests. Pass to ``semantic_match`` /
    ``semantic_drift`` as ``scorer=``.
    """
    try:
        from model2vec import StaticModel  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "local_embedding_scorer needs the 'embeddings' extra: "
            "pip install 'cendor-cassette[embeddings]' (installs model2vec). "
            "Or pass your own embed_fn to embedding_scorer()."
        ) from exc
    encoder = StaticModel.from_pretrained(model)

    def embed_fn(texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in row] for row in encoder.encode(list(texts))]

    return embedding_scorer(embed_fn)


def openai_embedding_scorer(
    client: Any, model: str = "text-embedding-3-small"
) -> Callable[[str, str], float]:
    """An embedding scorer over an *already-constructed* OpenAI-shaped ``client`` (no SDK import).

    Convenience for the common case of reusing the project's provider. Note this calls the
    embeddings endpoint at score time (network + cost, non-hermetic) — for hermetic test runs prefer
    :func:`local_embedding_scorer`. For other providers, build an ``embed_fn`` and use
    :func:`embedding_scorer` directly.
    """

    def embed_fn(texts: list[str]) -> list[list[float]]:
        resp = client.embeddings.create(model=model, input=list(texts))
        return [list(item.embedding) for item in resp.data]

    return embedding_scorer(embed_fn)


def _drift_text(obj: Any) -> str:
    """Canonical text of a recorded/live response for semantic comparison."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    return json.dumps(_to_jsonable(obj), sort_keys=True, ensure_ascii=False)


def semantic_drift(
    threshold: float = 0.8, scorer: Callable[[str, str], float] | None = None
) -> list[dict]:
    """Filter the last ``rerecord`` run's byte-level :func:`drift` to *meaningful* divergences.

    A model at non-zero temperature almost never reproduces output byte-for-byte, so raw
    :func:`drift` flags every run. This re-scores each divergence's recorded-vs-live text and keeps
    only those scoring **below** ``threshold`` (i.e. genuinely different in meaning), attaching the
    ``score``. Uses :func:`lexical_score` by default; pass an embedding/LLM-judge ``scorer`` for
    true semantics. For byte-stable drift instead, record/replay at ``temperature=0``.
    """
    score_fn = scorer or lexical_score
    out: list[dict] = []
    for d in _drift:
        score = score_fn(_drift_text(d.get("recorded")), _drift_text(d.get("live")))
        if score < threshold:
            out.append({**d, "score": score})
    return out


#: The session-store names people reach for out of habit — cassette has none (a cassette is a plain
#: JSON file). Redirect them to the SDK rather than raise a bare "no attribute".
_SESSION_STORE_ALIASES = ("SqliteSessionStore", "SQLiteSessionStore", "SessionStore")


def __getattr__(name: str) -> object:
    """PEP 562 module hook: redirect the common session-store mistake to its real home.

    cassette has **no** session store — a cassette is a plain JSON file you record once and replay
    (see :func:`use` / :func:`using`), not a durable store. A durable, resumable session store lives
    in the SDK. Any other unknown attribute raises the normal :class:`AttributeError`.
    """
    if name in _SESSION_STORE_ALIASES:
        raise AttributeError(
            f"cendor.cassette has no {name!r}: cassettes are plain JSON files "
            "(record/replay via cassette.use / cassette.using), not a session store. "
            "For a durable, resumable session store, use cendor.sdk.SQLiteSessionStore."
        )
    raise AttributeError(f"module 'cendor.cassette' has no attribute {name!r}")
