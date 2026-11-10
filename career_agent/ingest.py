"""Import documents (files, folders, mailboxes) and extract their text."""
from __future__ import annotations

import email
import email.policy
import hashlib
import mailbox
import re
import shutil
import subprocess
import sys
import zipfile
from email.utils import getaddresses, parsedate_to_datetime
from io import BytesIO
from pathlib import Path

from . import config, db, settings

SUPPORTED = {".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".html", ".htm", ".eml",
             ".mbox", ".pages", ".webarchive", ".pptx"}
MAX_FILE_BYTES = 40 * 1024 * 1024
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "Library", ".Trash"}


class ImportErrorUser(Exception):
    """A file couldn't be imported; the message is for the user."""


# ---------------------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------------------

def clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").replace("­", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:  # noqa: BLE001
            raise ImportErrorUser("The PDF is password protected.") from e
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page shouldn't sink the document
            pages.append("")
    return "\n\n".join(pages)


def _docx_text(data: bytes) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(BytesIO(data))
    out: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            out.append(Paragraph(child, doc).text)
        elif tag == "tbl":
            for row in Table(child, doc).rows:
                cells, seen = [], set()
                for cell in row.cells:  # merged cells repeat; keep the first copy
                    t = cell.text.strip()
                    if t and id(cell._tc) not in seen:
                        cells.append(t)
                    seen.add(id(cell._tc))
                if cells:
                    out.append(" | ".join(cells))
    return "\n".join(out)


_PPTX_SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml")
_PPTX_NOTES = re.compile(r"ppt/notesSlides/notesSlide(\d+)\.xml")
_A_PARA = re.compile(r"<a:p\b[^>]*>.*?</a:p>", re.S)
_A_TEXT = re.compile(r"<a:t\b[^>]*>([^<]*)</a:t>")


def _pptx_text(data: bytes) -> str:
    """Slide text and speaker notes, slide by slide (no extra dependency: a .pptx is zipped XML)."""
    import html

    def lines(xml: bytes) -> list[str]:
        out = []
        for p in _A_PARA.findall(xml.decode("utf-8", "replace")):
            t = html.unescape("".join(_A_TEXT.findall(p))).strip()
            if t:
                out.append(t)
        return out

    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            names = z.namelist()
            slides = sorted((int(m.group(1)), n) for n in names if (m := _PPTX_SLIDE.fullmatch(n)))
            notes = {int(m.group(1)): n for n in names if (m := _PPTX_NOTES.fullmatch(n))}
            out = []
            for num, name in slides:
                body = lines(z.read(name))
                note = lines(z.read(notes[num])) if num in notes else []
                note = [t for t in note if not t.isdigit()]  # notes pages repeat the slide number
                if body or note:
                    out.append(f"[Slide {num}]\n" + "\n".join(body)
                               + (("\nSpeaker notes: " + " ".join(note)) if note else ""))
    except zipfile.BadZipFile as e:
        raise ImportErrorUser("This PowerPoint file is damaged or not really a .pptx.") from e
    return "\n\n".join(out)


def _textutil(path: Path) -> str:
    if sys.platform != "darwin" or not shutil.which("textutil"):
        raise ImportErrorUser(f"{path.suffix} files need macOS textutil. Save it as PDF or Word instead.")
    res = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                         capture_output=True, timeout=120)
    if res.returncode != 0:
        raise ImportErrorUser(f"textutil couldn't read the file: {res.stderr.decode(errors='ignore')[:200]}")
    return res.stdout.decode("utf-8", errors="replace")


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _html_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup.get_text("\n")


_QUOTE_HEADER = re.compile(
    r"^(On .{5,200} wrote:|Den .{5,200} skrev .{0,200}:|-----Original Message-----|Fra: .+|From: .+)$",
    re.MULTILINE)
_SIGNATURE = re.compile(r"^(--|—|Sent from my|Sendt fra min)\s*", re.MULTILINE)


def _strip_quotes(body: str) -> str:
    m = _QUOTE_HEADER.search(body)
    if m and m.start() > 40:
        body = body[:m.start()]
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith(">")]
    body = "\n".join(lines)
    m = _SIGNATURE.search(body)
    if m and m.start() > 40:
        body = body[:m.start()]
    return body.strip()


def _email_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        plain = html = None
        for part in msg.walk():
            if part.get_content_maintype() == "multipart" or part.get_filename():
                continue
            ctype = part.get_content_type()
            try:
                payload = part.get_content() if hasattr(part, "get_content") else part.get_payload(decode=True)
            except Exception:  # noqa: BLE001
                payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                payload = _decode(payload)
            if ctype == "text/plain" and plain is None:
                plain = payload
            elif ctype == "text/html" and html is None:
                html = payload
        body = plain or (_html_text(html) if html else "")
    else:
        try:
            body = msg.get_content() if hasattr(msg, "get_content") else msg.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            body = msg.get_payload(decode=True)
        if isinstance(body, bytes):
            body = _decode(body)
        if msg.get_content_type() == "text/html":
            body = _html_text(body or "")
    return body or ""


def _email_to_text(msg: email.message.Message) -> tuple[str, str | None]:
    subject = str(msg.get("subject", "") or "")
    date = None
    try:
        date = parsedate_to_datetime(msg.get("date")).date().isoformat() if msg.get("date") else None
    except (TypeError, ValueError):
        pass
    body = _strip_quotes(_email_body(msg))
    return f"Subject: {subject}\n\n{body}", date


def extract_text(path: Path, data: bytes | None = None) -> str:
    ext = path.suffix.lower()
    data = data if data is not None else path.read_bytes()
    if ext == ".pdf":
        text = _pdf_text(data)
        if len(text.strip()) < 30:
            raise ImportErrorUser("This PDF has no text layer (probably a scan). Export it with text, or OCR it first.")
        return text
    if ext == ".docx":
        try:
            return _docx_text(data)
        except Exception:  # noqa: BLE001 - fall back to textutil for odd files
            return _textutil(path)
    if ext in (".doc", ".odt", ".rtf", ".webarchive"):
        return _textutil(path)
    if ext == ".pptx":
        return _pptx_text(data)
    if ext in (".txt", ".md"):
        return _decode(data)
    if ext in (".html", ".htm"):
        return _html_text(_decode(data))
    if ext == ".eml":
        msg = email.message_from_bytes(data, policy=email.policy.default)
        return _email_to_text(msg)[0]
    if ext == ".pages":
        try:
            with zipfile.ZipFile(BytesIO(data)) as z:
                name = next((n for n in z.namelist() if n.lower().endswith("preview.pdf")), None)
                if name:
                    return _pdf_text(z.read(name))
        except zipfile.BadZipFile:
            pass
        raise ImportErrorUser("Pages files can't be read directly. In Pages choose File → Export To → PDF or Word.")
    raise ImportErrorUser(f"Unsupported file type: {ext}")


# ---------------------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------------------

def _store(filename: str, text: str, sha: str, source_path: str | None = None,
           stored_path: str | None = None, kind: str = "unknown", doc_date: str | None = None,
           authored: int = 1) -> tuple[int, bool]:
    """Insert a document; returns (id, created)."""
    existing = db.q1("SELECT id FROM documents WHERE sha256 = ?", (sha,))
    if existing:
        return existing["id"], False
    text = clean_text(text)
    if len(text) < 20:
        raise ImportErrorUser("No readable text found.")
    doc_id = db.insert("documents", {
        "filename": filename, "source_path": source_path, "stored_path": stored_path, "sha256": sha,
        "ext": Path(filename).suffix.lower(), "kind": kind, "title": Path(filename).stem,
        "doc_date": doc_date, "text": text, "word_count": len(text.split()), "authored": authored,
        "status": "new", "created_at": db.now(),
    })
    from . import retrieval

    retrieval.index_document(doc_id)
    return doc_id, True


def import_bytes(filename: str, data: bytes) -> tuple[int, bool]:
    """Import an uploaded file. Keeps a copy inside the data folder."""
    if len(data) > MAX_FILE_BYTES:
        raise ImportErrorUser("File is larger than 40 MB.")
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise ImportErrorUser(f"Unsupported file type: {ext or 'no extension'}")
    sha = hashlib.sha256(data).hexdigest()
    existing = db.q1("SELECT id FROM documents WHERE sha256 = ?", (sha,))
    if existing:
        return existing["id"], False
    config.ensure_dirs()
    safe = re.sub(r"[^\w.\- ]+", "_", Path(filename).name)[:120] or f"upload{ext}"
    dest = config.FILES_DIR / f"{sha[:12]}-{safe}"
    dest.write_bytes(data)
    if ext == ".mbox":
        raise ImportErrorUser("Import mailboxes with “Import a folder or file path”, so the whole mailbox can be read.")
    text = extract_text(dest, data)
    return _store(Path(filename).name, text, sha, stored_path=str(dest))


def import_path(path: Path) -> tuple[int, bool]:
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ImportErrorUser("File is larger than 40 MB.")
    sha = hashlib.sha256(data).hexdigest()
    existing = db.q1("SELECT id FROM documents WHERE sha256 = ?", (sha,))
    if existing:
        return existing["id"], False
    text = extract_text(path, data)
    return _store(path.name, text, sha, source_path=str(path))


def _my_addresses() -> set[str]:
    raw = settings.get("profile").get("email", "")
    return {a.strip().lower() for a in re.split(r"[,;\s]+", raw) if "@" in a}


def import_mbox(path: Path, max_messages: int = 300, per_doc: int = 25) -> list[int]:
    """Import the emails *you* sent from an mbox (file, or Apple Mail's .mbox folder)."""
    mine = _my_addresses()
    if not mine:
        raise ImportErrorUser("Add your email address under Profile first, so I can pick out the emails you wrote.")
    mbox_file = path / "mbox" if path.is_dir() else path
    if not mbox_file.exists():
        raise ImportErrorUser("Couldn't find the mbox file inside this folder.")
    box = mailbox.mbox(str(mbox_file), factory=lambda f: email.message_from_binary_file(f, policy=email.policy.default))
    sent: list[tuple[str, str]] = []
    for msg in box:
        try:
            senders = {a.lower() for _, a in getaddresses([str(msg.get("from", ""))])}
        except Exception:  # noqa: BLE001
            continue
        if not senders & mine:
            continue
        text, date = _email_to_text(msg)
        if len(text.split()) < 25:
            continue
        sent.append((date or "", text))
    if not sent:
        raise ImportErrorUser("No emails sent from your address were found in this mailbox.")
    sent.sort(key=lambda x: x[0], reverse=True)
    sent = sent[:max_messages]
    ids = []
    for n in range(0, len(sent), per_doc):
        chunk = sent[n:n + per_doc]
        body = "\n\n---\n\n".join(f"[{d or 'undated'}]\n{t}" for d, t in chunk)
        sha = hashlib.sha256(f"{mbox_file}:{n}:{body[:2000]}".encode()).hexdigest()
        span = f"{chunk[-1][0][:7] or '?'} – {chunk[0][0][:7] or '?'}"
        doc_id, created = _store(f"{path.stem} — sent emails {n // per_doc + 1} ({span})", body, sha,
                                 source_path=str(path), kind="email", doc_date=chunk[0][0] or None)
        if created:
            ids.append(doc_id)
    return ids


def import_folder(folder: Path, limit: int = 2000) -> dict:
    """Recursively import supported files. Returns {'imported': [ids], 'skipped': n, 'errors': [(name, msg)]}."""
    folder = folder.expanduser()
    if not folder.exists():
        raise ImportErrorUser(f"Not found: {folder}")
    from . import knowledge

    result = {"imported": [], "skipped": 0, "errors": []}
    roots = knowledge.skip_roots()
    if knowledge.is_skipped_dir(folder if folder.is_dir() else folder.parent, roots):
        result["errors"].append((folder.name, "That's the agent folder: it holds summaries of your documents, "
                                              "not documents, so it isn't imported."))
        return result
    if folder.is_file() or folder.suffix.lower() == ".mbox":
        paths = [folder]
    else:
        paths = []
        skipped_dirs: dict[Path, bool] = {}
        for p in sorted(folder.rglob("*")):
            if any(part.startswith(".") or part in SKIP_DIRS for part in p.relative_to(folder).parts):
                continue
            parent = p.parent
            if parent not in skipped_dirs:
                skipped_dirs[parent] = knowledge.is_skipped_dir(parent, roots)
            if skipped_dirs[parent] or (p.is_dir() and knowledge.is_skipped_dir(p, roots)):
                continue
            if p.suffix.lower() == ".mbox" and p.is_dir():
                paths.append(p)
            elif p.is_file() and p.suffix.lower() in SUPPORTED and not any(
                    parent.suffix.lower() == ".mbox" for parent in p.parents):
                paths.append(p)
            if len(paths) >= limit:
                break
    for p in paths:
        try:
            if p.suffix.lower() == ".mbox":
                result["imported"].extend(import_mbox(p))
                continue
            doc_id, created = import_path(p)
            if created:
                result["imported"].append(doc_id)
            else:
                result["skipped"] += 1
        except ImportErrorUser as e:
            result["errors"].append((p.name, str(e)))
        except Exception as e:  # noqa: BLE001 - keep going on bad files
            result["errors"].append((p.name, f"Couldn't read: {e}"))
    return result


_CV_NAME = re.compile(r"\b(cv|resume|résumé|curriculum|lebenslauf|meritforteckning)\b", re.I)


def cv_first(doc_ids: list[int]) -> list[int]:
    """Order imports so CVs are analysed first: they have the most precise titles and dates."""
    if not doc_ids:
        return []
    marks = ",".join("?" for _ in doc_ids)
    names = {r["id"]: r["filename"] for r in db.q(f"SELECT id, filename FROM documents WHERE id IN ({marks})", doc_ids)}
    return sorted(doc_ids, key=lambda i: 0 if _CV_NAME.search(re.sub(r"[_\-.]", " ", names.get(i, ""))) else 1)
