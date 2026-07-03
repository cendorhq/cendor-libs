"""Record once, replay forever — offline, deterministic. Mock clients only, no network."""

from types import SimpleNamespace

import pytest
from cendor import cassette
from cendor.core import bus, instrument, instrument_tool


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


def _make_client(counter):
    """An OpenAI-shaped client that counts real calls and returns a structured response."""

    class Completions:
        def create(self, **kwargs):
            counter["llm"] += 1
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content="Sure, here is a refund."))
                ],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
            )

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_record_then_replay(tmp_path):
    path = str(tmp_path / "run.json")
    counter = {"llm": 0}

    def run_agent():
        client = _make_client(counter)
        resp = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "I was double charged"}]
        )
        return resp.choices[0].message.content

    # First run (auto -> record): the real client runs once and a cassette is written.
    recorded = cassette.use(path, mode="auto")(run_agent)()
    assert recorded == "Sure, here is a refund."
    assert counter["llm"] == 1
    assert (tmp_path / "run.json").exists()

    # Second run (auto -> replay): no real call; recorded response returned by hash.
    counter["llm"] = 0
    replayed = cassette.use(path, mode="auto")(run_agent)()
    assert replayed == "Sure, here is a refund."
    assert counter["llm"] == 0  # the real client never ran on replay


def test_using_context_manager_records_then_replays(tmp_path):
    path = str(tmp_path / "cm.json")
    counter = {"llm": 0}

    def call():
        client = _make_client(counter)
        resp = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "double charged"}]
        )
        return resp.choices[0].message.content

    with cassette.using(path, mode="auto"):  # records on first use
        first = call()
    assert first == "Sure, here is a refund." and counter["llm"] == 1

    counter["llm"] = 0
    with cassette.using(path, mode="auto"):  # replays — real client never runs
        second = call()
    assert second == "Sure, here is a refund." and counter["llm"] == 0


def test_replay_unknown_call_fails(tmp_path):
    path = str(tmp_path / "empty.json")
    (tmp_path / "empty.json").write_text('{"version": 1, "entries": []}', encoding="utf-8")
    counter = {"llm": 0}

    @cassette.use(path, mode="replay")
    def run():
        client = _make_client(counter)
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(cassette.CassetteError):
        run()


def test_tool_calls_recorded_and_replayed(tmp_path):
    path = str(tmp_path / "tools.json")
    counter = {"tool": 0}

    def run_agent():
        @instrument_tool("search")
        def search(query):
            counter["tool"] += 1
            return {"hits": [f"doc about {query}"]}

        return search("refunds")

    out1 = cassette.use(path, mode="auto")(run_agent)()
    assert out1 == {"hits": ["doc about refunds"]}
    assert counter["tool"] == 1

    counter["tool"] = 0
    out2 = cassette.use(path, mode="auto")(run_agent)()
    assert out2 == {"hits": ["doc about refunds"]}
    assert counter["tool"] == 0  # tool body skipped on replay


def test_secrets_redacted_on_record(tmp_path):
    path = str(tmp_path / "secret.json")
    counter = {"llm": 0}

    @cassette.use(path, mode="record")
    def run():
        client = _make_client(counter)
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "my key is sk-ABCDEFGH12345678 and a@b.com"}],
        )

    run()
    text = (tmp_path / "secret.json").read_text(encoding="utf-8")
    assert "sk-ABCDEFGH12345678" not in text
    assert "a@b.com" not in text
    assert "<redacted>" in text


def test_modern_secret_formats_redacted(tmp_path):
    # sk-ant-…/sk-proj-… (hyphenated), AWS, Google, and JWT must all be scrubbed on record.
    path = str(tmp_path / "secret.json")
    counter = {"llm": 0}
    secrets = [
        "sk-ant-api03-ABCDEFGH12345678",
        "sk-proj-ABCDEFGH12345678",
        "AKIA" + "A" * 16,
        "AIza" + "b" * 35,
        "eyJ" + "a" * 15 + "." + "b" * 15 + "." + "c" * 15,
    ]

    @cassette.use(path, mode="record")
    def run():
        client = _make_client(counter)
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": " ".join(secrets)}]
        )

    run()
    text = (tmp_path / "secret.json").read_text(encoding="utf-8")
    for raw in secrets:
        assert raw not in text
    assert "<redacted>" in text


def test_plain_hyphenated_text_not_redacted(tmp_path):
    # False-positive guard: ordinary hyphenated prose (with spaces) must survive verbatim.
    path = str(tmp_path / "plain.json")
    counter = {"llm": 0}
    sentence = "a well-known best-practice for multi-region fail-over"

    @cassette.use(path, mode="record")
    def run():
        client = _make_client(counter)
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": sentence}]
        )

    run()
    text = (tmp_path / "plain.json").read_text(encoding="utf-8")
    assert sentence in text  # not scrubbed
    assert "<redacted>" not in text


def test_promote_trace_to_replayable_cassette(tmp_path):
    import json

    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "kind": "llm",
                "request": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                "response": {
                    "choices": [{"message": {"content": "promoted answer"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cass = tmp_path / "from_trace.json"
    assert cassette.promote(str(trace), to=str(cass)) == 1

    counter = {"llm": 0}

    @cassette.use(str(cass), mode="replay")
    def run():
        client = _make_client(counter)
        resp = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        return resp.choices[0].message.content

    assert run() == "promoted answer"  # the promoted response replays by matching request hash
    assert counter["llm"] == 0  # real client never ran


def test_rerecord_detects_drift_without_overwriting(tmp_path):
    path = str(tmp_path / "r.json")

    def client_returning(text, counter):
        class Completions:
            def create(self, **kwargs):
                counter["n"] += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                    usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5),
                )

        return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    @cassette.use(path, mode="record")
    def rec():
        c = client_returning("first answer", {"n": 0})
        c.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "q"}])

    rec()
    before = (tmp_path / "r.json").read_text(encoding="utf-8")

    live = {"n": 0}

    @cassette.use(path, mode="rerecord")
    def rerec():
        c = client_returning("second answer", live)  # model behavior "changed"
        c.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "q"}])

    rerec()
    assert live["n"] == 1  # rerecord ran the real client (live)
    d = cassette.drift()
    assert len(d) == 1 and d[0]["kind"] == "llm"
    assert (tmp_path / "r.json").read_text(encoding="utf-8") == before  # cassette NOT overwritten


def test_distinct_long_token_requests_replay_to_distinct_entries(tmp_path):
    # Requests that differ only inside a redaction-triggering span (a 40-char token) must NOT
    # collapse onto one cassette entry: matching hashes the un-redacted request. Replaying in
    # REVERSED order would swap responses if they shared a hash.
    path = str(tmp_path / "collide.json")
    tok_a, tok_b = "A" * 40, "B" * 40

    def client_for(answer):
        class Completions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=answer))],
                    usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5),
                )

        return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    def ask(client, tok):
        return (
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": f"session {tok}"}]
            )
            .choices[0]
            .message.content
        )

    def record():
        ask(client_for("answer A"), tok_a)
        ask(client_for("answer B"), tok_b)

    cassette.use(path, mode="record")(record)()

    out = {}

    def replay():
        live = client_for("LIVE")  # short-circuited on replay
        out["b"] = ask(live, tok_b)  # reversed order vs record
        out["a"] = ask(live, tok_a)

    cassette.use(path, mode="replay")(replay)()
    assert out["a"] == "answer A" and out["b"] == "answer B"  # not swapped onto one entry


def test_now_redacted_modern_tokens_replay_to_distinct_entries(tmp_path):
    # Two requests differing only in a hyphenated sk-ant- key (previously NOT redacted, now
    # scrubbed) must still replay distinctly: matching hashes the UN-redacted request, so redaction
    # can't collapse them onto one entry.
    path = str(tmp_path / "keys.json")
    tok_a = "sk-ant-api03-" + "A" * 20
    tok_b = "sk-ant-api03-" + "B" * 20

    def client_for(answer):
        class Completions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=answer))],
                    usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5),
                )

        return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    def ask(client, tok):
        return (
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": f"key {tok}"}]
            )
            .choices[0]
            .message.content
        )

    cassette.use(path, mode="record")(
        lambda: (ask(client_for("A"), tok_a), ask(client_for("B"), tok_b))
    )()

    import json

    stored = (tmp_path / "keys.json").read_text(encoding="utf-8")
    assert tok_a not in stored and tok_b not in stored  # both scrubbed in the committed file
    payload = json.loads(stored)
    hashes = {e["request_hash"] for e in payload["entries"]}
    assert len(hashes) == 2  # distinct hashes despite identical redacted text

    out = {}

    def replay():
        live = client_for("LIVE")
        out["b"] = ask(live, tok_b)  # reversed order
        out["a"] = ask(live, tok_a)

    cassette.use(path, mode="replay")(replay)()
    assert out["a"] == "A" and out["b"] == "B"  # not swapped onto one entry


def test_redact_false_preserves_long_ids(tmp_path):
    path = str(tmp_path / "raw.json")
    long_id = "abcdef0123456789abcdef0123456789abcd"  # 36 chars — the default would redact it

    def run():
        class Completions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=long_id))],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                )

        instrument(
            SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        ).chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "go"}])

    cassette.use(path, mode="record", redact=False)(run)()
    text = (tmp_path / "raw.json").read_text(encoding="utf-8")
    assert long_id in text  # redact=False keeps legitimate long data verbatim


def test_secrets_still_redacted_but_request_hash_disambiguates(tmp_path):
    # Default redaction still keeps secrets out of the committed file...
    path = str(tmp_path / "sec.json")
    counter = {"llm": 0}

    @cassette.use(path, mode="record")
    def run():
        _make_client(counter).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "token sk-ABCDEFGH12345678"}]
        )

    run()
    import json

    payload = json.loads((tmp_path / "sec.json").read_text(encoding="utf-8"))
    stored = json.dumps(payload["entries"][0]["request"])
    assert "sk-ABCDEFGH12345678" not in stored  # ...the stored request is redacted
    assert len(payload["entries"][0]["request_hash"]) == 64  # ...but the hash is real (sha256)


def test_semantic_match():
    assert cassette.semantic_match("We can offer you a refund today", "offer a refund")
    assert cassette.semantic_match("identical", "identical")
    assert not cassette.semantic_match("the sky is blue", "process a tax return", threshold=0.6)


def test_semantic_match_pluggable_scorer():
    # A custom scorer (e.g. embeddings/LLM-judge) swaps the default without changing call sites.
    assert cassette.semantic_match("anything", "totally different", scorer=lambda a, e: 0.99)
    assert not cassette.semantic_match("identical", "identical", scorer=lambda a, e: 0.0)


def test_pluggable_normalizer_ignores_volatile_fields(tmp_path):
    # Normalizer that drops a volatile trailing token so two prompts match on replay.
    from cendor.core.types import LLMCall

    def normalizer(event):
        if isinstance(event, LLMCall):
            msgs = [
                {"role": m["role"], "content": m["content"].split("#")[0]} for m in event.messages
            ]
            return {
                "kind": "llm",
                "provider": event.provider,
                "model": event.model,
                "messages": msgs,
            }
        return {"kind": "tool", "name": event.name, "arguments": event.arguments}

    path = str(tmp_path / "norm.json")
    counter = {"llm": 0}

    def call(client, suffix):
        return client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": f"hi #{suffix}"}]
        )

    # record with request id #1
    cassette.use(path, mode="record", normalizer=normalizer)(
        lambda: call(_make_client(counter), "1")
    )()
    counter["llm"] = 0
    # replay with a DIFFERENT request id #2 — normalizer strips it, so it still matches
    out = cassette.use(path, mode="replay", normalizer=normalizer)(
        lambda: call(_make_client(counter), "2")
    )()
    assert out.choices[0].message.content == "Sure, here is a refund."
    assert counter["llm"] == 0  # matched despite the volatile suffix differing


def test_cosine_similarity():
    assert cassette.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cassette.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cassette.cosine([], [1.0]) == 0.0  # degenerate input


def test_embedding_scorer_with_byo_embed_fn():
    # A deterministic fake embedder (no model download): identical text -> identical vector -> 1.0
    vectors = {
        "refund issued": [1.0, 0.0, 0.0],
        "we processed your refund": [0.9, 0.1, 0.0],
        "the weather is sunny": [0.0, 0.0, 1.0],
    }

    def embed_fn(texts):
        return [vectors[t] for t in texts]

    scorer = cassette.embedding_scorer(embed_fn)
    # close in embedding space -> matches above threshold
    assert cassette.semantic_match("refund issued", "we processed your refund", scorer=scorer)
    # orthogonal -> below threshold
    assert not cassette.semantic_match("refund issued", "the weather is sunny", scorer=scorer)


def test_semantic_drift_filters_reworded_equivalents():
    # Seed the drift buffer with one reworded-but-equivalent pair and one genuinely changed pair.
    cassette._drift.clear()
    cassette._drift.extend(
        [
            {
                "request_hash": "a",
                "kind": "llm",
                "recorded": "Your refund has been processed successfully today",
                "live": "Your refund has been processed successfully today!",  # trivial reword
            },
            {
                "request_hash": "b",
                "kind": "llm",
                "recorded": "Your refund has been processed",
                "live": "We are unable to offer a refund",  # real behavior change
            },
        ]
    )
    meaningful = cassette.semantic_drift(threshold=0.8)
    assert len(meaningful) == 1
    assert meaningful[0]["request_hash"] == "b"
    assert meaningful[0]["score"] < 0.8
    # raw drift still reports both byte-level divergences
    assert len(cassette.drift()) == 2


def test_local_embedding_scorer_with_model2vec():
    pytest.importorskip("model2vec")  # skipped in CI without the optional extra
    scorer = cassette.local_embedding_scorer()
    assert cassette.semantic_match(
        "I would like a refund please", "please issue me a refund", scorer=scorer
    )


# --------------------------------------------------------------------------- Phase 1.2 hardening


def test_dict_provider_response_replays_as_dict(tmp_path):
    # Ollama/Bedrock callers use dict access on the response. Replay must preserve the container
    # type (not turn it into a SimpleNamespace, which would TypeError on subscripting).
    path = str(tmp_path / "ollama.json")

    class OllamaClient:
        def chat(self, **kwargs):
            return {"message": {"content": "local answer"}, "eval_count": 5, "prompt_eval_count": 3}

    def run():
        client = instrument(OllamaClient())
        resp = client.chat(model="llama3", messages=[{"role": "user", "content": "hi"}])
        return resp["message"]["content"]  # dict access

    assert cassette.use(path, mode="record")(run)() == "local answer"
    # Replay: the recorded response comes back as a dict, so dict access still works.
    assert cassette.use(path, mode="replay")(run)() == "local answer"


def test_object_provider_response_still_replays_as_namespace(tmp_path):
    # OpenAI-shaped (attribute access) must still reconstruct to a namespace.
    path = str(tmp_path / "openai.json")
    counter = {"llm": 0}

    def run():
        resp = _make_client(counter).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        return resp.choices[0].message.content  # attribute access

    assert cassette.use(path, mode="record")(run)() == "Sure, here is a refund."
    assert cassette.use(path, mode="replay")(run)() == "Sure, here is a refund."


def _delta(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


def _usage_chunk_c(p, c):
    return SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=p, completion_tokens=c))


def _stream_client(chunks):
    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_streaming_record_then_replay_sync(tmp_path):
    path = str(tmp_path / "stream.json")
    chunks = [_delta("Hel"), _delta("lo"), _usage_chunk_c(10, 5)]

    def run():
        stream = _stream_client(chunks).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        return "".join(c.choices[0].delta.content for c in stream if c.choices)

    assert cassette.use(path, mode="record")(run)() == "Hello"
    # Replay: the stream branch matches (stream is part of the hash) and re-yields recorded chunks.
    assert cassette.use(path, mode="replay")(run)() == "Hello"


async def test_streaming_record_then_replay_async(tmp_path):
    path = str(tmp_path / "astream.json")
    chunks = [_delta("Wor"), _delta("ld"), _usage_chunk_c(10, 5)]

    def _aclient():
        class Completions:
            async def create(self, **kwargs):
                async def agen():
                    for c in chunks:
                        yield c

                return agen()

        return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    async def run():
        stream = await _aclient().chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        return "".join([c.choices[0].delta.content async for c in stream if c.choices])

    with cassette.using(path, mode="record"):
        assert await run() == "World"
    with cassette.using(path, mode="replay"):
        assert await run() == "World"


def test_stream_and_nonstream_do_not_collide(tmp_path):
    # Same model+messages, one streamed and one not: they must record as DISTINCT entries.
    import json as _json

    path = str(tmp_path / "mix.json")
    stream_chunks = [_delta("streamed"), _usage_chunk_c(1, 1)]

    def run_record():
        _make_client({"llm": 0}).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        list(
            _stream_client(stream_chunks).chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
            )
        )

    cassette.use(path, mode="record")(run_record)()
    payload = _json.loads((tmp_path / "mix.json").read_text(encoding="utf-8"))
    assert payload["version"] == 2
    hashes = {e["request_hash"] for e in payload["entries"]}
    assert len(hashes) == 2  # stream=True vs stream=False -> two entries, not a collision


def test_unknown_version_raises_clean_error(tmp_path):
    path = tmp_path / "future.json"
    path.write_text('{"version": 99, "entries": []}', encoding="utf-8")

    def run():
        _make_client({"llm": 0}).chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(cassette.CassetteError, match="version"):
        cassette.use(str(path), mode="replay")(run)()


def test_v1_cassette_still_replays(tmp_path):
    # A committed v1 cassette (no `stream` in the hash, no response_type) must keep replaying.
    import json as _json

    from cendor.cassette import _hash

    req = {
        "kind": "llm",
        "provider": "openai",
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
    }  # v1 hashed without a stream key
    v1 = {
        "version": 1,
        "entries": [
            {
                "seq": 0,
                "kind": "llm",
                "request_hash": _hash(req),
                "request": req,
                "response": {"choices": [{"message": {"content": "legacy answer"}}]},
            }
        ],
    }
    path = tmp_path / "v1.json"
    path.write_text(_json.dumps(v1), encoding="utf-8")

    counter = {"llm": 0}

    def run():
        resp = _make_client(counter).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        return resp.choices[0].message.content

    assert cassette.use(str(path), mode="replay")(run)() == "legacy answer"
    assert counter["llm"] == 0


def test_concurrent_using_blocks_do_not_contaminate(tmp_path):
    # Two record contexts open at once must each capture only their own calls (ContextVar-scoped),
    # not every event on the shared bus.
    import json as _json

    p1, p2 = str(tmp_path / "c1.json"), str(tmp_path / "c2.json")
    with cassette.using(p1, mode="record"):
        _make_client({"llm": 0}).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "one"}]
        )
        with cassette.using(p2, mode="record"):
            _make_client({"llm": 0}).chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "two"}]
            )

    e1 = _json.loads((tmp_path / "c1.json").read_text(encoding="utf-8"))["entries"]
    e2 = _json.loads((tmp_path / "c2.json").read_text(encoding="utf-8"))["entries"]
    # Inner "two" call is captured only by the inner context; outer captures only "one".
    assert len(e1) == 1 and e1[0]["request"]["messages"][0]["content"] == "one"
    assert len(e2) == 1 and e2[0]["request"]["messages"][0]["content"] == "two"


def test_promote_tool_call_replays(tmp_path):
    import json as _json

    from cendor.core.instrument import instrument_tool

    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        _json.dumps(
            {
                "kind": "tool",
                "request": {"name": "search", "arguments": {"args": [], "kwargs": {"q": "refund"}}},
                "response": {"hits": 3},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cass = tmp_path / "tool.json"
    assert cassette.promote(str(trace), to=str(cass)) == 1

    ran = {"n": 0}

    @instrument_tool("search")
    def search(q):
        ran["n"] += 1
        return {"hits": 999}

    with cassette.using(str(cass), mode="replay"):
        result = search(q="refund")  # the same call the trace describes
    assert result == {"hits": 3}  # promoted response replayed
    assert ran["n"] == 0  # the real tool never ran
