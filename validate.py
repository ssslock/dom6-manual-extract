#!/usr/bin/env python3
"""Verify that the extracted Markdown lost no text from the source PDF.

Compares the two sides as *multisets of tokens* (``[A-Za-z0-9]+``) so that
layout, order, table shape and paragraph splitting are ignored — the only
question asked is "is every word present, and was anything invented?".

Two things are normalised away before comparing, because the extractor adds
or drops them on purpose:

  * page-number footers are removed from the PDF side;
  * the ``<!-- p.N -->`` markers and the ``*Dominions 6 manual, p.N*``
    subtitle lines are removed from the Markdown side.

Usage:
    python validate.py --pdf dom6manual.pdf --md chapters
    python validate.py --pdf dom6manual.pdf --md chapters --start 6 --end 98

Exit status is 0 when the token multisets match exactly, 1 otherwise, so this
can be wired into CI as a regression gate.
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

import fitz

TOKEN = re.compile(r"[A-Za-z0-9]+")
FOOTER = re.compile(r"^\s*\d{1,3}\s*$")
MD_COMMENT = re.compile(r"<!--.*?-->", re.S)
MD_SUBTITLE = re.compile(r"^\*Dominions 6 manual, p\.\d+\*\s*$", re.M)


def tokens(text: str) -> collections.Counter:
    return collections.Counter(TOKEN.findall(text))


def pdf_text(path: Path, start: int, end: int) -> str:
    doc = fitz.open(path)
    end = min(end, doc.page_count)
    out = []
    for i in range(start - 1, end):
        pno = i + 1
        # Drop only the line that is exactly this page's number.  Table cells
        # that happen to contain a bare number must survive.
        lines = [ln for ln in doc[i].get_text().split("\n") if ln.strip() != str(pno)]
        out.append("\n".join(lines))
    doc.close()
    return "\n".join(out)


def md_text(root: Path) -> str:
    files = sorted(root.glob("*.md"))
    if not files:
        sys.exit(f"error: no *.md found in {root}")
    text = "".join(f.read_text(encoding="utf-8") for f in files)
    text = MD_COMMENT.sub("", text)
    return MD_SUBTITLE.sub("", text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--md", type=Path, default=Path("chapters"))
    ap.add_argument("--start", type=int, default=6)
    ap.add_argument("--end", type=int, default=98)
    ap.add_argument("--show", type=int, default=15,
                    help="max differing tokens to print per side")
    ap.add_argument("--expect-known-delta", action="store_true",
                    help="accept the one documented p.71 row-spanning-cell "
                         "difference and exit 0 if it is the ONLY difference")
    args = ap.parse_args(argv)

    if not args.pdf.is_file():
        sys.exit(f"error: PDF not found: {args.pdf}")
    if not args.md.is_dir():
        sys.exit(f"error: markdown directory not found: {args.md}")

    src = tokens(pdf_text(args.pdf, args.start, args.end))
    dst = tokens(md_text(args.md))

    missing = src - dst          # in the PDF, absent from the Markdown  -> data loss
    extra = dst - src            # in the Markdown, absent from the PDF  -> invented
    n_missing = sum(missing.values())
    n_extra = sum(extra.values())

    print(f"PDF  (p.{args.start}-{args.end}) : {sum(src.values()):>8,} tokens")
    print(f"Markdown            : {sum(dst.values()):>8,} tokens")
    print(f"missing from md     : {n_missing:>8,}")
    print(f"not in pdf          : {n_extra:>8,}")

    def report(label: str, diff: collections.Counter) -> None:
        if not diff:
            return
        print(f"\n{label}:")
        for tok, n in diff.most_common(args.show):
            print(f"  {tok!r} x{n}")

    report("MISSING from markdown (source text lost)", missing)
    report("EXTRA in markdown (not in the PDF)", extra)

    if n_missing == 0 and n_extra == 0:
        print("\nOK: token multisets are identical.")
        return 0

    # A cell spanning two rows may merge or split two tokens ("Turn"+"Effects"
    # -> "TurnEffects").  That is a known structural limitation, not data loss,
    # so say so rather than leaving the reader to guess.
    print("\nFAIL: the two sides differ.")

    if args.expect_known_delta:
        # Exact, hand-verified allowlist -- deliberately NOT a fuzzy match.
        # If the delta ever changes shape (a real defect, or a new row-spanning
        # cell appearing), this comparison fails, which is the whole point.
        expected_missing = collections.Counter({"Turn": 1, "Effects": 1})
        expected_extra = collections.Counter({"TurnEffects": 1, "32": 1})
        if missing == expected_missing and extra == expected_extra:
            print("KNOWN DELTA: exactly the documented p.71 Twilight-table "
                  "row-spanning-cell artefact.\nOK: nothing else was lost or invented.")
            return 0
        print("The delta does NOT match the documented known issue "
              "(p.71 'Turn' + 'Effects' -> 'TurnEffects', plus a stray '32').")
        print("Treat this as a real defect until proven otherwise.")
        return 1

    print("A single merged/split identifier (< 4 tokens, concatenation-preserving) "
          "is the known row-spanning-cell limitation; anything else is a real defect.")
    print("Re-run with --expect-known-delta to accept that one artefact and nothing else.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
