# Contributing

The single most useful thing you can send is **a page number**.

This project makes one narrow, checkable claim: *the Markdown contains every word
of the source PDF and nothing that isn't there.* Everything below is about
keeping that claim true.

---

## Reporting a defect

### A word or number is wrong / missing

Reproduce it first — don't trust the extractor's output, check it against the PDF:

```bash
python extract_dom6.py --pdf dom6manual.pdf --out chapters
python validate.py --pdf dom6manual.pdf --md chapters
```

If `validate.py` exits non-zero, that is a **real** finding (the only tolerated
difference is the documented p.71 *Twilight* table artefact, and only with
`--expect-known-delta`). Open an issue containing:

- the exact commands you ran and the full `validate.py` output (the `MISSING` /
  `EXTRA` token lists are what make a report actionable);
- the **PDF page number** where the text lives — this is the one thing a
  maintainer cannot derive from your report and the one thing that shortcuts
  everything else;
- which revision of the manual you have (see "Which manual" below).

`validate.py` compares token multisets, so it tells you *that* something is
wrong but not *where*. If the token is common the page number is the only way to
find it; if it is rare, `grep` the PDF's text layer for the surrounding sentence.

### A table is scrambled, a heading is at the wrong level, a paragraph is split

`validate.py` will pass in this case — the words are all present, only the
structure is off. Include:

- page number,
- a short before/after sketch (three or four cells is enough),
- and, if you can, whether the same pattern appears on other pages. Structural
  bugs in this document come in families: whatever produced your page almost
  certainly produced others.

### Please don't paste the manual into an issue

The manual is © Illwinter and carries no redistribution licence. Quote the
minimum needed to identify the problem (a cell, a sentence) and give page
numbers for the rest.

---

## Which manual

The extractor is tuned to the **programmatically typeset** PDF
(`Haru Free PDF Library 2.3.0`, 449 pages, `Manual Dominions 6 / Illwinter Game
Design (revision 2)`, downloadable from <http://ulm.illwinter.com/dom6/dom6manual.pdf>).

If a future revision re-typesets the book, font sizes, column geometry and
page ranges will shift and the heuristics will need re-tuning. State the
revision in any report, and note that "it does nothing on my file" plus
`pdffonts` output usually means a **scanned** copy — this tool reads a text
layer and deliberately refuses to guess from pixels.

---

## What kinds of change are welcome

| Change | Notes |
|---|---|
| Fix a heuristic so a page (or a family of pages) extracts correctly | Most welcome. See the constraint below. |
| Reassemble **row-spanning cells** | The p.71 *Twilight* table is the known case: cells that span rows (`TurnEffects`) end up outside the table. This is a genuine gap, not a cosmetic one. |
| Extend coverage to p.99+ (spells, items, nations) | Welcome, but keep it opt-in via `--start` / `--end`. Do **not** change the defaults, and do not claim it works without running `validate.py` over the new range. Those pages are large multi-page tables with a different shape; the row/column heuristics need testing there, not assuming. |
| Correct a documented number that doesn't reproduce | Please do. Every figure in the README is meant to be reproducible; one that isn't is a bug in the documentation. |
| Better diagnostics | e.g. having the extractor report *where* it applied a heuristic. Useful, as long as output stays byte-identical. |

Out of scope: OCR, LLM post-processing, PDF rendering to images, and
re-distributing the manual or the extracted text.

---

## The one rule for heuristic changes

**A change must be validated across all 93 pages, not on the page you were
looking at.**

This is not a formality — it is the main failure mode of this codebase, learned
the hard way. Concrete example from development: a caption-detection rule was
relaxed to fix a mangled table on p.33. It fixed p.33 and *simultaneously* pulled
the word `Example` out of a table on p.75 and moved a caption on p.81. Inspecting
only the target page would have shipped that.

So for any change to grouping, caption detection, column detection or paragraph
splitting:

1. capture `md5sum chapters/*.md` **before**;
2. make the change;
3. re-run and diff. Every chapter that changed must be explained — "it changed
   because it was wrong" is only acceptable if you looked at it. A file whose
   byte count moved by a few bytes is exactly where silent damage hides;
4. `validate.py --expect-known-delta` must still exit 0. If the token delta
   changed shape, you either lost text or introduced a new row-spanning artefact —
   in both cases the allowlist in `validate.py` is intentionally not adjusted to
   make the build green without understanding why.

Run the same check before and after, so before/after are comparable:

```bash
# 1. extract
python extract_dom6.py --pdf dom6manual.pdf --out chapters

# 2. no text lost or invented (exit 0, delta == the one documented artefact)
python validate.py --pdf dom6manual.pdf --md chapters --expect-known-delta
echo "exit=$?"

# 3. determinism: same input -> byte-identical output
md5sum chapters/*.md > /tmp/a
python extract_dom6.py --pdf dom6manual.pdf --out chapters
md5sum -c /tmp/a

# 4. table integrity: every row of every table has the same column count
python - <<'PY'
import pathlib, re
bad = 0
for f in sorted(pathlib.Path('chapters').glob('*.md')):
    lines = f.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines) - 1:
        if lines[i].startswith('|') and re.fullmatch(r'\|[\s:|-]+\|', lines[i+1].strip()):
            n = lines[i].count('|')
            j = i + 2
            while j < len(lines) and lines[j].startswith('|'):
                if lines[j].count('|') != n:
                    print('ragged', f.name, 'line', j + 1)
                    bad += 1
                j += 1
            i = j
        else:
            i += 1
print('ragged rows:', bad)
PY
```

Expected: step 2 exits 0; step 3 prints `OK` for every file; step 4 prints
`ragged rows: 0`.

---

## Constraints

These aren't style preferences; each one is load-bearing.

- **Text layer only. No OCR, no VLM, no LLM, no network calls in the extraction
  path.** This document has a complete text layer, so its characters can be
  *read*. An image pipeline can only *guess*, and the content people query is
  numbers — gem costs, HP, DRN modifiers. A guessed number is a hallucination
  that looks like data. (There is more on this in the README.)
- **Deterministic.** Same PDF in, byte-identical Markdown out. No timestamps, no
  randomness, no dependence on dict/set iteration order, no model calls. This is
  what makes `validate.py` and `md5sum -c` meaningful at all.
- **Dependencies: PyMuPDF, nothing else.** The validation snippet above uses only
  the standard library. Please don't add pandas/opencv/beautifulsoup to solve a
  problem that is a dozen lines of geometry.
- **Python 3.9+.** Keep the `from __future__ import annotations` line; the code is
  checked with `ast.parse` against 3.9–3.12.
- **Never commit the PDF or `chapters/`.** Both are ignored by `.gitignore`. See
  the README's *Copyright* section: the manual is freely downloadable but no
  redistribution licence is stated, so it is not ours to publish.
- **Keep the public interface stable** (`--pdf`, `--out`, `--start`, `--end`,
  `--manifest`) so existing invocations and CI keep working.

---

## Pull requests

Small and single-purpose, please — one heuristic, one fix. In the description:

- what was wrong, with a page number and a before/after;
- which other chapters changed, and why that's correct;
- the four checks above.

If a fix helps one page but you can't verify it doesn't hurt the other 92,
say so in the PR and leave it unmerged by default. A half-verified heuristic is
worse than a documented limitation, because the limitation doesn't silently
damage pages nobody looked at.
