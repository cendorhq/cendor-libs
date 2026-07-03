"""Property test (Layer E): a fresh chain verifies; any single-entry tamper breaks it. No network.

Uses tempfile directly (not the tmp_path fixture) so Hypothesis can re-run examples cleanly.
"""

import json
import os
import tempfile
from pathlib import Path

from cendor.acttrace import AuditLog, verify
from hypothesis import given, settings
from hypothesis import strategies as st


# This example does real file I/O (~30ms each), so disable Hypothesis's per-example deadline —
# otherwise a loaded run trips a spurious DeadlineExceeded. The invariant is timing-independent.
@settings(deadline=None)
@given(inputs=st.lists(st.text(max_size=40), min_size=1, max_size=6))
def test_chain_verifies_and_tamper_is_detected(inputs):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        log = AuditLog(system="s", path=path)
        log.detach()  # we only use explicit decisions here — no bus involvement
        for value in inputs:
            with log.decision(input=value):
                pass

        assert verify(path)[0] is True  # freshly written chain is valid

        lines = Path(path).read_text(encoding="utf-8").split("\n")
        row = json.loads(lines[1])  # line 0 is audit_open; line 1 is the first decision
        row.setdefault("payload", {})["tampered"] = "yes"
        lines[1] = json.dumps(row)
        Path(path).write_text("\n".join(lines), encoding="utf-8")

        assert verify(path)[0] is False  # any edit breaks the hash chain
    finally:
        os.remove(path)
