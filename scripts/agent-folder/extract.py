#!/usr/bin/env python3
"""Print the text of one document, for subagents summarising a person's files. Read-only.

Usage: uv run --project <repo> python -I scripts/agent-folder/extract.py FILE [--max-chars N] [--meta] [--stats]

Uses Career Agent's own text extraction (PDF, Word, PowerPoint, ODT, RTF, Pages, HTML, e-mail, text), plus JSON
(pretty-printed) and a few plain-text formats. Run it from anywhere except inside the folder being read.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from career_agent import ingest  # noqa: E402

PLAIN = (".csv", ".tsv", ".log", ".yml", ".yaml", ".toml", ".ini", ".cfg")


def meta(path: Path) -> dict:
    """Author, dates, title and page count where the format has them."""
    ext = path.suffix.lower()
    info: dict = {"file": path.name, "bytes": path.stat().st_size}
    try:
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            info["pages"] = len(reader.pages)
            m = reader.metadata or {}
            for key in ("/Author", "/Creator", "/Producer", "/CreationDate", "/ModDate", "/Title"):
                if m.get(key):
                    info[key.strip("/").lower()] = str(m.get(key))
        elif ext == ".docx":
            from docx import Document

            cp = Document(str(path)).core_properties
            info.update({"author": cp.author, "last_modified_by": cp.last_modified_by,
                         "created": str(cp.created or ""), "modified": str(cp.modified or ""), "title": cp.title})
        elif ext == ".pptx":
            with zipfile.ZipFile(path) as z:
                core = z.read("docProps/core.xml").decode("utf-8", "replace") if "docProps/core.xml" in z.namelist() else ""
            for tag in ("dc:creator", "cp:lastModifiedBy", "dcterms:created", "dcterms:modified", "dc:title"):
                m = re.search(rf"<{tag}\b[^>]*>([^<]*)</{tag}>", core)
                if m:
                    info[tag.split(":")[1]] = html.unescape(m.group(1))
    except Exception as e:  # noqa: BLE001 - metadata is a bonus
        info["meta_error"] = str(e)
    return info


def text_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".json":
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=1)
        except ValueError:
            return raw
    if ext in PLAIN:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext not in ingest.SUPPORTED:
        raise ingest.ImportErrorUser(f"Unsupported file type for text extraction: {ext or 'no extension'}")
    return ingest.extract_text(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--max-chars", type=int, default=60_000)
    ap.add_argument("--meta", action="store_true", help="print author, dates and pages first")
    ap.add_argument("--stats", action="store_true", help="print only the size of the text")
    a = ap.parse_args()
    path = Path(a.file).expanduser()
    if not path.is_file():
        raise SystemExit(f"Not a file: {path}")
    if a.meta:
        print(json.dumps(meta(path), ensure_ascii=False, default=str))
    try:
        text = ingest.clean_text(text_of(path))
    except ingest.ImportErrorUser as e:
        hint = " It's probably a scan: open it as an image to read the pages." if path.suffix.lower() == ".pdf" else ""
        print(f"[no text extracted: {e}]{hint}")
        return
    except zipfile.BadZipFile:
        print("[no text extracted: the file is damaged or not really this format]")
        return
    if a.stats:
        print(json.dumps({"chars": len(text), "words": len(text.split())}))
    elif len(text) > a.max_chars:
        print(text[: a.max_chars])
        print(f"\n[... truncated: {len(text) - a.max_chars} more characters; rerun with --max-chars to see more]")
    else:
        print(text)


if __name__ == "__main__":
    main()
