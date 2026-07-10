"""Every shippable package must carry a ``py.typed`` marker (PEP 561).

Without it, a consumer's type checker (Pyright/Pylance) sees ``Any`` for the whole package and the
inline "Type Teach" call-shape guidance (docstrings + narrowed ``Literal`` params) delivers nothing.
This walks every ``src/cendor/<tool>/`` in the monorepo and asserts the marker is present, so a new
package (or a dropped marker) can't silently ship untyped. The two meta-packages (``cendor`` /
``cendor-libs``) have no ``src/`` and are correctly skipped.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # tests -> cendor-core -> packages -> repo root


def _tool_packages() -> list[Path]:
    """Every ``src/cendor/<tool>/`` dir that is an importable package (has ``__init__.py``)."""
    return sorted(init.parent for init in _REPO_ROOT.glob("packages/*/src/cendor/*/__init__.py"))


def test_repo_layout_discovers_the_tool_packages() -> None:
    names = {p.name for p in _tool_packages()}
    # sanity: all seven libraries are present (guards against a broken glob silently passing)
    assert {
        "core",
        "tokenguard",
        "contextkit",
        "squeeze",
        "guardrails",
        "cassette",
        "acttrace",
    } <= names


def test_every_package_ships_py_typed() -> None:
    missing = [str(p) for p in _tool_packages() if not (p / "py.typed").is_file()]
    assert not missing, f"packages missing a py.typed marker: {missing}"
