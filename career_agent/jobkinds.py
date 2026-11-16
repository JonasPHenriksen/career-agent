"""Kinds of job the user may not want (student jobs, part-time, ...), spotted by rules only.

No model is involved: the point is to skip these ads before any tokens are spent on them.
The title is strong evidence, and so are the employment type and level of an ad that has been read.
Body text counts only for a few unmistakable phrases ("we also employ student workers" must not trigger).
"""
from __future__ import annotations

import re

from . import settings

KINDS: list[tuple[str, str]] = [
    ("student", "student job"),
    ("internship", "internship"),
    ("part_time", "part-time job"),
    ("temporary", "temporary job"),
    ("freelance", "freelance job"),
    ("research", "research or academic post"),
    ("volunteer", "volunteer or unpaid job"),
]
LABELS = dict(KINDS)


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.I)


# Word boundaries everywhere, so "international", "internal", "part of" and "Associate" never match.
TITLE_RULES: dict[str, re.Pattern] = {
    "student": _rx(r"\bstudent\b", r"\bwerkstudent\b",
                   r"\bstudenter(?:medhjælper|medarbejder|job|ansat|assistent|stilling)\w*",
                   r"\bstudie(?:job|medhjælper|assistent)\w*"),
    # "intern" alone is Danish for "internal" (intern revisor), so it only counts at the end or before a separator.
    "internship": _rx(r"\binternships?\b", r"\bpraktik\b", r"\bpraktikant\w*", r"\bpraktikophold\b",
                      r"\bpraktikplads\w*", r"\bpraktikforløb\b", r"\bintern\s*$", r"\bintern\s*[-–—,(/|:]"),
    "part_time": _rx(r"\bpart[- ]?time\b", r"\bdeltid\w*", r"\btimeansat\w*", r"\bfritidsjob\b", r"\bweekendjob\b"),
    "temporary": _rx(r"\w*vikar\w*", r"\btidsbegrænset\w*", r"\bmidlertidig\w*", r"\btemporary\b", r"\btemp\b",
                     r"\bfixed[- ]term\b", r"\bmaternity (?:leave )?cover\b", r"\binterim\b", r"\bseasonal\b",
                     r"\bsæson\w*", r"\b\d+[- ]?(?:months?|måneders?|mdr)\b"),
    "freelance": _rx(r"\bfreelanc\w*", r"\bcontractor\b"),
    "research": _rx(r"\bph\.?\s?d\b", r"\bpost[- ]?doc\w*", r"\bprofessor\b", r"\blektor\b", r"\badjunkt\b",
                    r"\bresearch assistant\b", r"\bforskningsassistent\b", r"\bstipendiat\w*", r"\btenure[- ]track\b"),
    "volunteer": _rx(r"\bfrivillig\w*", r"\bvolunteer\w*", r"\bulønnet\b", r"\bunpaid\b"),
}
# A paid job that manages volunteers is not a volunteer job.
VOLUNTEER_MANAGER = re.compile(
    r"coordinator|manager|koordinator|leder|chef|konsulent|consultant|ansvarlig|recruit|rekrutter", re.I)
UNPAID = re.compile(r"\bunpaid\b|\bulønnet\b", re.I)

# What a read ad's employment type says. Mixed values ("full-time or part-time") are not flagged.
EMPLOYMENT_RULES: dict[str, re.Pattern] = {
    "part_time": _rx(r"\bpart[- ]?time\b", r"\bdeltid\w*"),
    "temporary": _rx(r"\btemporary\b", r"\btemp\b", r"\bfixed[- ]term\b", r"\btidsbegrænset\w*", r"\bmidlertidig\w*",
                     r"\w*vikar\w*", r"\bseasonal\b", r"\binterim\b"),
    "freelance": _rx(r"\bfreelanc\w*", r"\bcontractor\b", r"\bself[- ]employed\b", r"\bselvstændig\w*"),
    "internship": _rx(r"\binternships?\b", r"\bpraktik\w*"),
    "student": _rx(r"\bstudent\b", r"\bstudenter\w*", r"\bstudiejob\b"),
    "volunteer": _rx(r"\bvolunteer\b", r"\bfrivillig\w*", r"\bunpaid\b", r"\bulønnet\b"),
}
PERMANENT = re.compile(r"\bpermanent\b|\bfast\b|\bfastans\w*|\btidsubegrænset", re.I)
FULL_TIME = re.compile(r"\bfull[- ]?time\b|\bfuldtid\w*|\bfuld-|\bpermanent\b|\bfast\b|\bfastansat|\btidsubegrænset", re.I)

# Body text: only phrases that can't be about something else in the ad.
TEXT_RULES: dict[str, re.Pattern] = {
    "temporary": _rx(r"\bbarselsvikariat\w*", r"\borlovsvikariat\w*", r"\bmaternity (?:leave )?cover\b"),
    "internship": _rx(r"\bunpaid internship\b"),
}


def kinds_of(title: str | None, text: str | None = None, employment_type: str | None = None,
             seniority: str | None = None) -> list[str]:
    """Every kind this job is, in KINDS order."""
    found: set[str] = set()
    t = (title or "").strip()
    for key, rx in TITLE_RULES.items():
        if not rx.search(t):
            continue
        if key == "volunteer" and VOLUNTEER_MANAGER.search(t) and not UNPAID.search(t):
            continue
        found.add(key)
    emp = (employment_type or "").strip()
    if emp:
        # "permanent" or "full-time" only cancels the soft kinds; an explicit student or internship label still counts
        # "Full-time" says how many hours, not for how long: a full-time maternity cover is still temporary,
        # unless the ad also calls the job permanent.
        keys = tuple(EMPLOYMENT_RULES) if not FULL_TIME.search(emp) else (
            ("internship", "student") if PERMANENT.search(emp) else ("internship", "student", "temporary"))
        found.update(k for k in keys if EMPLOYMENT_RULES[k].search(emp))
    if (seniority or "").strip().lower() == "student":
        found.add("student")
    for key, rx in TEXT_RULES.items():
        if text and rx.search(text):
            found.add(key)
    return [k for k, _ in KINDS if k in found]


def kind_of(title: str | None, text: str | None = None, employment_type: str | None = None,
            seniority: str | None = None) -> str | None:
    """The first kind this job is, or None for an ordinary job."""
    found = kinds_of(title, text, employment_type, seniority)
    return found[0] if found else None


def wanted_kinds() -> dict[str, bool]:
    saved = settings.get("profile").get("job_kinds") or {}
    return {k: bool(saved.get(k, True)) for k, _ in KINDS}


def unwanted(title: str | None, text: str | None = None, employment_type: str | None = None,
             seniority: str | None = None) -> tuple[str, str] | None:
    """(key, label) when the job is of a kind the user has switched off."""
    wanted = wanted_kinds()
    if all(wanted.values()):
        return None
    for key in kinds_of(title, text, employment_type, seniority):
        if not wanted[key]:
            return key, LABELS[key]
    return None


def skip_note(label: str) -> str:
    return f"Skipped: {label}. You've switched these off under Profile → About you."
