"""Redo work with another model: read documents again, score jobs again, write new drafts, fact-check again,
rebuild the style guide. Each redo uses the model you pick (or the configured ones) and keeps what matters:
earlier drafts stay under Versions, and facts you've ticked or hidden stay.

Documents whose facts came from the agent folder aren't re-read here (they were never read by a model in this
app): improve the folder and import it again instead.
"""
from __future__ import annotations

from . import db, llm, settings, tasks

KINDS = {
    "screen": "Screen job listings again: the ones the quick screening dropped, and the ones it kept",
    "ads": "Read the job ads again, and then score them again",
    "documents": "Read your documents again (their unticked facts are replaced)",
    "jobs": "Score open jobs again",
    "drafts": "Write new drafts of applications you haven't sent (the current ones stay under Versions)",
    "checks": "Fact-check unsent drafts again",
    "style": "Rebuild your writing style guide",
}
PURPOSES = {"screen": ("triage",), "ads": ("parse_job",), "documents": ("analyze_document",), "jobs": ("fit_score",), "drafts": ("write_letter", "write_cv"),
            "checks": ("claim_check",), "style": ("style_guide",)}


def _latest_models(purposes: tuple[str, ...]) -> dict[str, str]:
    """ref -> model behind the latest successful call for these purposes."""
    marks = ",".join("?" * len(purposes))
    rows = db.q(f"SELECT ref, model FROM llm_calls WHERE id IN (SELECT MAX(id) FROM llm_calls WHERE ok = 1 "
                f"AND ref IS NOT NULL AND purpose IN ({marks}) GROUP BY ref)", purposes)
    return {r["ref"]: r["model"] for r in rows}


def candidates(only_model: str | None = None) -> dict[str, list[str]]:
    """Per kind, the refs a redo would touch; with `only_model`, only work that model made."""
    read = _latest_models(PURPOSES["documents"])
    found = {
        # Listings that came from a job source and that you haven't acted on: screened again in one task.
        "screen": [f"job:{r['id']}" for r in db.q(
            "SELECT id FROM jobs WHERE source_id IS NOT NULL AND status IN ('new', 'filtered') "
            "AND id NOT IN (SELECT job_id FROM applications) ORDER BY id")],
        "ads": [f"job:{r['id']}" for r in db.q("SELECT id FROM jobs WHERE parse_status = 'parsed' "
                                               "AND status IN ('new', 'shortlisted') ORDER BY id")],
        "documents": [f"doc:{r['id']}" for r in db.q("SELECT id FROM documents WHERE status = 'analyzed' ORDER BY id")
                      if f"doc:{r['id']}" in read],
        "jobs": [f"job:{r['id']}" for r in db.q("SELECT id FROM jobs WHERE parse_status = 'parsed' "
                                                "AND status IN ('new', 'shortlisted') ORDER BY id")],
        "drafts": [f"app:{r['id']}" for r in db.q("SELECT id FROM applications WHERE status IN ('draft', 'ready') "
                                                  "AND (letter IS NOT NULL OR cv IS NOT NULL) ORDER BY id")],
        "style": ["style"] if settings.get("style_guide") else [],
    }
    found["checks"] = list(found["drafts"])
    if only_model:
        for kind, refs in found.items():
            if kind == "screen":  # screening is recorded per source, not per job: "only this model's work" can't narrow it
                continue
            latest = _latest_models(PURPOSES[kind])
            found[kind] = [r for r in refs if latest.get(r) == only_model]
    return found


def models_used() -> list[tuple[str, str]]:
    """Models that made something in this app, most used first: [(model, label)]."""
    rows = db.q("SELECT provider, model, COUNT(*) AS n FROM llm_calls WHERE ok = 1 AND ref IS NOT NULL "
                "GROUP BY provider, model ORDER BY n DESC")
    return [(r["model"], f"{llm.model_label(r['provider'], r['model'])} · {r['n']} calls") for r in rows]


def enqueue(kinds: list[str], model: str | None = None, only_model: str | None = None) -> dict[str, int]:
    """Queue the redo. A new draft is fact-checked as part of writing it, so drafts and checks don't double up."""
    found = candidates(only_model)
    extra = {"model": model} if llm.parse_choice(model) else {}
    counts: dict[str, int] = {}
    for kind in [k for k in KINDS if k in kinds]:  # in the order the work depends on: screen, read, score, write, check
        refs = found.get(kind, [])
        if kind == "checks" and "drafts" in kinds:
            refs = [r for r in refs if r not in found.get("drafts", [])]
        if kind == "jobs" and "ads" in kinds:  # an ad that is read again is scored again as part of that
            refs = [r for r in refs if r not in found.get("ads", [])]
        if kind == "screen":
            if refs:
                tasks.enqueue("rescreen_jobs", {**extra}, ref="screen")
            counts[kind] = len(refs)
            continue
        for ref in refs:
            num = int(ref.split(":", 1)[1]) if ":" in ref else 0
            if kind == "ads":
                has_url = db.scalar("SELECT url IS NOT NULL AND url != '' FROM jobs WHERE id = ?", (num,))
                tasks.enqueue("parse_job", {"job_id": num, **({} if has_url else {"use_text": True}), **extra}, ref=ref)
            elif kind == "documents":
                tasks.enqueue("analyze_document", {"doc_id": num, "replace": True, **extra}, ref=ref)
            elif kind == "jobs":
                tasks.enqueue("score_job", {"job_id": num, **extra}, ref=ref)
            elif kind == "drafts":
                tasks.enqueue("draft_application", {"application_id": num, **extra}, ref=ref)
            elif kind == "checks":
                tasks.enqueue("check_application", {"application_id": num, **extra}, ref=ref)
            elif kind == "style":
                settings.update("state", {"style_locked": False})
                tasks.enqueue("build_style", {**extra}, ref="style")
        counts[kind] = len(refs)
    return counts
