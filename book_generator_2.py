from __future__ import annotations
"""
Book Generator — Atticus-style output
======================================
Converts a .docx or .doc manuscript into:
  • An EPUB 3.0  matching the Atticus formatter structure
    (Aldrich + Alegreya fonts, chapter title cards with decoration, EPUB3 nav + NCX)
  • A KDP-ready paperback PDF  (6×9 in, mirror margins, Alegreya headings,
    TOC with page numbers, drop caps, running page numbers, chapter decoration)

Usage:
    python3 book_generator.py \
        --input   "manuscript.docx" \
        --title   "My Book Title"   \
        --subtitle "A Great Subtitle" \
        --author  "Your Name"

Optional:
    --out-dir      ./output
    --fonts-dir    ./fonts       (folder with Aldrich-Regular.ttf + Alegreya-Regular.ttf)
    --decoration   ./leaf.png    (image shown above every chapter title)
    --bonus-json   ./bonus.json  (JSON file describing the bonus page)

Bonus page JSON format:
    {
        "title":      "Keep the Blessings Flowing Every Day",
        "paragraphs": [
            "Thank you so much for choosing this book!",
            "Because you are already investing in your spiritual journey..."
        ],
        "cta":  "Claim your FREE bonus of Daily Prayers & Devotionals at",
        "url":  "https://www.blessingflow.com/daily",
        "closing": "We are so grateful to walk alongside you..."
    }
"""

import argparse, json, os, re, subprocess, sys, tempfile, uuid, zipfile
from datetime import date
from pathlib import Path

from docx import Document as DocxDocument
from docx.oxml.ns import qn as _qn
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    HRFlowable, NextPageTemplate, PageBreak,
    Paragraph, Spacer, Table, TableStyle, Image,
    Flowable,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Page geometry (KDP 6×9) ──────────────────────────────────────────────────
PAGE_W  = 6   * inch
PAGE_H  = 9   * inch
T_MAR   = 0.892 * inch
B_MAR   = 0.449 * inch
L_INNER = 0.875 * inch   # inside margin (spine side)
L_OUTER = 0.500 * inch   # outside margin
SCRIPT_DIR = Path(__file__).parent

# Decoration display size in PDF (width; height scales proportionally)
DECOR_PDF_WIDTH = 2.2 * inch


# ══════════════════════════════════════════════════════════════════════════════
# 0.  FONT REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

def register_fonts(fonts_dir: str | None = None) -> tuple[str, str, str, str]:
    fdir = Path(fonts_dir) if fonts_dir else SCRIPT_DIR

    # Search paths for each required font (custom dir first, then system locations)
    _SEARCH = [
        fdir,
        # macOS
        Path.home() / "Library/Fonts/BookFonts",
        Path.home() / "Library/Fonts/EBGaramond",
        # Linux (user-level)
        Path.home() / ".local/share/fonts/BookFonts",
        Path.home() / ".local/share/fonts",
        # Linux (system-level)
        Path("/usr/local/share/fonts/BookFonts"),
        Path("/usr/share/fonts/BookFonts"),
    ]

    def _find(name: str) -> Path | None:
        for d in _SEARCH:
            p = d / name
            if p.exists():
                return p
        return None

    aldrich_path         = _find("Aldrich-Regular.ttf")
    alegreya_path        = _find("Alegreya-Regular.ttf")
    alegreya_italic_path = _find("Alegreya-Italic.ttf")
    ebg_path             = _find("EBGaramond-Regular.ttf")
    ebg_italic_path      = _find("EBGaramond-Italic.ttf")
    ebg_bold_path        = _find("EBGaramond-Bold.ttf")

    if aldrich_path and alegreya_path and ebg_path:
        pdfmetrics.registerFont(TTFont("Aldrich",       str(aldrich_path)))
        pdfmetrics.registerFont(TTFont("Alegreya",      str(alegreya_path)))
        pdfmetrics.registerFont(TTFont("EBGaramond",    str(ebg_path)))
        italic_body = "EBGaramond-Italic" if ebg_italic_path else "EBGaramond"
        bold_body   = "EBGaramond-Bold"   if ebg_bold_path   else "EBGaramond"
        if ebg_italic_path:
            pdfmetrics.registerFont(TTFont("EBGaramond-Italic", str(ebg_italic_path)))
        if ebg_bold_path:
            pdfmetrics.registerFont(TTFont("EBGaramond-Bold", str(ebg_bold_path)))
        # Register font family so <b> and <i> markup tags resolve correctly
        pdfmetrics.registerFontFamily(
            "EBGaramond",
            normal=     "EBGaramond",
            bold=        bold_body,
            italic=      italic_body,
            boldItalic=  bold_body,
        )
        return "EBGaramond", "Alegreya", "Aldrich", italic_body, bold_body

    if aldrich_path and alegreya_path:
        pdfmetrics.registerFont(TTFont("Aldrich",  str(aldrich_path)))
        pdfmetrics.registerFont(TTFont("Alegreya", str(alegreya_path)))
        italic_body = "Alegreya-Italic" if alegreya_italic_path else "Alegreya"
        if alegreya_italic_path:
            pdfmetrics.registerFont(TTFont("Alegreya-Italic", str(alegreya_italic_path)))
        pdfmetrics.registerFontFamily(
            "Alegreya",
            normal="Alegreya", bold="Alegreya", italic=italic_body, boldItalic=italic_body,
        )
        return "Alegreya", "Alegreya", "Aldrich", italic_body, "Alegreya"

    # Fall back to system Times New Roman TTF (embeds correctly for KDP).
    # Search common locations across macOS, Windows, and Linux.
    _TNR_CANDIDATES = [
        Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf"),       # macOS
        Path("/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf"),
        Path("/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"),
        Path("C:/Windows/Fonts/times.ttf"),                                    # Windows
        Path("C:/Windows/Fonts/timesi.ttf"),
        Path("C:/Windows/Fonts/timesbd.ttf"),
        Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf"),   # Linux
        Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Italic.ttf"),
        Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Bold.ttf"),
    ]
    tnr        = next((p for p in _TNR_CANDIDATES if "Italic" not in p.name and "Bold" not in p.name and p.exists()), None)
    tnr_italic = next((p for p in _TNR_CANDIDATES if "Italic" in p.name and p.exists()), None)
    tnr_bold   = next((p for p in _TNR_CANDIDATES if "Bold" in p.name and "Italic" not in p.name and p.exists()), None)

    if tnr and tnr_italic and tnr_bold:
        pdfmetrics.registerFont(TTFont("TimesNR",        str(tnr)))
        pdfmetrics.registerFont(TTFont("TimesNR-Italic", str(tnr_italic)))
        pdfmetrics.registerFont(TTFont("TimesNR-Bold",   str(tnr_bold)))
        pdfmetrics.registerFontFamily(
            "TimesNR",
            normal="TimesNR", bold="TimesNR-Bold", italic="TimesNR-Italic", boldItalic="TimesNR-Italic",
        )
        print(f"  i  Custom fonts not found in {fdir} — using Times New Roman TTF fallback")
        return "TimesNR", "TimesNR-Bold", "TimesNR", "TimesNR-Italic", "TimesNR-Bold"

    print(f"  i  Custom fonts not found in {fdir} — using built-in Times-Roman fallback")
    return "Times-Roman", "Times-Bold", "Times-Roman", "Times-Italic", "Times-Bold"


# ══════════════════════════════════════════════════════════════════════════════
# 1.  DOCX LOADING
# ══════════════════════════════════════════════════════════════════════════════

def convert_doc_to_docx(doc_path: str) -> str:
    tmp = tempfile.mkdtemp()
    try:
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "docx",
             "--outdir", tmp, doc_path],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        sys.exit(f"LibreOffice failed: {e.stderr.decode()}")
    base = os.path.splitext(os.path.basename(doc_path))[0]
    out  = os.path.join(tmp, base + ".docx")
    if not os.path.exists(out):
        sys.exit("Converted .docx not found after LibreOffice conversion.")
    return out


def load_docx(path: str) -> DocxDocument:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".doc":
        print("  i  Legacy .doc — converting via LibreOffice …")
        path = convert_doc_to_docx(path)
    elif ext != ".docx":
        sys.exit(f"Unsupported type '{ext}'. Use .docx or .doc")
    return DocxDocument(path)


# ══════════════════════════════════════════════════════════════════════════════
# 2.  CHAPTER SEGMENTATION
# ══════════════════════════════════════════════════════════════════════════════

# Only Heading1/Title triggers a chapter break; Heading2/3 become subheadings within a chapter
CHAPTER_HEADINGS    = {"heading 1", "title"}
SUBHEADING_STYLES   = {"heading 2", "heading 3"}
HEADING_RE = re.compile(
    r"^(chapter\s+[\divxlcdm]+[:\.\s]|prologue|epilogue|introduction"
    r"|foreword|preface|acknowledgements?|conclusion|afterword|part\s+\d+)",
    re.IGNORECASE,
)


def is_chapter_heading(para) -> bool:
    style = (para.style.name or "").lower().strip()
    if style in CHAPTER_HEADINGS:
        return True
    t = para.text.strip()
    return len(t) < 120 and bool(HEADING_RE.match(t))


def is_subheading(para) -> bool:
    return (para.style.name or "").lower().strip() in SUBHEADING_STYLES


def _clean_text(text: str) -> str:
    """Decode artefacts that n8n/JSON generation leaves in docx content."""
    # Decode literal \uXXXX unicode escapes (e.g. \u2014 → —, \u2019 → ')
    import re as _re
    text = _re.sub(r'\\u([0-9a-fA-F]{4})',
                   lambda m: chr(int(m.group(1), 16)), text)
    # Strip backslash-escaped quotes: \" → "
    text = text.replace('\\"', '"')
    # Strip backslash-escaped apostrophes: \' → '
    text = text.replace("\\'", "'")
    return text


def _para_default_italic(para) -> bool:
    """Return True if runs with italic=None in this paragraph should be considered italic.

    run.italic == None means 'inherited' — the actual value comes from three places
    (in priority order):
      1. The paragraph's own rPr (pPr/rPr/w:i) — paragraph-mark character formatting
      2. The paragraph's character (run) style chain
      3. The paragraph's style font chain

    All three are checked here so style-based italic (e.g. Word's 'Quote', 'Emphasis'
    character style, or direct paragraph-mark formatting) is detected correctly.
    """
    # 1. Paragraph-mark rPr (direct XML: <w:pPr><w:rPr><w:i/> …)
    try:
        pPr = para._element.pPr
        if pPr is not None:
            rPr = pPr.rPr
            if rPr is not None:
                i_elem = rPr.find(_qn('w:i'))
                if i_elem is not None:
                    val = i_elem.get(_qn('w:val'), 'true')
                    if val.lower() not in ('false', '0', 'off'):
                        return True
    except Exception:
        pass

    # 2. Paragraph style font chain
    try:
        style = para.style
        while style:
            if style.font.italic is True:
                return True
            if style.font.italic is False:
                return False
            style = getattr(style, 'base_style', None)
    except Exception:
        pass

    return False


def _run_is_italic(run, para_default: bool) -> bool:
    """Resolve the effective italic for a run, following the full inheritance chain.

    Priority: explicit run formatting → character style chain → paragraph default.
    """
    if run.italic is True:
        return True
    if run.italic is False:
        return False
    # None = inherited. Check the run's character style first.
    try:
        style = run.style  # character style applied to this run
        while style:
            if style.font.italic is True:
                return True
            if style.font.italic is False:
                return False
            style = getattr(style, 'base_style', None)
    except Exception:
        pass
    return para_default


def _para_info(para) -> dict:
    """Return a dict with text and formatting flags for a paragraph."""
    text = _clean_text(para.text.strip())
    runs = [r for r in para.runs if r.text.strip()]
    para_italic = _para_default_italic(para)
    total_chars  = sum(len(r.text) for r in runs)
    italic_chars = sum(len(r.text) for r in runs if _run_is_italic(r, para_italic))
    bold_chars   = sum(len(r.text) for r in runs if r.bold is True)
    # A paragraph is italic if:
    #   - majority (>50%) of chars are italic, OR
    #   - the first run is italic (handles quote+attribution: "Quote." — Author, Source
    #     where the quote is italic but the non-italic attribution makes majority < 50%)
    first_run_italic = _run_is_italic(runs[0], para_italic) if runs else False
    italic = bool(runs) and (
        first_run_italic
        or (total_chars > 0 and italic_chars / total_chars > 0.5)
    )
    bold   = bool(runs) and total_chars > 0 and (bold_chars / total_chars) > 0.5
    subheading = is_subheading(para) or bold or bool(text and _SUBHEAD_LABELS.match(text))
    return {"text": text, "italic": italic, "bold": bold, "subheading": subheading}


def _expand_para(para) -> list[dict]:
    """Split a paragraph that uses \\n as paragraph separators (n8n single-paragraph format)
    into multiple virtual para_info dicts. Falls back to a single-item list for normal paragraphs."""
    has_newlines = any('\n' in r.text for r in para.runs)
    if not has_newlines:
        return [_para_info(para)]

    # Build segments split at newline run boundaries.
    # Resolve effective italic per run (handles style/character-style inheritance).
    para_italic = _para_default_italic(para)
    segments: list[list[tuple]] = []
    current: list[tuple] = []
    for run in para.runs:
        eff_italic = _run_is_italic(run, para_italic)
        parts = run.text.split('\n')
        for i, part in enumerate(parts):
            if part:
                current.append((part, run.bold, eff_italic))
            if i < len(parts) - 1:
                segments.append(current)
                current = []
    if current:
        segments.append(current)

    raw = []
    for seg in segments:
        text = _clean_text(''.join(t for t, b, it in seg).strip())
        if not text:
            continue
        raw_italic = False
        if text.startswith('*'):
            text = text.lstrip('*').rstrip('*').strip()
            raw_italic = True
            seg = [(t, b, True) for t, b, it in seg]
        total_chars   = sum(len(t) for t, b, it in seg if t.strip())
        italic_chars  = sum(len(t) for t, b, it in seg if it is True and t.strip())
        bold_chars    = sum(len(t) for t, b, it in seg if b  is True and t.strip())
        # Italic if: asterisk-marked, majority italic, OR first run is italic
        # (last case handles "Quote." — Author where attribution is non-italic)
        first_seg_italic = next((it for t, b, it in seg if t.strip()), False)
        italic = raw_italic or first_seg_italic or (total_chars > 0 and italic_chars / total_chars > 0.5)
        bold   = total_chars > 0 and bold_chars / total_chars > 0.5
        subheading = bold or bool(text and _SUBHEAD_LABELS.match(text))
        # Build inline markup for body paragraphs to preserve per-run bold within a line
        inline_parts = []
        for chunk, b, it in seg:
            esc = chunk.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            if b and not italic:   # don't add <b> inside an all-italic paragraph
                esc = f"<b>{esc}</b>"
            inline_parts.append(esc)
        markup = "".join(inline_parts)
        raw.append({"text": text, "markup": markup, "italic": italic, "bold": bold, "subheading": subheading})

    # Merge consecutive segments with the same formatting so multi-line verses
    # (e.g. poetry split by \n) become one paragraph with <br/> line breaks
    results = []
    for item in raw:
        if (results
                and results[-1]["italic"]     == item["italic"]
                and results[-1]["bold"]       == item["bold"]
                and results[-1]["subheading"] == item["subheading"]):
            results[-1]["text"]   += "\n" + item["text"]
            results[-1]["markup"] += "<br/>" + item["markup"]
        else:
            results.append(item)

    return results if results else [_para_info(para)]


def extract_chapters(doc: DocxDocument) -> list[dict]:
    chapters, current = [], {"title": "Introduction", "paragraphs": []}
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if is_chapter_heading(para):
            if current["paragraphs"]:
                chapters.append(current)
            current = {"title": text, "paragraphs": []}
        else:
            current["paragraphs"].extend(_expand_para(para))
    if current["paragraphs"] or not chapters:
        chapters.append(current)
    return chapters or [{"title": "Content", "paragraphs": []}]


# ══════════════════════════════════════════════════════════════════════════════
# 3.  EPUB 3.0  (Atticus-compatible structure)
# ══════════════════════════════════════════════════════════════════════════════

def _xe(t: str) -> str:
    return (t.replace("&","&amp;").replace("<","&lt;")
             .replace(">","&gt;").replace('"',"&quot;"))

def _xe_br(t: str) -> str:
    """Escape XML and convert newlines to <br/> for multi-line verses."""
    return _xe(t).replace("\n", "<br/>")


# Short standalone labels used as in-chapter section headers (text-based detection)
_SUBHEAD_LABELS = re.compile(
    r"^(the (daily practice|prayer of the day|daily reflection|weekly challenge"
    r"|key takeaway|action step|journal prompt|reflection|application"
    r"|practice|meditation|affirmation)|today'?s practice|today'?s prayer"
    r"|daily challenge|stoic exercise|stoic practice)$",
    re.IGNORECASE,
)

# Default bonus page content (shown after copyright by default)
DEFAULT_BONUS = {
    "title": "Keep the Blessings Flowing Every Day",
    "paragraphs": [
        "Thank you so much for choosing this book!",
        "Because you are already investing in your spiritual journey, we want to offer you an "
        "exclusive bonus that pairs perfectly with your reading.",
        "As a special thank-you gift, we will send a beautiful, guided prayer and devotional "
        "directly to your inbox every single morning.",
        "This way, you will never miss a day of reflection and will always have fresh inspiration "
        "to start your morning right, long after you finish these pages.",
    ],
    "cta":     "Claim your FREE bonus of Daily Prayers & Devotionals at",
    "url":     "https://www.blessingflow.com/daily",
    "closing": "We are so grateful to walk alongside you, and we pray this extra gift brings "
               "even more abundant grace and peace to your daily life!",
}

THEME_ID = "th-" + str(uuid.uuid4())

EPUB_CSS = f"""
@font-face {{
  font-family: AldrichRegular;
  src: url("fonts/Aldrich-Regular.ttf");
}}
@font-face {{
  font-family: AlegreyaRegular;
  src: url("fonts/Alegreya-Regular.ttf");
}}

.{THEME_ID} html, .{THEME_ID} body, .{THEME_ID} div, .{THEME_ID} span,
.{THEME_ID} h1, .{THEME_ID} h2, .{THEME_ID} h3, .{THEME_ID} h4,
.{THEME_ID} p, .{THEME_ID} blockquote, .{THEME_ID} a,
.{THEME_ID} ul, .{THEME_ID} ol, .{THEME_ID} li {{
  margin: 0; margin-block: 0; padding: 0; border: 0;
  font-size: 100%; font: inherit;
}}
.{THEME_ID} body {{ line-height: 1; }}
.{THEME_ID} b {{ font-weight: bold; }}
.{THEME_ID} em, .{THEME_ID} i {{ font-style: italic; }}

.{THEME_ID} .wrapper {{
  overflow-wrap: break-word;
  hyphens: auto;
  text-align: justify;
}}

.{THEME_ID} p {{
  orphans: 2; widows: 2;
  padding-bottom: 0em; margin-top: 0em; padding-top: 0em;
  line-height: 1.6em;
  text-indent: 0cm !important;
  margin-block-end: 1.6em;
  font-family: AlegreyaRegular;
  font-size: 1em;
}}

.{THEME_ID} h1 {{
  font-size: 1.3em;
  padding: 0.6em 0em;
  font-family: AlegreyaRegular;
}}

.{THEME_ID} h2 {{
  font-size: 1.2em;
  font-family: AlegreyaRegular;
  font-weight: 600;
  padding: 0.6em 0em;
}}

.{THEME_ID} h3 {{
  font-size: 1.1em;
  font-family: AlegreyaRegular;
  font-weight: 600;
  padding: 0.6em 0em;
}}

/* Title page */
.{THEME_ID} .title {{
  display: flex; flex-direction: column;
  justify-content: space-between; align-items: center;
  text-align: center; height: 100vh;
}}
.{THEME_ID} .title-card {{
  position: relative; top: 0; width: 100%;
  text-align: center; padding: 2rem 0.4rem;
}}
.{THEME_ID} .title-card h1 {{
  font-size: 44px; text-align: inherit;
  padding: 0.6em 0em; font-family: AlegreyaRegular;
}}
.{THEME_ID} .title-card h2 {{
  font-size: 22px; text-align: inherit;
  padding: 1em 0em; font-weight: normal;
  font-family: AlegreyaRegular;
}}
.{THEME_ID} .title-card h3 {{
  font-size: 20px; text-align: inherit;
  padding: 1em 0em; font-family: AlegreyaRegular;
}}
.{THEME_ID} .publisher-details {{
  position: absolute; bottom: 16px;
  display: flex; flex-direction: column;
  justify-content: center; align-items: center;
  width: 100%; padding: 2rem 1.6rem;
  text-transform: capitalize; font-family: AldrichRegular;
}}

/* Chapter title card */
.{THEME_ID} .chapter-title-card {{
  display: flex; flex-direction: column;
  position: relative; width: 100%;
  min-height: 15em; justify-content: center;
  padding-top: 12px; padding-bottom: 12px;
}}

/* Chapter decoration image */
.{THEME_ID} .chapter-title-card .chp_img {{
  order: 1;
  text-align: center;
  padding-top: 0.3em;
  z-index: 10;
}}
.{THEME_ID} .chapter-title-card .chp_img img {{
  width: 50%;
}}

.{THEME_ID} .chapter-number {{
  text-align: left !important; order: 2;
  text-transform: capitalize; z-index: 10;
}}
.{THEME_ID} .chapter-number span {{
  display: inline-block; font-family: AldrichRegular;
  font-size: 15px; text-align: left; width: 100%;
}}
.{THEME_ID} .chapter-title {{
  text-align: center !important; order: 3; z-index: 10;
}}
.{THEME_ID} .chapter-title h2 {{
  display: inline-block; font-family: AlegreyaRegular !important;
  font-size: 21px; text-align: center !important;
  width: 100%; font-weight: 400;
}}

/* Chapter body */
.{THEME_ID} .chapter-body {{ }}
.{THEME_ID} blockquote {{
  line-height: 1.6em;
  padding-left: 10%; padding-right: 10%;
  margin-top: 2rem; margin-bottom: 2rem;
  orphans: 3; widows: 3;
}}

/* Copyright */
.{THEME_ID} .copyrights {{ font-size: 0.75rem; }}
.{THEME_ID} .copyrights p {{
  text-indent: 0em !important; margin-bottom: 0.8em;
}}

/* TOC */
.{THEME_ID} .epub-toc-title-card h2 {{
  text-align: center; font-family: AlegreyaRegular;
  font-size: 21px; display: inline-block; width: 100%;
}}
.{THEME_ID} .toc-block {{ list-style: none; padding: 0; margin-left: 0; }}
.{THEME_ID} .toc-entry {{ line-height: 1.6rem; }}
.{THEME_ID} a {{ text-decoration: none; color: inherit; }}

/* Bonus / centered pages */
.{THEME_ID} .align-center {{ text-align: center; }}
.{THEME_ID} .align-center p {{ text-align: center; }}

/* Dedication */
.{THEME_ID} .dedication {{
  padding-top: 33.33%; text-align: center;
  display: flex; justify-content: center;
}}
"""


def _xhtml_page(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"'
        ' xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">\n'
        '<head>\n'
        '  <meta charset="UTF-8"/>\n'
        f'  <title>{_xe(title)}</title>\n'
        '  <link rel="stylesheet" type="text/css" href="style.css"/>\n'
        '</head>\n<body>\n'
        + body +
        '\n</body>\n</html>'
    )


def _wrap(inner: str) -> str:
    return f'  <div class="{THEME_ID}">\n{inner}\n  </div>'


def _chapter_title_card(ch_title: str, decoration_epub_name: str | None) -> str:
    """Build the chapter title card HTML, with decoration if available."""
    decor_html = ""
    if decoration_epub_name:
        decor_html = (
            f'<div class="chp_img chp_clr_all">'
            f'<img src="images/{decoration_epub_name}" alt=""/>'
            f'</div>'
        )
    return (
        '<div class="chapter-title-card">'
        + decor_html +
        '<div class="chapter-title">'
        f'<h2>{_xe(ch_title)}</h2>'
        '</div></div>'
    )


def _build_bonus_page_epub(
    bonus: dict,
    decoration_epub_name: str | None,
) -> str:
    """Build the bonus page XHTML body."""
    title = bonus.get("title", "Special Bonus")
    paragraphs = bonus.get("paragraphs", [])
    cta = bonus.get("cta", "")
    url = bonus.get("url", "")
    closing = bonus.get("closing", "")

    title_card = _chapter_title_card(title, decoration_epub_name)

    paras_html = ""
    for p in paragraphs:
        paras_html += f'<div class="align-center"><p>{_xe(p)}</p></div>\n'

    cta_html = ""
    if cta:
        cta_html = f'<div class="align-center"><h2 id="subhead-1"><b>{_xe(cta)}</b></h2></div>\n'
    if url:
        cta_html += (
            f'<div class="align-center"><h2 id="subhead-2">'
            f'<b><a target="_blank" href="{_xe(url)}">{_xe(url)}</a></b>'
            f'</h2></div>\n'
        )

    closing_html = ""
    if closing:
        closing_html = f'<div class="align-center"><p>{_xe(closing)}</p></div>\n'

    body = _wrap(
        '    <div class="chapter">'
        + title_card +
        '<div class="chapter-body withDropcap">'
        '<div class="wrapper">'
        + paras_html + cta_html + closing_html +
        '</div></div></div>'
    )
    return _xhtml_page(title, body)


def build_epub(
    output_path: str,
    title: str,
    subtitle: str,
    author: str,
    chapters: list[dict],
    fonts_dir: str | None = None,
    decoration_path: str | None = None,
    bonus: dict | None = None,
) -> None:
    book_id = str(uuid.uuid4())
    today   = date.today().isoformat()
    fdir    = Path(fonts_dir) if fonts_dir else SCRIPT_DIR

    # Decoration image setup
    decor_name = None
    decor_bytes = None
    if decoration_path and os.path.isfile(decoration_path):
        decor_ext  = Path(decoration_path).suffix.lower()
        decor_name = f"decoration{decor_ext}"
        with open(decoration_path, "rb") as f:
            decor_bytes = f.read()
        print(f"  i  Decoration image: {decoration_path}")
    else:
        if decoration_path:
            print(f"  !  Decoration not found: {decoration_path} — skipping")

    # ── Title page ────────────────────────────────────────────────────────────
    title_body = _wrap(
        '    <div class="title">\n'
        '      <div class="title-card">\n'
        f'        <h1>{_xe(title)}</h1>\n'
        + (f'        <h3>{_xe(subtitle)}</h3>\n' if subtitle else '') +
        f'        <h2>{_xe(author)}</h2>\n'
        '      </div>\n'
        '      <div class="publisher-details"></div>\n'
        '    </div>'
    )
    title_html = _xhtml_page("Title Page", title_body)

    # ── Bonus page ────────────────────────────────────────────────────────────
    bonus_html = None
    if bonus:
        bonus_html = _build_bonus_page_epub(bonus, decor_name)

    # ── Copyright page ────────────────────────────────────────────────────────
    copy_body = _wrap(
        '    <div class="copyrights">'
        '<div class="chapter-body"><div class="wrapper">'
        f'<p>Copyright &#xA9; {date.today().year} by {_xe(author)}</p>'
        '<p>All rights reserved.</p>'
        '<p>No portion of this book may be reproduced in any form without '
        'written permission from the publisher or author.</p>'
        '</div></div></div>'
    )
    copy_html = _xhtml_page("Copyright", copy_body)

    # ── Chapter pages ─────────────────────────────────────────────────────────
    ch_files: list[tuple[str, str]] = []
    for i, ch in enumerate(chapters):
        fname = f"{uuid.uuid4().hex[:16]}.xhtml"
        para_parts = []
        for p in ch["paragraphs"]:
            t      = _xe_br(p["text"])
            markup = p.get("markup") or t
            if p["subheading"]:
                para_parts.append(f'<h3>{t}</h3>\n')
            elif p["italic"] and p["bold"]:
                para_parts.append(f'<p><em><b>{t}</b></em></p>\n')
            elif p["italic"]:
                para_parts.append(f'<p><em>{t}</em></p>\n')
            elif p["bold"]:
                para_parts.append(f'<p><b>{t}</b></p>\n')
            else:
                para_parts.append(f'<p>{markup}</p>\n')
        paras = "".join(para_parts)
        title_card = _chapter_title_card(ch["title"], decor_name)
        body  = _wrap(
            '    <div class="chapter">'
            + title_card +
            '<div class="chapter-body withDropcap">'
            f'<div class="wrapper">{paras}</div>'
            '</div></div>'
        )
        ch_files.append((fname, _xhtml_page(ch["title"], body)))

    # ── TOC page (EPUB3 nav) ──────────────────────────────────────────────────
    toc_items = ""
    for i, (fname, _) in enumerate(ch_files):
        toc_items += (
            f'      <li class="toc-entry">'
            f'<a href="{fname}">'
            f'<span class="chapter-num">{i+1}.</span>'
            f' <span>{_xe(chapters[i]["title"])}</span>'
            f'</a></li>\n'
        )
    toc_body = _wrap(
        '    <div class="epub-toc-title-card"><h2>Table Of Content</h2></div>\n'
        '    <nav id="toc" epub:type="toc">\n'
        f'      <ol class="toc-block">\n{toc_items}      </ol>\n'
        '    </nav>'
    )
    toc_html = _xhtml_page("Table Of Content", toc_body)

    # ── OPF ──────────────────────────────────────────────────────────────────
    # Order: title → copyright → bonus → toc → chapters
    all_items = [('title', 'title.xhtml'), ('copyright', 'copyright.xhtml')]
    if bonus_html:
        all_items.append(('bonus', 'bonus.xhtml'))
    all_items.append(('toc', 'toc.xhtml', 'properties="nav"'))
    for i, (fname, _) in enumerate(ch_files):
        all_items.append((f'ch{i}', fname))

    manifest_lines = [
        '        <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '        <item id="css" href="style.css" media-type="text/css"/>',
        '        <item id="font0" href="fonts/Aldrich-Regular.ttf" media-type="application/x-font-ttf"/>',
        '        <item id="font1" href="fonts/Alegreya-Regular.ttf" media-type="application/x-font-ttf"/>',
    ]
    if decor_name:
        ext_to_mime = {".png": "image/png", ".jpg": "image/jpeg",
                       ".jpeg": "image/jpeg", ".gif": "image/gif",
                       ".webp": "image/webp"}
        mime = ext_to_mime.get(Path(decor_name).suffix.lower(), "image/png")
        manifest_lines.append(
            f'        <item id="image_decoration" href="images/{decor_name}"'
            f' media-type="{mime}"/>'
        )

    spine_lines = []
    for item in all_items:
        iid, href = item[0], item[1]
        props = f' {item[2]}' if len(item) > 2 else ''
        manifest_lines.append(
            f'        <item id="{iid}" href="{href}"'
            f' media-type="application/xhtml+xml"{props}/>'
        )
        spine_lines.append(f'        <itemref idref="{iid}"/>')

    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        ' unique-identifier="BookId"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:opf="http://www.idpf.org/2007/opf">\n'
        f'    <dc:identifier id="BookId">{book_id}</dc:identifier>\n'
        f'    <dc:title>{_xe(title)}</dc:title>\n'
        f'    <dc:creator id="creator">{_xe(author)}</dc:creator>\n'
        f'    <dc:description>{_xe(subtitle)}</dc:description>\n'
        '    <dc:language>en</dc:language>\n'
        f'    <dc:date>{today}</dc:date>\n'
        '    <dc:rights>All rights reserved</dc:rights>\n'
        f'    <meta property="dcterms:modified">{today}T00:00:00Z</meta>\n'
        '    <meta name="generator" content="BookGenerator"/>\n'
        '    <meta property="ibooks:specified-fonts">true</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        + "\n".join(manifest_lines) + "\n"
        '  </manifest>\n'
        '  <spine toc="ncx">\n'
        + "\n".join(spine_lines) + "\n"
        '  </spine>\n'
        '</package>'
    )

    # ── NCX ───────────────────────────────────────────────────────────────────
    nav_pts = (
        '    <navPoint id="np_title" playOrder="1">\n'
        '      <navLabel><text>Title</text></navLabel>\n'
        '      <content src="title.xhtml"/>\n'
        '    </navPoint>\n'
        '    <navPoint id="np_copyright" playOrder="2">\n'
        '      <navLabel><text>Copyright</text></navLabel>\n'
        '      <content src="copyright.xhtml"/>\n'
        '    </navPoint>\n'
    )
    order = 3
    if bonus_html:
        bonus_title = bonus.get("title", "Bonus") if bonus else "Bonus"
        nav_pts += (
            f'    <navPoint id="np_bonus" playOrder="{order}">\n'
            f'      <navLabel><text>{_xe(bonus_title)}</text></navLabel>\n'
            '      <content src="bonus.xhtml"/>\n'
            '    </navPoint>\n'
        )
        order += 1
    # TOC as a readable navpoint
    nav_pts += (
        f'    <navPoint id="np_toc" playOrder="{order}">\n'
        '      <navLabel><text>Table Of Content</text></navLabel>\n'
        '      <content src="toc.xhtml"/>\n'
        '    </navPoint>\n'
    )
    order += 1
    for i, (fname, _) in enumerate(ch_files):
        nav_pts += (
            f'    <navPoint id="np{i}" playOrder="{order+i}">\n'
            f'      <navLabel><text>{_xe(chapters[i]["title"])}</text></navLabel>\n'
            f'      <content src="{fname}"/>\n'
            '    </navPoint>\n'
        )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"\n'
        '  "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        '  <head>\n'
        f'    <meta name="dtb:uid" content="{book_id}"/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        '  </head>\n'
        f'  <docTitle><text>{_xe(title)}</text></docTitle>\n'
        f'  <navMap>\n{nav_pts}  </navMap>\n</ncx>'
    )

    # ── Write ZIP ─────────────────────────────────────────────────────────────
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0"'
            ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf"'
            ' media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n</container>')
        zf.writestr("OEBPS/content.opf",    opf)
        zf.writestr("OEBPS/toc.ncx",        ncx)
        zf.writestr("OEBPS/style.css",       EPUB_CSS)
        zf.writestr("OEBPS/title.xhtml",    title_html)
        if bonus_html:
            zf.writestr("OEBPS/bonus.xhtml", bonus_html)
        zf.writestr("OEBPS/copyright.xhtml", copy_html)
        zf.writestr("OEBPS/toc.xhtml",      toc_html)
        for fname, html in ch_files:
            zf.writestr(f"OEBPS/{fname}", html)
        # Decoration image
        if decor_name and decor_bytes:
            zf.writestr(f"OEBPS/images/{decor_name}", decor_bytes)
        # Fonts
        for font_name in ("Aldrich-Regular.ttf", "Alegreya-Regular.ttf"):
            fp = fdir / font_name
            if fp.exists():
                zf.write(str(fp), f"OEBPS/fonts/{font_name}")

    print(f"  OK  EPUB  ->  {output_path}")


# ══════════════════════════════════════════════════════════════════════════════
# 4.  KDP PAPERBACK PDF
# ══════════════════════════════════════════════════════════════════════════════

class _MirrorMarginDoc(BaseDocTemplate):
    """BaseDocTemplate with automatic mirror margins for book printing.

    Odd pages  (right-hand): inner margin on LEFT  (spine side)
    Even pages (left-hand):  inner margin on RIGHT (spine side)
    """

    def __init__(self, filename, on_page_cb=None, **kwargs):
        super().__init__(filename, pagesize=(PAGE_W, PAGE_H),
                         topMargin=T_MAR, bottomMargin=B_MAR,
                         leftMargin=L_INNER, rightMargin=L_OUTER, **kwargs)
        self._on_page_cb = on_page_cb
        usable_w = PAGE_W - L_INNER - L_OUTER
        usable_h = PAGE_H - T_MAR - B_MAR

        self.addPageTemplates([
            PageTemplate(id='Odd',
                frames=[Frame(L_INNER, B_MAR, usable_w, usable_h,
                              leftPadding=0, rightPadding=0,
                              topPadding=0, bottomPadding=0, id='odd')],
                onPage=self._on_page),
            PageTemplate(id='Even',
                frames=[Frame(L_OUTER, B_MAR, usable_w, usable_h,
                              leftPadding=0, rightPadding=0,
                              topPadding=0, bottomPadding=0, id='even')],
                onPage=self._on_page),
        ])

    def _on_page(self, canvas, doc):
        if self._on_page_cb:
            self._on_page_cb(canvas, doc)

    def handle_pageBegin(self):
        # self.page is still the previous count here; after super() it becomes current.
        # Select the correct template before super() applies it.
        next_page = self.page + 1
        self.pageTemplate = self.pageTemplates[0 if next_page % 2 == 1 else 1]
        super().handle_pageBegin()


class _MirrorPageTemplate:
    def __init__(self, title: str, author: str,
                 body_font: str, body_size: float = 10):
        self.title     = title
        self.author    = author
        self.body_font = body_font
        self.body_size = body_size

    def on_page(self, canvas, doc):
        pg = doc.page
        canvas.saveState()
        canvas.setFont(self.body_font, 9)
        canvas.setFillColor(colors.black)
        canvas.drawCentredString(PAGE_W / 2, B_MAR * 0.55, str(pg))
        canvas.restoreState()


def _make_decoration_flowable(decoration_path: str | None) -> list:
    """Return a centered decoration image flowable + spacer, or empty list."""
    if not decoration_path or not os.path.isfile(decoration_path):
        return []
    try:
        _probe = Image(decoration_path)
        ratio  = _probe.imageHeight / _probe.imageWidth
        w = DECOR_PDF_WIDTH
        h = w * ratio
        img = Image(decoration_path, width=w, height=h)
        img.hAlign = "CENTER"
        return [img, Spacer(1, 0.04 * inch)]
    except Exception as e:
        print(f"  !  Could not load decoration for PDF: {e}")
        return []


def build_paperback_pdf(
    output_path: str,
    title: str,
    subtitle: str,
    author: str,
    chapters: list[dict],
    body_font: str   = "Alegreya",
    head_font: str   = "Alegreya",
    label_font: str  = "Aldrich",
    italic_font: str = "Times-Italic",
    bold_body_font: str = "Times-Bold",
    decoration_path: str | None = None,
    bonus: dict | None = None,
    para_spacing: int = 14,
) -> None:

    BODY_SZ  = 10       # EB Garamond 10pt (Atticus setting)
    HEAD_SZ  = 15       # Alegreya 15pt chapter title (Atticus setting)
    SUBH_SZ  = 11       # Subheading size (in-body label, e.g. "The Daily Practice")
    SMALL_SZ = 9
    GREY     = colors.HexColor("#555555")
    LEAD     = 16       # 10pt × 1.6 line spacing (Atticus setting)

    s_body = ParagraphStyle("Body",
        fontName=body_font, fontSize=BODY_SZ, leading=LEAD,
        alignment=TA_JUSTIFY, spaceAfter=para_spacing, spaceBefore=0)
    s_body_italic = ParagraphStyle("BodyItalic",
        fontName=italic_font, fontSize=BODY_SZ, leading=LEAD,
        alignment=TA_JUSTIFY, spaceAfter=para_spacing, spaceBefore=0)
    s_subhead = ParagraphStyle("SubHead",
        fontName=bold_body_font, fontSize=BODY_SZ, leading=LEAD,
        alignment=TA_LEFT, spaceAfter=4, spaceBefore=14)
    s_head = ParagraphStyle("ChHead",
        fontName=head_font, fontSize=HEAD_SZ, leading=20,
        alignment=TA_CENTER, spaceAfter=18, spaceBefore=6)
    s_center = ParagraphStyle("Center",
        fontName=body_font, fontSize=BODY_SZ, leading=LEAD,
        alignment=TA_CENTER, spaceAfter=0, spaceBefore=0)
    s_center_bold = ParagraphStyle("CenterBold",
        fontName=body_font, fontSize=BODY_SZ, leading=LEAD,
        alignment=TA_CENTER, spaceAfter=0, spaceBefore=0)
    s_title_pg = ParagraphStyle("TitlePg",
        fontName=head_font, fontSize=36, leading=44,
        alignment=TA_CENTER, spaceAfter=14)
    s_subtitle_pg = ParagraphStyle("SubtitlePg",
        fontName=head_font, fontSize=15, leading=20,
        alignment=TA_CENTER, spaceAfter=12, textColor=GREY)
    s_author_pg = ParagraphStyle("AuthorPg",
        fontName=head_font, fontSize=13, leading=18,
        alignment=TA_CENTER)
    s_copy = ParagraphStyle("Copy",
        fontName=body_font, fontSize=SMALL_SZ, leading=13,
        alignment=TA_CENTER, textColor=GREY)
    s_toc_title = ParagraphStyle("TocTitle",
        fontName=head_font, fontSize=HEAD_SZ, leading=20,
        alignment=TA_CENTER, spaceAfter=18)
    s_toc_entry = ParagraphStyle("TocEntry",
        fontName=body_font, fontSize=BODY_SZ, leading=20,
        alignment=TA_LEFT)

    cb = _MirrorPageTemplate(title, author, body_font, BODY_SZ)

    from reportlab.platypus import SimpleDocTemplate, Flowable

    class ChapterAnchor(Flowable):
        def __init__(self, ch_title): self.ch_title = ch_title
        def wrap(self, aw, ah): return 0, 0
        def draw(self): pass

    def _chapter_header(ch_title: str) -> list:
        """Decoration + title + space before first paragraph."""
        items = []
        items += _make_decoration_flowable(decoration_path)
        items.append(Paragraph(_xe(ch_title), s_head))
        # No horizontal rule — just breathing room before the opening quote
        items.append(Spacer(1, 0.18 * inch))
        return items

    def _bonus_page_story() -> list:
        """Build the bonus page story elements."""
        if not bonus:
            return []
        items = [Spacer(1, 0.5 * inch)]
        items += _make_decoration_flowable(decoration_path)
        bonus_title = bonus.get("title", "Special Bonus")
        items.append(Paragraph(_xe(bonus_title), s_head))
        items.append(Spacer(1, 0.4 * inch))
        for p in bonus.get("paragraphs", []):
            items.append(Paragraph(_xe(p), s_center))
            items.append(Spacer(1, 0.2 * inch))
        cta = bonus.get("cta", "")
        url = bonus.get("url", "")
        if cta:
            items.append(Spacer(1, 0.15 * inch))
            items.append(Paragraph(f"<b>{_xe(cta)}</b>", s_center_bold))
        if url:
            items.append(Spacer(1, 0.2 * inch))
            items.append(Paragraph(f"<b>{_xe(url)}</b>", s_center_bold))
        closing = bonus.get("closing", "")
        if closing:
            items.append(Spacer(1, 0.4 * inch))
            items.append(Paragraph(_xe(closing), s_center))
        items.append(PageBreak())
        return items

    def make_story_with_anchors():
        story = []
        # Title page
        story.append(Spacer(1, 2*inch))
        story.append(Paragraph(_xe(title), s_title_pg))
        if subtitle:
            story.append(Spacer(1, 0.1*inch))
            story.append(Paragraph(_xe(subtitle), s_subtitle_pg))
        story.append(Spacer(1, 0.4*inch))
        story.append(HRFlowable(width="50%", thickness=0.5, color=GREY, hAlign="CENTER"))
        story.append(Spacer(1, 0.4*inch))
        story.append(Paragraph(f"by {_xe(author)}", s_author_pg))
        story.append(PageBreak())
        # Copyright (before bonus)
        story.append(Spacer(1, 3.5*inch))
        story.append(Paragraph(f"Copyright &#169; {date.today().year} by {_xe(author)}", s_copy))
        story.append(Paragraph("All rights reserved.", s_copy))
        story.append(Paragraph("No portion of this book may be reproduced without written permission.", s_copy))
        story.append(PageBreak())
        # Bonus page (after copyright)
        story += _bonus_page_story()
        for ch in chapters:
            story.append(ChapterAnchor(ch["title"]))
            story += _chapter_header(ch["title"])
            for p in ch["paragraphs"]:
                t      = _xe_br(p["text"])
                markup = p.get("markup") or t
                if p["subheading"]:
                    story.append(Paragraph(t, s_subhead))
                elif p["italic"]:
                    story.append(Paragraph(t, s_body_italic))
                elif p["bold"]:
                    story.append(Paragraph(f'<b>{t}</b>', s_body))
                else:
                    story.append(Paragraph(markup, s_body))
            story.append(PageBreak())
        return story

    def make_story(include_toc_pages: dict | None = None):
        story = []
        # Title page
        story.append(Spacer(1, 2*inch))
        story.append(Paragraph(_xe(title), s_title_pg))
        if subtitle:
            story.append(Spacer(1, 0.1*inch))
            story.append(Paragraph(_xe(subtitle), s_subtitle_pg))
        story.append(Spacer(1, 0.4*inch))
        story.append(HRFlowable(width="50%", thickness=0.5, color=GREY, hAlign="CENTER"))
        story.append(Spacer(1, 0.4*inch))
        story.append(Paragraph(f"by {_xe(author)}", s_author_pg))
        story.append(PageBreak())
        # Copyright (before bonus)
        story.append(Spacer(1, 3.5*inch))
        story.append(Paragraph(f"Copyright &#169; {date.today().year} by {_xe(author)}", s_copy))
        story.append(Paragraph("All rights reserved.", s_copy))
        story.append(Paragraph("No portion of this book may be reproduced in any form "
                                "without written permission from the publisher or author.", s_copy))
        story.append(PageBreak())
        # Bonus page (after copyright)
        story += _bonus_page_story()
        # Chapters (no TOC in paperback)
        for ch in chapters:
            story += _chapter_header(ch["title"])
            for p in ch["paragraphs"]:
                t      = _xe_br(p["text"])
                markup = p.get("markup") or t
                if p["subheading"]:
                    story.append(Paragraph(t, s_subhead))
                elif p["italic"]:
                    story.append(Paragraph(t, s_body_italic))
                elif p["bold"]:
                    story.append(Paragraph(f'<b>{t}</b>', s_body))
                else:
                    story.append(Paragraph(markup, s_body))
            story.append(PageBreak())
        return story

    from io import BytesIO

    class _TrackingDoc2(_MirrorMarginDoc):
        def handle_flowable(self, flowables):
            if flowables and isinstance(flowables[0], ChapterAnchor):
                _chapter_pages[flowables[0].ch_title] = self.page
            super().handle_flowable(flowables)

    _chapter_pages: dict[str, int] = {}
    buf = BytesIO()
    tdoc = _TrackingDoc2(buf)
    tdoc.build(make_story_with_anchors())

    real_doc = _MirrorMarginDoc(output_path, on_page_cb=cb.on_page)
    real_doc.build(make_story(include_toc_pages=_chapter_pages))

    print(f"  OK  PDF   ->  {output_path}")


# ══════════════════════════════════════════════════════════════════════════════
# 5.  CLI
# ══════════════════════════════════════════════════════════════════════════════

# ── URL download helpers ──────────────────────────────────────────────────────

def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "s3://"))


def _gdrive_file_id(url: str):
    import re
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _download_url(url: str) -> str:
    """Download a remote file and return the local temp-file path."""
    import tempfile

    suffix = ".docx"
    # ── Google Docs (docs.google.com/document/d/ID/...) ──────────────────────
    # Export directly as docx — no gdown needed, no permission quirks
    if "docs.google.com/document" in url:
        file_id = _gdrive_file_id(url)
        if not file_id:
            sys.exit(f"Cannot parse Google Docs file ID from: {url}")
        export_url = f"https://docs.google.com/document/d/{file_id}/export?format=docx"
        print(f"  i  Google Docs export URL: {export_url}")
        import requests as _req
        r = _req.get(export_url, stream=True, timeout=120)
        if r.status_code == 403:
            sys.exit("Google Docs file is private. Open the doc → Share → 'Anyone with the link' → Viewer.")
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        for chunk in r.iter_content(32768):
            tmp.write(chunk)
        tmp.close()
        return tmp.name

    # ── Google Drive file (drive.google.com/file/d/ID/...) ───────────────────
    if "drive.google.com" in url:
        file_id = _gdrive_file_id(url)
        if not file_id:
            sys.exit(f"Cannot parse Google Drive file ID from: {url}")
        print(f"  i  Google Drive file ID: {file_id}")
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.close()
        try:
            import gdown
            gdown.download(id=file_id, output=tmp.name, quiet=False, fuzzy=True)
        except ImportError:
            import requests as _req
            sess = _req.Session()
            dl_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            r = sess.get(dl_url, stream=True)
            for k, v in r.cookies.items():
                if k.startswith("download_warning"):
                    dl_url += f"&confirm={v}"
                    r = sess.get(dl_url, stream=True)
                    break
            with open(tmp.name, "wb") as fh:
                for chunk in r.iter_content(32768):
                    fh.write(chunk)
        return tmp.name

    # ── Amazon S3 (s3://bucket/key) ───────────────────────────────────────────
    if url.startswith("s3://"):
        parts = url[5:].split("/", 1)
        bucket, key = parts[0], parts[1] if len(parts) > 1 else ""
        ext = "." + key.rsplit(".", 1)[-1] if "." in key else suffix
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.close()
        try:
            import boto3
            boto3.client("s3").download_file(bucket, key, tmp.name)
        except ImportError:
            sys.exit("boto3 is required for s3:// URLs.  Install: pip install boto3")
        return tmp.name

    # ── Direct HTTP/HTTPS ─────────────────────────────────────────────────────
    import requests
    from urllib.parse import urlparse
    path = urlparse(url).path
    ext  = "." + path.rsplit(".", 1)[-1] if "." in path else suffix
    tmp  = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp.close()
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(tmp.name, "wb") as fh:
        for chunk in r.iter_content(32768):
            fh.write(chunk)
    return tmp.name


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert .docx/.doc manuscript to Atticus-style EPUB3 + KDP PDF")
    parser.add_argument("--input",      required=True,
                        help="Local path OR URL (Google Drive share link, S3 URI, direct HTTPS)")
    parser.add_argument("--title",      required=True)
    parser.add_argument("--subtitle",   default="")
    parser.add_argument("--author",     required=True)
    parser.add_argument("--out-dir",    default=None)
    parser.add_argument("--fonts-dir",  default=None,
                        help="Path to folder with Aldrich-Regular.ttf + Alegreya-Regular.ttf")
    parser.add_argument("--decoration", default=None,
                        help="Path to decoration image (PNG) shown above each chapter title. "
                             "If omitted, looks for 'decoration.png' next to the script.")
    parser.add_argument("--bonus-json", default=None,
                        help="Path to JSON file describing the bonus page (overrides default)")
    parser.add_argument("--para-spacing", type=int, default=14,
                        help="Space after each body paragraph in points (default: 14)")
    parser.add_argument("--no-bonus",  action="store_true",
                        help="Omit the bonus page entirely")
    args = parser.parse_args()

    _tmp_download = None
    input_path = args.input
    if _is_url(input_path):
        print(f"  i  Downloading from URL: {input_path}")
        input_path = _download_url(input_path)
        _tmp_download = input_path
        print(f"  i  Saved to temp: {input_path}")
    elif not os.path.isfile(input_path):
        sys.exit(f"File not found: {input_path}")

    out_dir   = args.out_dir or os.path.dirname(os.path.abspath(input_path))
    fonts_dir = args.fonts_dir or str(SCRIPT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[/\\]", "", args.title)
    safe = re.sub(r"[^\w\-. ]", "_", safe).strip("_").replace(" ", "_")

    # Load bonus page: custom JSON > default content > disabled
    bonus = None
    if args.no_bonus:
        print("  i  Bonus page disabled (--no-bonus)")
    elif args.bonus_json:
        if not os.path.isfile(args.bonus_json):
            print(f"  !  Bonus JSON not found: {args.bonus_json} — using default bonus page")
            bonus = DEFAULT_BONUS
        else:
            with open(args.bonus_json, "r", encoding="utf-8") as f:
                bonus = json.load(f)
            print(f"  i  Bonus page (custom): {bonus.get('title', '(untitled)')}")
    else:
        bonus = DEFAULT_BONUS
        print(f"  i  Bonus page (default): {bonus['title']}")

    # Auto-discover decoration.png next to the script if --decoration not given
    decoration = args.decoration
    if not decoration:
        for _auto in [SCRIPT_DIR / "decoration.png",
                      Path.home() / ".local/share/fonts/BookFonts/decoration.png",
                      Path("/usr/local/share/fonts/BookFonts/decoration.png")]:
            if _auto.exists():
                decoration = str(_auto)
                print(f"  i  Auto-detected decoration: {decoration}")
                break

    body_f, head_f, label_f, italic_f, bold_body_f = register_fonts(fonts_dir)

    print(f"\nReading: {input_path}")
    doc      = load_docx(input_path)
    chapters = extract_chapters(doc)
    total_p  = sum(len(c["paragraphs"]) for c in chapters)
    print(f"  {len(chapters)} chapter(s), {total_p} paragraphs")

    epub_path = os.path.join(out_dir, f"{safe}.epub")
    pdf_path  = os.path.join(out_dir, f"{safe}_paperback.pdf")

    print("\nBuilding EPUB 3.0 (Atticus-style) ...")
    build_epub(epub_path, args.title, args.subtitle, args.author,
               chapters, fonts_dir=fonts_dir,
               decoration_path=decoration, bonus=bonus)

    print("Building KDP Paperback PDF ...")
    build_paperback_pdf(pdf_path, args.title, args.subtitle, args.author,
                        chapters, body_font=body_f, head_font=head_f,
                        label_font=label_f, italic_font=italic_f,
                        bold_body_font=bold_body_f,
                        decoration_path=decoration, bonus=bonus,
                        para_spacing=args.para_spacing)

    print(f"\nDone! Output: {os.path.abspath(out_dir)}")

    # Clean up temp file from URL download
    if _tmp_download and os.path.exists(_tmp_download):
        os.unlink(_tmp_download)


if __name__ == "__main__":
    main()
