"""Single interception point: wrap a provider client (or tool) once; emit normalized events.

docs/core.md §6. Idempotent (re-wrapping is a no-op) and additive (coexists with other
instrumentation like OpenLLMetry). Supports sync and async, and **streaming** responses
(``stream=True``): the chunk iterator is passed through unchanged while usage is accumulated, so
the ``LLMCall`` is emitted once with usage/cost/latency when the stream completes — not the
unconsumed iterator. Uses duck typing — the provider SDKs are never imported here, so they stay
optional.

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
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from . import bus, prices, tokens
from .types import LLMCall, Money, ToolCall, Usage

T = TypeVar("T")

_WRAPPED = "_cendor_wrapped"

#: Sentinel an interceptor returns to decline a call (let it proceed normally). A recorded
#: response may legitimately be ``None``, so "no replay" needs its own distinct value.
MISS: Any = object()


class Reroute:
    """Returned by an interceptor to modify the outgoing request, then run the real call.

    Used by ``tokenguard`` for ``on_exceed="downgrade"`` (e.g. ``Reroute(model="gpt-4o-mini")``).
    Any keyword updates are applied to the call's kwargs before it executes.
    """

    def __init__(self, **updates: Any) -> None:
        self.updates = updates


_interceptors: list[Callable[[Any], Any]] = []
_interceptors_lock = threading.Lock()
_install_lock = threading.Lock()  # serialize wrapping: concurrent instrument() must not double-wrap


def add_interceptor(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Register a pre-call interceptor. It receives the event (``LLMCall``/``ToolCall``) and
    returns a response to short-circuit the real call, or :data:`MISS` to proceed. Idempotent.

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

    * **OpenAI** — ``chat.completions.create`` (Chat Completions) **and** ``responses.create`` (the
      Responses API, primary for new OpenAI apps + the Agents SDK); both are wrapped when present.
    * **Anthropic** — ``messages.create``.
    * **AWS Bedrock** — ``converse``.
    * **Google Gemini** — the legacy ``google-generativeai`` ``GenerativeModel.generate_content``
      (model read from the object's ``model_name``) **and** the current ``google-genai`` SDK
      ``client.models.generate_content`` / ``client.aio.models.generate_content`` (model from the
      ``model=`` kwarg).
    * **Ollama** — ``chat`` (a callable on the client itself).

    Unknown clients are returned untouched. Wrapping is idempotent (re-wrapping is a no-op) and
    returns the same client object.
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
    chat = getattr(client, "chat", None)
    completions = getattr(chat, "completions", None) if chat is not None else None
    if completions is not None and callable(getattr(completions, "create", None)):
        targets.append((completions, "create", "openai"))
    responses = getattr(client, "responses", None)  # OpenAI Responses API
    if responses is not None and callable(getattr(responses, "create", None)):
        targets.append((responses, "create", "openai_responses"))
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
_PUBLIC_PROVIDER = {"openai_responses": "openai"}


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
                _apply_reroute(call, kwargs, directive)
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
            _apply_reroute(call, kwargs, directive)
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


def _apply_reroute(call: LLMCall, kwargs: dict, directive: Reroute) -> None:
    kwargs.update(directive.updates)
    if "model" in directive.updates:
        call.model = directive.updates["model"]
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
        ts=datetime.now(UTC),
    )
    call.metadata["request_kwargs"] = kwargs  # so pre-flight interceptors can read e.g. max_tokens
    return call, time.perf_counter()


def _extract_request(
    provider: str, args: tuple, kwargs: dict, model_default: str = ""
) -> tuple[str, list[dict]]:
    """Normalize (model, messages) out of a provider's call signature."""
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


def _proxy_stream(call: LLMCall, stream: Any, provider: str, start: float) -> Any:
    """Pass a sync streaming response through unchanged, collecting chunks; emit once on completion.

    The caller iterates exactly the provider's chunks. When the stream is exhausted (or closed
    early), the ``LLMCall`` — now with usage, cost, and true end-to-end latency — is emitted once.
    Without this, a streamed call returns an unconsumed iterator and emits no usable usage.
    """

    def gen() -> Any:
        chunks: list[Any] = []
        try:
            for chunk in stream:
                chunks.append(chunk)
                yield chunk
        finally:
            _finalize_stream(call, chunks, provider, start)

    return gen()


def _aproxy_stream(call: LLMCall, stream: Any, provider: str, start: float) -> Any:
    """Async counterpart of :func:`_proxy_stream` for ``async for`` streaming responses.

    A plain function that *returns* an async generator (not ``async def``), so the wrapper can hand
    the async iterator straight back to the caller's ``async for`` — not an un-awaited coroutine.
    """

    async def agen() -> Any:
        chunks: list[Any] = []
        try:
            async for chunk in stream:
                chunks.append(chunk)
                yield chunk
        finally:
            _finalize_stream(call, chunks, provider, start)

    return agen()


def _replay_stream(call: LLMCall, recorded: Any, provider: str, start: float) -> Any:
    """Re-yield a recorded stream (a chunk sequence) so a replayed streamed call still iterates."""
    chunks = list(recorded) if recorded is not None else []

    def gen() -> Any:
        try:
            yield from chunks
        finally:
            _finalize_stream(call, chunks, provider, start)

    return gen()


def _areplay_stream(call: LLMCall, recorded: Any, provider: str, start: float) -> Any:
    """Async counterpart of :func:`_replay_stream` (yields the recorded chunks for ``async for``).

    Like :func:`_aproxy_stream`, a plain function returning an async generator.
    """
    chunks = list(recorded) if recorded is not None else []

    async def agen() -> Any:
        try:
            for chunk in chunks:
                yield chunk
        finally:
            _finalize_stream(call, chunks, provider, start)

    return agen()


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
        if provider == "openai":
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
        if provider in ("openai", "openai_responses"):
            # Dual-shape: Chat Completions uses prompt_tokens/completion_tokens (+ details); the
            # Responses API uses input_tokens/output_tokens (+ input/output_tokens_details). Read
            # whichever the response carries so one branch covers both entrypoints.
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
        ts=datetime.now(UTC),
    )
    return tc, time.perf_counter()


def _post_tool(tc: ToolCall, result: Any, start: float) -> None:
    tc.latency_ms = (time.perf_counter() - start) * 1000.0
    tc.result = result
    bus.emit(tc)
