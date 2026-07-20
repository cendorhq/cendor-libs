"""Single interception point: wrap a provider client (or tool) once; emit normalized events.

docs/core.md §6. Idempotent (re-wrapping is a no-op) and additive (coexists with other
instrumentation like OpenLLMetry). Supports sync and async, and **streaming** responses
(``stream=True``): the streamed value is passed through unchanged while usage is accumulated, so
the ``LLMCall`` is emitted once with usage/cost/latency when the stream completes — not the
unconsumed iterator. That value is *both* an iterator and a context manager (matching the SDK), so
``for chunk in stream`` and ``with client…create(stream=True) as stream:`` both work. Uses duck
typing — the provider SDKs are never imported here, so they stay optional.

Two cooperation hooks (used by ``cassette``; harmless otherwise):
  * **record** — the raw provider response is attached at ``call.metadata["response"]`` before
    the event is emitted, so a subscriber can persist it.
  * **replay** — registered *interceptors* run *before* the real call; one may return a response
    to short-circuit it (returning :data:`MISS` to decline). This is how record/replay avoids a
    second instrumentation point: tools cooperate through ``core``, they never patch the client.
"""

from __future__ import annotations

import functools
import inspect
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, TypeVar

from . import bus, prices, tokens
from .types import LLMCall, Money, ToolCall, Usage

T = TypeVar("T")

_WRAPPED = "_cendor_wrapped"

#: Sentinel an interceptor returns to decline a call (let it proceed normally). A recorded
#: response may legitimately be ``None``, so "no replay" needs its own distinct value.
MISS: Any = object()


class Reroute:
    """Returned by an interceptor to modify the outgoing request, then run the real call.

    Used by ``tokenguard`` for ``on_exceed="downgrade"`` (e.g. ``Reroute(model="gpt-4o-mini")``)
    and by ``acttrace``'s ``guard()`` for redact-before-send (``Reroute(messages=[…])``). Any
    keyword updates are applied to the call's kwargs before it executes. Two are special-cased so
    the emitted ``LLMCall`` stays consistent with what is actually sent and so message rewrites work
    across providers:

    * ``model=`` also updates ``call.model``.
    * ``messages=`` rewrites the outbound messages — mapped to the provider's own kwarg
      (``messages`` / ``input`` / ``contents``) — and also updates ``call.messages``.
    """

    def __init__(self, **updates: Any) -> None:
        self.updates = updates


_interceptors: list[Callable[[Any], Any]] = []
_interceptors_lock = threading.Lock()
_install_lock = threading.Lock()  # serialize wrapping: concurrent instrument() must not double-wrap

#: Ambient trace id stamped onto every ``LLMCall``/``ToolCall`` emitted from the current context.
#: Default ``""`` ⇒ no correlation (unchanged behaviour). Set it with :func:`trace` to group a
#: unit of work — e.g. a direct-SDK agent run — the same way the LangChain callback path derives a
#: ``trace_id`` from ``parent_run_id``. Correlation is a *hook*, not an orchestrator: cendor stamps
#: the id you set; it never invents a run graph, schedules turns, or drives an agent loop.
_trace_id: ContextVar[str] = ContextVar("cendor_trace_id", default="")


def current_trace_id() -> str:
    """The ambient ``trace_id`` for the current context (``""`` when unset)."""
    return _trace_id.get()


@contextmanager
def trace(trace_id: str) -> Iterator[None]:
    """Stamp ``trace_id`` onto every ``LLMCall``/``ToolCall`` emitted inside the block.

    Gives direct-SDK (non-framework) agents the same run correlation the LangChain callback path
    gets for free. Nests and works across sync/async calls (it's a ``contextvars`` binding).

    ```python
    with trace("run-42"):
        client.chat.completions.create(...)   # emitted LLMCall.trace_id == "run-42"
    ```
    """
    token = _trace_id.set(str(trace_id))
    try:
        yield
    finally:
        _trace_id.reset(token)


def add_interceptor(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Register a pre-call interceptor. It receives the event (``LLMCall``/``ToolCall``) and
    returns a response to short-circuit the real call, or :data:`MISS` to proceed. Idempotent.

    Top-level on ``cendor.core`` — **not** on ``bus`` (``bus`` only has ``subscribe`` /
    ``unsubscribe`` / ``emit``). Return :class:`Reroute` to rewrite the outgoing request instead.

    ```python
    from cendor.core import add_interceptor, MISS
    add_interceptor(lambda call: MISS)   # inspect every call; MISS lets it proceed unchanged
    ```

    Thread-safe: registration is guarded by a lock and :func:`_intercept` runs over a snapshot.
    """
    with _interceptors_lock:
        if fn not in _interceptors:
            _interceptors.append(fn)
    return fn


def remove_interceptor(fn: Callable[[Any], Any]) -> None:
    """Unregister a previously added interceptor (no error if absent)."""
    with _interceptors_lock:
        if fn in _interceptors:
            _interceptors.remove(fn)


def _intercept(event: Any) -> Any:
    with _interceptors_lock:
        interceptors = list(_interceptors)
    for fn in interceptors:
        result = fn(event)
        if result is not MISS:
            return result
    return MISS


# --------------------------------------------------------------------------- model clients


def instrument(client: T) -> T:
    """Wrap a provider client so each call emits an ``LLMCall`` on the bus.

    Detection is structural (SDKs are never imported, so they stay optional):

    * **OpenAI** — ``chat.completions.create`` (Chat Completions), ``responses.create`` (the
      Responses API, primary for new OpenAI apps + the Agents SDK), **and** ``embeddings.create``
      (embedding calls emit an ``LLMCall`` with ``metadata["embedding"] = True``; covers Azure
      OpenAI too, which shares the client shape); all are wrapped when present.
    * **Anthropic** — ``messages.create``.
    * **AWS Bedrock** — ``converse``.
    * **Google Gemini** — the legacy ``google-generativeai`` ``GenerativeModel.generate_content``
      (model read from the object's ``model_name``) **and** the current ``google-genai`` SDK
      ``client.models.generate_content`` / ``client.aio.models.generate_content`` (model from the
      ``model=`` kwarg).
    * **Hugging Face** — ``huggingface_hub`` ``InferenceClient.chat_completion`` (an
      OpenAI-shaped response; the client also exposes an OpenAI-compatible
      ``chat.completions.create``, but binding to ``chat_completion`` attributes the call to
      ``huggingface`` rather than ``openai``).
    * **Ollama** — ``chat`` (a callable on the client itself).

    Unknown clients are returned untouched. Wrapping is idempotent (re-wrapping is a no-op) and
    returns the same client object.

    Wrap the client **once**, at construction — not per request:

    ```python
    from cendor.core import instrument
    client = instrument(OpenAI())   # every call now emits an LLMCall on the bus; sync/async/stream
    ```
    """
    targets = _find_targets(client)
    # The check-then-setattr below is a race; serialize so two threads can't both wrap the same fn.
    with _install_lock:
        for owner, attr, provider in targets:
            fn = getattr(owner, attr)
            if getattr(fn, _WRAPPED, False):
                continue  # already instrumented — no double-wrap
            model_default = ""
            if provider == "google":
                # The legacy GenerativeModel binds the model to the object (not the call kwargs);
                # read it so the LLMCall carries a real, priceable model id (strip "models/"). The
                # google-genai Client has no model_name — its model rides the model= kwarg instead.
                name = getattr(client, "model_name", None) or getattr(client, "_model_name", "")
                model_default = str(name).removeprefix("models/")
            setattr(owner, attr, _wrap(fn, provider, model_default))
    return client


def _find_targets(client: Any) -> list[tuple[Any, str, str]]:
    """Every instrumentable (owner, attr, internal-provider) entrypoint on ``client``.

    Returns a list because one client can expose several (e.g. an OpenAI client has both
    ``chat.completions.create`` and ``responses.create``). ``"openai_responses"`` is an internal
    tag distinguishing the Responses API shape (different usage/stream handling); the emitted
    ``LLMCall.provider`` is still ``"openai"`` (see :func:`_public_provider`).
    """
    targets: list[tuple[Any, str, str]] = []
    # Hugging Face InferenceClient exposes chat_completion(...) as a method on the client itself
    # (it also has an OpenAI-compatible chat.completions.create). Bind to chat_completion first —
    # before the OpenAI check below matches that compat namespace — so the LLMCall is attributed to
    # "huggingface". The response is OpenAI-shaped, so usage/parse reuse the OpenAI path.
    if callable(getattr(client, "chat_completion", None)):
        return [(client, "chat_completion", "huggingface")]
    chat = getattr(client, "chat", None)
    completions = getattr(chat, "completions", None) if chat is not None else None
    if completions is not None and callable(getattr(completions, "create", None)):
        targets.append((completions, "create", "openai"))
    responses = getattr(client, "responses", None)  # OpenAI Responses API
    if responses is not None and callable(getattr(responses, "create", None)):
        targets.append((responses, "create", "openai_responses"))
    # OpenAI-shaped embeddings endpoint (OpenAI + Azure-via-openai). Wrapping it closes the
    # embeddings capture gap: pre-flight interceptors (budget block/clamp, guard redaction) run,
    # and the emitted LLMCall carries metadata["embedding"] = True.
    embeddings = getattr(client, "embeddings", None)
    if embeddings is not None and callable(getattr(embeddings, "create", None)):
        targets.append((embeddings, "create", "openai_embeddings"))
    if targets:
        return targets  # an OpenAI-shaped client; don't also match the fallbacks below
    messages = getattr(client, "messages", None)
    if messages is not None and callable(getattr(messages, "create", None)):
        return [(messages, "create", "anthropic")]
    if callable(getattr(client, "converse", None)):  # AWS Bedrock Converse API
        return [(client, "converse", "bedrock")]
    if callable(getattr(client, "generate_content", None)):  # legacy google-generativeai
        return [(client, "generate_content", "google")]
    # google-genai SDK: sync client.models + async client.aio.models generate_content
    google: list[tuple[Any, str, str]] = []
    models = getattr(client, "models", None)
    if models is not None and callable(getattr(models, "generate_content", None)):
        google.append((models, "generate_content", "google"))
    aio_models = getattr(getattr(client, "aio", None), "models", None)
    if aio_models is not None and callable(getattr(aio_models, "generate_content", None)):
        google.append((aio_models, "generate_content", "google"))
    if google:
        return google
    if callable(chat):  # Ollama: client.chat(...) is itself callable (vs OpenAI's chat namespace)
        return [(client, "chat", "ollama")]
    return []


#: Internal provider tags that map to a public ``LLMCall.provider`` name.
_PUBLIC_PROVIDER = {"openai_responses": "openai", "openai_embeddings": "openai"}


def _public_provider(provider: str) -> str:
    """Map an internal detection tag to the public provider recorded on the ``LLMCall``."""
    return _PUBLIC_PROVIDER.get(provider, provider)


def _wrap(fn: Any, provider: str, model_default: str = "") -> Any:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            call, start = _pre(provider, args, kwargs, model_default)
            _ensure_stream_usage_options(provider, kwargs)
            directive = _intercept(call)
            if isinstance(directive, Reroute):
                _apply_reroute(call, kwargs, directive, provider)
                response = await fn(*args, **kwargs)
            elif directive is not MISS:
                call.metadata["replayed"] = True
                if kwargs.get("stream"):
                    return _areplay_stream(call, directive, provider, start)
                _post(call, directive, provider, start)
                return directive
            else:
                response = await fn(*args, **kwargs)
            if kwargs.get("stream"):
                return _aproxy_stream(call, response, provider, start)
            _post(call, response, provider, start)
            return response

        setattr(awrapper, _WRAPPED, True)
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call, start = _pre(provider, args, kwargs, model_default)
        _ensure_stream_usage_options(provider, kwargs)
        directive = _intercept(call)
        if isinstance(directive, Reroute):
            _apply_reroute(call, kwargs, directive, provider)
            response = fn(*args, **kwargs)
        elif directive is not MISS:
            call.metadata["replayed"] = True
            if kwargs.get("stream"):
                return _replay_stream(call, directive, provider, start)
            _post(call, directive, provider, start)
            return directive
        else:
            response = fn(*args, **kwargs)
        if kwargs.get("stream"):
            return _proxy_stream(call, response, provider, start)
        _post(call, response, provider, start)
        return response

    setattr(wrapper, _WRAPPED, True)
    return wrapper


#: Per-provider kwarg carrying the request messages, so ``Reroute(messages=…)`` rewrites the right
#: field (Chat Completions/Anthropic/Bedrock/Ollama use ``messages``; the OpenAI Responses API uses
#: ``input``; Gemini uses ``contents``).
_MESSAGES_KWARG = {"openai_responses": "input", "google": "contents"}

_MISSING: Any = object()  # so ``Reroute(messages=[])`` (a valid empty rewrite) is still detected


def _apply_reroute(call: LLMCall, kwargs: dict, directive: Reroute, provider: str = "") -> None:
    updates = dict(directive.updates)
    messages = updates.pop("messages", _MISSING)
    kwargs.update(updates)  # generic updates (model, max_tokens, …)
    if "model" in updates:
        call.model = updates["model"]
    if messages is not _MISSING:
        # Rewrite the provider's own messages kwarg and keep the emitted event consistent with what
        # is actually sent. (If a Gemini caller passed contents positionally, set the kwarg form.)
        if provider == "openai_embeddings":
            # The embeddings endpoint takes raw text(s) on `input`, not message dicts — map the
            # rerouted messages back to the original input shape (str stays str, list stays list)
            # so e.g. a guard's redact-before-send sends the provider cleaned text.
            contents = [str(_get(m, "content", "") or "") for m in messages or []]
            original = kwargs.get("input")
            kwargs["input"] = contents[0] if isinstance(original, str) and contents else contents
        else:
            kwargs[_MESSAGES_KWARG.get(provider, "messages")] = messages
        call.messages = messages
    call.metadata["rerouted"] = True


def _pre(
    provider: str, args: tuple, kwargs: dict, model_default: str = ""
) -> tuple[LLMCall, float]:
    model, messages = _extract_request(provider, args, kwargs, model_default)
    call = LLMCall(
        id=uuid.uuid4().hex,
        provider=_public_provider(provider),  # internal "openai_responses" surfaces as "openai"
        model=model,
        messages=messages,
        trace_id=_trace_id.get(),  # ambient correlation hook (default "" — unchanged)
        ts=datetime.now(UTC),
    )
    call.metadata["request_kwargs"] = kwargs  # so pre-flight interceptors can read e.g. max_tokens
    if provider == "openai_embeddings":
        call.metadata["embedding"] = True  # so subscribers can tell embedding calls apart
    return call, time.perf_counter()


def _extract_request(
    provider: str, args: tuple, kwargs: dict, model_default: str = ""
) -> tuple[str, list[dict]]:
    """Normalize (model, messages) out of a provider's call signature."""
    if provider == "openai_embeddings":
        # Embeddings API: embeddings.create(model=…, input=…). `input` is a string or a list of
        # strings (token arrays pass through as-is inside content). Normalize each text to a
        # message dict so interceptors (guard redaction, budget projection) see the payload the
        # same way they see chat messages.
        inp = kwargs.get("input")
        if isinstance(inp, str):
            texts: list = [inp]
        elif isinstance(inp, list):
            texts = inp
        else:
            texts = []
        return kwargs.get("model", ""), [{"role": "user", "content": t} for t in texts]
    if provider == "openai_responses":
        # Responses API: responses.create(model=…, input=…). `input` is a string or a message list.
        inp = kwargs.get("input")
        if inp is None:
            inp = kwargs.get("messages")
        if isinstance(inp, str):
            messages = [{"role": "user", "content": inp}]
        elif isinstance(inp, list):
            messages = inp
        else:
            messages = []
        return kwargs.get("model", ""), messages
    if provider == "bedrock":
        return kwargs.get("modelId", ""), list(kwargs.get("messages") or [])
    if provider == "google":
        contents = kwargs.get("contents")
        if contents is None and args:
            contents = args[0]
        if isinstance(contents, list):
            messages = contents
        elif contents:
            messages = [{"role": "user", "content": str(contents)}]
        else:
            messages = []
        # model id is bound to the GenerativeModel object (model_default), not the call kwargs
        return kwargs.get("model") or model_default, messages
    # openai / anthropic / ollama all take model= + messages=
    return kwargs.get("model", ""), list(kwargs.get("messages") or [])


def _post(call: LLMCall, response: Any, provider: str, start: float) -> None:
    call.latency_ms = (time.perf_counter() - start) * 1000.0
    usage = _extract_usage(response, provider)
    call.usage = usage
    _set_cost(call, usage, _extract_reported_cost(response))
    call.metadata["response"] = response  # for recorders (cassette); a reference, not a copy
    bus.emit(call)


def _set_cost(call: LLMCall, usage: Usage | None, reported: Money | None) -> None:
    """Set ``call.cost`` and label its provenance: a provider-/gateway-reported figure is preferred
    over an offline estimate from the price table.

    Tags ``metadata["cost_reported"]`` when the provider billed us a real amount (e.g. OpenRouter's
    ``usage.cost``), or ``metadata["cost_estimated"]`` when we priced it from the snapshot — so
    downstream tools and audits can tell a billed cost apart from an estimate (mirrors the existing
    ``usage_estimated`` flag). Unknown model + no reported cost leaves ``cost = None``.
    """
    if reported is not None:
        call.cost = reported
        call.metadata["cost_reported"] = True
        return
    if usage is not None:
        try:
            call.cost = prices.estimate(
                call.model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                usage.cache_write,
            )
            call.metadata["cost_estimated"] = True
        except KeyError:
            call.cost = None


def _extract_reported_cost(response: Any) -> Money | None:
    """Read a provider-/gateway-reported cost off a response (or stream chunk), if present.

    Gateways like OpenRouter attach the real billed cost at ``usage.cost``; standard OpenAI/
    Anthropic SDK responses carry no cost, so this returns ``None`` and the caller falls back to the
    offline estimate. Best-effort and exception-safe — never breaks the call.
    """
    u = _get(response, "usage")
    candidates = []
    if u is not None:
        candidates += [_get(u, "cost"), _get(u, "total_cost")]
    candidates += [_get(response, "cost"), _get(response, "total_cost")]
    for c in candidates:
        if c is None:
            continue
        try:
            amount = Decimal(str(c))
        except (ArithmeticError, ValueError, TypeError):
            continue
        if amount >= 0:
            return Money(amount)
    return None


def _ensure_stream_usage_options(provider: str, kwargs: dict) -> None:
    """For an OpenAI stream, ask the provider to emit a final usage chunk so streamed usage is the
    real billed count, not an offline estimate.

    Injects ``stream_options={"include_usage": True}`` only when ``stream=True`` and the caller
    hasn't set ``stream_options`` themselves (their value is always left intact). No-op for other
    providers. docs/core.md §6.
    """
    if provider == "openai" and kwargs.get("stream") and "stream_options" not in kwargs:
        kwargs["stream_options"] = {"include_usage": True}


# --------------------------------------------------------------------------- streaming


class _ProxyStream:
    """A sync streaming response wrapper that is *both* an iterator and a context manager.

    The provider SDK's streamed return value supports both ``for chunk in stream`` **and**
    ``with client…create(stream=True) as stream:`` (the latter is how ``langchain_openai`` consumes
    it). A bare generator only supports the former, so ``with`` raised ``TypeError: 'generator'
    object does not support the context manager protocol`` — this wrapper restores the full surface:

    * ``__iter__``/``__next__`` pass each provider chunk through unchanged, collecting it, and call
      :func:`_finalize_stream` **once** on exhaustion (so usage/cost/latency emit exactly once).
    * ``__enter__``/``__exit__`` run the underlying stream's own context manager when it has one and
      guarantee a single finalize on block exit (idempotent with iteration-driven finalize).
    * ``close()`` finalizes once and closes the underlying stream (releasing the HTTP connection).
    * ``__getattr__`` forwards any other attribute (``.response``, …) to the underlying stream, so
      the SDK's surface is preserved for callers that reach past iteration.

    ``replay_chunks`` (used by :func:`_replay_stream`) makes finalize account for the full recorded
    sequence regardless of how much the caller consumed, matching the previous replay behaviour.
    """

    def __init__(
        self,
        call: LLMCall,
        stream: Any,
        provider: str,
        start: float,
        replay_chunks: list | None = None,
    ) -> None:
        self._stream = stream
        self._call = call
        self._provider = provider
        self._start = start
        self._chunks: list[Any] = []
        self._replay_chunks = replay_chunks
        self._iter: Any = None
        self._finalized = False

    def __iter__(self) -> _ProxyStream:
        self._iter = iter(self._stream)
        return self

    def __next__(self) -> Any:
        if self._iter is None:
            self._iter = iter(self._stream)
        try:
            chunk = next(self._iter)
        except StopIteration:
            self._finalize()
            raise
        if not self._chunks and self._replay_chunks is None:  # first live chunk → TTFT (G23)
            self._call.metadata["ttft_ms"] = (time.perf_counter() - self._start) * 1000.0
        self._chunks.append(chunk)
        return chunk

    def __enter__(self) -> _ProxyStream:
        enter = getattr(self._stream, "__enter__", None)
        if enter is not None:
            enter()  # run the SDK stream's own setup, but keep *this* wrapper as the bound value
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        try:
            exit_ = getattr(self._stream, "__exit__", None)
            if exit_ is not None:
                exit_(*exc)
            else:
                self._close_underlying()
        finally:
            self._finalize()
        return False

    def close(self) -> None:
        """Close the underlying stream and finalize once (mirrors the SDK stream's ``close``)."""
        try:
            self._close_underlying()
        finally:
            self._finalize()

    def _close_underlying(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        chunks = self._replay_chunks if self._replay_chunks is not None else self._chunks
        _finalize_stream(self._call, chunks, self._provider, self._start)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ runs only on a normal-lookup miss; forward the rest of the SDK surface. The
        # _stream guard avoids infinite recursion if _stream isn't set (e.g. during unpickling).
        if name == "_stream":
            raise AttributeError(name)
        return getattr(self._stream, name)


class _AProxyStream:
    """Async counterpart of :class:`_ProxyStream`: an ``async for`` iterator **and** an ``async
    with`` context manager, finalizing the ``LLMCall`` exactly once."""

    def __init__(
        self,
        call: LLMCall,
        stream: Any,
        provider: str,
        start: float,
        replay_chunks: list | None = None,
    ) -> None:
        self._stream = stream
        self._call = call
        self._provider = provider
        self._start = start
        self._chunks: list[Any] = []
        self._replay_chunks = replay_chunks
        self._iter: Any = None
        self._finalized = False

    def __aiter__(self) -> _AProxyStream:
        self._iter = self._stream.__aiter__()
        return self

    async def __anext__(self) -> Any:
        if self._iter is None:
            self._iter = self._stream.__aiter__()
        try:
            chunk = await self._iter.__anext__()
        except StopAsyncIteration:
            self._finalize()
            raise
        if not self._chunks and self._replay_chunks is None:  # first live chunk → TTFT (G23)
            self._call.metadata["ttft_ms"] = (time.perf_counter() - self._start) * 1000.0
        self._chunks.append(chunk)
        return chunk

    async def __aenter__(self) -> _AProxyStream:
        aenter = getattr(self._stream, "__aenter__", None)
        if aenter is not None:
            await aenter()  # SDK stream's own async setup; keep this wrapper as the bound value
        return self

    async def __aexit__(self, *exc: object) -> Literal[False]:
        try:
            aexit = getattr(self._stream, "__aexit__", None)
            if aexit is not None:
                await aexit(*exc)
            else:
                await self._aclose_underlying()
        finally:
            self._finalize()
        return False

    async def aclose(self) -> None:
        """Close the underlying async stream and finalize once."""
        try:
            await self._aclose_underlying()
        finally:
            self._finalize()

    async def _aclose_underlying(self) -> None:
        close = getattr(self._stream, "close", None) or getattr(self._stream, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        chunks = self._replay_chunks if self._replay_chunks is not None else self._chunks
        _finalize_stream(self._call, chunks, self._provider, self._start)

    def __getattr__(self, name: str) -> Any:
        if name == "_stream":
            raise AttributeError(name)
        return getattr(self._stream, name)


async def _aiter_list(items: list) -> Any:
    """Yield a materialized list as an async iterator (for replaying a recorded async stream)."""
    for item in items:
        yield item


def _proxy_stream(call: LLMCall, stream: Any, provider: str, start: float) -> Any:
    """Wrap a sync streaming response: chunks pass through unchanged and usage is accumulated, so
    the ``LLMCall`` is emitted once with usage/cost/latency on completion (or early close). The
    result is both an iterator and a context manager — see :class:`_ProxyStream`."""
    return _ProxyStream(call, stream, provider, start)


def _aproxy_stream(call: LLMCall, stream: Any, provider: str, start: float) -> Any:
    """Async counterpart of :func:`_proxy_stream` for ``async for`` / ``async with`` responses."""
    return _AProxyStream(call, stream, provider, start)


def _replay_stream(call: LLMCall, recorded: Any, provider: str, start: float) -> Any:
    """Re-yield a recorded stream (a chunk sequence) so a replayed streamed call still iterates —
    and, like a live stream, supports ``with``. Finalize accounts for the full recording."""
    chunks = list(recorded) if recorded is not None else []
    return _ProxyStream(call, iter(chunks), provider, start, replay_chunks=chunks)


def _areplay_stream(call: LLMCall, recorded: Any, provider: str, start: float) -> Any:
    """Async counterpart of :func:`_replay_stream` (``async for`` / ``async with`` over recorded
    chunks)."""
    chunks = list(recorded) if recorded is not None else []
    return _AProxyStream(call, _aiter_list(chunks), provider, start, replay_chunks=chunks)


def _finalize_stream(call: LLMCall, chunks: list, provider: str, start: float) -> None:
    """Close out a streamed call: recover (or estimate) usage, price it, emit on the bus once."""
    call.latency_ms = (time.perf_counter() - start) * 1000.0
    usage = _stream_usage(chunks, provider)
    if usage is None:
        usage = _estimate_stream_usage(call, chunks, provider)
    call.usage = usage
    reported = None
    for ch in chunks:  # a gateway may report cost on the final usage chunk
        reported = _extract_reported_cost(ch)
        if reported is not None:
            break
    _set_cost(call, usage, reported)
    call.metadata["streamed"] = True
    call.metadata["response"] = chunks  # the collected chunks, so a recorder (cassette) can persist
    bus.emit(call)


def _stream_usage(chunks: list, provider: str) -> Usage | None:
    """Recover real usage from streamed chunks where the provider reports it.

    OpenAI/Ollama/Gemini carry usage on a single (final) chunk shaped like a full response, so
    :func:`_extract_usage` reads it directly; Anthropic splits it across ``message_start`` (input)
    and ``message_delta`` (output) events; Bedrock puts it on a ``metadata`` event. Returns ``None``
    when no chunk reports usage (e.g. OpenAI without ``stream_options={"include_usage": True}``).
    """
    if provider == "anthropic":
        inp = out = None
        cached = 0
        cache_write = 0
        for ch in chunks:
            etype = _get(ch, "type")
            if etype == "message_start":
                u = _get(_get(ch, "message"), "usage")
                inp = _get(u, "input_tokens", inp)
                cached = _get(u, "cache_read_input_tokens", 0) or 0
                cache_write = _get(u, "cache_creation_input_tokens", 0) or 0
            elif etype == "message_delta":
                u = _get(ch, "usage")
                if u is not None:
                    out = _get(u, "output_tokens", out)
        if inp is None:
            return None
        # Anthropic's input_tokens excludes cache reads; fold them in so cached ⊆ input holds
        # uniformly (see _extract_usage). Cost then bills the cached portion once; cache_write is
        # a separate billed category, kept out of input_tokens.
        return Usage(
            int(inp) + int(cached or 0),
            int(out or 0),
            int(cached or 0),
            cache_write=int(cache_write or 0),
        )
    if provider == "bedrock":
        for ch in chunks:
            u = _get(_get(ch, "metadata"), "usage")
            if u is not None:
                return Usage(
                    int(_get(u, "inputTokens", 0) or 0), int(_get(u, "outputTokens", 0) or 0)
                )
        return None
    if provider == "openai_responses":
        # Responses streaming emits typed events; the terminal event (response.completed) carries
        # the full Response with usage. Read it off whichever event has a usage-bearing `response`.
        for ch in chunks:
            resp = _get(ch, "response")
            if resp is not None and _get(resp, "usage") is not None:
                return _extract_usage(resp, "openai_responses")
        return None
    for ch in chunks:  # openai / ollama / google: usage rides one chunk, full-response shaped
        u = _extract_usage(ch, provider)
        if u is not None:
            return u
    return None


def _estimate_stream_usage(call: LLMCall, chunks: list, provider: str) -> Usage | None:
    """Offline fallback when a stream reports no usage: count input messages + streamed output text.

    Marks ``call.metadata["usage_estimated"] = True`` so downstream tools (and audits) can tell the
    figure is an offline estimate, not the provider's billed count. Exact with the ``[tiktoken]``
    extra for OpenAI; a heuristic otherwise (see :mod:`cendor.core.tokens`).
    """
    text = "".join(_stream_text(ch, provider) for ch in chunks)
    if not text and not call.messages:
        return None
    inp = tokens.count(call.messages, call.model) if call.messages else 0
    out = tokens.count(text, call.model) if text else 0
    call.metadata["usage_estimated"] = True
    return Usage(int(inp), int(out))


def _stream_text(chunk: Any, provider: str) -> str:
    """Best-effort text of one streamed chunk, per provider (only for the offline estimate)."""
    try:
        if provider == "openai_responses":
            # Responses streaming: text arrives on response.output_text.delta events (.delta = str).
            if _get(chunk, "type") == "response.output_text.delta":
                return str(_get(chunk, "delta", "") or "")
            return ""
        if provider in ("openai", "huggingface"):  # both stream Chat Completions-shaped chunks
            choices = _get(chunk, "choices") or []
            return "".join(str(_get(_get(c, "delta"), "content", "") or "") for c in choices)
        if provider == "anthropic":
            if _get(chunk, "type") == "content_block_delta":
                return str(_get(_get(chunk, "delta"), "text", "") or "")
            return ""
        if provider == "ollama":
            return str(_get(_get(chunk, "message"), "content", "") or "")
        if provider == "google":
            return str(_get(chunk, "text", "") or "")
        if provider == "bedrock":
            return str(_get(_get(_get(chunk, "contentBlockDelta"), "delta"), "text", "") or "")
    except Exception:  # noqa: BLE001 - estimation must never break the passthrough
        return ""
    return ""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _extract_usage(response: Any, provider: str) -> Usage | None:
    cached = 0
    cache_write = 0  # tokens written to the prompt cache this call (Anthropic); a separate category
    reasoning = 0  # tokens the model spent reasoning/thinking; a subset of output (see Usage)
    if provider == "google":  # usage lives under usage_metadata
        meta = _get(response, "usage_metadata")
        inp = _get(meta, "prompt_token_count")
        # Gemini reports thinking-model reasoning under thoughts_token_count, *separate* from
        # candidates_token_count. Both are billed as output, so fold thoughts into the output total
        # (otherwise reasoning models are under-counted) and also surface it as reasoning_tokens.
        reasoning = _get(meta, "thoughts_token_count", 0) or 0
        out = (_get(meta, "candidates_token_count", 0) or 0) + reasoning
    elif provider == "ollama":  # token counts are top-level on the response
        inp = _get(response, "prompt_eval_count")
        out = _get(response, "eval_count", 0) or 0
    else:
        u = _get(response, "usage")
        if u is None:
            return None
        if provider in ("openai", "openai_responses", "openai_embeddings", "huggingface"):
            # Dual-shape: Chat Completions uses prompt_tokens/completion_tokens (+ details); the
            # Responses API uses input_tokens/output_tokens (+ input/output_tokens_details). Read
            # whichever the response carries so one branch covers both entrypoints. Hugging Face's
            # chat_completion returns the Chat Completions shape (prompt_tokens/completion_tokens).
            # Embeddings responses carry prompt_tokens/total_tokens only (no completion_tokens ->
            # output stays 0).
            inp = _get(u, "prompt_tokens")
            if inp is None:
                inp = _get(u, "input_tokens")
            out = _get(u, "completion_tokens")
            if out is None:
                out = _get(u, "output_tokens", 0)
            out = out or 0
            details = _get(u, "prompt_tokens_details") or _get(u, "input_tokens_details")
            cached = _get(details, "cached_tokens", 0) or 0 if details is not None else 0
            # o-series/GPT-5 reasoning tokens are a subset of the output tokens (already in `out`).
            cdetails = _get(u, "completion_tokens_details") or _get(u, "output_tokens_details")
            reasoning = _get(cdetails, "reasoning_tokens", 0) or 0 if cdetails is not None else 0
        elif provider == "bedrock":  # Converse usage uses camelCase token keys
            inp = _get(u, "inputTokens")
            out = _get(u, "outputTokens", 0) or 0
        else:  # anthropic — thinking tokens are folded into output_tokens with no separate count
            base_in = _get(u, "input_tokens")
            out = _get(u, "output_tokens", 0) or 0
            cached = _get(u, "cache_read_input_tokens", 0) or 0
            cache_write = _get(u, "cache_creation_input_tokens", 0) or 0
            # Anthropic reports input_tokens *excluding* cache reads (disjoint), unlike OpenAI where
            # prompt_tokens already includes cached. Normalize to the documented subset convention
            # (cached ⊆ input) so pricing/accounting is uniform across providers. cache_write is a
            # separate billed category, so it stays out of input_tokens.
            inp = None if base_in is None else int(base_in) + int(cached)
    if inp is None:
        return None
    return Usage(
        input_tokens=int(inp),
        output_tokens=int(out),
        cached_tokens=int(cached),
        reasoning_tokens=int(reasoning),
        cache_write=int(cache_write),
    )


# --------------------------------------------------------------------------- tools


def instrument_tool(name: str | Callable | None = None) -> Callable:
    """Wrap a tool/function so each invocation emits a ``ToolCall`` on the bus.

    Usable as ``@instrument_tool`` or ``@instrument_tool("search")``. Mirrors :func:`instrument`:
    idempotent, sync + async, replay-aware. The return value is stored on ``ToolCall.result`` so
    ``cassette`` can record/replay tool side effects.
    """
    if callable(name):  # bare @instrument_tool
        return _wrap_tool(name, str(getattr(name, "__name__", "tool")))

    def decorator(fn: Callable) -> Callable:
        return _wrap_tool(fn, name or str(getattr(fn, "__name__", "tool")))

    return decorator


def _wrap_tool(fn: Callable, tool_name: str) -> Callable:
    if getattr(fn, _WRAPPED, False):
        return fn

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            tc, start = _pre_tool(tool_name, args, kwargs)
            replayed = _intercept(tc)
            if replayed is not MISS:
                tc.metadata["replayed"] = True
                result = replayed
            else:
                result = await fn(*args, **kwargs)
            _post_tool(tc, result, start)
            return result

        setattr(awrapper, _WRAPPED, True)
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        tc, start = _pre_tool(tool_name, args, kwargs)
        replayed = _intercept(tc)
        if replayed is not MISS:
            tc.metadata["replayed"] = True
            result = replayed
        else:
            result = fn(*args, **kwargs)
        _post_tool(tc, result, start)
        return result

    setattr(wrapper, _WRAPPED, True)
    return wrapper


def _pre_tool(name: str, args: tuple, kwargs: dict) -> tuple[ToolCall, float]:
    tc = ToolCall(
        id=uuid.uuid4().hex,
        name=name,
        arguments={"args": list(args), "kwargs": dict(kwargs)},
        trace_id=_trace_id.get(),  # ambient correlation hook (default "" — unchanged)
        ts=datetime.now(UTC),
    )
    return tc, time.perf_counter()


def _post_tool(tc: ToolCall, result: Any, start: float) -> None:
    tc.latency_ms = (time.perf_counter() - start) * 1000.0
    tc.result = result
    bus.emit(tc)
