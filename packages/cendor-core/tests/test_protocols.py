"""Protocols are structural: an object satisfies one by shape, with no import or base class."""

from cendor.core import protocols


def test_compressor_is_satisfied_by_shape():
    class MyCompressor:
        def compress(self, content, *, target_tokens=None, model=None, kind="auto"):
            return content[: target_tokens or 10], None

    assert isinstance(MyCompressor(), protocols.Compressor)
    assert not isinstance(object(), protocols.Compressor)


def test_subscriber_and_sink_shapes():
    assert isinstance(lambda event: None, protocols.Subscriber)

    class JsonlSink:
        def write(self, entry):
            pass

    assert isinstance(JsonlSink(), protocols.Sink)


def test_handle_and_eviction_protocols_exist():
    assert hasattr(protocols, "Handle")
    assert hasattr(protocols, "EvictionStrategy")
