#!/usr/bin/env python
"""Regenerate the bundled offline price snapshot from the cendor-prices feed.

    uv run python scripts/sync_prices.py            # fetch the live feed, apply curation, write
    uv run python scripts/sync_prices.py --check    # verify the committed snapshot, write nothing
    uv run python scripts/sync_prices.py --from PATH  # use a local feed file (offline / CI)

**The snapshot is GENERATED from here on — never hand-fed.** It used to be a 44-row file edited by
hand; regeneration was deferred on 2026-07-27 for want of an official source, and by 2026-08-01 two
of its rows had drifted from every other source (`gpt-5.6-luna` by 5x). The feed
(`cendorhq/cendor-prices`) is that source: dated, per-row provenanced, and gate-validated before it
is committed.

Which rows ship is `curation.json` in the feed repo — the reviewed D4 policy, "labs complete, hosts
top". The feed carries the superset and `refresh()` reaches all of it.

⚠️ The TypeScript snapshot is generated FROM THIS FILE, not from the feed
(`cendor-libs-js/scripts/sync-prices.mjs`), so the two languages cannot drift. Run that one after
this one, then `pnpm fixtures` in `cendor-libs-js` to refresh the conformance fixtures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "packages" / "cendor-core" / "src" / "cendor" / "core" / "prices.json"

FEED_URL = "https://raw.githubusercontent.com/cendorhq/cendor-prices/main/prices.json"
CURATION_URL = "https://raw.githubusercontent.com/cendorhq/cendor-prices/main/curation.json"
#: Prefer a sibling checkout when there is one — it is what a maintainer is actually editing.
SIBLING = REPO.parent / "cendor-prices"

#: A release must not ship a snapshot older than this. Wired into `scripts/verify-hold.sh` and
#: printed by `scripts/release.mjs`. Floor rot is the failure this whole wave removes; a gate is
#: what stops it coming back.
MAX_SNAPSHOT_AGE_DAYS = 30

NOTE = (
    "Offline snapshot of per-token USD rates, GENERATED from the cendor-prices feed "
    "(https://github.com/cendorhq/cendor-prices) — do not hand-edit. Dated LIST prices with "
    "per-row provenance: not live, not a billing guarantee, and negotiated or enterprise rates "
    "differ. A provider-reported cost always beats an estimate. Refresh live with "
    "prices.refresh() (the feed), or prices.refresh(source='azure'|'aws'|'modelsdev'|'litellm'|"
    "'openrouter'|'vercel'). prices.explain(model) says where a rate came from. See docs/core.md."
)


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "cendor sync_prices"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - https only
        return json.loads(resp.read().decode("utf-8"), parse_float=Decimal)


def _read(name: str, url: str, local: Path | None) -> dict:
    if local is not None:
        return json.loads(local.read_text(encoding="utf-8"), parse_float=Decimal)
    sib = SIBLING / name
    if sib.exists():
        print(f"  {name}: sibling checkout {sib}")
        return json.loads(sib.read_text(encoding="utf-8"), parse_float=Decimal)
    print(f"  {name}: {url}")
    return _get(url)


def curate(models: dict, curation: dict) -> tuple[dict, list[str]]:
    """Apply `curation.json`. Order matters: exclude beats include, `always` beats both.

    The reference implementation is `cendor-prices/builder/curate.mjs`; this is the same ~20 lines
    against the same JSON, so the POLICY has one home even though the code has three.
    """
    inc = [re.compile(m) for g in curation["include"] for m in g["match"]]
    exc = [re.compile(m) for g in curation["exclude"] for m in g["match"]]
    always = set(curation.get("always", []))
    kept: dict = {}
    for mid in sorted(models):
        if mid in always:
            kept[mid] = models[mid]
            continue
        if any(r.search(mid) for r in exc):
            continue
        if any(r.search(mid) for r in inc):
            kept[mid] = models[mid]
    missing = sorted(m for m in always if m not in models)
    return kept, missing


def render(models: dict, updated: str, provenance: dict) -> str:
    """Write the `prices/1` shape with rates as plain, unquoted decimal literals.

    `json.dumps` would render a `Decimal` as a string (or refuse it), and a `float` would round
    through IEEE-754 — the one thing the spec forbids. So the models block is emitted by hand.
    """
    lines = [
        "{",
        f'  "_note": {json.dumps(NOTE)},',
        f'  "_updated": "{updated}",',
        '  "_schema": "prices/1",',
        f'  "_feed": "{FEED_URL}",',
        '  "models": {',
    ]
    ids = sorted(models)
    for i, mid in enumerate(ids):
        rates = models[mid]
        body = ", ".join(
            f'"{k}": {format(Decimal(str(rates[k])), "f")}'
            for k in ("input", "output", "cached", "cache_write")
            if k in rates
        )
        comma = "," if i < len(ids) - 1 else ""
        lines.append(f"    {json.dumps(mid)}: {{{body}}}{comma}")
    lines.append("  },")
    lines.append('  "_provenance": {')
    prov_ids = [m for m in ids if m in provenance]
    for i, mid in enumerate(prov_ids):
        p = provenance[mid]
        comma = "," if i < len(prov_ids) - 1 else ""
        src = json.dumps(p.get("src"))
        asof = json.dumps(p.get("asof"))
        lines.append(f'    {json.dumps(mid)}: {{"src": {src}, "asof": {asof}}}{comma}')
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def age_days(updated: str, today: date | None = None) -> int | None:
    try:
        y, m, d = (int(x) for x in updated.split("-"))
        return ((today or date.today()) - date(y, m, d)).days
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify the committed snapshot only")
    ap.add_argument("--from", dest="src", help="a local feed file instead of the live URL")
    ap.add_argument("--curation", help="a local curation.json")
    args = ap.parse_args()

    if args.check:
        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"), parse_float=Decimal)
        rows = len(data.get("models", {}))
        updated = data.get("_updated")
        age = age_days(str(updated))
        print(f"snapshot: {rows} rows, _updated={updated}, age={age} days")
        problems = []
        if rows < 100:
            problems.append(f"only {rows} rows — the generated snapshot should carry hundreds")
        if age is None:
            problems.append("_updated is missing or unparseable — the snapshot must be datable")
        elif age > MAX_SNAPSHOT_AGE_DAYS:
            problems.append(
                f"the bundled snapshot is {age} days old (limit {MAX_SNAPSHOT_AGE_DAYS}). "
                f"Run `uv run python scripts/sync_prices.py` before releasing."
            )
        for mid, rates in data.get("models", {}).items():
            if "input" not in rates:
                problems.append(f"{mid} has no input rate")
            elif Decimal(str(rates["input"])) <= 0:
                problems.append(f"{mid} has a zero/negative input rate")
        if problems:
            for p in problems[:10]:
                print(f"  FAIL {p}", file=sys.stderr)
            return 1
        print("snapshot: PASS")
        return 0

    print("reading the feed…")
    feed = _read("prices.json", FEED_URL, Path(args.src) if args.src else None)
    curation = _read("curation.json", CURATION_URL, Path(args.curation) if args.curation else None)
    models = feed.get("models") or {}
    if not models:
        print("FAIL: the feed carried no models", file=sys.stderr)
        return 1

    kept, missing = curate(models, curation)
    if missing:
        # An `always` id the feed does not carry means a documented example has gone unpriced. That
        # is a decision for a human, not something to shrug off in a generator.
        print(f"FAIL: curation `always` ids absent from the feed: {missing}", file=sys.stderr)
        return 1

    prov = {k: v for k, v in (feed.get("_provenance") or {}).items() if k in kept}
    updated = str(feed.get("_updated") or date.today().isoformat())
    text = render(kept, updated, prov)
    SNAPSHOT.write_text(text, encoding="utf-8")
    print(
        f"wrote {SNAPSHOT.relative_to(REPO)}: {len(kept)} rows "
        f"(feed had {len(models)}), _updated={updated}"
    )
    print("next: cendor-libs-js `node scripts/sync-prices.mjs` then `pnpm fixtures`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
