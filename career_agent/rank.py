"""Which jobs to look at first.

The fit score says how well you match a job, and stays as it is. Two things it leaves out decide where your time is
best spent, so the order you see takes a few points off for them:
- distance: a job further away than the distance you'd rather stay within (Profile → About you) ranks lower the
  further out it is, up to MAX_DISTANCE_POINTS at your limit; hybrid work half as much, remote not at all;
- competition: big, well-known employers get the most applicants for every job (companies.py judges the size).

"Most urgent" knows that a job without a date usually says to apply as soon as possible: it counts as due a week
after it was posted, while an ad posted long ago without a date may be filled already or hire all year round."""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import db, jobs, places, profile, settings

MAX_DISTANCE_POINTS = 12
SIZE_POINTS = {"off": {}, "moderate": {"large": 4, "very_large": 8}, "strong": {"large": 8, "very_large": 16}}
SIZE_WHY = {"large": "large employer", "very_large": "very large or very well-known employer"}
ROLLING_DAYS = 7  # "apply as soon as possible" counts as due this long after the ad was posted…
SOONEST_DAYS = 2  # …but never sooner than this from today, so a deadline in the next day or two still comes first
STALE_DAYS = 28  # posted longer ago than this without a date: it may be filled, or they hire all year round
LONG_RUNNING_DAYS = 91  # up this long without a date: worth asking whether it's still open before writing
_UNSOLICITED = re.compile(r"uopfordret|unsolicited|spontan|open application|åben ansøgning", re.I)


def preferred_km() -> float | None:
    """The distance you'd rather stay within, in km, or None."""
    try:
        km = float(settings.get("profile").get("preferred_km") or 0)
    except (TypeError, ValueError):
        return None
    return km if km > 0 else None


def size_nudge() -> str:
    v = settings.get("profile").get("big_employers") or "moderate"
    return v if v in SIZE_POINTS else "moderate"


def distance_points(km: float | None, work_mode: str | None, preferred: float | None, limit: float | None) -> int:
    if km is None or not preferred or work_mode == "remote" or km <= preferred:
        return 0
    span = limit - preferred if limit and limit > preferred else preferred
    points = MAX_DISTANCE_POINTS * min(1.0, (km - preferred) / span)
    return round(points / 2 if work_mode == "hybrid" else points)


def annotate(rows: list[dict]) -> list[dict]:
    """Give each job row (from the jobs table) r["rank"]: {"score": fit minus the points, "points": [(n, why)…],
    "km", "size"}. The score is None for a job not scored yet."""
    p = settings.get("profile")
    home, limit, preferred, per_size = p.get("city"), profile.distance_limit(), preferred_km(), SIZE_POINTS[size_nudge()]
    ids = sorted({r["company_id"] for r in rows if r.get("company_id")})
    sizes = {c["id"]: c["size"] for c in db.q(
        f"SELECT id, size FROM companies WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    for r in rows:
        reach = places.reach(r.get("location"), home, limit) if home else None
        km = reach["km"] if reach else None
        points: list[tuple[int, str]] = []
        n = distance_points(km, r.get("work_mode"), preferred, limit)
        if n:
            points.append((n, f"{reach['town']}, {km} km" + (" (hybrid)" if r.get("work_mode") == "hybrid" else "")))
        size = sizes.get(r.get("company_id"))
        if per_size.get(size):
            points.append((per_size[size], SIZE_WHY[size]))
        fit = r.get("fit_score")
        r["rank"] = {"score": None if fit is None else fit - sum(n for n, _ in points), "points": points,
                     "km": km, "size": size}
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
