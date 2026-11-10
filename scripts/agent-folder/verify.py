#!/usr/bin/env python3
"""Check the subagents' staging output before anything is merged or published.

Usage: uv run --project <repo> python -I scripts/agent-folder/verify.py STAGING --root ~/Nextcloud [AREA ...] [--pronouns]

For every area (a subfolder of STAGING): each fact's and example's quote is looked up in the extracted text of its
source file, the JSONL files are parsed, and every Markdown/JSONL line is scanned for things that look like ID
numbers, e-mail addresses, phone numbers, account numbers or password fields. Writes STAGING/_verify.json.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
from career_agent import ingest  # noqa: E402
from extract import text_of  # noqa: E402

CATEGORIES = {"experience", "education", "project", "achievement", "skill", "certification", "language",
              "volunteering", "value", "personal"}
SENSITIVE = {
    "ID-like number": re.compile(r"\b\d{6}-?\d{4}\b"),
    "e-mail address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone-like number": re.compile(r"(?<![\d.,-])(?:\+\d{2}[ ]?)?\d{2}[ ]?\d{2}[ ]?\d{2}[ ]?\d{2}(?![\d.,-])"),
    "account number": re.compile(r"\b[A-Z]{2}\d{2}[ ]?\d{4}[ ]?\d{4}|\breg\.? ?nr\b|\bkontonr\b", re.I),
    "password field": re.compile(r"\b(password|adgangskode|api[_ -]?key|secret|token)\s*[:=]", re.I),
}
DATE_LIKE = re.compile(r"(19|20)\d{6}")  # 20260304 in a file name is a date, not a phone number
PRONOUNS = re.compile(r"\b(he|him|his|himself|she|hers|herself)\b", re.I)  # "her" is also Danish for "here"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace("­", "")
    s = re.sub(r"[‘’‚‛′]", "'", s)
    s = re.sub(r"[“”„‟″«»]", '"', s)
    s = re.sub(r"[‐-―−]", "-", s)
    s = re.sub(r"-\s*\n\s*", "-", s)  # hyphenation at line ends
    return re.sub(r"\s+", " ", s).strip().lower()


class Sources:
    def __init__(self, root: Path):
        self.root, self._cache = root, {}

    def text(self, rel: str) -> str | None:
        if rel not in self._cache:
            p = self.root / rel
            try:
                self._cache[rel] = norm(ingest.clean_text(text_of(p))) if p.is_file() else None
            except Exception:  # noqa: BLE001 - scans, damaged files, unsupported types
                self._cache[rel] = None
        return self._cache[rel]

    def check_quote(self, quote: str, rel: str) -> str:
        if not quote.strip():
            return "no quote"
        if re.search(r"\.(zip|7z|rar)/", rel, re.I):
            return "inside an archive"
        text = self.text(rel)
        if text is None:
            return "source unreadable" if (self.root / rel).exists() else "source missing"
        q = norm(quote)
        if q in text:
            return "ok"
        m = difflib.SequenceMatcher(None, q, text, autojunk=False).find_longest_match(0, len(q), 0, len(text))
        if m.size >= 0.9 * len(q):
            return "ok (near)"
        if q.replace(" ", "") in text.replace(" ", ""):  # PDF text sometimes drops or adds spaces
            return "ok (spacing)"
        return "not found"


def jsonl(path: Path) -> tuple[list[dict], list[str]]:
    rows, bad = [], []
    if path.is_file():
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as e:
                bad.append(f"line {n}: {e}")
                continue
            rows.append(row) if isinstance(row, dict) else bad.append(f"line {n}: not an object")
    return rows, bad


def check_area(d: Path, src: Sources, pronouns: bool = False) -> dict:
    out: dict = {"problems": [], "quotes": {}, "examples": {}}
    facts, bad = jsonl(d / "facts.jsonl")
    out["problems"] += [f"facts.jsonl {b}" for b in bad]
    for i, f in enumerate(facts, 1):
        if f.get("category") not in CATEGORIES:
            out["problems"].append(f"fact {i}: unknown category {f.get('category')!r}")
        status = src.check_quote(str(f.get("quote") or ""), str(f.get("source") or "").strip("/"))
        out["quotes"][status] = out["quotes"].get(status, 0) + 1
        if status in ("not found", "source missing"):
            out["problems"].append(f"fact {i} ({f.get('title')!r}): quote {status}: {str(f.get('quote'))[:90]!r}")
    examples, bad = jsonl(d / "examples.jsonl")
    out["problems"] += [f"examples.jsonl {b}" for b in bad]
    for i, e in enumerate(examples, 1):
        status = src.check_quote(str(e.get("quote") or ""), str(e.get("source") or "").strip("/"))
        out["examples"][status] = out["examples"].get(status, 0) + 1
        if not status.startswith("ok"):
            out["problems"].append(f"example {i}: {status}: {str(e.get('quote'))[:90]!r}")
    sources, bad = jsonl(d / "sources.jsonl")
    out["problems"] += [f"sources.jsonl {b}" for b in bad]
    out["facts"], out["sources"] = len(facts), len(sources)
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.suffix not in (".md", ".jsonl"):
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for label, rx in SENSITIVE.items():
                for m in rx.finditer(line):
                    if label == "phone-like number" and DATE_LIKE.fullmatch(m.group(0).replace(" ", "")):
                        continue
                    out["problems"].append(f"{p.relative_to(d)}:{n}: {label}: {m.group(0)!r}")
            if pronouns and p.suffix == ".md":
                for m in PRONOUNS.finditer(line):
                    out["problems"].append(f"{p.relative_to(d)}:{n}: pronoun {m.group(0)!r}")
    return out


def check(staging: Path, root: Path, areas: list[str] | None = None, pronouns: bool = False) -> dict:
    src = Sources(root)
    names = areas or sorted(p.name for p in staging.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    return {name: check_area(staging / name, src, pronouns) for name in names}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("staging", type=Path)
    ap.add_argument("areas", nargs="*")
    ap.add_argument("--root", type=Path, default=Path("~/Nextcloud"), help="the folder the source paths are relative to")
    ap.add_argument("--pronouns", action="store_true", help="also flag he/she/his/hers in summaries")
    a = ap.parse_args()
    report = check(a.staging, a.root.expanduser(), a.areas, a.pronouns)
    for name, r in report.items():
        print(f"== {name}: {r['facts']} facts, quotes {r['quotes']}" + (f", examples {r['examples']}" if r["examples"] else "")
              + f", {r['sources']} source lines, {len(r['problems'])} problems")
        for prob in r["problems"]:
            print("   -", prob)
    (a.staging / "_verify.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
