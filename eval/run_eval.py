#!/usr/bin/env python3
"""Retrieval regression eval: Chinese question -> English manual chunk.

What this measures
------------------
A per-chunk score is not the thing users care about. What matters is: after the
whole retrieval pipeline (dense + BM25 + RRF fusion + reranker) returns top_k
chunks, does the chunk that actually holds the answer make it into that list?

So this script talks to a *live* knowledge base instead of embedding files
locally. Running dense-only locally hides two things that matter:

  * the sparse/BM25 branch behaves very differently from dense when the query
    is Chinese and the document is English (BM25 contributes ~nothing, and the
    fusion can still push noise up);
  * the reranker is not a monotonic improvement -- it has been observed
    dropping a dense rank-2 answer out of the top 5.

Both are only visible end-to-end.

Judging a hit
-------------
Every chunk starts with a synthesised header line:

    # Combat > Melee combat > Armor defeating hits (p.60) [1/2]

`expect` in cases.json is a substring of that header. Case hits if any of the
top_k headers contains it. Using the header (rather than the body) keeps the
expectation readable and stable across re-chunking, as long as the section
structure survives.

Known weakness: some `expect` strings match several chunks (e.g. "Fatigue"),
which makes those cases lenient. Tighten by using the full "Chapter > Section"
path when a collision shows up in the output.

Usage
-----
    python run_eval.py --kb dominions6
    python run_eval.py --kb dominions6 --out run.json --baseline baseline.json
    python run_eval.py --kb dominions6 --min-hit5 0.85

Credentials come from flags or env: KB_TOKEN, or KB_USER / KB_PASSWORD (which
performs the dashboard login). Nothing is read from disk outside this repo.

Exit codes
----------
0  all thresholds met and (with --baseline) no case regressed
1  a threshold was missed, a case regressed, or a query errored
2  bad arguments / unreadable cases file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BASE = os.environ.get("KB_BASE", "http://127.0.0.1:6185/api")


def post(url: str, payload: dict, token: str | None = None, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def login(base: str, user: str, password: str) -> str:
    r = post(f"{base}/auth/login", {"username": user, "password": password})
    if r.get("status") != "ok":
        raise SystemExit(f"login failed: {r}")
    return r["data"]["token"]


def head_of(content: str) -> str:
    """First line of a chunk: the synthesised '# Chapter > Section (p.N)' header."""
    return (content or "").split("\n", 1)[0].strip()


def retrieve(base: str, token: str | None, kb: str, query: str, top_k: int) -> list[str]:
    """Return the raw contents of the top_k chunks, in ranked order."""
    r = post(
        f"{base}/kb/retrieve",
        {"query": query, "kb_names": [kb], "top_k": top_k},
        token,
    )
    if r.get("status") != "ok":
        raise RuntimeError(r.get("message") or str(r))
    items = (r.get("data") or {}).get("results") or []
    return [i.get("content", "") for i in items]


def rank_of(heads: list[str], expect: str) -> int | None:
    needle = expect.lower()
    for i, h in enumerate(heads, 1):
        if needle in h.lower():
            return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=DEFAULT_BASE, help="dashboard API root")
    ap.add_argument("--kb", default="dominions6", help="knowledge base name")
    ap.add_argument("--cases", default=str(HERE / "cases.json"))
    ap.add_argument("--top-k", type=int, default=None,
                    help="override cases.json top_k")
    ap.add_argument("--token", default=os.environ.get("KB_TOKEN"))
    ap.add_argument("--user", default=os.environ.get("KB_USER"))
    ap.add_argument("--password", default=os.environ.get("KB_PASSWORD"))
    ap.add_argument("--out", default=None, help="write per-case detail JSON here")
    ap.add_argument("--baseline", default=None,
                    help="previous --out file; any case that stops hitting is a regression")
    ap.add_argument("--min-hit5", type=float, default=0.85,
                    help="fail if overall hit@k is below this (default 0.85)")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    a = ap.parse_args()

    try:
        spec = json.loads(Path(a.cases).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"cannot read cases: {e}", file=sys.stderr)
        return 2
    cases = spec["cases"]
    top_k = a.top_k or spec.get("top_k", 5)
    base = a.base.rstrip("/")

    token = a.token
    if not token and a.user:
        token = login(base, a.user, a.password or "")

    rows = []
    errors = 0
    t_start = time.monotonic()
    for c in cases:
        t0 = time.monotonic()
        try:
            contents = retrieve(base, token, a.kb, c["question"], top_k)
        except (URLError, urllib.error.URLError, RuntimeError, TimeoutError) as e:
            print(f"ERROR {c['id']}: {e}", file=sys.stderr)
            contents = []
            errors += 1
        heads = [head_of(x) for x in contents]
        rank = rank_of(heads, c["expect"])
        rows.append({
            "id": c["id"],
            "group": c.get("group", "default"),
            "question": c["question"],
            "expect": c["expect"],
            "rank": rank,
            "elapsed": round(time.monotonic() - t0, 2),
            "top": heads,
        })
        if not a.quiet:
            mark = f"OK  r={rank}" if rank else "MISS    "
            print(f"{mark:10s} {c['id']:4s} {c['question'][:30]:30s} -> {c['expect'][:46]}")

    print(f"\n=== summary (hit = expect appears in a top-{top_k} header) ===")
    groups = sorted({r["group"] for r in rows})
    for g in groups:
        rs = [r for r in rows if r["group"] == g]
        n = len(rs)
        h1 = sum(1 for r in rs if r["rank"] == 1)
        hk = sum(1 for r in rs if r["rank"])
        print(f"{g:12s} n={n:3d}  hit@1={h1}/{n}  hit@{top_k}={hk}/{n}")
    n = len(rows)
    h1 = sum(1 for r in rows if r["rank"] == 1)
    hk = sum(1 for r in rows if r["rank"])
    elapsed = time.monotonic() - t_start
    print(f"{'total':12s} n={n:3d}  hit@1={h1}/{n}  hit@{top_k}={hk}/{n}"
          f"  ({elapsed / max(n, 1):.2f}s per query)")

    if a.out:
        Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print(f"detail -> {a.out}")

    failed = False
    if errors:
        print(f"\nFAIL: {errors} query error(s)", file=sys.stderr)
        failed = True
    if n and hk / n < a.min_hit5:
        print(f"\nFAIL: hit@{top_k} {hk}/{n} is below --min-hit5 {a.min_hit5:.2f}",
              file=sys.stderr)
        failed = True

    if a.baseline:
        prev = {r["id"]: r for r in json.loads(Path(a.baseline).read_text(encoding="utf-8"))}
        regressed = [r for r in rows
                     if prev.get(r["id"], {}).get("rank")
                     and not r["rank"]]
        improved = [r for r in rows
                    if not prev.get(r["id"], {}).get("rank") and r["rank"]]
        if improved:
            print(f"\nimproved vs baseline: {len(improved)} "
                  f"({', '.join(r['id'] for r in improved)})")
        if regressed:
            print(f"\nFAIL: {len(regressed)} case(s) regressed vs baseline:", file=sys.stderr)
            for r in regressed:
                print(f"  {r['id']} was rank {prev[r['id']]['rank']}, now out of top {top_k}",
                      file=sys.stderr)
            failed = True
        elif not a.quiet:
            print("no case regressed vs baseline")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
