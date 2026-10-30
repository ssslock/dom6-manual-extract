# Retrieval eval

A 26-case regression set for asking a Dominions 6 question **in Chinese** and finding
the right **English** manual section. It exists because "the chunks look fine" is not a
measurement: the failures that matter here are silent (a query returns five plausible
chunks, one of which is merely wrong), and they only show up as a hit rate.

Companion to the extractor: `extract_dom6.py` guarantees *what* is in the chunks,
this directory measures *whether they can be found*.

## What it needs

A running knowledge base containing the extracted chapters. `run_eval.py` talks to the
retrieval HTTP API rather than embedding files locally, because the interesting parts of
a real pipeline live outside the embedding model:

* the BM25/sparse branch behaves very differently from dense when the query is Chinese
  and the document is English — it contributes ~nothing, and fusion can still lift noise;
* the reranker is not a monotonic improvement. In testing it has dropped a dense rank-2
  answer out of the top 5, so dense-only numbers are not a substitute.

## Usage

```bash
export KB_BASE=http://127.0.0.1:6185/api      # default
export KB_USER=... KB_PASSWORD=...            # or KB_TOKEN=...
python run_eval.py --kb dominions6 --out run.json --baseline baseline.json
```

`--baseline baseline.json` makes this a regression gate: any case that used to appear in
the top k and no longer does exits non-zero, naming the case. `--min-hit5 0.85` (default)
additionally fails the run if the overall hit rate drops below a floor.

`cases.json` holds `expect`, a substring of the chunk header
(`# Chapter > Section (p.N)`). Judging on the header rather than the body keeps the
expectation readable and stable under re-chunking. Some `expect` strings match more than
one chunk (e.g. `Fatigue`), which makes those cases lenient — use the full
`Chapter > Section` path when a collision actually shows up in the output.

## Baseline

`baseline.json` is the measured result of the 26 cases against the chunk set produced by
`build_chunks_v2` (one chunk per mechanism, section-level). Overall `hit@1 = 17/26`,
`hit@5 = 26/26`.

Reproduce with any pipeline you build; if your numbers are worse, the gap is diagnostic,
not noise.

## A note on what this set does *not* cover

Every case asks for a mechanism. Numbers inside tables (unit stats, spell properties)
were deliberately left out — the manual is not a good source for those, and a table row
retrieved in isolation is not a useful answer. Those belong in a different layer.
