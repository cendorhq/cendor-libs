"""``acttrace`` CLI: an offline verifier for the hash chain. docs/acttrace.md §3.

acttrace verify evidence_q3.jsonl     # exits non-zero if the chain is broken
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import verify


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``acttrace`` console script. Returns a process exit code."""
    parser = argparse.ArgumentParser(prog="acttrace", description="Audit-log tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    verify_cmd = sub.add_parser("verify", help="re-walk a JSONL log's hash chain")
    verify_cmd.add_argument("path", help="path to the .jsonl audit/evidence file")
    verify_cmd.add_argument(
        "--key", default=None, help="HMAC signing key; also verifies entry signatures"
    )
    verify_cmd.add_argument(
        "--expect-head", default=None, help="expected head hash; fails if trailing entries are gone"
    )
    verify_cmd.add_argument(
        "--expect-entries", type=int, default=None, help="expected entry count (truncation check)"
    )

    args = parser.parse_args(argv)
    if args.command == "verify":
        ok, detail = verify(
            args.path,
            key=args.key,
            expected_head=args.expect_head,
            expect_entries=args.expect_entries,
        )
        print(detail)
        return 0 if ok else 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
