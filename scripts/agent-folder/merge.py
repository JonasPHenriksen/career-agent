#!/usr/bin/env python3
"""Combine the subagents' staging areas into the Agents/ folder layout, for review before publishing.

Usage: python3 scripts/agent-folder/merge.py STAGING OUT_AGENTS_DIR [--general AREA ...] [--confidential PREFIX ...]
       [--name FIRST_NAME]

Every subfolder of STAGING is an area (names starting with "_" are ignored). Files are placed by folder:
  <area>/experience/*.md  -> Career Agent/experience/
  <area>/career/*.md      -> Career Agent/
  <area>/agents/**        -> the Agents/ root (keeping subfolders)
Facts and sources of all areas except --general ones are merged into Career Agent/facts.jsonl and sources.jsonl and
rendered as facts.md and sources.md; notes go to Career Agent/notes-by-area.md (general areas: open-questions.md in
the root). Paths inside a --confidential folder (e.g. "Work/Employer") are cut back to its top folder, since file
names there often name clients, and confidential facts lose their quotes. Hand-written files are never written.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

HAND_WRITTEN = {"README.md", "about-me.md", "Career Agent/README.md", "Career Agent/profile.md",
                "Career Agent/open-questions.md"}
CATEGORY_ORDER = ["experience", "education", "project", "achievement", "certification", "skill", "language",
                  "volunteering", "value", "personal"]
CATEGORY_TITLES = {"experience": "Work experience", "education": "Education", "project": "Projects",
                   "achievement": "Achievements and results", "certification": "Certificates", "skill": "Skills",
                   "language": "Languages", "volunteering": "Volunteering and associations",
                   "value": "Values and motivation (as stated by the person)", "personal": "Personal"}


def jsonl(path: Path) -> list[dict]:
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def norm(s) -> str:
    return re.sub(r"\W+", " ", str(s or "").lower()).strip()


def public_path(path: str, confidential: list[str]) -> str:
    """'Work/Acme/01_Repos/Client/repo/README.md' -> 'Work/Acme/01_Repos/' for a confidential 'Work/Acme'."""
    for prefix in confidential:
        prefix = prefix.strip("/")
        if path.startswith(prefix + "/"):
            rest = path[len(prefix) + 1:].split("/")
            if len(rest) > 1:
                return f"{prefix}/{rest[0]}/"
    return path


def date_range(f: dict) -> str:
    s, e = (f.get("start") or "").strip(), (f.get("end") or "").strip()
    return f"{s}–{e}" if s and e else (s or e)


def build(staging: Path, out: Path, general: list[str] | None = None, confidential: list[str] | None = None,
          name: str = "the person") -> dict:
    general, confidential = general or [], confidential or []
    career = out / "Career Agent"
    (career / "experience").mkdir(parents=True, exist_ok=True)
    areas = sorted(d.name for d in staging.iterdir() if d.is_dir() and not d.name.startswith(("_", ".")))
    career_areas = [a for a in areas if a not in general]
    written: dict[str, str] = {}
    skipped: list[str] = []

    def place(src: Path, rel: str, area: str) -> None:
        if rel in HAND_WRITTEN:
            skipped.append(f"{area}: {rel} is hand-written, not copied")
            return
        if rel in written:
            stem, dot, ext = rel.rpartition(".")
            rel = f"{stem}-{area}.{ext}"
            skipped.append(f"{area}: name clash, saved as {rel}")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        written[rel] = area

    for area in areas:
        d = staging / area
        for p in sorted((d / "experience").glob("*.md")):
            place(p, f"Career Agent/experience/{p.name}", area)
        for p in sorted((d / "career").glob("*.md")):
            place(p, f"Career Agent/{p.name}", area)
        for p in sorted((d / "agents").rglob("*")) if (d / "agents").is_dir() else []:
            if p.is_file():
                place(p, p.relative_to(d / "agents").as_posix(), area)

    facts, seen = [], set()
    for area in career_areas:
        for f in jsonl(staging / area / "facts.jsonl"):
            key = (f.get("category"), norm(f.get("title")), norm(f.get("organization")), str(f.get("source") or ""))
            if key in seen:
                continue
            seen.add(key)
            row = {k: f.get(k) for k in ("category", "title", "organization", "start", "end", "detail", "source",
                                         "quote", "work", "confidential") if k in f}
            if row.get("confidential"):
                row["quote"] = ""
            row["source"] = public_path(str(row.get("source") or ""), confidential)
            facts.append(row)
    facts.sort(key=lambda f: CATEGORY_ORDER.index(f["category"]) if f.get("category") in CATEGORY_ORDER else 99)
    (career / "facts.jsonl").write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in facts), encoding="utf-8")
    lines = ["# Facts with sources", "",
             f"Every fact about {name} found in the files, with the file it comes from and a quote where the file has a "
             "text layer (scans and files inside archives have none). Facts from employer material are marked "
             "confidential and shown without quotes. The app imports the same list from facts.jsonl.", ""]
    for cat in CATEGORY_ORDER:
        rows = [f for f in facts if f.get("category") == cat]
        if not rows:
            continue
        lines += [f"## {CATEGORY_TITLES[cat]}", ""]
        for f in rows:
            head = f"**{(f.get('title') or '').strip()}**"
            if f.get("organization"):
                head += f", {f['organization'].strip()}"
            if date_range(f):
                head += f" ({date_range(f)})"
            detail = (f.get("detail") or "").strip()
            tail = " · confidential" if f.get("confidential") else (f' · “{f["quote"].strip()}”' if f.get("quote") else "")
            work = f" · {f['work']} work" if f.get("work") in ("solo", "group") else ""
            lines.append(f"- {head}{': ' + detail if detail else ''}  \n  _{f.get('source') or 'no file'}_{work}{tail}")
        lines.append("")
    (career / "facts.md").write_text("\n".join(lines), encoding="utf-8")

    sources: dict[str, dict] = {}
    for area in career_areas:
        for s in jsonl(staging / area / "sources.jsonl"):
            raw = str(s.get("path") or "").strip("/")
            path = public_path(raw, confidential)
            if not path:
                continue
            s = {**s, "path": path, "area": area}
            if path != raw:
                s.update(summary="", why="Employer material; file names left out (they can name clients).")
            old = sources.get(path)
            if old is None or (s.get("used") and not old.get("used")):
                sources[path] = {**(old or {}), **{k: v for k, v in s.items() if v not in ("", None)}}
            else:
                for k, v in s.items():
                    old.setdefault(k, v)
    rows = sorted(sources.values(), key=lambda s: s["path"].lower())
    (career / "sources.jsonl").write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in rows), encoding="utf-8")
    lines = ["# Sources", "",
             "Every file or folder the summaries are based on, and what was skipped and why. Authorship: own = written "
             f"by {name} alone, group = written with others, about-me = written by others about {name} (diplomas, "
             "references), others = not about them.", ""]
    for used, title in ((True, "Used"), (False, "Skipped")):
        part = [s for s in rows if bool(s.get("used")) == used]
        if not part:
            continue
        lines += [f"## {title} ({len(part)})", "", "| Path | Kind | Authorship | Date | Notes |", "|---|---|---|---|---|"]
        for s in part:
            note = (s.get("summary") if used else s.get("why")) or s.get("why") or ""
            if used and s.get("kind") == "application" and s.get("company"):
                note = f"{s['company']}{' – ' + s['role'] if s.get('role') else ''}; outcome {s.get('outcome', 'unknown')}. {note}"
            cells = [s["path"], s.get("kind", ""), s.get("authorship", ""), str(s.get("date", "")), re.sub(r"\s+", " ", note)]
            lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
        lines.append("")
    (career / "sources.md").write_text("\n".join(lines), encoding="utf-8")

    for dest, group, title in ((career / "notes-by-area.md", career_areas, "Notes by area: conflicts, gaps and questions"),
                               (out / "open-questions.md", [a for a in areas if a in general], "Open questions")):
        parts = [f"# {title}", ""]
        for area in group:
            p = staging / area / "notes.md"
            if p.is_file():
                body = re.sub(r"^#.*\n", "", p.read_text(encoding="utf-8", errors="replace"), count=1).strip()
                parts += [f"## {area}", "", body, ""]
        if len(parts) > 2:
            dest.write_text("\n".join(parts), encoding="utf-8")
    return {"facts": len(facts), "sources": len(rows), "files": len(written), "notes": skipped}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("staging", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--general", nargs="*", default=["general"], help="areas whose files are for any agent, not career facts")
    ap.add_argument("--confidential", nargs="*", default=[], help="folders whose file paths must not be published in full")
    ap.add_argument("--name", default="the person", help="first name used in the generated text")
    a = ap.parse_args()
    r = build(a.staging, a.out, a.general, a.confidential, a.name)
    for note in r["notes"]:
        print(note)
    print(f"{r['facts']} facts, {r['sources']} sources, {r['files']} files copied")


if __name__ == "__main__":
    main()
