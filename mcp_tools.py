#!/usr/bin/env python3
"""
mcp_tools.py — Python helper called by mcp.php for anything that needs
to read/edit DOCX or EPUB. Prints JSON to stdout.

Subcommands:
  qc <job_dir>                 — quality-check the book (source.docx if present, else EPUB)
  entry <job_dir> <day>        — fetch one day's text (source.docx if present, else EPUB)
  repair <job_dir> <day> <action> [<payload-json>]
                               — truncate_junk | replace | delete
  insert <job_dir> <json-file> — insert a new entry (payload file with heading/verse/etc.)
  rebuild <job_dir>            — rebuild PDF+EPUB from source.docx with the saved settings
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, sys, time, zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GENERATOR = SCRIPT_DIR / "book_generator_2.py"

MONTHS = ["January","February","March","April","May","June",
          "July","August","September","October","November","December"]
DAYS_IN_MONTH = {"January":31,"February":29,"March":31,"April":30,"May":31,"June":30,
                 "July":31,"August":31,"September":30,"October":31,"November":30,"December":31}
DAY_HEADING_RE = re.compile(
    r"^\s*(" + "|".join(MONTHS) + r")\s+(\d{1,2})\s*[:\-–—]\s*(.+?)\s*$"
)

# Leak / junk markers (model chatter and stray JSON)
JUNK_PATTERNS = [
    (r'"document"\s*:', "JSON fragment"),
    (r'",\s*"key"\s*:', "JSON fragment"),
    (r'\{\s*"document"', "JSON fragment"),
    (r'\bWait,\s*I\b', "model chatter"),
    (r'\bWait,\s*let me\b', "model chatter"),
    (r'\bCorrection\s*[—-]', "model chatter"),
    (r'\bNote:\s*Due to formatting\b', "model chatter"),
    (r'\bJSON\b', "model chatter (JSON mention)"),
    (r'^\s*#\s+', "literal markdown heading"),
    (r'\*\*[^*]+\*\*', "literal markdown bold"),
    (r'```', "literal markdown fence"),
    (r'\bundefined\b', "undefined/null leak"),
    (r'\bnull\b', "undefined/null leak"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Source loading: prefer DOCX (editable), fall back to EPUB (read-only)
# ─────────────────────────────────────────────────────────────────────────────

def _docx_paragraphs(path: Path) -> list[tuple[str, str]]:
    """Return [(text, 'docx:<idx>')] for a .docx (preserves order)."""
    from docx import Document
    doc = Document(str(path))
    return [(p.text, f"docx:{i}") for i, p in enumerate(doc.paragraphs)]

def _epub_paragraphs(path: Path) -> list[tuple[str, str]]:
    """Return [(text, 'epub:<file>:<line>')] from EPUB XHTML chapter bodies."""
    import html, re as _re
    out = []
    with zipfile.ZipFile(path) as z:
        xhtmls = sorted(n for n in z.namelist()
                        if n.endswith(".xhtml") and ("ch" in n or "chapter" in n.lower()
                                                     or n.count("/") == 1))
        if not xhtmls:
            xhtmls = sorted(n for n in z.namelist() if n.endswith(".xhtml"))
        for name in xhtmls:
            body = z.read(name).decode("utf-8", errors="replace")
            body = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", body)
            # One "paragraph" per block element
            for m in _re.finditer(r"(?is)<(h[1-6]|p|div)[^>]*>(.*?)</\1>", body):
                txt = _re.sub(r"(?is)<[^>]+>", "", m.group(2))
                txt = html.unescape(txt).strip()
                if txt:
                    out.append((txt, f"epub:{name}:{m.start()}"))
    return out

def _load_paragraphs(job_dir: Path) -> tuple[list[tuple[str, str]], str]:
    src = job_dir / "source.docx"
    if src.is_file():
        return _docx_paragraphs(src), "docx"
    out_dir = job_dir / "output"
    if out_dir.is_dir():
        for f in out_dir.iterdir():
            if f.suffix.lower() == ".epub":
                return _epub_paragraphs(f), "epub"
    raise FileNotFoundError("No source.docx and no EPUB in output/ — nothing to read.")

# ─────────────────────────────────────────────────────────────────────────────
# Entry parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_entries(paras: list[tuple[str, str]]) -> list[dict]:
    """Group paragraphs into entries keyed by day heading. Each entry has:
       heading, month, day_num, title, paragraphs (list of (text, loc)), start_idx, end_idx."""
    entries: list[dict] = []
    current: dict | None = None
    for idx, (text, loc) in enumerate(paras):
        m = DAY_HEADING_RE.match(text or "")
        if m:
            if current is not None:
                current["end_idx"] = idx - 1
                entries.append(current)
            current = {
                "heading": f"{m.group(1)} {int(m.group(2))} : {m.group(3).strip()}",
                "month": m.group(1),
                "day_num": int(m.group(2)),
                "title": m.group(3).strip(),
                "paragraphs": [],
                "start_idx": idx,
                "end_idx": idx,
            }
        else:
            if current is not None:
                current["paragraphs"].append((text, loc))
    if current is not None:
        current["end_idx"] = len(paras) - 1
        entries.append(current)
    return entries

def _day_key(e: dict) -> tuple[int, int]:
    return (MONTHS.index(e["month"]), e["day_num"])

# ─────────────────────────────────────────────────────────────────────────────
# Tool: qc
# ─────────────────────────────────────────────────────────────────────────────

def _find_junk(text: str) -> list[str]:
    hits = []
    for pat, label in JUNK_PATTERNS:
        if re.search(pat, text):
            hits.append(label)
    return hits

def cmd_qc(job_dir: Path) -> dict:
    paras, src_kind = _load_paragraphs(job_dir)
    entries = _parse_entries(paras)

    # --- Day coverage ---
    seen: dict[tuple[str, int], int] = {}
    for e in entries:
        key = (e["month"], e["day_num"])
        seen[key] = seen.get(key, 0) + 1
    expected: list[tuple[str, int]] = []
    for m in MONTHS:
        for d in range(1, DAYS_IN_MONTH[m] + 1):
            expected.append((m, d))
    missing = [f"{m} {d}" for (m, d) in expected if seen.get((m, d), 0) == 0]
    duplicate = [f"{m} {d} (x{c})" for (m, d), c in seen.items() if c > 1]
    # Order check only makes sense on the manuscript: EPUB spine order is
    # driven by the TOC, not the calendar, so skip it when reading EPUB.
    order_errors = []
    if src_kind == "docx":
        last_key = (-1, -1)
        for e in entries:
            k = _day_key(e)
            if k < last_key:
                order_errors.append(e["heading"])
            last_key = k

    # --- Junk + prayer duplicates per entry ---
    junk_hits: dict[str, list[str]] = {}
    prayer_dupes: list[str] = []
    for e in entries:
        body = "\n".join(t for t, _ in e["paragraphs"])
        hits = _find_junk(body)
        if hits:
            junk_hits.setdefault("|".join(sorted(set(hits))), []).append(e["heading"])
        n_prayer = sum(1 for t, _ in e["paragraphs"]
                       if "the prayer of the day" in t.lower())
        if n_prayer > 1:
            prayer_dupes.append(f"{e['heading']} (x{n_prayer})")

    # Flatten junk hits for the report, cap at 5 examples per category
    junk_report = {}
    for cat, days in junk_hits.items():
        junk_report[cat] = {"count": len(days), "examples": days[:5]}

    return {
        "source": src_kind,
        "entries_total": len(entries),
        "days_expected": 366,
        "days_present": sum(1 for v in seen.values() if v >= 1),
        "missing_days": {"count": len(missing), "examples": missing[:5], "all": missing},
        "duplicate_days": {"count": len(duplicate), "examples": duplicate[:5]},
        "out_of_order": {"count": len(order_errors), "examples": order_errors[:5]},
        "junk_categories": junk_report,
        "prayer_duplicates": {"count": len(prayer_dupes), "examples": prayer_dupes[:5]},
        "editable": (job_dir / "source.docx").is_file(),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Tool: entry
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_day(s: str) -> tuple[str, int] | None:
    s = (s or "").strip()
    m = re.match(r"^(" + "|".join(MONTHS) + r")\s+(\d{1,2})\s*$", s, re.IGNORECASE)
    if not m:
        return None
    month = m.group(1).capitalize()
    for mm in MONTHS:
        if mm.lower() == month.lower():
            return (mm, int(m.group(2)))
    return None

def _format_runs(path: Path, start_para: int, end_para: int) -> list[str]:
    """Return paragraph text with inline **bold** / *italic* markers from DOCX runs."""
    from docx import Document
    doc = Document(str(path))
    out = []
    for i in range(start_para, min(end_para + 1, len(doc.paragraphs))):
        p = doc.paragraphs[i]
        parts = []
        for r in p.runs:
            t = r.text
            if r.bold and r.italic:
                parts.append(f"***{t}***")
            elif r.bold:
                parts.append(f"**{t}**")
            elif r.italic:
                parts.append(f"*{t}*")
            else:
                parts.append(t)
        out.append("".join(parts).rstrip())
    return out

def cmd_entry(job_dir: Path, day: str) -> dict:
    nd = _normalize_day(day)
    if nd is None:
        return {"error": f"Bad day format: {day!r}. Use 'January 26' or 'September 12'."}
    month, day_num = nd
    paras, src_kind = _load_paragraphs(job_dir)
    entries = _parse_entries(paras)
    for e in entries:
        if (e["month"], e["day_num"]) == (month, day_num):
            if src_kind == "docx":
                lines = _format_runs(job_dir / "source.docx", e["start_idx"], e["end_idx"])
            else:
                lines = [e["heading"]] + [t for t, _ in e["paragraphs"]]
            return {
                "heading": e["heading"],
                "source": src_kind,
                "lines": lines,
                "junk_hits": _find_junk("\n".join(t for t, _ in e["paragraphs"])),
            }
    return {"error": f"Day not found: {month} {day_num}"}

# ─────────────────────────────────────────────────────────────────────────────
# Tool: repair (DOCX only — editing EPUB would get thrown away by rebuild)
# ─────────────────────────────────────────────────────────────────────────────

def _require_docx(job_dir: Path) -> Path:
    src = job_dir / "source.docx"
    if not src.is_file():
        raise RuntimeError(
            "This job has no source.docx (likely created before MCP source storage). "
            "Re-run the book via generate_book or run import_source to enable edits."
        )
    return src

def _entry_range_in_docx(src: Path, month: str, day_num: int) -> tuple[int, int]:
    from docx import Document
    doc = Document(str(src))
    paras = [p.text for p in doc.paragraphs]
    entries = _parse_entries([(t, f"docx:{i}") for i, t in enumerate(paras)])
    for e in entries:
        if (e["month"], e["day_num"]) == (month, day_num):
            return (e["start_idx"], e["end_idx"])
    raise RuntimeError(f"Day not found: {month} {day_num}")

def _remove_paragraphs(src: Path, first_idx: int, last_idx: int) -> None:
    from docx import Document
    doc = Document(str(src))
    for p in list(doc.paragraphs)[first_idx:last_idx + 1]:
        p._element.getparent().remove(p._element)
    doc.save(str(src))

# ── Run/segment helpers ───────────────────────────────────────────────────────
def _qn(tag: str):
    from docx.oxml.ns import qn
    return qn(tag)

def _extract_segments(para):
    """Flatten a paragraph into [(kind, text, bold, italic)] slots.
    kind in {'text','break'}. Breaks represent <w:br/> (line breaks within the para)."""
    out = []
    W_R, W_T, W_BR, W_TAB, W_RPR, W_B, W_I = (
        _qn('w:r'), _qn('w:t'), _qn('w:br'), _qn('w:tab'),
        _qn('w:rPr'), _qn('w:b'), _qn('w:i'))
    for r in para._element.findall(W_R):
        rPr = r.find(W_RPR)
        bold = rPr is not None and rPr.find(W_B) is not None
        italic = rPr is not None and rPr.find(W_I) is not None
        for child in r:
            if child.tag == W_T:
                out.append(('text', child.text or '', bold, italic))
            elif child.tag == W_BR:
                out.append(('break', '\n', False, False))
            elif child.tag == W_TAB:
                out.append(('text', '\t', bold, italic))
    return out

def _segments_text(segments):
    return ''.join(s[1] for s in segments)

def _write_segments(para, segments):
    """Replace all <w:r> in para with runs rebuilt from segments. Keep <w:pPr>."""
    from docx.oxml import OxmlElement
    for r in list(para._element.findall(_qn('w:r'))):
        para._element.remove(r)
    for kind, text, bold, italic in segments:
        r = OxmlElement('w:r')
        if bold or italic:
            rPr = OxmlElement('w:rPr')
            if bold:   rPr.append(OxmlElement('w:b'))
            if italic: rPr.append(OxmlElement('w:i'))
            r.append(rPr)
        if kind == 'break':
            r.append(OxmlElement('w:br'))
        else:
            t = OxmlElement('w:t')
            t.text = text
            t.set(_qn('xml:space'), 'preserve')
            r.append(t)
        para._element.append(r)

# ── Markdown → runs (**bold**, *italic*, \n → <w:br/>) ────────────────────────
_MD_TOKEN = re.compile(r'(\*\*\*.+?\*\*\*|\*\*.+?\*\*|\*[^*\n]+\*)')

def _parse_markdown_runs(text: str) -> list[tuple[str, bool, bool]]:
    """Return [(text, bold, italic)] by parsing **bold**, *italic*, ***both***."""
    out = []
    pos = 0
    for m in _MD_TOKEN.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False, False))
        tok = m.group(1)
        if tok.startswith('***'):
            out.append((tok[3:-3], True, True))
        elif tok.startswith('**'):
            out.append((tok[2:-2], True, False))
        else:
            out.append((tok[1:-1], False, True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False, False))
    return out

def _render_markdown_into(para, text: str) -> None:
    """Replace runs in para with markdown-parsed content. \n → <w:br/>."""
    segments = []
    lines = text.split('\n')
    for i, line in enumerate(lines):
        for seg_text, bold, italic in _parse_markdown_runs(line):
            if seg_text:
                segments.append(('text', seg_text, bold, italic))
        if i < len(lines) - 1:
            segments.append(('break', '\n', False, False))
    _write_segments(para, segments)

def _append_empty_like(after_p, style_src=None):
    """Insert an empty paragraph after after_p. If style_src given, copy its <w:pPr>;
    otherwise inherit after_p's <w:pPr>."""
    from copy import deepcopy
    from docx.text.paragraph import Paragraph
    src_elem = (style_src._element if style_src is not None else after_p._element)
    new_p = deepcopy(src_elem)
    for child in list(new_p):
        if child.tag != _qn('w:pPr'):
            new_p.remove(child)
    after_p._element.addnext(new_p)
    return Paragraph(new_p, after_p._parent)

# ── Junk-marker finder (for truncate_junk) ────────────────────────────────────
_TRUNC_JUNK_RE = re.compile(
    r'(?P<json_cont>",\s*"[^"\n]{0,80}"\s*:)'
    r'|(?P<json_open>\{\s*"document")'
    r'|(?P<wait>\bWait,\s*(?:I|let me)\b)'
    r'|(?P<corr>\bCorrection\s*[—-])'
    r'|(?P<note>\bNote:\s*Due to formatting\b)'
)

def _first_junk_position(text: str) -> int | None:
    m = _TRUNC_JUNK_RE.search(text)
    return m.start() if m else None

def cmd_repair(job_dir: Path, day: str, action: str, payload: dict) -> dict:
    src = _require_docx(job_dir)
    nd = _normalize_day(day)
    if nd is None:
        return {"error": f"Bad day format: {day!r}"}
    month, day_num = nd
    first, last = _entry_range_in_docx(src, month, day_num)

    if action == "delete":
        _remove_paragraphs(src, first, last)
        return {"ok": True, "action": "delete", "paragraphs_removed": last - first + 1}

    if action == "truncate_junk":
        from docx import Document
        doc = Document(str(src))
        # Walk every body paragraph as a flat segment stream; find first junk char.
        body_paras = list(doc.paragraphs)[first + 1:last + 1]
        if not body_paras:
            return {"ok": True, "note": "no body paragraphs"}
        chunks = []  # (para, segments, flat_text)
        offsets = []  # cumulative char position where each para's text starts
        running = 0
        full_parts = []
        for bp in body_paras:
            segs = _extract_segments(bp)
            txt = _segments_text(segs)
            offsets.append(running)
            running += len(txt) + 1  # +1 for the implicit paragraph break
            full_parts.append(txt)
            chunks.append((bp, segs, txt))
        joined = '\n'.join(full_parts)
        cut = _first_junk_position(joined)
        if cut is None:
            return {"ok": True, "action": "truncate_junk", "note": "no junk found in body"}

        # Map `cut` back to (chunk_index, local_offset)
        target = None
        for idx, (_, _, txt) in enumerate(chunks):
            start = offsets[idx]
            end_text = start + len(txt)
            if cut <= end_text:
                target = (idx, cut - start)
                break
        if target is None:
            return {"ok": True, "note": "could not map junk position"}
        tgt_idx, local = target
        kept_chars = 0
        kept_body_removed = 0

        # Within target paragraph, keep segments up to `local` characters
        tgt_para, tgt_segs, tgt_text = chunks[tgt_idx]
        new_segs = []
        consumed = 0
        for kind, s_text, b, i in tgt_segs:
            if consumed >= local:
                break
            if consumed + len(s_text) <= local:
                new_segs.append((kind, s_text, b, i))
                consumed += len(s_text)
            else:
                remaining = local - consumed
                if remaining > 0:
                    new_segs.append((kind, s_text[:remaining], b, i))
                break
        # Trim trailing whitespace and dangling breaks from kept segments
        while new_segs and new_segs[-1][0] == 'break':
            new_segs.pop()
        if new_segs and new_segs[-1][0] == 'text':
            kind, t, b, i = new_segs[-1]
            new_segs[-1] = (kind, t.rstrip(), b, i)

        if new_segs:
            _write_segments(tgt_para, new_segs)
            kept_chars = sum(len(s[1]) for s in new_segs)
        else:
            # Nothing kept inside this paragraph → remove it entirely
            tgt_para._element.getparent().remove(tgt_para._element)
            kept_body_removed += 1

        # Delete every paragraph after the target
        for bp, _, _ in chunks[tgt_idx + 1:]:
            bp._element.getparent().remove(bp._element)
            kept_body_removed += 1

        doc.save(str(src))
        return {
            "ok": True, "action": "truncate_junk",
            "cut_char": cut, "chars_kept_in_cut_para": kept_chars,
            "paragraphs_removed_after_cut": kept_body_removed - (0 if new_segs else 1),
            "cut_paragraph_cleared": not new_segs,
        }

    if action == "replace":
        content = payload.get("content")
        if not isinstance(content, list) or not all(isinstance(x, str) for x in content):
            return {"error": "replace needs payload.content = list[str] (one string per paragraph)"}
        from docx import Document
        doc = Document(str(src))
        heading_p = doc.paragraphs[first]
        body_paras = list(doc.paragraphs)[first + 1:last + 1]
        body_style_src = body_paras[0] if body_paras else heading_p

        # Detect heading on content[0] AFTER stripping surrounding markdown
        # (**...**, *...*, whitespace). get_entry returns headings wrapped in
        # ** because their runs are bold, so callers paste them back verbatim.
        heading_text = None
        body_items = list(content)
        if content:
            first_clean = re.sub(r'^\s*\*+\s*', '', content[0])
            first_clean = re.sub(r'\s*\*+\s*$', '', first_clean).strip()
            if first_clean and DAY_HEADING_RE.match(first_clean):
                # Safety: refuse to rename this entry to a day that already exists elsewhere
                m2 = DAY_HEADING_RE.match(first_clean)
                new_mo, new_dn = m2.group(1), int(m2.group(2))
                if (new_mo, new_dn) != (month, day_num):
                    try:
                        _entry_range_in_docx(src, new_mo, new_dn)
                        return {"error": f"Would duplicate an existing day: {new_mo} {new_dn}. "
                                         f"Delete that one first or keep the original heading."}
                    except RuntimeError:
                        pass
                heading_text = first_clean
                body_items = content[1:]

        # Safety: refuse stray day headings in the body
        for item in body_items:
            peek = re.sub(r'^\s*\*+\s*', '', item)
            peek = re.sub(r'\s*\*+\s*$', '', peek).strip()
            if DAY_HEADING_RE.match(peek):
                return {"error": f"Body contains what looks like a day heading: {peek!r}. "
                                 f"Use only content[0] for the heading."}

        # Remove current body
        for p in body_paras:
            p._element.getparent().remove(p._element)
        # Overwrite the heading paragraph's runs. Wrap in ** so the run is
        # bold (matches the format get_entry reports for existing headings),
        # independent of whether the paragraph style already makes it bold.
        if heading_text is not None:
            _render_markdown_into(heading_p, f"**{heading_text}**")
        # Append new body paragraphs with markdown runs + \n → <w:br/>
        anchor = heading_p
        for line in body_items:
            anchor = _append_empty_like(anchor, style_src=body_style_src)
            _render_markdown_into(anchor, line)
        doc.save(str(src))
        return {"ok": True, "action": "replace",
                "heading_replaced": heading_text is not None,
                "body_paragraphs_written": len(body_items)}

    return {"error": f"Unknown action: {action}"}

# ─────────────────────────────────────────────────────────────────────────────
# Tool: insert
# ─────────────────────────────────────────────────────────────────────────────

_OPEN_QUOTES  = '"“‘'
_CLOSE_QUOTES = '"”’'

def _strip_outer_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in _OPEN_QUOTES and s[-1] in _CLOSE_QUOTES:
        return s[1:-1].strip()
    return s

def _space_join(a: str, b: str) -> str:
    a, b = (a or ''), (b or '')
    if not a: return b
    if not b: return a
    return a.rstrip() + ('' if a[-1].isspace() or b[0].isspace() else ' ') + b.lstrip()

def cmd_insert(job_dir: Path, payload: dict) -> dict:
    src = _require_docx(job_dir)
    required = ["after_day", "heading", "verse_text", "verse_ref",
                "paragraph1_before_bold", "bold_sentence", "paragraph1_after_bold",
                "paragraph2", "prayer"]
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return {"error": f"Missing fields: {', '.join(missing)}"}
    if not DAY_HEADING_RE.match(payload["heading"]):
        return {"error": "heading must look like 'September 12 : Title'"}

    nd = _normalize_day(payload["after_day"])
    if nd is None:
        return {"error": f"Bad after_day: {payload['after_day']!r}"}
    month, day_num = nd
    first, last = _entry_range_in_docx(src, month, day_num)

    # Safety: refuse to insert a duplicate day
    m_new = DAY_HEADING_RE.match(payload["heading"])
    new_month, new_day = m_new.group(1), int(m_new.group(2))
    try:
        _entry_range_in_docx(src, new_month, new_day)
        return {"error": f"Day already exists in this book: {new_month} {new_day}. "
                         f"Delete or replace it instead of inserting a duplicate."}
    except RuntimeError:
        pass  # not found, safe to insert

    from docx import Document
    doc = Document(str(src))
    prev_heading_p   = doc.paragraphs[first]
    prev_body_paras  = list(doc.paragraphs)[first + 1:last + 1]
    prev_body_p      = prev_body_paras[0] if prev_body_paras else prev_heading_p
    num_prev_body    = len(prev_body_paras)
    anchor           = doc.paragraphs[last]

    # Heading — clone the preceding heading's paragraph style AND wrap in **
    # so the run itself is bold (get_entry shows existing headings as **…**).
    heading_p = _append_empty_like(anchor, style_src=prev_heading_p)
    _render_markdown_into(heading_p, f"**{payload['heading']}**")

    # Verse: strip any outer quote the caller already added, then add one pair
    verse_clean = _strip_outer_quotes(payload["verse_text"])
    verse_line  = f'*"{verse_clean}" — {payload["verse_ref"]} (WEB)*'

    # Paragraph 1: join before + **bold** + after with real single spaces
    p1 = _space_join(
            _space_join(payload["paragraph1_before_bold"],
                        '**' + payload["bold_sentence"].strip() + '**'),
            payload["paragraph1_after_bold"])

    p2         = payload["paragraph2"].strip()
    prayer     = payload["prayer"].strip()
    prayer_hdr = "**The Prayer of the Day**"

    # Match the preceding entry's body structure: if it was one paragraph with
    # inline <w:br/> breaks, emit one paragraph using the book's actual spacing
    # (leading \n, \n\n between sections, single \n before the prayer sentence).
    # If it was many separate paragraphs, emit one paragraph per section.
    if num_prev_body <= 1:
        body_md = ("\n" + verse_line
                   + "\n\n" + p1
                   + "\n\n" + p2
                   + "\n\n" + prayer_hdr
                   + "\n" + prayer)
        body_p = _append_empty_like(heading_p, style_src=prev_body_p)
        _render_markdown_into(body_p, body_md)
    else:
        sections = [verse_line, p1, p2, prayer_hdr, prayer]
        a = heading_p
        for s in sections:
            a = _append_empty_like(a, style_src=prev_body_p)
            _render_markdown_into(a, s)

    doc.save(str(src))
    return {"ok": True, "inserted_after": f"{month} {day_num}",
            "new_heading": payload["heading"],
            "body_format": "single-paragraph" if num_prev_body <= 1 else "multi-paragraph"}

# ─────────────────────────────────────────────────────────────────────────────
# Tool: rebuild
# ─────────────────────────────────────────────────────────────────────────────

def cmd_rebuild(job_dir: Path) -> dict:
    src = job_dir / "source.docx"
    settings_path = job_dir / "job.json"
    if not src.is_file():
        return {"error": "No source.docx; can't rebuild. Re-run the book via generate_book."}
    if not settings_path.is_file():
        return {"error": "No job.json; can't rebuild (this job predates settings storage)."}
    settings = json.loads(settings_path.read_text())

    out_dir = job_dir / "output"
    out_dir.mkdir(exist_ok=True)

    # Back up existing outputs
    ts = time.strftime("%Y%m%d-%H%M%S")
    backed = []
    for f in list(out_dir.iterdir()):
        if f.is_file() and (f.suffix.lower() in (".pdf", ".epub")):
            bak = f.with_name(f.name + f".bak-{ts}")
            shutil.copy(f, bak)
            backed.append(bak.name)

    cmd = [
        "/usr/bin/python3", str(GENERATOR),
        "--input", str(src),
        "--title", settings["title"],
        "--author", settings["author"],
        "--out-dir", str(out_dir),
        "--bonus-type", settings.get("bonus_type") or "prayer",
    ]
    if settings.get("subtitle"):
        cmd += ["--subtitle", settings["subtitle"]]
    if not settings.get("include_toc"):
        cmd += ["--no-toc"]
    if settings.get("para_spacing"):
        cmd += ["--para-spacing", str(int(settings["para_spacing"]))]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        return {"error": f"Rebuild failed (exit {r.returncode})",
                "log_tail": "\n".join(r.stdout.splitlines()[-30:])}
    # Refresh status.json so download links stay fresh
    status_path = job_dir / "status.json"
    try:
        status = json.loads(status_path.read_text()) if status_path.is_file() else {}
    except Exception:
        status = {}
    status.setdefault("status", "done")
    status["rebuilt_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2))
    return {"ok": True, "backed_up": backed}

# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────

def _die(msg: str, code: int = 2):
    print(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(code)

def main():
    if len(sys.argv) < 2:
        _die("usage: mcp_tools.py <subcommand> ...")
    sub = sys.argv[1]
    try:
        if sub == "qc":
            print(json.dumps(cmd_qc(Path(sys.argv[2])), ensure_ascii=False))
        elif sub == "entry":
            print(json.dumps(cmd_entry(Path(sys.argv[2]), sys.argv[3]), ensure_ascii=False))
        elif sub == "repair":
            payload = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
            print(json.dumps(cmd_repair(Path(sys.argv[2]), sys.argv[3], sys.argv[4], payload),
                             ensure_ascii=False))
        elif sub == "insert":
            payload = json.loads(Path(sys.argv[3]).read_text())
            print(json.dumps(cmd_insert(Path(sys.argv[2]), payload), ensure_ascii=False))
        elif sub == "rebuild":
            print(json.dumps(cmd_rebuild(Path(sys.argv[2])), ensure_ascii=False))
        else:
            _die(f"unknown subcommand: {sub}")
    except Exception as e:
        _die(f"{type(e).__name__}: {e}", code=0)  # still exit 0 so PHP sees the JSON

if __name__ == "__main__":
    main()
