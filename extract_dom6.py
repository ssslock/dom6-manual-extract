#!/usr/bin/env python3
"""Dominions 6 manual -> structured markdown (text layer only, no OCR).

Design notes (all verified against this specific PDF, Haru 2.3.0, 449p):
  * Page content stream is written column-by-column, so PyMuPDF's default
    (unsorted) block order IS the correct reading order.  1x verified on
    p81 where a naive y-then-x sort interleaves the two columns.
  * Font sizes carry the whole hierarchy:
        14.2 chapter / 12.4 section / 11.4 + 10.4 subsection / 9.5 body /
        8.6 small (table cells, formulas, bullet lists)
    Bookmark depth (407 bookmarks) overrides size-based guessing.
  * Paragraph break == y-gap.  Within-paragraph leading is 14.2, between
    paragraphs 24.2; nothing in between (measured over all of p6-98).
  * Tables == contiguous runs of 8.6pt lines whose cell x0 cluster into
    >= 2 columns and >= 2 rows.  A row missing the first column is a
    wrapped continuation of the previous row (verified on p81 Smite table:
    'Underworld' at x=92.4 belongs to column 2 of the row above).
  * Formula / note blocks == 8.6pt single-column runs, indented slightly.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import fitz
HERE = Path(__file__).resolve().parent
# Default location only.  Override with --pdf; nothing below depends on it.
DEFAULT_PDF = HERE / "dom6manual.pdf"

BODY, SMALL = 9.5, 8.6
FOOTER_Y = 735.0
LEADING = {"body": 14.2, "small": 14.2}
PARA_GAP_RATIO = 1.45      # 14.2 * 1.45 = 20.6  (24.2 real break, 14.2 same para)
X_CLUSTER_TOL = 4.0        # cell column tolerance
Y_ROW_TOL = 3.0


def norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


@dataclass
class Line:
    page: int
    x0: float
    x1: float
    y0: float
    y1: float
    size: float
    font: str
    text: str
    spans: list[tuple[float, float, str]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if self.size > 9.6:
            return "head"
        if self.size > 9.0:
            return "body"
        return "small"


def page_lines(page: fitz.Page, pno: int) -> list[Line]:
    out: list[Line] = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            txt = "".join(s["text"] for s in spans)
            y0 = l["bbox"][1]
            if y0 > FOOTER_Y and re.fullmatch(r"\s*\d+\s*", txt):
                continue                      # running page number
            out.append(Line(pno, l["bbox"][0], l["bbox"][2], y0, l["bbox"][3],
                            max(s["size"] for s in spans),
                            spans[0]["font"], txt,
                            [(s["bbox"][0], s["bbox"][2], s["text"])
                             for s in spans]))
    return out


def cluster(vals: list[float], tol: float) -> list[float]:
    """Greedy 1-D clustering -> sorted cluster centres."""
    centres: list[list[float]] = []
    for v in sorted(vals):
        if centres and v - centres[-1][-1] <= tol:
            centres[-1].append(v)
        else:
            centres.append([v])
    return [sum(c) / len(c) for c in centres]


def row_groups(run: list[Line]) -> list[list[Line]]:
    groups: list[list[Line]] = []
    for l in run:
        if groups and abs(l.y0 - groups[-1][0].y0) <= Y_ROW_TOL:
            groups[-1].append(l)
        else:
            groups.append([l])
    return groups


def build_table(groups: list[list[Line]]) -> list[list[str]] | None:
    """Rebuild a grid from rows of cells.

    Column centres are computed ONLY from rows that actually hold >= 2 cells,
    otherwise a wrapped cell ('damage (will mostly harm unarmored targets).'
    starting at x=262 while its column starts at x=192.7) invents a phantom
    column and every real row gets shifted one cell right.
    """
    primary = [g for g in groups if len(g) >= 2]
    if len(primary) < 2:
        return None
    cols = cluster([l.x0 for g in primary for l in g], X_CLUSTER_TOL)
    if len(cols) < 2:
        return None

    def col_of(x: float) -> int:
        return min(range(len(cols)), key=lambda i: abs(cols[i] - x))

    rows: list[list[str]] = []
    for g in groups:
        # Build cells from SPANS, not whole lines: the main text is drawn
        # as one line per row, so adjacent cells often arrive as one merged
        # line ("GlamourReturn of the Past") with zero x-gap between spans.
        parts: dict[int, list[str]] = {}
        for l in sorted(g, key=lambda l: l.x0):
            for sx0, _sx1, stext in (l.spans or [(l.x0, l.x1, l.text)]):
                if stext.strip():
                    parts.setdefault(col_of(sx0), []).append(stext.strip())
        cells = {c: " ".join(v) for c, v in parts.items()}
        if rows and 0 not in cells:           # wrapped continuation of prev row
            for c, t in cells.items():
                while len(rows[-1]) <= c:
                    rows[-1].append("")
                rows[-1][c] = (rows[-1][c] + " " + t).strip()
        else:
            rows.append([cells.get(i, "") for i in range(len(cols))])
    return rows if len(rows) >= 2 else None


def analyse_small_run(run: list[Line]):
    """8.6pt runs -> table / caption / paragraph / bullet segments.

    This text is a mix of: real grids (p25 fortress table), definition lists
    (p19, p38: label at x=54, description at x=64 - only 10pt apart, so the
    y-gap alone cannot tell entries apart), '*' bullets indented under a
    definition (p38 "Age" -> five bullets), and note boxes (p37 "From the
    tomes of the lore of Dominions").

    Indentation is therefore the primary signal: deeper indent continues the
    current block, shallower starts a new one, equal indent continues only if
    the vertical gap is small.
    """
    rows = row_groups(run)
    multi = [len(g) >= 2 for g in rows]
    out: list[tuple[str, object]] = []
    buf: list[list[Line]] = []
    blocks: list[list] = []          # [kind, [Line], x0]

    def flush_table() -> None:
        nonlocal buf
        if not buf:
            return
        built = build_table(buf)
        if built:
            out.append(("table", built))
        else:
            blocks.extend(["para", list(g), g[0].x0] for g in buf)
        buf = []

    def flush_blocks() -> None:
        nonlocal blocks
        # A single-row block immediately followed by a deeper-indented block
        # is a definition label ("Magic Resistance") -> emphasise it, so a
        # chunk retrieved for a mechanism question still names its subject.
        kinds: list[str] = []
        for idx, (kind, lines, x0) in enumerate(blocks):
            if (kind == "para" and len(lines) == 1
                    and idx + 1 < len(blocks)
                    and blocks[idx + 1][2] > x0 + 6
                    and len(lines[0].text.strip()) <= 60):
                kinds.append("label")
            else:
                kinds.append(kind)
        pending: list[tuple[str, object]] = []
        for (kind, lines, _x0), k in zip(blocks, kinds):
            if k == "bullet":
                pending.append(("bullets", [join_lines(
                    [l.text.lstrip("*").strip() for l in lines])]))
            elif k == "label":
                pending.append(("label", lines[0].text.strip()))
            else:
                pending.append(("para", lines))
        # consecutive single bullets -> one bullet element
        merged: list[tuple[str, object]] = []
        for kind, payload in pending:
            if kind == "bullets" and merged and merged[-1][0] == "bullets":
                merged[-1][1].extend(payload)       # type: ignore[union-attr]
            elif kind == "bullets":
                merged.append((kind, list(payload)))  # type: ignore[arg-type]
            else:
                merged.append((kind, payload))
        out.extend(merged)
        blocks.clear()

    cur: list | None = None
    for idx, g in enumerate(rows):
        first = g[0]
        txt = first.text.strip()

        # A column or page jump makes the y-delta NEGATIVE.  That always starts
        # a new block: it can never be a wrapped continuation of the row above.
        # The old code had only an upper bound (`dy <= 16`), so the last row of
        # a left-column grid (y~700) swallowed the whole right-column grid that
        # restarts at y~54 - p33: Air table ate the entire Water table, and the
        # page break made the Death table eat the Nature table.
        dy = (first.y0 - buf[-1][0].y0) if buf else None

        if multi[idx]:
            if cur:
                blocks.append(cur)
                cur = None
            if dy is not None and dy < -0.5:
                flush_table()                 # column / page jump -> new grid
            buf.append(g)
            continue

        wide = (first.x1 - first.x0) > 200
        adjacent = dy is not None and -0.5 <= dy <= 16
        if buf and adjacent and not wide:
            buf.append(g)                     # wrapped table cell
            continue
        flush_table()

        # A lone WIDE line just above a grid row is a caption for that grid.
        # Narrow one-line table titles ("Fire Bless effects", y=337 vs the
        # grid's y=351) look identical in shape, and widening this test to
        # cover them was tried and reverted: it also yanked the real cell
        # "Example" out of the p75 Indirect Magic grid and reshuffled p81.
        # p33's titles are therefore patched by hand instead.
        nxt_is_table = (idx + 1 < len(rows) and multi[idx + 1]
                        and (rows[idx + 1][0].y0 - first.y0) <= 20)
        if wide and nxt_is_table:
            if cur:
                blocks.append(cur)
                cur = None
            out.append(("caption", txt))
            continue

        if txt.startswith("*"):
            if cur:
                blocks.append(cur)
            cur = ["bullet", [first], first.x0]
            continue

        if cur is not None:
            gap = first.y0 - cur[1][-1].y0
            deeper = first.x0 > cur[2] + 6
            same = abs(first.x0 - cur[2]) <= 6
            if cur[0] == "bullet" and deeper and gap <= 20:
                cur[1].append(first)          # wrapped bullet text
                continue
            if same and gap <= 20:
                cur[1].append(first)          # wrapped paragraph line
                continue
            blocks.append(cur)
            cur = None
        cur = ["para", [first], first.x0]

    flush_table()
    if cur:
        blocks.append(cur)
    flush_blocks()

    # Captions are drawn AFTER their table in this PDF's content stream
    # (p81: table rows, then "Divine spells replacing ..."): flip the pair so
    # the caption reads above the grid it labels.
    for i in range(len(out) - 1):
        if out[i][0] == "table" and out[i + 1][0] == "caption":
            out[i], out[i + 1] = out[i + 1], out[i]
    return out


def md_table(rows: list[list[str]]) -> list[str]:
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    esc = lambda s: s.replace("|", "\\|").strip()
    head = rows[0]
    lines = ["| " + " | ".join(esc(c) or " " for c in head) + " |",
             "|" + "|".join([" --- "] * ncol) + "|"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(esc(c) or " " for c in r) + " |")
    return lines


def join_lines(parts: list[str]) -> str:
    """Join wrapped lines, repairing line-break hyphenation.

    In p6-98 there are only 9 line-end hyphens and every one is a compound
    ('province-by-' + 'province', 'non-' + 'stealthy'), so dropping the hyphen
    (the usual de-hyphenation rule) would corrupt them into 'province-byprovince'.
    Keep the hyphen unless the fragment looks like a mid-word split.
    """
    txt = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not txt:
            txt = p
        elif txt.endswith("-") and not txt.endswith("--") and p[:1].islower():
            frag = txt.split()[-1] if txt.split() else ""
            if frag.strip("-") == "":
                txt = txt + " " + p                 # a dash used as a marker
            elif "-" in frag[:-1] or len(frag.rstrip("-")) <= 4:
                txt = txt + p                       # compound / short prefix
            else:
                txt = txt[:-1] + p                  # genuine mid-word split
        else:
            txt = txt + " " + p
    return re.sub(r"\s+", " ", txt).strip()


def emit_paragraph(lines: list[Line]) -> str:
    return join_lines([l.text for l in lines])


# Content-stream quirk on p.33 (bless effects) and p.68 (battlefield clouds):
# the PDF draws every short table title AFTER its own grid, and the grids are
# stacked with no body text between them, so all titles end up as one block of
# lone paragraphs at the end of the 8.6pt run.  Geometry cannot re-attach them
# - the title sits BELOW its grid (p.75 "Indirect Magic bonuses" is at y=728
# while its grid ends at y=714), which is also why the generic "line above a
# grid row" test cannot be used without breaking other pages.
# The titles are therefore declared per page and hoisted in front of their own
# grid, in reading order.  Both lists were checked by hand against the rendered
# PDF pages (p.33: Fire/Air in the left column, Water/Earth/Astral/Death in the
# right; p.68: ten clouds top to bottom).
_TITLE_FIXES: dict[int, list[str]] = {
    33: ["Fire Bless effects", "Air Bless Effects", "Water Bless Effects",
         "Earth Bless Effects", "Astral Bless Effects", "Death Bless Effects",
         "Nature Bless Effects", "Glamour Bless effects", "Blood Bless Effects"],
    68: ["Frost Cloud", "Heat Cloud", "Poison Cloud", "Disease Cloud",
         "Sulphur Cloud", "Rust Cloud", "Death Cloud",
         "Shimmering Light Cloud", "Leeching Cloud", "Sleep Cloud"],
}


def _hoist_one_page(md: list[str], page: int) -> list[str]:
    """Move the declared titles of one page from the tail of `md` up above it."""
    plain = [t.strip().strip("*").strip() for t in _TITLE_FIXES[page]]

    cleaned: list[str] = []
    skip_blank = False
    for line in md:
        if not line.startswith("|") and line.strip().strip("*").strip() in plain:
            skip_blank = True          # drop the title and the blank line after it
            continue
        if skip_blank and not line.strip():
            skip_blank = False
            continue
        skip_blank = False
        cleaned.append(line)

    marker = f"<!-- table p.{page} -->"
    out: list[str] = []
    n = 0
    for line in cleaned:
        if line.strip() == marker and n < len(plain):
            out.append(f"**{plain[n]}**")
            out.append("")
            n += 1
        out.append(line)
    if n != len(plain):
        print(f"WARNING: page {page}: hoisted {n}/{len(plain)} titles")
    return out


def hoist_stray_titles(md: list[str]) -> list[str]:
    """Apply the title fixes for whichever fixed pages this chapter contains."""
    pages = set()
    for line in md:
        m = re.match(r"<!-- table p\.(\d+) -->$", line.strip())
        if m:
            pages.add(int(m.group(1)))
    for page in sorted(pages & _TITLE_FIXES.keys()):
        md = _hoist_one_page(md, page)
    return md


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Extract the Dominions 6 manual text layer into chapter markdown.")
    ap.add_argument("--pdf", type=Path, default=DEFAULT_PDF,
                    help="path to dom6manual.pdf (default: alongside this script)")
    ap.add_argument("--out", type=Path, default=HERE / "chapters",
                    help="output directory for *.md (default: ./chapters)")
    ap.add_argument("--start", type=int, default=6,
                    help="first PDF page, 1-based (default: 6, skips the TOC)")
    ap.add_argument("--end", type=int, default=98,
                    help="last PDF page, 1-based (default: 98 = end of Origins of Nations)")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="manifest.json path (default: next to --out)")
    args = ap.parse_args(argv)

    if not args.pdf.is_file():
        sys.exit(f"error: PDF not found: {args.pdf}\n"
                 "Download the manual from http://ulm.illwinter.com/dom6/dom6manual.pdf "
                 "or pass --pdf.")
    if args.start < 1 or args.end < args.start:
        sys.exit(f"error: bad page range {args.start}..{args.end}")

    doc = fitz.open(args.pdf)
    start, end = args.start, min(args.end, doc.page_count)

    # bookmark index.  Same title can carry different levels on different
    # pages ("Magic" is h3 on p32 but h1 on p73), so match on (title, page)
    # first and only fall back to a bare-title match when it is unique.
    by_title_page: dict[tuple[str, int], int] = {}
    by_title: dict[str, list[tuple[int, int]]] = {}
    for level, title, page in doc.get_toc():
        nt = norm(title)
        by_title_page.setdefault((nt, page), level)
        by_title.setdefault(nt, []).append((level, page))

    def heading_level(title: str, page: int, size: float) -> int:
        nt = norm(title)
        for p in (page, page - 1, page + 1):
            if (nt, p) in by_title_page:
                return by_title_page[(nt, p)]
        hits = by_title.get(nt, [])
        if len({l for l, _ in hits}) == 1:
            return hits[0][0]
        return 2 if size > 13 else 3

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. stream-order element list --------------------------------
    stream: list[Line] = []
    for pno in range(start, end + 1):
        stream.extend(page_lines(doc[pno - 1], pno))

    elements: list[tuple[str, object]] = []
    i = 0
    while i < len(stream):
        line = stream[i]

        if line.kind == "head":
            title = line.text.strip()
            # multi-line headings: same size, next line close below
            j = i + 1
            while (j < len(stream) and stream[j].kind == "head"
                   and abs(stream[j].size - line.size) < 0.1
                   # same page + same column, going DOWN and close.
                   # A column jump makes the gap negative and must not merge
                   # (that bug swallowed "Magic" + 8 following headings).
                   and stream[j].page == line.page
                   and abs(stream[j].x0 - line.x0) < 10
                   and 0 < stream[j].y0 - stream[j - 1].y0
                   <= max(18.0, line.size * 1.6)):
                title += " " + stream[j].text.strip()
                j += 1
            level = heading_level(title, line.page, line.size)
            elements.append(("head", (level, re.sub(r"\s+", " ", title), line.page)))
            i = j
            continue

        # gather a contiguous run of the same kind.  Body paragraphs are
        # allowed to flow across columns and pages, so no artificial break.
        j = i
        while j < len(stream) and stream[j].kind == line.kind:
            j += 1
        run = stream[i:j]

        if line.kind == "small":
            for seg_kind, seg in analyse_small_run(run):
                if seg_kind == "para":
                    elements.append(("para", (emit_paragraph(seg), run[0].page)))
                elif seg_kind == "caption":
                    elements.append(("caption", (seg, run[0].page)))
                elif seg_kind == "bullets":
                    elements.append(("bullets", (seg, run[0].page)))
                elif seg_kind == "label":
                    elements.append(("label", (seg, run[0].page)))
                else:
                    elements.append(("table", (seg, run[0].page)))
            i = j
            continue

        # body paragraphs: split on real vertical gaps.  A column / page jump
        # makes the gap negative - that only ends the paragraph if the
        # sentence was actually closed (otherwise 'consists of' /
        # 'battlefield spells.' ended up as two paragraphs).
        para = [run[0]]
        for prev, cur in zip(run, run[1:]):
            gap = cur.y0 - prev.y0
            if gap <= 0:
                brk = bool(re.search(r"[.!?:;\"”’)\]]\s*$", prev.text.strip())) \
                    or cur.text.strip()[:1].isupper()
            else:
                brk = gap > LEADING["body"] * PARA_GAP_RATIO
            if brk:
                elements.append(("para", (emit_paragraph(para), para[0].page)))
                para = [cur]
            else:
                para.append(cur)
        elements.append(("para", (emit_paragraph(para), para[0].page)))
        i = j

    # A sentence can be interrupted by a table or caption drawn between its
    # two halves (p80/81: "...from one type to another" | table | "through
    # alchemy.").  Stitch the halves back when the tail is still open.
    merged: list[tuple[str, object]] = []
    for kind, payload in elements:
        if kind == "para" and merged:
            k = len(merged) - 1
            while k >= 0 and merged[k][0] in ("table", "caption", "bullets"):
                k -= 1
            if (k >= 0 and merged[k][0] == "para"
                    and not re.search(r"[.!?:;\"”’)\]]\s*$", merged[k][1][0])
                    and payload[0][:1].islower()):
                tail, tpage = merged[k][1]          # type: ignore[misc]
                merged[k] = ("para", (join_lines([tail, payload[0]]), tpage))
                continue
        merged.append((kind, payload))
    elements = merged

    # ---- 2. split into chapters (level-1 headings) -------------------
    chapters: list[tuple[str, int, list[str]]] = []
    cur_title, cur_page, cur_md = "00_front", start, []
    for kind, payload in elements:
        if kind == "head":
            level, title, page = payload            # type: ignore[misc]
            if level == 1 and cur_md:
                chapters.append((cur_title, cur_page, cur_md))
                cur_md = []
            if level == 1:
                cur_title, cur_page = title, page
                cur_md.append(f"# {title}")
                cur_md.append("")
                cur_md.append(f"*Dominions 6 manual, p.{page}*")
                cur_md.append("")
                continue
            cur_md.append("")
            cur_md.append("#" * min(level + 1, 6) + f" {title}  <!-- p.{page} -->")
            cur_md.append("")
        elif kind == "label":
            text, _page = payload                 # type: ignore[misc]
            cur_md.append(f"**{text}**")
            cur_md.append("")
        elif kind == "caption":
            text, _page = payload                 # type: ignore[misc]
            cur_md.append(f"**{text}**")
            cur_md.append("")
        elif kind == "table":
            rows, page = payload                    # type: ignore[misc]
            # A grid title drawn BELOW its grid arrives as a final row with
            # only the first cell filled ("Indirect Magic bonuses", p.75, plus
            # p.13/45/55 terrain, XP and reanimation tables).  Rows starting
            # with "(" are genuine table notes ("(damage is capped to 1 ...)")
            # and stay put - that is the only shape this test must preserve.
            if (len(rows) > 2 and rows[-1][0] and not any(rows[-1][1:])
                    and not rows[-1][0].startswith("(")):
                cur_md.append(f"**{rows.pop()[0]}**")
                cur_md.append("")
            cur_md.append(f"<!-- table p.{page} -->")
            cur_md.extend(md_table(rows))
            cur_md.append("")
        elif kind == "bullets":
            items, _page = payload                  # type: ignore[misc]
            for it in items:
                cur_md.append("- " + re.sub(r"^\*\s*", "", it))
            cur_md.append("")
        else:
            text, _page = payload                   # type: ignore[misc]
            cur_md.append(text)
            cur_md.append("")
    if cur_md:
        chapters.append((cur_title, cur_page, cur_md))

    # ---- 3. write ---------------------------------------------------
    manifest = []
    for idx, (title, page, md) in enumerate(chapters, 1):
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:60] or "front"
        name = f"{idx:02d}_{slug}.md"
        md = hoist_stray_titles(md)          # re-attach p33/p68 grid titles
        body = "\n".join(md).strip() + "\n"
        (out_dir / name).write_text(body, encoding="utf-8")
        manifest.append({"file": name, "title": title, "page": page,
                         "chars": len(body)})

    man_path = args.manifest or (out_dir.parent / "manifest.json")
    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    total = sum(m["chars"] for m in manifest)
    print(f"pages={start}..{end}  chapters={len(manifest)}  total_chars={total}")
    for m in manifest:
        print(f"{m['file']:52s} p.{m['page']:<4} {m['chars']:>7}")



if __name__ == "__main__":
    main()
