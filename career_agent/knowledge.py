"""The agent folder: summaries and guides written for AI agents, e.g. ~/Nextcloud/Agents.

    Agents/                       general files any agent can read (about the person, a map of their files…)
    Agents/Career Agent/          this app's part:
        profile.md                one-page career profile
        experience/*.md           one summary per education, job, internship, project, free time
        writing-style-<lang>.md   how the candidate writes, per language (da, en…)
        facts.jsonl               optional: facts with their source file and a verbatim quote
        sources.jsonl             optional: every source file with its kind and authorship
        anything else             for people and other agents; the app ignores it

The profile, the experience summaries and the style guides go into the writing prompts as orientation,
which helps small local models most: they get the whole picture in a few thousand characters. They are
never evidence: the writer may only claim what the career database backs, and the fact check still
compares every sentence with the career database and the candidate's own documents.

The folder is never imported as source documents: summaries of your documents aren't your documents.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path

from . import db, settings

log = logging.getLogger(__name__)

AGENTS_DIRNAME = "Agents"
APP_DIRNAME = "Career Agent"

# Budgets for the writing prompt: (profile chars, number of summaries, chars per summary). Locally that's
# about 4,000 tokens in all, well inside a 32k context; a summary of ~500 words fits whole.
LOCAL_BUDGET = (4_500, 3, 3_600)
CLOUD_BUDGET = (9_000, 6, 6_000)
STYLE_MAX_CHARS = 9_000

_SOURCES_SECTION = re.compile(r"^#{2,3}\s+Sources?\b.*?(?=^#{1,3}\s|\Z)", re.M | re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_lock = threading.Lock()
_cache: dict = {"key": None, "data": None}


# ---------------------------------------------------------------- where the folder is

def agents_root() -> Path | None:
    """The general agent folder: the setting, or <Nextcloud>/Agents when it exists."""
    cfg = settings.get("knowledge")
    if cfg.get("folder"):
        p = Path(cfg["folder"]).expanduser()
        return p if p.is_dir() else None
    from . import watch  # late import: watch imports ingest, which asks us what to skip

    for nc in watch.nextcloud_candidates():
        p = Path(nc) / AGENTS_DIRNAME
        if p.is_dir():
            return p
    return None


def app_folder() -> Path | None:
    root = agents_root()
    if root is None:
        return None
    p = root / APP_DIRNAME
    return p if p.is_dir() else None


def skip_roots() -> set[Path]:
    """Folders that file imports and folder watching must never read as source documents."""
    root = agents_root()
    if root is None:
        return set()
    try:
        return {root.resolve()}
    except OSError:
        return set()


def is_skip_point(path: Path, roots: set[Path]) -> bool:
    """This exact folder is the agent folder, or a copy of one elsewhere (a WebDAV mirror, a backup):
    an "Agents" folder that holds a "Career Agent" folder. For directory walks that prune as they go."""
    if path.name == AGENTS_DIRNAME and (path / APP_DIRNAME).is_dir():
        return True
    if not roots:
        return False
    try:
        return path.resolve() in roots
    except OSError:
        return False


def is_skipped_dir(path: Path, roots: set[Path] | None = None) -> bool:
    """True for the agent folder or anything inside it."""
    roots = skip_roots() if roots is None else roots
    try:
        p = path.resolve()
    except OSError:
        p = path
    return any(is_skip_point(d, roots) for d in (p, *p.parents))


# ---------------------------------------------------------------- reading it

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def clean(text: str) -> str:
    """Prompt-ready Markdown: no comments, no Sources section (file paths only distract a model)."""
    text = _COMMENT.sub("", text or "")
    text = _SOURCES_SECTION.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _title(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    return m.group(1).strip() if m else fallback


def _ref_id(key: str) -> int:
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], 16)


def _signature(folder: Path) -> tuple:
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".md", ".jsonl") and not any(
                part.startswith(".") for part in p.relative_to(folder).parts):
            st = p.stat()
            out.append((p.relative_to(folder).as_posix(), st.st_mtime_ns, st.st_size))
    return tuple(out)


def _load(folder: Path) -> dict:
    profile_text = clean(_read(folder / "profile.md"))
    areas = []
    exp = folder / "experience"
    if exp.is_dir():
        for p in sorted(exp.glob("*.md")):
            text = clean(_read(p))
            if len(text) < 40:
                continue
            key = p.relative_to(folder).as_posix()
            areas.append({"key": key, "id": _ref_id(key), "title": _title(text, p.stem), "text": text})
    style = {}
    for p in sorted(folder.glob("writing-style-*.md")):
        lang = p.stem.rsplit("-", 1)[-1].lower()[:2]
        text = clean(_read(p))
        if lang and text:
            style[lang] = text[:STYLE_MAX_CHARS]
    return {"folder": str(folder), "profile": profile_text, "areas": areas, "style": style}


def load() -> dict:
    """The folder's prompt material, re-read whenever a file in it changes."""
    empty = {"folder": None, "profile": "", "areas": [], "style": {}}
    if not settings.get("knowledge").get("use", True):
        return empty
    folder = app_folder()
    if folder is None:
        return empty
    try:
        key = (str(folder), _signature(folder))
    except OSError as e:
        log.warning("couldn't read the agent folder: %s", e)
        return empty
    with _lock:
        if _cache["key"] == key:
            return _cache["data"]
    data = _load(folder)
    with _lock:
        _cache.update(key=key, data=data)
    return data


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rfind("\n")
    return (head[:cut] if cut > limit * 0.6 else head).rstrip() + "\n…"


def relevant_areas(query: str | None, k: int) -> list[dict]:
    """The k experience summaries most relevant to the query (keywords plus meaning, as elsewhere)."""
    areas = load()["areas"]
    if not areas or k <= 0:
        return []
    if not query:
        return areas[:k]
    from . import retrieval

    items = [(a["id"], f"{a['title']}\n{a['text']}") for a in areas]
    order = retrieval.rank_items("area", query, items)
    by_id = {a["id"]: a for a in areas}
    return [by_id[i] for i in order[:k]]


def context_block(query: str | None, local: bool) -> str:
    """Profile plus the most relevant summaries, sized for the writing model. "" without a folder."""
    data = load()
    if not data["profile"] and not data["areas"]:
        return ""
    max_profile, k, max_area = LOCAL_BUDGET if local else CLOUD_BUDGET
    parts = []
    if data["profile"]:
        parts.append(_cut(data["profile"], max_profile))
    parts += [_cut(a["text"], max_area) for a in relevant_areas(query, k)]
    return "\n\n".join(parts)


def style_guide(language: str | None) -> str:
    style = load()["style"]
    return style.get((language or "")[:2], "")


_TARGET = re.compile(r"^\s*[-*]\s*`?([a-z_0-9]+)`?\s*:\s*([0-9.]+)\s*(?:–|-|to)\s*([0-9.]+)", re.M)


def voice_targets(language: str | None) -> dict[str, tuple[float, float]]:
    """Ranges for the voice check, from the style guide's "## Targets" section, e.g. "- avg_sentence: 12–20".
    {} when the guide has none: the voice check then compares with the candidate's own applications."""
    text = style_guide(language)
    m = re.search(r"^##\s+Targets\b.*?$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    if not m:
        return {}
    return {k: (float(lo), float(hi)) for k, lo, hi in _TARGET.findall(m.group(1)) if float(lo) <= float(hi)}


# ---------------------------------------------------------------- importing its facts

_DATE = re.compile(r"^(\d{4})(-\d{2})?(-\d{2})?$")
_KINDS = {"cv", "application", "email", "reference", "report", "certificate", "job_ad", "course_material", "other"}
# Source kinds worth having as documents (for the fact check's search and your voice); "notes" → other.
_IMPORT_KINDS = {"cv", "application", "email", "reference", "report", "certificate", "notes", "other"}
_OUTCOMES = {"offer", "interview", "rejected", "no_response", "unknown"}


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(_read(path).splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            log.warning("%s line %d isn't valid JSON, skipped", path.name, n)
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _date(value) -> str:
    v = str(value or "").strip()
    if v.lower() in ("present", "nu", "now", "current"):
        return "present"
    return v if _DATE.match(v) else ""


def import_facts() -> dict:
    """Bring the folder's facts.jsonl into the career database.

    Each fact's source file (path relative to the Nextcloud folder that holds Agents/) is imported as a
    document if it can be read, so the fact check can search it; documents listed in sources.jsonl get
    their kind, authorship and outcome from there and aren't read again by the local model. Duplicates
    merge like any other import, facts stay unticked until you check them, and running it twice is safe.
    """
    from . import ingest, profile  # late imports: profile → voice → … would be circular at load time

    folder = app_folder()
    if folder is None:
        raise FileNotFoundError("No agent folder found.")
    base = folder.parent.parent  # <Nextcloud>/Agents/Career Agent → <Nextcloud>
    facts = _jsonl(folder / "facts.jsonl")
    sources = {}
    for s in _jsonl(folder / "sources.jsonl"):
        if s.get("path") and s.get("used", True):
            sources.setdefault(str(s["path"]).strip("/"), s)

    doc_ids: dict[str, int | None] = {}
    report = {"documents": 0, "facts_added": 0, "facts_merged": 0, "skipped": 0, "unreadable": []}

    def document_for(rel: str) -> int | None:
        if rel in doc_ids:
            return doc_ids[rel]
        doc_id = None
        p = base / rel
        if rel and p.is_file() and p.suffix.lower() in ingest.SUPPORTED and p.suffix.lower() != ".mbox":
            try:
                doc_id, created = ingest.import_path(p)
                report["documents"] += int(created)
                _apply_source(doc_id, sources.get(rel), created)
            except (ingest.ImportErrorUser, OSError) as e:
                report["unreadable"].append(f"{rel}: {e}")
        doc_ids[rel] = doc_id
        return doc_id

    # Every used source document, so past applications count as examples of your voice even when
    # no fact came from them.
    for rel, s in sources.items():
        if s.get("kind") in _IMPORT_KINDS:
            document_for(rel)

    by_doc: dict[int | None, list[dict]] = {}
    for f in facts:
        cat = (f.get("category") or "").strip()
        if not (f.get("title") or "").strip() or not profile.category_of(cat):
            report["skipped"] += 1
            continue
        rel = str(f.get("source") or "").strip("/")
        tags = [t for t in (f.get("tags") or []) if isinstance(t, str)]
        if f.get("confidential"):
            tags.append("confidential")
        by_doc.setdefault(document_for(rel), []).append({
            "category": cat, "title": f["title"].strip(), "organization": (f.get("organization") or "").strip(),
            "location": (f.get("location") or "").strip(), "start_date": _date(f.get("start")),
            "end_date": _date(f.get("end")), "detail": (f.get("detail") or "").strip(),
            "evidence": (f.get("quote") or "").strip(), "tags": tags,
        })
    for doc_id, rows in by_doc.items():
        kind = db.scalar("SELECT kind FROM documents WHERE id = ?", (doc_id,)) if doc_id else None
        added, merged = profile.save_facts(doc_id, rows, authoritative=(kind == "cv"))
        report["facts_added"] += added
        report["facts_merged"] += merged
    return report


def _apply_source(doc_id: int, s: dict | None, created: bool) -> None:
    """Classify a document from sources.jsonl, so the local model doesn't have to read it."""
    doc = db.q1("SELECT status FROM documents WHERE id = ?", (doc_id,))
    if not doc or (not created and doc["status"] == "analyzed"):
        return
    s = s or {}
    kind = s.get("kind") if s.get("kind") in _KINDS else "other"
    authorship = s.get("authorship") or "unknown"
    patch = {
        "kind": kind,
        "authored": 1 if authorship == "own" and kind not in ("reference", "certificate", "job_ad") else 0,
        "status": "analyzed", "analyzed_at": db.now(), "error": None,
    }
    if s.get("language"):
        patch["language"] = str(s["language"])[:2].lower()
    if _date(s.get("date")) not in ("", "present"):
        patch["doc_date"] = _date(s.get("date"))
    if s.get("summary"):
        patch["summary"] = str(s["summary"])[:600]
    if s.get("use_for_style") is False:  # e.g. old template letters: their facts count, their wording doesn't
        patch["use_for_style"] = 0
    if kind == "application":
        if s.get("company"):
            patch["target_company"] = str(s["company"])[:200]
        if s.get("role"):
            patch["target_role"] = str(s["role"])[:200]
        if s.get("outcome") in _OUTCOMES:
            patch["outcome"] = s["outcome"]
        # Keep only the letter: a Google Docs export with tabs carries notes, the job ad or private notes after it.
        from . import voice

        text = db.scalar("SELECT text FROM documents WHERE id = ?", (doc_id,)) or ""
        letter = voice.letter_part(text)
        if letter != text:
            patch.update(text=letter, word_count=len(letter.split()))
    db.update("documents", doc_id, patch)
    if "text" in patch:
        from . import retrieval

        retrieval.index_document(doc_id)


def status() -> dict:
    root, folder = agents_root(), app_folder()
    data = load()
    return {
        "root": str(root) if root else "",
        "folder": str(folder) if folder else "",
        "configured": bool(settings.get("knowledge").get("folder")),
        "setting": settings.get("knowledge").get("folder") or "",
        "use": bool(settings.get("knowledge").get("use", True)),
        "profile": bool(data["profile"]),
        "areas": [a["title"] for a in data["areas"]],
        "style": sorted(data["style"]),
        "has_facts": bool(folder and (folder / "facts.jsonl").is_file()),
    }
