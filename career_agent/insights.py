"""Numbers about your search: funnel, response rates, what works, skills employers ask for."""
from __future__ import annotations

import re
from collections import Counter
from statistics import median

from . import companies, db


def _rate(n: int, d: int) -> float | None:
    return round(100 * n / d) if d else None


def funnel() -> list[dict]:
    jobs_found = db.scalar("SELECT COUNT(*) FROM jobs") or 0
    considered = db.scalar("SELECT COUNT(*) FROM jobs WHERE status != 'filtered'") or 0
    drafted = db.scalar("SELECT COUNT(*) FROM applications") or 0
    applied = db.scalar("SELECT COUNT(*) FROM applications WHERE applied_at IS NOT NULL") or 0
    responded = db.scalar("SELECT COUNT(*) FROM applications WHERE first_response_at IS NOT NULL") or 0
    interviews = db.scalar("SELECT COUNT(DISTINCT application_id) FROM events WHERE type IN ('interview_invite', 'interview_done')") or 0
    offers = db.scalar("SELECT COUNT(DISTINCT application_id) FROM events WHERE type = 'offer'") or 0
    steps = [("Jobs found", jobs_found), ("Worth a look", considered), ("Applications written", drafted),
             ("Sent", applied), ("Got a reply", responded), ("Interviews", interviews), ("Offers", offers)]
    top = max(jobs_found, 1)
    return [{"label": k, "n": v, "pct": round(100 * v / top, 1)} for k, v in steps]


def response_stats() -> dict:
    apps = db.q("SELECT a.*, j.fit_score, j.language FROM applications a JOIN jobs j ON j.id = a.job_id "
                "WHERE a.applied_at IS NOT NULL")
    waits = []
    for a in apps:
        if a["first_response_at"]:
            s, e = db.parse_ts(a["applied_at"]), db.parse_ts(a["first_response_at"])
            if s and e:
                waits.append((e - s).total_seconds() / 86400)
    mature = [a for a in apps if a["first_response_at"] or a["applied_at"] < db.days_ago(14)]
    ghosted = sum(1 for a in apps if a["status"] == "ghosted")
    feedback = db.scalar("SELECT COUNT(*) FROM events WHERE type = 'rejected_feedback'") or 0
    rejections = db.scalar("SELECT COUNT(*) FROM events WHERE type IN ('rejected_feedback', 'rejected_generic')") or 0

    def band(score):
        if score is None:
            return "Not scored"
        return "Fit 80+" if score >= 80 else "Fit 65-79" if score >= 65 else "Fit under 65"

    bands: dict[str, list] = {}
    for a in mature:
        bands.setdefault(band(a["fit_score"]), []).append(a)
    by_band = [{"label": k, "sent": len(v), "replies": sum(1 for a in v if a["first_response_at"]),
                "interviews": sum(1 for a in v if a["status"] in ("interview", "offer", "accepted")),
                "rate": _rate(sum(1 for a in v if a["first_response_at"]), len(v))}
               for k, v in sorted(bands.items())]
    return {
        "sent": len(apps), "mature": len(mature),
        "response_rate": _rate(sum(1 for a in mature if a["first_response_at"]), len(mature)),
        "median_wait": round(median(waits), 1) if waits else None,
        "ghost_rate": _rate(ghosted, len(mature)), "ghosted": ghosted,
        "feedback_rate": _rate(feedback, rejections), "rejections": rejections,
        "by_band": by_band,
    }


_SPLIT = re.compile(r"[,/;()]|\bog\b|\band\b")


def skills_demand(limit: int = 20) -> list[dict]:
    """Keywords from ads you looked at, and whether your career database covers them."""
    rows = db.q("SELECT details FROM jobs WHERE parse_status = 'parsed' AND status != 'filtered'")
    counts: Counter = Counter()
    for r in rows:
        d = db.jload(r["details"], {}) or {}
        seen = set()
        for kw in d.get("keywords") or []:
            k = kw.strip().lower()
            if 1 < len(k) <= 40 and k not in seen:
                counts[k] += 1
                seen.add(k)
    mine = " ".join(f"{f['title']} {f.get('tags') or ''} {f.get('detail') or ''}".lower()
                    for f in db.q("SELECT title, tags, detail FROM facts WHERE hidden = 0"))
    out = []
    for kw, n in counts.most_common(limit):
        out.append({"skill": kw, "jobs": n, "covered": kw in mine})
    return out


def company_rankings() -> dict:
    rows = [c for c in companies.all_with_metrics() if c["m"]["score"] is not None]
    rows.sort(key=lambda c: c["m"]["score"], reverse=True)
    return {"best": rows[:5], "worst": list(reversed(rows[-5:])) if len(rows) > 5 else []}


def spend(days: int = 30) -> dict:
    """Model calls per model: Claude through your subscription counts against your plan's limits, local models are free."""
    from . import llm

    rows = db.q("SELECT provider, model, COUNT(*) AS calls, SUM(input_tokens) AS inp, SUM(output_tokens) AS outp, "
                "SUM(cache_read_tokens) AS cached FROM llm_calls WHERE created_at >= ? "
                "GROUP BY provider, model ORDER BY calls DESC", (db.days_ago(days),))
    merged: dict[str, dict] = {}  # one row per model, also when older records name it differently
    for r in rows:
        label = llm.model_label(r["provider"], r["model"])
        m = merged.setdefault(label, {"label": label, "provider": r["provider"], "calls": 0, "inp": 0, "outp": 0, "cached": 0})
        for k in ("calls", "inp", "outp", "cached"):
            m[k] += r[k] or 0
    return {"rows": sorted(merged.values(), key=lambda m: -m["calls"]), "days": days}
