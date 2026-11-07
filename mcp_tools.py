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
    order_errors = []
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
            "Re-run the book via generate_book to enable edits."
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
    # python-docx doesn't expose a clean remove; use the underlying XML.
    for p in list(doc.paragraphs)[first_idx:last_idx + 1]:
        p._element.getparent().remove(p._element)
    doc.save(str(src))

def _append_paragraph_after(doc_para, text: str, bold: bool = False, italic: bool = False):
    """Insert a new paragraph after doc_para with a single run."""
    from docx.oxml.ns import qn
    from copy import deepcopy
    new_p = deepcopy(doc_para._element)
    # Strip children (keep pPr for style inheritance)
    for child in list(new_p):
        if child.tag != qn('w:pPr'):
            new_p.remove(child)
    doc_para._element.addnext(new_p)
    from docx.text.paragraph import Paragraph
    para = Paragraph(new_p, doc_para._parent)
    run = para.add_run(text)
    run.bold = bool(bold)
    run.italic = bool(italic)
    return para

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
        # Find first paragraph inside the entry that contains a junk marker;
        # keep everything before it (the first clean copy). If no junk, no-op.
        from docx import Document
        doc = Document(str(src))
        cut_at = None
        for i in range(first, last + 1):
            if _find_junk(doc.paragraphs[i].text or ""):
                cut_at = i
                break
        if cut_at is None:
            return {"ok": True, "action": "truncate_junk", "note": "no junk found"}
        _remove_paragraphs(src, cut_at, last)
        return {"ok": True, "action": "truncate_junk",
                "paragraphs_removed": last - cut_at + 1,
                "kept_through_paragraph": cut_at - 1}

    if action == "replace":
        content = payload.get("content")
        if not isinstance(content, list) or not all(isinstance(x, str) for x in content):
            return {"error": "replace needs payload.content = list[str] (one string per paragraph)"}
        from docx import Document
        doc = Document(str(src))
        heading_p = doc.paragraphs[first]
        # Remove current body (keep heading paragraph, replace its text too if needed)
        for p in list(doc.paragraphs)[first + 1:last + 1]:
            p._element.getparent().remove(p._element)
        # Replace heading text if first line looks like a heading
        if content and DAY_HEADING_RE.match(content[0]):
            for r in list(heading_p.runs):
                r.text = ""
            heading_p.runs[0].text = content[0] if heading_p.runs else None
            if not heading_p.runs:
                heading_p.add_run(content[0])
            body_lines = content[1:]
        else:
            body_lines = content
        anchor = heading_p
        for line in body_lines:
            anchor = _append_paragraph_after(anchor, line)
        doc.save(str(src))
        return {"ok": True, "action": "replace", "paragraphs_written": len(content)}

    return {"error": f"Unknown action: {action}"}

# ─────────────────────────────────────────────────────────────────────────────
# Tool: insert
# ─────────────────────────────────────────────────────────────────────────────

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
    _, last = _entry_range_in_docx(src, month, day_num)

    from docx import Document
    doc = Document(str(src))
    anchor = doc.paragraphs[last]
    # Heading
    anchor = _append_paragraph_after(anchor, payload["heading"], bold=True)
    # Verse (italic) + reference
    verse = f'"{payload["verse_text"]}" — {payload["verse_ref"]} (WEB)'
    anchor = _append_paragraph_after(anchor, verse, italic=True)
    # Paragraph 1: before bold / bold / after bold → single paragraph with three runs
    anchor = _append_paragraph_after(anchor, payload["paragraph1_before_bold"])
    # Add bold + after-bold to the same paragraph
    anchor.add_run(payload["bold_sentence"]).bold = True
    anchor.add_run(payload["paragraph1_after_bold"])
    # Paragraph 2
    anchor = _append_paragraph_after(anchor, payload["paragraph2"])
    # Prayer heading
    anchor = _append_paragraph_after(anchor, "The Prayer of the Day", bold=True)
    # Prayer sentence
    _append_paragraph_after(anchor, payload["prayer"])
    doc.save(str(src))
    return {"ok": True, "inserted_after": f"{month} {day_num}", "new_heading": payload["heading"]}

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
