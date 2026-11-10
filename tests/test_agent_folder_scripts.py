"""The agent-folder scripts (scripts/agent-folder): merging staging areas and checking quotes, on made-up data."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "agent-folder"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"agent_folder_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def jl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def make_staging(tmp: Path) -> Path:
    s = tmp / "staging"
    write(s / "cv/experience/work-acme.md", "# Developer, Acme (2022–present)\nBuilt things.\n")
    write(s / "cv/career/cv.md", "# CVs\nWhat the CVs say.\n")
    write(s / "cv/career/profile.md", "# Hand-written elsewhere, so never copied\n")
    write(s / "cv/facts.jsonl", jl([
        {"category": "experience", "title": "Developer", "organization": "Acme", "start": "2022", "end": "present",
         "detail": "Builds integrations.", "source": "Job/cv.txt", "quote": "Developer, Acme, 2022 – nu",
         "work": "solo", "confidential": False},
        {"category": "project", "title": "Billing integration", "organization": "Acme", "detail": "Client work.",
         "source": "Work/Acme/01_Repos/ClientX/billing/README.md", "quote": "ClientX billing bridge",
         "work": "group", "confidential": True},
    ]))
    write(s / "cv/sources.jsonl", jl([
        {"path": "Job/cv.txt", "kind": "cv", "authorship": "own", "used": True, "summary": "The CV."},
        {"path": "Work/Acme/01_Repos/ClientX/billing/README.md", "kind": "notes", "used": True,
         "summary": "ClientX billing bridge."},
    ]))
    write(s / "cv/notes.md", "# Notes\n- Two CVs disagree on the start year.\n")
    write(s / "general/agents/nextcloud-map.md", "# Map\nWhat's where.\n")
    write(s / "general/agents/Housekeeping/README.md", "# Housekeeping\n")
    write(s / "general/notes.md", "# Notes\n- Decide about the old photos.\n")
    write(s / "_scratch/facts.jsonl", jl([{"category": "skill", "title": "Ignored"}]))
    return s


def test_merge_places_files_and_hides_confidential_paths(tmp_path):
    merge = load("merge")
    out = tmp_path / "out" / "Agents"
    r = merge.build(make_staging(tmp_path), out, general=["general"], confidential=["Work/Acme"], name="Sara")
    assert (out / "Career Agent/experience/work-acme.md").is_file()
    assert (out / "Career Agent/cv.md").is_file() and (out / "nextcloud-map.md").is_file()
    assert (out / "Housekeeping/README.md").is_file()
    assert not (out / "Career Agent/profile.md").exists() and any("hand-written" in n for n in r["notes"])

    facts = [json.loads(line) for line in (out / "Career Agent/facts.jsonl").read_text().splitlines()]
    assert len(facts) == r["facts"] == 2
    secret = next(f for f in facts if f["confidential"])
    assert secret["source"] == "Work/Acme/01_Repos/" and secret["quote"] == ""
    published = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert "ClientX" not in published and "Ignored" not in published
    assert "Developer, Acme, 2022 – nu" in (out / "Career Agent/facts.md").read_text()
    assert "Two CVs disagree" in (out / "Career Agent/notes-by-area.md").read_text()
    assert "old photos" in (out / "open-questions.md").read_text()


def test_verify_checks_quotes_and_flags_sensitive_text(tmp_path):
    verify = load("verify")
    root = tmp_path / "root"
    write(root / "Job/cv.txt", "Sara Lund\nDeveloper, Acme, 2022 – nu\nBuilds integrations for customers.\n")
    s = tmp_path / "staging"
    write(s / "cv/facts.jsonl", jl([
        {"category": "experience", "title": "Developer", "source": "Job/cv.txt", "quote": "Developer, Acme, 2022 – nu"},
        {"category": "skill", "title": "Go", "source": "Job/cv.txt", "quote": "Wrote Go every day"},
        {"category": "made_up", "title": "X", "source": "Job/missing.txt", "quote": "anything"},
    ]))
    write(s / "cv/experience/a.md", "# A\nHe built it. Call 12 34 56 78 or mail sara@example.com. "
                                     "The file from 20260304 is a date. ID 010203-1234.\n")
    r = verify.check(s, root)["cv"]
    assert r["quotes"] == {"ok": 1, "not found": 1, "source missing": 1}
    problems = " ".join(r["problems"])
    for expected in ("unknown category", "e-mail address", "phone-like number", "ID-like number"):
        assert expected in problems
    assert "20260304" not in problems  # a date isn't a phone number
    assert "pronoun" not in problems
    assert "pronoun 'He'" in " ".join(verify.check(s, root, pronouns=True)["cv"]["problems"])
