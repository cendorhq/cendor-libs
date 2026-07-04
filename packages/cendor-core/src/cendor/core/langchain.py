"""Optional LangChain / LangGraph integration — the SDK-aligned way to observe a framework.

The SDK-aligned integration point for a framework is its **callback system**, not client
monkeypatching: `langchain_openai` calls `client.with_raw_response.create().parse()` (so
`instrument()` sees a usage-less `LegacyAPIResponse`) and consumes streams via a context manager —
observing through callbacks sidesteps both. :class:`CendorCallbackHandler` reads LangChain's own
`usage_metadata` (which carries **reasoning** and **cached** token breakdowns), prices the call
offline, correlates multi-node / multi-agent runs via `trace_id`, and emits normalized
:class:`~cendor.core.types.LLMCall` / :class:`~cendor.core.types.ToolCall` events on the bus — so
`tokenguard`, `acttrace`, and any other subscriber see LangChain activity with no client touch.

**Recording-only.** This path is post-call: it *observes*, it never enforces. `tokenguard`'s caps
and `acttrace`'s `guard()` act on the `instrument()` seam, which the callback path does not touch —
so pre-flight enforcement (`on_exceed="block"`, redact-before-send) is a **no-op** here. Use the
direct provider SDK with `instrument()` when you need enforcement.

Requires the optional extra, keeping `cendor-core` dependency-light (like `[tiktoken]`/`[otel]`):

    pip install "cendor-core[langchain]"

Importing this module without `langchain-core` raises a clear :class:`ImportError`.

Usage::

    from cendor.core.langchain import CendorCallbackHandler
    handler = CendorCallbackHandler()

    llm = ChatOpenAI(model="gpt-4o", callbacks=[handler])   # every call recorded
    llm.invoke("hi")

    # or per-call / per-agent — propagates to all LangGraph nodes, correlated by root run:
    agent.invoke({"messages": [...]}, config={"callbacks": [handler]})
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from . import bus, prices
from .types import LLMCall, ToolCall, Usage

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "cendor.core.langchain requires langchain-core. "
        'Install it with:  pip install "cendor-core[langchain]"'
    ) from exc

__all__ = ["CendorCallbackHandler"]


class CendorCallbackHandler(BaseCallbackHandler):
    """A LangChain/LangGraph callback handler that records usage, reasoning, tool calls, and run
    correlation onto cendor's bus. **Recording-only** — never enforces (see the module docstring).

    Attach it globally (``ChatOpenAI(..., callbacks=[CendorCallbackHandler()])``), per call
    (``config={"callbacks": [handler]}``), or on an agent
    (``agent.invoke(..., config={"callbacks": [handler]})``) — for LangGraph it propagates to every
    node and its tools.

    **Correlation.** Every emitted event carries a ``trace_id`` that is the **root run id** of the
    invocation — resolved by tracking the callback run tree (each run's ``parent_run_id``) and
    walking to the top. So all model/tool calls of one ``agent.invoke`` (across its nodes and the
    react loop) share one ``trace_id``, while separate invocations get distinct ones. A standalone
    ``llm.invoke`` uses its own run id. This is a correlation *hook*, not an orchestrator (see the
    plan's non-goals): cendor groups by the framework's own run tree; it invents no run graph.

    Every handler body is exception-safe (a recorder must never break the app); ``raise_error`` is
    left ``False`` so LangChain also swallows any escape.
    """

    #: LangChain swallows callback exceptions when this is False (its default) — belt-and-suspenders
    #: alongside the per-method try/except below.
    raise_error = False

    def __init__(self) -> None:
        super().__init__()
        # run_id -> parent_run_id (str, or None for a root), built from the *_start callbacks so we
        # can resolve the root run each event belongs to. Bounded: entries are removed on run end.
        self._parents: dict[str, str | None] = {}
        # tool run_id -> pending {name, input}, bridged from on_tool_start to on_tool_end.
        self._tool_runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()  # LangGraph may run nodes on threads; guard the shared dicts

    # ------------------------------------------------------------------ run-tree bookkeeping

    def _register(self, run_id: Any, parent_run_id: Any) -> None:
        if run_id is None:
            return
        with self._lock:
            self._parents[str(run_id)] = str(parent_run_id) if parent_run_id is not None else None

    def _forget(self, run_id: Any) -> None:
        if run_id is None:
            return
        with self._lock:
            self._parents.pop(str(run_id), None)

    def _trace_id(self, run_id: Any, parent_run_id: Any) -> str:
        """The root run id for this event: walk ``parent`` links up to the top. Falls back to
        ``parent_run_id``/``run_id`` when the run tree wasn't observed (e.g. a bare model call)."""
        rid = str(run_id) if run_id is not None else ""
        if not rid:
            return str(parent_run_id) if parent_run_id is not None else ""
        with self._lock:
            seen: set[str] = set()
            while rid in self._parents and self._parents[rid] and rid not in seen:
                seen.add(rid)
                rid = self._parents[rid]  # type: ignore[assignment]  # guarded truthy above
        return rid

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        self._register(run_id, parent_run_id)

    def on_chain_end(self, outputs: Any, *, run_id: Any = None, **_: Any) -> None:
        self._forget(run_id)

    def on_chain_error(self, error: BaseException, *, run_id: Any = None, **_: Any) -> None:
        self._forget(run_id)

    def on_chat_model_start(
        self,
        serialized: Any,
        messages: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        self._register(run_id, parent_run_id)

    def on_llm_start(
        self,
        serialized: Any,
        prompts: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        self._register(run_id, parent_run_id)

    # ------------------------------------------------------------------ LLM calls

    def on_llm_end(
        self, response: Any, *, run_id: Any = None, parent_run_id: Any = None, **_: Any
    ) -> None:
        """Emit an ``LLMCall`` with usage/reasoning/cost and a run-correlated ``trace_id``."""
        try:
            usage = _usage_from_result(response)
            call = LLMCall(
                id=uuid.uuid4().hex,
                provider="langchain",  # observed via the framework; the real model rides call.model
                model=_model_from_result(response),
                messages=[],  # the callback path is usage-focused; prompts aren't recorded here
                usage=usage,
                trace_id=self._trace_id(run_id, parent_run_id),
            )
            call.metadata["source"] = "langchain"
            _set_cost(call, usage)
            bus.emit(call)
        except Exception:  # noqa: BLE001 - recording must never break the app
            pass
        finally:
            self._forget(run_id)

    def on_llm_error(
        self, error: BaseException, *, run_id: Any = None, parent_run_id: Any = None, **_: Any
    ) -> None:
        """Record a failed model call (no usage) with the error on metadata — never re-raised."""
        try:
            call = LLMCall(
                id=uuid.uuid4().hex,
                provider="langchain",
                model="",
                messages=[],
                trace_id=self._trace_id(run_id, parent_run_id),
            )
            call.metadata["source"] = "langchain"
            call.metadata["error"] = str(error)
            bus.emit(call)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._forget(run_id)

    # ------------------------------------------------------------------ tool calls

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        inputs: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        """Record the tool's parent (for correlation) and stash its name/args until it ends."""
        self._register(run_id, parent_run_id)
        try:
            name = (serialized or {}).get("name") or "tool"
            pending = {"name": name, "input": inputs if inputs is not None else input_str}
            with self._lock:
                self._tool_runs[str(run_id)] = pending
        except Exception:  # noqa: BLE001
            pass

    def on_tool_end(
        self, output: Any, *, run_id: Any = None, parent_run_id: Any = None, **_: Any
    ) -> None:
        """Emit a ``ToolCall`` (name, args, result) correlated to its run's root."""
        try:
            with self._lock:
                pending = self._tool_runs.pop(str(run_id), {})
            tc = ToolCall(
                id=uuid.uuid4().hex,
                name=pending.get("name", "tool"),
                arguments={"input": pending.get("input")},
                result=output,
                trace_id=self._trace_id(run_id, parent_run_id),
            )
            tc.metadata["source"] = "langchain"
            bus.emit(tc)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._forget(run_id)

    def on_tool_error(
        self, error: BaseException, *, run_id: Any = None, parent_run_id: Any = None, **_: Any
    ) -> None:
        """Emit a ``ToolCall`` marking the failure; drop the pending entry. Never re-raised."""
        try:
            with self._lock:
                pending = self._tool_runs.pop(str(run_id), {})
            tc = ToolCall(
                id=uuid.uuid4().hex,
                name=pending.get("name", "tool"),
                arguments={"input": pending.get("input")},
                trace_id=self._trace_id(run_id, parent_run_id),
            )
            tc.metadata["source"] = "langchain"
            tc.metadata["error"] = str(error)
            bus.emit(tc)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._forget(run_id)


# --------------------------------------------------------------------------- extraction helpers


def _usage_from_result(result: Any) -> Usage | None:
    """Recover usage from an ``LLMResult``: prefer a generation message's ``usage_metadata`` (it
    carries reasoning + cache breakdowns), falling back to ``llm_output``'s token usage."""
    for gens in getattr(result, "generations", None) or []:
        for gen in gens:
            meta = getattr(getattr(gen, "message", None), "usage_metadata", None)
            if meta:
                return _usage_from_metadata(meta)
    llm_output = getattr(result, "llm_output", None) or {}
    token_usage = llm_output.get("token_usage") or llm_output.get("usage")
    if token_usage:
        inp = token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0
        out = token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0
        cdetails = token_usage.get("completion_tokens_details") or {}
        reasoning = (cdetails or {}).get("reasoning_tokens", 0) or 0
        pdetails = token_usage.get("prompt_tokens_details") or {}
        cached = (pdetails or {}).get("cached_tokens", 0) or 0
        return Usage(int(inp), int(out), int(cached), int(reasoning))
    return None


def _usage_from_metadata(meta: dict) -> Usage:
    """Map LangChain's ``usage_metadata`` to :class:`~cendor.core.types.Usage`.

    LangChain already reports ``input_tokens`` *including* the cached read (``cached ⊆ input``, the
    same convention cendor normalizes to), so no folding is needed. Reasoning is under
    ``output_token_details.reasoning``; cache read/creation under ``input_token_details``.
    """
    inp = int(meta.get("input_tokens", 0) or 0)
    out = int(meta.get("output_tokens", 0) or 0)
    out_details = meta.get("output_token_details") or {}
    reasoning = int(out_details.get("reasoning", 0) or 0)
    in_details = meta.get("input_token_details") or {}
    cached = int(in_details.get("cache_read", 0) or 0)
    cache_write = int(in_details.get("cache_creation", 0) or 0)
    return Usage(inp, out, cached, reasoning, cache_write=cache_write)


def _model_from_result(result: Any) -> str:
    """Read the model id from ``llm_output`` or a generation message's ``response_metadata``."""
    llm_output = getattr(result, "llm_output", None) or {}
    model = llm_output.get("model_name") or llm_output.get("model")
    if model:
        return str(model)
    for gens in getattr(result, "generations", None) or []:
        for gen in gens:
            meta = getattr(getattr(gen, "message", None), "response_metadata", None) or {}
            model = meta.get("model_name") or meta.get("model")
            if model:
                return str(model)
    return ""


def _set_cost(call: LLMCall, usage: Usage | None) -> None:
    """Price the call offline from the bundled snapshot; unknown model ⇒ ``cost`` stays ``None``."""
    if usage is None:
        return
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
