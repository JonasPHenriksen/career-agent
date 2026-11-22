"""Is the ad still up? Checked before an application is written, when the app starts, every few hours while it runs,
and when you open the application or the job, so you don't spend time on a job that's gone.

No model is asked: the ad's page is fetched and read for plain signs. Gone means the site says there is no such page
(404), the address now leads somewhere else on the same site (the job list), the page says so ("Opslag ikke fundet",
"no longer available") and isn't the ad, its "valid through" date has passed, or a Workday ad page no longer carries
the ad. A job found in several places counts as open while any copy is up. A page that can't be read (blocked, no
answer, built in the browser) changes nothing; it's looked at again next time."""
from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urlparse

from . import db, fetch, jobs, notify, robots, settings

log = logging.getLogger("career_agent.adcheck")

CHECK_HOURS = 6  # an ad that matters is looked at again after this long while the app runs
GONE_RECHECK_HOURS = 24  # one already found gone is looked at once a day, in case it was a hiccup
BEFORE_WRITING_HOURS = 1  # before an application is written, the ad must have been seen up this recently
TOP_INBOX = 30  # the highest-ranked jobs in the inbox are watched too
SHORT_PAGE = 2500  # characters: an error page is short; an ad that mentions "når stillingen er besat" isn't
MAX_COPIES = 4

# What a site writes on an ad it has closed. An ad never says this about itself, so it counts however long the page
# is and even with the job's title on it (Jobindex keeps the ad's heading above "Annoncen er udløbet!").
CLOSED_NOTICE = re.compile(
    r"opslag(?:et)? (?:blev )?ikke fundet|(?:job)?opslaget er (?:udløbet|fjernet)|annoncen (?:er )?(?:udløbet|fjernet)"
    r"|annoncen findes ikke|lukket for ansøgninger|jobbet (?:findes ikke|blev ikke fundet)|job not found"
    r"|no longer accepting applications|(?:job|position|posting|vacancy) you are looking for"
    r"|this (?:job|position|posting|vacancy|job posting) (?:has expired|has been closed|is closed|is no longer available)",
    re.I)
# Words that also turn up in an ad's own text ("når ansøgningsfristen er udløbet, indkalder vi…"): they count only on
# a short page, or one without the job's title.
GONE_WORDS = re.compile(
    r"(?:job)?opslaget er (?:lukket|ikke længere aktivt?)|annoncen (?:er )?lukket"
    r"|stillingen er (?:ikke længere \w+|lukket|blevet besat)"
    r"|er ikke længere (?:aktiv|tilgængelig|ledig|slået op)|siden (?:blev ikke fundet|findes ikke|kunne ikke findes)"
    r"|jobbet er ikke længere|ansøgningsfristen er (?:udløbet|overskredet)"
    r"|page not found|no longer (?:available|open|active)"
    r"|(?:job|position|posting|vacancy|role|requisition) (?:is |has )?(?:been )?(?:closed|filled|removed|expired)", re.I)
_WORKDAY_JOB = re.compile(r"\.myworkdayjobs\.com$", re.I)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _moved_away(url: str, final: str) -> bool:
    """Sent on to another page on the same site that isn't the ad (the job list, the front page). Another site is
    normal: job boards send you on to the employer's page."""
    if final == url or _host(url) != _host(final):
        return False
    a, b = urlparse(url), urlparse(final)
    ids = re.findall(r"\d{4,}", a.path + "?" + a.query)
    if ids:
        return not any(i in final for i in ids)
    return len(b.path.rstrip("/")) < len(a.path.rstrip("/")) and a.path.startswith(b.path.rstrip("/") or "/")


def _title_on_page(title: str | None, text: str) -> bool:
    words = [w for w in jobs.title_key(title).split() if len(w) >= 3]
    if not words:
        return False
    page = " " + " ".join(jobs.title_key(text).split()) + " "
    return sum(f" {w} " in page for w in words) / len(words) >= 0.6


def page_state(url: str, title: str | None) -> tuple[str, str]:
    """("open" | "gone" | "unknown", what was found) for one address of an ad."""
    if not robots.allowed(url):
        return "unknown", f"{_host(url)} asks robots not to read it"
    try:
        r = fetch.get(url)
    except fetch.FetchError as e:
        return ("gone", f"{_host(url)} says the page doesn't exist") if e.gone else ("unknown", str(e))
    final = str(r.url)
    html = r.text if "html" in r.headers.get("content-type", "html").lower() else ""
    jp = fetch.jobposting_jsonld(html) if html else None
    if jp:
        until = (str(jp.get("validThrough") or ""))[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until) and until < date.today().isoformat():
            return "gone", f"{_host(url)} says the listing ended {until}"
        return "open", f"up on {_host(final)}"
    if _WORKDAY_JOB.search(_host(final)) and "/job/" in urlparse(final).path:
        return "gone", f"the ad is no longer on {_host(final)}"  # a Workday ad page always carries the ad as data
    if _moved_away(url, final):
        return "gone", f"the address now leads to {final[:120]}"
    text = fetch.html_to_text(html)[1] if html else ""
    notice = CLOSED_NOTICE.search(text)
    if notice:
        return "gone", f'{_host(final)} says "{notice.group(0)}"'
    on_page = _title_on_page(title, text)
    m = GONE_WORDS.search(text)
    if m and (len(text) < SHORT_PAGE or not on_page):
        return "gone", f'{_host(final)} says "{m.group(0)}"'
    if on_page:
        return "open", f"up on {_host(final)}"
    return "unknown", f"couldn't tell from {_host(final)} (the page may be built in the browser)"


def _addresses(job: dict) -> list[str]:
    """The ad's own address first, then where else it was found."""
    rows = db.q("SELECT id, url, link_locked FROM jobs WHERE (id = ? OR duplicate_of = ?) AND url LIKE 'http%' "
                "ORDER BY id = ? DESC, id", (job["id"], job["id"], job["id"]))
    urls = [r["url"] for r in rows if not r["link_locked"]]
    return list(dict.fromkeys(urls))[:MAX_COPIES]


def check_job(job_id: int) -> dict:
    """Look at the ad (and its copies) and record what was found: {"state", "note"}."""
    job = db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        return {"state": "unknown", "note": "job deleted"}
    if job["duplicate_of"]:  # a copy set aside: its original stands for it
        return check_job(job["duplicate_of"])
    found = [(url, *page_state(url, job["title"])) for url in _addresses(job)]
    up = [f for f in found if f[1] == "open"]
    gone = [f for f in found if f[1] == "gone"]
    if up:
        state, note = "open", up[0][2] + (f" (gone from {_host(gone[0][0])})" if gone else "")
    elif gone and (gone[0][0] == job["url"] or len(gone) == len(found)):
        state, note = "gone", gone[0][2]
    else:
        state, note = "unknown", (found[0][2] if found else "no address the app may open")
    patch: dict = {"checked_at": db.now(), "check_note": note[:300]}
    if state == "open":
        patch["gone_at"] = None
    elif state == "gone" and not job["open_said_at"]:
        patch["gone_at"] = job["gone_at"] or db.now()
    db.update("jobs", job_id, patch)
    if state == "gone" and not job["gone_at"] and not job["open_said_at"]:
        _tell(job, note)
    return {"state": state, "note": note}


def _tell(job: dict, note: str) -> None:
    """A notification when an ad you've put work into is gone."""
    app = db.q1("SELECT status FROM applications WHERE job_id = ?", (job["id"],))
    if (app and app["status"] in ("drafting", "draft", "ready")) or job["status"] == "shortlisted":
        notify.send("A job ad is gone", f"{job['title']} at {job['company_name'] or '?'}: {note}.")


def is_gone(job: dict) -> bool:
    return bool(job.get("gone_at")) and not job.get("open_said_at")


def seen_recently(job: dict, hours: float) -> bool:
    at = db.parse_ts(job.get("checked_at"))
    return bool(at) and (db.parse_ts(db.now()) - at).total_seconds() < hours * 3600


def still_up(job_id: int) -> bool:
    """Before writing: False when the ad is gone (looked at again unless that was just done)."""
    job = db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job or job["open_said_at"]:
        return True
    if not seen_recently(job, BEFORE_WRITING_HOURS) or job["gone_at"]:
        try:
            check_job(job_id)
        except Exception:  # noqa: BLE001 - a failed check never stops the writing
            log.exception("checking job %s failed", job_id)
        job = db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return not is_gone(job)


def to_check() -> list[int]:
    """The ads worth looking at now: applications not yet sent, shortlisted jobs, and the top of the inbox."""
    from . import rank

    due, gone_due = db.days_ago(CHECK_HOURS / 24), db.days_ago(GONE_RECHECK_HOURS / 24)
    ids = [r["id"] for r in db.q(
        "SELECT j.id FROM jobs j JOIN applications a ON a.job_id = j.id WHERE a.status IN ('drafting', 'draft', 'ready') "
        "AND (j.checked_at IS NULL OR (j.gone_at IS NULL AND j.checked_at < ?) OR j.checked_at < ?)", (due, gone_due))]
    ids += [r["id"] for r in db.q(
        "SELECT j.id FROM jobs j WHERE j.status = 'shortlisted' AND j.gone_at IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM applications a WHERE a.job_id = j.id) AND (j.checked_at IS NULL OR j.checked_at < ?)", (due,))]
    inbox = db.q("SELECT j.* FROM jobs j WHERE j.status IN ('new', 'shortlisted') AND j.fit_score IS NOT NULL "
                 f"AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id) AND {jobs.open_sql()}",
                 (date.today().isoformat(),))
    for r in rank.best_first(inbox)[:TOP_INBOX]:
        if (r["rank"]["score"] or 0) >= rank.POOR_BELOW and not seen_recently(r, CHECK_HOURS):
            ids.append(r["id"])
    return list(dict.fromkeys(ids))


def check_many(job_ids: list[int] | None = None) -> dict:
    """The task: the given jobs, or every ad worth looking at now."""
    ids = job_ids if job_ids else to_check()
    out = {"checked": 0, "open": 0, "gone": 0, "unknown": 0}
    for job_id in ids:
        out[check_job(job_id)["state"]] += 1
        out["checked"] += 1
    if not job_ids:
        settings.update("state", {"ads_checked_at": db.now()})
    return out


def due() -> bool:
    """Time for the regular look (on start, then every CHECK_HOURS)."""
    at = db.parse_ts(settings.get("state").get("ads_checked_at"))
    return not at or (db.parse_ts(db.now()) - at).total_seconds() >= CHECK_HOURS * 3600


def queue_if_stale(job: dict) -> None:
    """Opening a job or an application: look at the ad if that hasn't been done for a while."""
    from . import tasks

    if job.get("url") and not job.get("link_locked") and not seen_recently(job, CHECK_HOURS):
        tasks.enqueue("check_ads", {"job_ids": [job["id"]]}, ref=f"job:{job['id']}")


UNSENT = ("drafting", "draft", "ready")


def removable(job: dict, app: dict | None) -> bool:
    """A job whose ad is gone can be removed while nothing has been sent for it."""
    return is_gone(job) and (not app or app["status"] in UNSENT)


def remove(job_id: int) -> dict | None:
    """Remove a job whose ad is gone: the job, your unsent application for it, and its copies from other places.
    One step in History, so it can be undone there. None when it can't be removed."""
    job = db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))
    app = db.q1("SELECT id, status FROM applications WHERE job_id = ?", (job_id,)) if job else None
    if not job or not removable(job, app):
        return None
    refs = [f"job:{job_id}"] + ([f"app:{app['id']}"] if app else [])
    db.ex(f"DELETE FROM tasks WHERE status = 'queued' AND ref IN ({','.join('?' * len(refs))})", refs)
    db.ex("DELETE FROM jobs WHERE duplicate_of = ?", (job_id,))
    db.ex("DELETE FROM jobs WHERE id = ?", (job_id,))  # the application goes with it
    return {"title": job["title"], "company": job["company_name"], "application": bool(app)}
