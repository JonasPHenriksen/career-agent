"""Employers, and how they treat candidates: response rates, speed, feedback, ghosting, reposts."""
from __future__ import annotations

import re
import unicodedata
from statistics import median

from . import db

_SUFFIX = re.compile(r"\b(a/s|aps|a\.s\.|ab|as|asa|gmbh|ltd|limited|inc|llc|i/s|p/s|plc|oy|bv|nv|sa|ag)\b\.?", re.I)


def norm_name(name: str | None) -> str:
    s = unicodedata.normalize("NFKD", (name or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"[^\w]+", " ", s).strip()


def upsert(name: str | None, website: str | None = None, description: str | None = None,
           location: str | None = None) -> int | None:
    name = " ".join((name or "").split())[:200]
    if not name or norm_name(name) in ("", "unknown", "ukendt", "confidential", "fortrolig"):
        return None
    key = norm_name(name)
    row = next((r for r in db.q("SELECT id, name, website, description, location FROM companies")
                if norm_name(r["name"]) == key), None)
    if row:
        patch = {}
        if website and not row["website"]:
            patch["website"] = website[:300]
        if description and not row["description"]:
            patch["description"] = description[:1500]
        if location and not row["location"]:
            patch["location"] = location[:200]
        if patch:
            db.update("companies", row["id"], patch)
        return row["id"]
    return db.insert("companies", {
        "name": name, "website": (website or "")[:300] or None, "description": (description or "")[:1500] or None,
        "location": (location or "")[:200] or None, "created_at": db.now(),
    })


def _days(a: str | None, b: str | None) -> float | None:
    da, dbb = db.parse_ts(a), db.parse_ts(b)
    if not da or not dbb:
        return None
    return (dbb - da).total_seconds() / 86400


def metrics(company_id: int) -> dict:
    apps = db.q(
        "SELECT a.* FROM applications a JOIN jobs j ON j.id = a.job_id "
        "WHERE j.company_id = ? AND a.applied_at IS NOT NULL", (company_id,))
    events = db.q("SELECT * FROM events WHERE company_id = ?", (company_id,))
    now = db.now()
    mature = [a for a in apps if a["first_response_at"] or (_days(a["applied_at"], now) or 0) >= 14]
    responded = [a for a in apps if a["first_response_at"]]
    waits = [d for a in responded if (d := _days(a["applied_at"], a["first_response_at"])) is not None]
    by_app: dict[int, set[str]] = {}
    for e in events:
        by_app.setdefault(e["application_id"], set()).add(e["type"])
    interviewed = [a for a in apps if {"interview_invite", "interview_done"} & by_app.get(a["id"], set())]
    generic_after_interview = sum(1 for a in interviewed if "rejected_generic" in by_app.get(a["id"], set()))
    jobs_seen = db.scalar("SELECT COUNT(*) FROM jobs WHERE company_id = ?", (company_id,)) or 0
    reposts = db.scalar("SELECT COUNT(*) FROM jobs WHERE company_id = ? AND reposted = 1", (company_id,)) or 0
    m = {
        "applications": len(apps),
        "mature": len(mature),
        "responded": len(responded),
        "response_rate": (len(responded) / len(mature)) if mature else None,
        "median_days": round(median(waits), 1) if waits else None,
        "ghosted": sum(1 for a in apps if a["status"] == "ghosted"),
        "generic_rejections": sum(1 for e in events if e["type"] == "rejected_generic"),
        "feedback": sum(1 for e in events if e["type"] == "rejected_feedback" or (e["feedback"] or "").strip()),
        "interviews": len(interviewed),
        "offers": sum(1 for e in events if e["type"] == "offer"),
        "generic_after_interview": generic_after_interview,
        "jobs_seen": jobs_seen,
        "reposts": reposts,
    }
    rating = db.scalar("SELECT user_rating FROM companies WHERE id = ?", (company_id,))
    m["score"], m["grade"], m["reasons"] = respect_score(m, rating)
    return m


def respect_score(m: dict, rating: int | None) -> tuple[int | None, str, list[str]]:
    """How well this employer treats candidates (0-100), with the reasons spelled out."""
    reasons: list[str] = []
    if not m["mature"] and not rating:
        return None, "–", ["Not enough history yet"]
    score = 50
    if m["mature"]:
        rr = m["response_rate"] or 0
        score += round(40 * rr - 20)
        reasons.append(f"Answered {m['responded']} of {m['mature']} applications")
    if m["median_days"] is not None:
        if m["median_days"] <= 7:
            score += 10
            reasons.append(f"Fast replies (median {m['median_days']:g} days)")
        elif m["median_days"] <= 14:
            score += 5
            reasons.append(f"Replies in about {m['median_days']:g} days")
        elif m["median_days"] > 30:
            score -= 10
            reasons.append(f"Slow replies (median {m['median_days']:g} days)")
    if m["feedback"]:
        score += 10
        reasons.append("Gave real feedback")
    if m["generic_after_interview"]:
        score -= 10
        reasons.append("Sent a template rejection after an interview")
    if m["ghosted"]:
        score -= 10 * min(m["ghosted"], 3)
        reasons.append(f"Never answered {m['ghosted']} application{'s' if m['ghosted'] > 1 else ''}")
    if m["reposts"]:
        score -= 5 * min(m["reposts"], 3)
        reasons.append(f"Re-posted the same job {m['reposts']} time{'s' if m['reposts'] > 1 else ''}")
    if rating:
        score += (int(rating) - 3) * 5
        reasons.append(f"Your rating: {rating}/5")
    score = max(0, min(100, score))
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D" if score >= 40 else "F"
    return score, grade, reasons


def all_with_metrics() -> list[dict]:
    rows = db.q("SELECT * FROM companies ORDER BY name COLLATE NOCASE")
    for r in rows:
        r["m"] = metrics(r["id"])
    return rows


def badge(company_id: int | None) -> dict | None:
    if not company_id:
        return None
    m = metrics(company_id)
    if m["score"] is None:
        return None
    return {"grade": m["grade"], "score": m["score"], "reasons": m["reasons"]}
