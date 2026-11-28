"""Which jobs to look at first.

Two scores, kept apart so nothing counts twice:
- fit (jobs.score, the model): could you get this job? How well you meet what the ad asks for. It never sees where
  the job is, how big the employer is, or whether it's the kind of job you want;
- rank (here): is it worth your time? The fit, less points for what the fit leaves out:
  - distance: PER_KM for every km from home in a straight line, up to your limit (Profile → About you). Hybrid
    counts in full (you're at the office much of the time, most of all at the start), remote not at all, and a job
    that doesn't say counts as on site. When the ad names a main workplace among several, the distance is to that
    one. A place the app can't measure (none stated, or not in places.py) costs UNKNOWN_PLACE_POINTS, what a job in
    Aarhus costs, since most jobs are there; one abroad costs as much as your limit;
  - the kind of job: ROLE_POINTS when it is next to or outside your target roles;
  - DEALBREAKER_POINTS for a conflict with a deal-breaker of yours that isn't about place.
  The last two come from fit verdicts made since jobs.FIT_BASIS 2; on older ones they're already in the score.
  Employer size can count too (Profile → About you; companies.py judges the size), but is off: wanting a big
  firm's structure and facing its competition cancel out, and most employers near Horsens are big.

The numbers were set on 2026-11-28 from choices between pairs of jobs, then scaled for a job hunt with a deadline:
near home is better, but a good fit in Aarhus (40 km, −10) should beat a weak one in Horsens. The pairs put Aarhus
at −18, a job next to your targets at −13 and one outside them at −30.

"Most urgent" knows that a job without a date usually says to apply as soon as possible: it counts as due a week
after it was posted, while an ad posted long ago without a date may be filled already or hire all year round."""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import db, jobs, profile, settings

PER_KM = 0.25
UNKNOWN_PLACE_POINTS = 10
ROLE_POINTS = {"related": 7, "outside": 30}
ROLE_WHY = {"related": "next to your target roles", "outside": "outside your target roles"}
DEALBREAKER_POINTS = 40
SIZE_POINTS = {"off": {}, "moderate": {"large": 4, "very_large": 8}, "strong": {"large": 8, "very_large": 16}}
SIZE_WHY = {"large": "large employer", "very_large": "very large or very well-known employer"}
ROLLING_DAYS = 7  # "apply as soon as possible" counts as due this long after the ad was posted…
SOONEST_DAYS = 2  # …but never sooner than this from today, so a deadline in the next day or two still comes first
STALE_DAYS = 28  # posted longer ago than this without a date: it may be filled, or they hire all year round
LONG_RUNNING_DAYS = 91  # up this long without a date: worth asking whether it's still open before writing
_UNSOLICITED = re.compile(r"uopfordret|unsolicited|spontan|open application|åben ansøgning", re.I)


def size_nudge() -> str:
    v = settings.get("profile").get("big_employers") or "off"
    return v if v in SIZE_POINTS else "off"


def distance_points(km: float | None, work_mode: str | None, limit: float | None) -> int:
    """PER_KM a km from home; a job further out than your limit (kept anyway) costs no more than one at it."""
    if km is None or work_mode == "remote":
        return 0
    return round(PER_KM * (min(km, limit) if limit else km))


def _place_points(r: dict, reach: dict | None, limit: float | None) -> tuple[int, str] | None:
    """What the job's place costs, with the reason shown on the page."""
    if r.get("work_mode") == "remote":
        return None
    if reach and reach["km"] is not None:
        n = distance_points(reach["km"], r.get("work_mode"), limit)
        return (n, f"{reach['town']}, {reach['km']} km" + (" (main workplace)" if reach.get("main") else "")
                + (" (hybrid)" if r.get("work_mode") == "hybrid" else "")) if n else None
    if reach and reach["abroad"]:
        return distance_points(limit or 50, None, limit), "outside Denmark"
    where = (r.get("location") or "").strip()
    return UNKNOWN_PLACE_POINTS, f"distance not known ({where[:40]})" if where else "location not stated"


def _wish_points(fit: dict) -> list[tuple[int, str]]:
    """Role and deal-breakers from a fit verdict made on the current basis (jobs.FIT_BASIS)."""
    if (fit.get("basis") or 0) < jobs.FIT_BASIS:
        return []
    out = []
    if ROLE_POINTS.get(fit.get("role_match")):
        out.append((ROLE_POINTS[fit["role_match"]], ROLE_WHY[fit["role_match"]]))
    if fit.get("dealbreakers"):
        out.append((DEALBREAKER_POINTS, "deal-breaker: " + str(fit["dealbreakers"][0])[:80]))
    return out


def annotate(rows: list[dict]) -> list[dict]:
    """Give each job row (from the jobs table) r["rank"]: {"score": fit minus the points, "points": [(n, why)…],
    "km", "size"}. The score is None for a job not scored yet."""
    p = settings.get("profile")
    home, limit, per_size = p.get("city"), profile.distance_limit(), SIZE_POINTS[size_nudge()]
    ids = sorted({r["company_id"] for r in rows if r.get("company_id")})
    sizes = {c["id"]: c["size"] for c in db.q(
        f"SELECT id, size FROM companies WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    for r in rows:
        reach = jobs.reach_of(r.get("location"), jobs.main_location(r), home, limit) if home else None
        points: list[tuple[int, str]] = []
        place = _place_points(r, reach, limit) if home else None
        if place:
            points.append(place)
        size = sizes.get(r.get("company_id"))
        if per_size.get(size):
            points.append((per_size[size], SIZE_WHY[size]))
        fit_verdict = r.get("f") if isinstance(r.get("f"), dict) else db.jload(r.get("fit"), {}) or {}
        points += _wish_points(fit_verdict)
        fit = r.get("fit_score")
        r["rank"] = {"score": None if fit is None else fit - sum(n for n, _ in points), "points": points,
                     "km": reach["km"] if reach else None, "size": size}
    return rows


def best_first(rows: list[dict]) -> list[dict]:
    """Highest ranked first; jobs not scored yet last, newest first among them."""
    return sorted(annotate(rows), key=lambda r: (r["rank"]["score"] is None, -(r["rank"]["score"] or 0), -r["id"]))


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def urgency(r: dict, today: date | None = None) -> dict:
    """How soon to apply: {"kind": "dated" | "rolling" | "stale" | "open" | "gone", "due": date or None, "note",
    "age_days", "since", "long_running"}. Long running: up for months without a date, so ask first.
    Takes a job row, or an application row joined with its job's deadline, details, title, posted_at and
    job_created_at."""
    today = today or date.today()
    d = db.jload(r.get("details"), {}) or {}
    note = d.get("deadline_note") or ""
    seen = _day(r.get("posted_at")) or _day(r.get("job_created_at") or r.get("created_at")) or today
    age = (today - seen).days
    soon = max(seen + timedelta(days=ROLLING_DAYS), today + timedelta(days=SOONEST_DAYS))
    out = {"note": note, "age_days": age, "listing_ends": d.get("listing_ends"), "since": seen.isoformat(),
           "long_running": not r.get("deadline") and age >= LONG_RUNNING_DAYS}
    if r.get("gone_at") and not r.get("open_said_at"):  # the ad was found gone (adcheck.py)
        return {**out, "kind": "gone", "due": None}
    deadline = _day(r.get("deadline"))
    if deadline:
        # "Ansøgningsfrist 30. november, samtaler løbende": the job may go before the date
        due = min(deadline, soon) if jobs.rolling_note(note) and age <= STALE_DAYS else deadline
        return {**out, "kind": "dated", "due": due}
    if _UNSOLICITED.search(r.get("title") or r.get("job_title") or ""):
        return {**out, "kind": "open", "due": None}
    if age > STALE_DAYS:
        return {**out, "kind": "stale", "due": None}
    return {**out, "kind": "rolling", "due": soon}


_GROUP = {"dated": 0, "rolling": 0, "stale": 1, "open": 2, "gone": 3}
POOR_BELOW = 50  # ranked lower than this, a job isn't worth hurrying for: it goes after the rest


def urgent_first(rows: list[dict]) -> list[dict]:
    """Soonest due first (a rolling job counts as due a week after it was posted); then ads posted long ago
    without a date; then open applications; poor matches after all of those, and ads found gone last. Ties: highest
    ranked first."""
    today = date.today()
    for r in rows:
        r["urgency"] = urgency(r, today)
    if rows and "fit_score" in rows[0]:
        annotate(rows)

    def key(r):
        score = (r.get("rank") or {}).get("score")
        return (r["urgency"]["kind"] == "gone", score is not None and score < POOR_BELOW, _GROUP[r["urgency"]["kind"]],
                r["urgency"]["due"] or date.max, -(score or 0))
    return sorted(rows, key=key)
