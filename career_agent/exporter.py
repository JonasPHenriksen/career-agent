"""Render letters and CVs to PDF (fpdf2) and Word (python-docx)."""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from io import BytesIO
from pathlib import Path

from . import config, settings

FONT_SETS = {
    "sans": ["/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Italic.ttf", "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf"],
    "serif": ["/System/Library/Fonts/Supplemental/Georgia.ttf", "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
              "/System/Library/Fonts/Supplemental/Georgia Italic.ttf", "/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf"],
}
DOCX_FONTS = {"sans": "Arial", "serif": "Georgia"}
MONTHS = {
    "da": ["januar", "februar", "marts", "april", "maj", "juni", "juli", "august", "september", "oktober", "november", "december"],
    "no": ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember"],
    "sv": ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
}
DOC_WORDS = {
    "letter": {"da": "Ansøgning", "no": "Søknad", "sv": "Ansökan", "de": "Bewerbung", "en": "Cover letter"},
    "cv": {"da": "CV", "no": "CV", "sv": "CV", "de": "Lebenslauf", "en": "CV"},
    "att": {"da": "Att.", "no": "Att.", "sv": "Att.", "de": "z. Hd.", "en": "Attn."},
}


def _lang(language: str | None) -> str:
    code = (language or "en")[:2].lower()
    return "no" if code in ("nb", "nn") else code


def letter_date(language: str | None, city: str | None) -> str:
    d = date.today()
    lang = _lang(language)
    months = MONTHS.get(lang, MONTHS["en"])
    if lang == "en":
        txt = f"{d.day} {months[d.month - 1]} {d.year}"
    elif lang == "de":
        txt = f"{d.day}. {months[d.month - 1]} {d.year}"
    else:
        txt = f"{d.day}. {months[d.month - 1]} {d.year}"
    return f"{city}, {txt}" if city else txt


def short_url(url: str) -> str:
    """The address as it reads on paper: no https://, no www., no trailing slash."""
    return re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")


def contact_groups(p: dict) -> list[list[tuple[str, str]]]:
    """(text, link) pairs: how to reach you first, then your links (shown short, but still clickable)."""
    reach = [(p["email"].strip(), f"mailto:{p['email'].strip()}")] if (p.get("email") or "").strip() else []
    reach += [(p[k].strip(), "") for k in ("phone", "city") if (p.get(k) or "").strip()]
    links = []
    for k in ("linkedin", "website"):
        url = (p.get(k) or "").strip()
        if url:
            links.append((short_url(url), url if re.match(r"^[a-z]+:", url, re.I) else f"https://{url}"))
    return [g for g in (reach, links) if g]


def contact_line(p: dict) -> str:
    """For Word: each item's own spaces are non-breaking, so a line only wraps between items."""
    return "  ·  ".join(text.replace(" ", " ") for group in contact_groups(p) for text, _ in group)


ASCII_LETTERS = str.maketrans({"æ": "ae", "Æ": "Ae", "ø": "oe", "Ø": "Oe", "å": "aa", "Å": "Aa",
                               "ä": "ae", "Ä": "Ae", "ö": "oe", "Ö": "Oe", "ü": "ue", "Ü": "Ue", "ß": "ss"})


def _file_part(text: str) -> str:
    """Plain letters and digits, words joined by _ (a hyphen inside a word stays): æ ø å become ae oe aa,
    other accents are dropped, and anything else (spaces, &, dots, slashes) goes."""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFC", text).translate(ASCII_LETTERS))
    return "_".join(re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", text.encode("ascii", "ignore").decode()))


def filename(kind: str, ext: str, company: str | None, language: str | None) -> str:
    """Jonas_Henriksen_CV_Bredana.pdf: no spaces or æøå, so upload forms and applicant systems take it as it is."""
    p = settings.get("profile")
    word = DOC_WORDS[kind].get(_lang(language), DOC_WORDS[kind]["en"])
    company = re.sub(r"\s+(A/S|ApS|I/S|P/S|K/S|AB|AS|ASA|GmbH|Ltd\.?|Inc\.?|LLC)$", "", (company or "").strip())
    parts = (_file_part(x) for x in (p.get("name") or "Application", word, company))
    return "_".join(x for x in parts if x)[:150].rstrip("_-") + f".{ext}"


# ---------------------------------------------------------------------------------------
# Markdown-ish parsing shared by both renderers
# ---------------------------------------------------------------------------------------

def parse_cv(md: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    para: list[str] = []
    after_h3 = False

    def flush() -> None:
        if para:
            blocks.append(("para", " ".join(para)))
            para.clear()

    for raw in (md or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("### "):
            flush()
            blocks.append(("h3", line[4:].strip()))
            after_h3 = True
            continue
        if line.startswith("## "):
            flush()
            blocks.append(("h2", line[3:].strip()))
            after_h3 = False
            continue
        if line.startswith("# "):
            flush()
            blocks.append(("h1", line[2:].strip()))
            after_h3 = False
            continue
        if re.match(r"^[-*•]\s+", line):
            flush()
            blocks.append(("bullet", re.sub(r"^[-*•]\s+", "", line)))
            after_h3 = False
            continue
        if after_h3:
            blocks.append(("meta", line.strip("*_ ")))
            after_h3 = False
            continue
        para.append(line)
    flush()
    return blocks


def paragraphs(text: str) -> list[str]:
    """Blank lines separate paragraphs; single line breaks (e.g. sign-off and name) are kept."""
    return [p.strip() for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]


_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|_[^_\s][^_]*_)")


def inline_runs(text: str) -> list[tuple[str, bool, bool]]:
    """Split **bold** and *italic* markers into (text, bold, italic) runs."""
    out = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append((part[2:-2], True, False))
        elif (part.startswith("*") and part.endswith("*")) or (part.startswith("_") and part.endswith("_")):
            out.append((part[1:-1], False, True))
        else:
            out.append((part, False, False))
    return out


# ---------------------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------------------

_LATIN1 = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...",
                         "•": "-", "→": "->", " ": " ", "‐": "-", "‑": "-"})


def _hex(color: str) -> tuple[int, int, int]:
    c = (color or "#1f4e79").lstrip("#")
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except (ValueError, IndexError):
        return 31, 78, 121


class _PDF:
    def __init__(self) -> None:
        from fpdf import FPDF

        w = settings.get("writing")
        self.accent = _hex(w.get("accent_color"))
        self.pdf = FPDF(format="A4", unit="mm")
        self.pdf.set_margins(21, 18, 21)
        self.pdf.set_auto_page_break(True, margin=17)
        self.pdf.set_title("Application")
        self.unicode = False
        fonts = FONT_SETS.get(w.get("pdf_font") or "sans", FONT_SETS["sans"])
        if all(Path(f).exists() for f in fonts):
            for style, path in zip(("", "B", "I", "BI"), fonts):
                self.pdf.add_font("Body", style, path)
            self.family, self.unicode = "Body", True
        else:
            self.family = "helvetica"
        self.pdf.add_page()

    def t(self, text: str) -> str:
        text = (text or "").replace("--", "–")
        if self.unicode:
            return text
        return text.translate(_LATIN1).encode("latin-1", "replace").decode("latin-1")

    def font(self, style: str = "", size: float = 10.5, color: tuple[int, int, int] = (25, 25, 25)) -> None:
        self.pdf.set_font(self.family, style, size)
        self.pdf.set_text_color(*color)

    def para(self, text: str, size: float = 10.5, lh: float = 5.4, style: str = "",
             color: tuple[int, int, int] = (25, 25, 25), after: float = 0, indent: float = 0) -> None:
        self.font(style, size, color)
        md = re.sub(r"(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)", r"__\1__", text)  # *italic* → fpdf's __italic__
        self.pdf.set_x(self.pdf.l_margin + indent)
        self.pdf.multi_cell(self.pdf.epw - indent, lh, self.t(md), markdown=True, align="L", new_x="LMARGIN", new_y="NEXT")
        if after:
            self.pdf.ln(after)

    def bullet(self, text: str, size: float = 10, lh: float = 5.0) -> None:
        self.font("", size)
        y = self.pdf.get_y()
        self.pdf.set_xy(self.pdf.l_margin + 1.5, y)
        self.pdf.cell(4, lh, self.t("•") if self.unicode else "-")
        self.para(text, size=size, lh=lh, indent=6)
        self.pdf.ln(0.6)

    def rule(self, gap: float = 1.5) -> None:
        self.pdf.set_draw_color(*self.accent)
        self.pdf.set_line_width(0.3)
        y = self.pdf.get_y() + gap
        self.pdf.line(self.pdf.l_margin, y, self.pdf.w - self.pdf.r_margin, y)
        self.pdf.set_y(y + gap)

    def header(self, name: str, sub: str | None, contact: list[list[tuple[str, str]]]) -> None:
        self.font("B", 19, (20, 20, 20))
        self.pdf.cell(0, 9, self.t(name), new_x="LMARGIN", new_y="NEXT")
        if sub:
            self.font("", 11.5, self.accent)
            self.pdf.multi_cell(0, 6, self.t(sub), align="L", new_x="LMARGIN", new_y="NEXT")
        if contact:
            self.contact(contact)

    def contact(self, groups: list[list[tuple[str, str]]], lh: float = 5) -> None:
        """Left-aligned and wrapped only between items, never inside a phone number or an address.
        When it doesn't all fit on one line, the links start the second line."""
        self.font("", 9, (95, 95, 95))
        pad = 2 * self.pdf.c_margin

        def width(s: str) -> float:
            return self.pdf.get_string_width(self.t(s)) + pad

        sep = "·"
        sep_w = width(sep)
        items = [text for group in groups for text, _ in group]
        one_line = sum(map(width, items)) + sep_w * (len(items) - 1) <= self.pdf.epw
        left, right = self.pdf.l_margin, self.pdf.w - self.pdf.r_margin
        for g, group in enumerate(groups):
            if g and not one_line:
                self.pdf.ln(lh)
            for text, url in group:
                w = width(text)
                if self.pdf.get_x() > left + 0.01:  # something is already on this line
                    if self.pdf.get_x() + sep_w + w > right:
                        self.pdf.ln(lh)
                    else:
                        self.pdf.cell(sep_w, lh, sep)
                self.pdf.cell(w, lh, self.t(text), link=url)
        self.pdf.ln(lh)

    def bytes(self) -> bytes:
        return bytes(self.pdf.output())


def letter_pdf(app: dict) -> bytes:
    p = settings.get("profile")
    doc = _PDF()
    doc.header(p.get("name") or "", None, contact_groups(p))
    doc.pdf.ln(9)
    lang = _lang(app.get("language"))
    y = doc.pdf.get_y()
    doc.font("", 10, (40, 40, 40))
    recipient = [app.get("company_name") or ""]
    if app.get("contact_name"):
        recipient.append(f"{DOC_WORDS['att'].get(lang, 'Attn.')} {app['contact_name']}")
    doc.pdf.multi_cell(doc.pdf.epw * 0.6, 5, doc.t("\n".join(r for r in recipient if r)), align="L", new_x="LMARGIN", new_y="NEXT")
    after_recipient = doc.pdf.get_y()
    doc.pdf.set_xy(doc.pdf.l_margin, y)
    doc.pdf.cell(0, 5, doc.t(letter_date(lang, p.get("city"))), align="R")
    doc.pdf.set_y(max(after_recipient, y + 5) + 8)
    if app.get("subject"):
        doc.para(f"**{app['subject']}**", size=12, lh=6, after=4)
    for para in paragraphs(app.get("letter") or ""):
        if re.match(r"^[-•]\s", para):
            for line in para.splitlines():
                doc.bullet(re.sub(r"^[-•]\s+", "", line.strip()), size=10.5, lh=5.4)
            doc.pdf.ln(2)
        else:
            doc.para(para, after=3.2)
    return doc.bytes()


def cv_pdf(app: dict) -> bytes:
    data = app.get("cv_data")
    if data:
        import json

        from . import cvrender

        return cvrender.pdf(json.loads(data) if isinstance(data, str) else data, app.get("language"))
    p = settings.get("profile")
    blocks = parse_cv(app.get("cv") or "")
    headline = next((t for k, t in blocks if k == "h1"), p.get("headline") or "")
    doc = _PDF()
    doc.header(p.get("name") or "", headline, contact_groups(p))
    doc.rule(2.5)
    for kind, text in blocks:
        if kind == "h1":
            continue
        if kind == "h2":
            doc.pdf.ln(3)
            doc.font("B", 10.5, doc.accent)
            doc.pdf.cell(0, 6, doc.t(text.upper()), new_x="LMARGIN", new_y="NEXT")
            doc.pdf.set_draw_color(205, 205, 205)
            doc.pdf.set_line_width(0.2)
            yy = doc.pdf.get_y() + 0.4
            doc.pdf.line(doc.pdf.l_margin, yy, doc.pdf.w - doc.pdf.r_margin, yy)
            doc.pdf.ln(2.2)
        elif kind == "h3":
            doc.pdf.ln(1.2)
            title, _, org = text.partition(" — ")
            md = f"**{title.strip()}**" + (f" — {org.strip()}" if org else "")
            doc.para(md, size=10.5, lh=5.4)
        elif kind == "meta":
            doc.para(text, size=9, lh=4.6, style="I", color=(110, 110, 110), after=1)
        elif kind == "bullet":
            doc.bullet(text, size=10, lh=5.0)
        else:
            doc.para(text, size=10, lh=5.2, after=1.5)
    return doc.bytes()


# ---------------------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------------------

def _docx_base():
    from docx import Document
    from docx.shared import Mm, Pt

    w = settings.get("writing")
    d = Document()
    for s in d.sections:
        s.top_margin, s.bottom_margin = Mm(18), Mm(17)
        s.left_margin = s.right_margin = Mm(21)
    normal = d.styles["Normal"]
    normal.font.name = DOCX_FONTS.get(w.get("pdf_font") or "sans", "Arial")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(0)
    return d, _hex(w.get("accent_color"))


def _runs(par, text: str, size=None, color=None, bold=False, italic=False):
    from docx.shared import Pt, RGBColor

    lines = text.split("\n")
    for n, line in enumerate(lines):
        if n:
            par.add_run().add_break()
        _line_runs(par, line, size, color, bold, italic)
    return par


def _line_runs(par, text: str, size, color, bold, italic):
    from docx.shared import Pt, RGBColor

    for chunk, b, i in inline_runs(text):
        run = par.add_run(chunk)
        run.bold = bold or b
        run.italic = italic or i
        if size:
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = RGBColor(*color)


def _docx_header(d, p: dict, sub: str | None, accent) -> None:
    from docx.shared import Pt

    _runs(d.add_paragraph(), p.get("name") or "", size=19, bold=True)
    if sub:
        _runs(d.add_paragraph(), sub, size=11.5, color=accent)
    contact = contact_line(p)
    if contact:
        par = _runs(d.add_paragraph(), contact, size=9, color=(95, 95, 95))
        par.paragraph_format.space_after = Pt(10)


def letter_docx(app: dict) -> bytes:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    p = settings.get("profile")
    d, accent = _docx_base()
    _docx_header(d, p, None, accent)
    lang = _lang(app.get("language"))
    date_par = d.add_paragraph(letter_date(lang, p.get("city")))
    date_par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    recipient = [x for x in (app.get("company_name"),
                             f"{DOC_WORDS['att'].get(lang, 'Attn.')} {app['contact_name']}" if app.get("contact_name") else None) if x]
    for r in recipient:
        d.add_paragraph(r)
    d.add_paragraph().paragraph_format.space_after = Pt(6)
    if app.get("subject"):
        par = _runs(d.add_paragraph(), app["subject"], size=12, bold=True)
        par.paragraph_format.space_after = Pt(10)
    for para in paragraphs(app.get("letter") or ""):
        if re.match(r"^[-•]\s", para):
            for line in para.splitlines():
                _runs(d.add_paragraph(style="List Bullet"), re.sub(r"^[-•]\s+", "", line.strip()))
        else:
            par = _runs(d.add_paragraph(), para)
            par.paragraph_format.space_after = Pt(8)
            par.paragraph_format.line_spacing = 1.15
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def cv_docx(app: dict) -> bytes:
    from docx.shared import Pt

    p = settings.get("profile")
    blocks = parse_cv(app.get("cv") or "")
    headline = next((t for k, t in blocks if k == "h1"), p.get("headline") or "")
    d, accent = _docx_base()
    _docx_header(d, p, headline, accent)
    for kind, text in blocks:
        if kind == "h1":
            continue
        if kind == "h2":
            par = _runs(d.add_paragraph(), text.upper(), size=10.5, color=accent, bold=True)
            par.paragraph_format.space_before = Pt(10)
            par.paragraph_format.space_after = Pt(3)
        elif kind == "h3":
            title, _, org = text.partition(" — ")
            par = d.add_paragraph()
            par.paragraph_format.space_before = Pt(5)
            _runs(par, title.strip(), bold=True)
            if org:
                _runs(par, f" — {org.strip()}")
        elif kind == "meta":
            par = _runs(d.add_paragraph(), text, size=9, color=(110, 110, 110), italic=True)
            par.paragraph_format.space_after = Pt(2)
        elif kind == "bullet":
            _runs(d.add_paragraph(style="List Bullet"), text, size=10)
        else:
            par = _runs(d.add_paragraph(), text, size=10)
            par.paragraph_format.space_after = Pt(4)
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def render(app: dict, kind: str, fmt: str) -> tuple[bytes, str]:
    from .writer import strip_citations

    app = dict(app)
    app["letter"] = strip_citations(app.get("letter") or "")
    app["cv"] = strip_citations(app.get("cv") or "")
    fn = {("letter", "pdf"): letter_pdf, ("letter", "docx"): letter_docx,
          ("cv", "pdf"): cv_pdf, ("cv", "docx"): cv_docx}[(kind, fmt)]
    return fn(app), filename(kind, fmt, app.get("company_name"), app.get("language"))


def save_exports(app: dict) -> list[Path]:
    """Write PDFs of letter and CV into the data folder (used for email attachments)."""
    folder = config.EXPORTS_DIR / f"application-{app['id']}"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for kind in ("letter", "cv"):
        if not app.get(kind):
            continue
        data, name = render(app, kind, "pdf")
        path = folder / name
        path.write_bytes(data)
        paths.append(path)
    return paths
