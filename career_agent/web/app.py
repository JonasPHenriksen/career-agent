"""The local web app (http://127.0.0.1:8765). Server-rendered pages; slow work goes to background tasks."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import (adcheck, backup, brand, collect, companies, config, cvdata, cvrender, db, emails, exporter, history, ingest, insights, jobkinds, jobs, keychain, knowledge, llm, mailer,
                profile, rank, redo, retrieval, review, settings, sources, tasks, todos, tracker, voice, watch, writer)
from . import helpers
from .. import suggestions

HERE = Path(__file__).parent
log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(HERE / "templates"))
helpers.install(templates.env)
templates.env.globals["model_choices"] = llm.model_choices  # for the "with which model" picker on redo buttons
templates.env.globals["effort_kind"] = llm.effort_kind  # what effort means for a model: levels or thinking on/off
templates.env.globals["still_open_email"] = emails.still_open  # the ad notices offer an email asking whether it's open
templates.env.globals["ad_uncertain"] = adcheck.uncertain


def chosen_model(f: dict) -> dict:
    """The model and effort picked next to a button, as task payload ({} = your settings)."""
    m, effort = (f.get("model") or "").strip(), (f.get("effort") or "").strip()
    if effort in llm.EFFORTS:
        m = "|".join((m.split("|") + ["", ""])[:2] + [effort])  # "provider|model|effort", or "||effort" alone
    return {"model": m} if llm.parse_choice(m) else {}


def with_model_msg(f: dict, text: str) -> str:
    choice = llm.parse_choice(chosen_model(f).get("model")) or {}
    bits = ([llm.model_label(choice["provider"], choice["model"])] if choice.get("provider") else []) \
        + ([f"{choice['effort']} effort"] if choice.get("effort") else [])
    return f"{text} with {' at '.join(bits)}" if bits else text


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    try:
        history.daily_copy()
        history.prune()
    except Exception:  # noqa: BLE001 - a full disk mustn't stop the app from starting
        log.exception("daily copy or history pruning failed")
    with history.op("Starting the app"):
        llm.bootstrap()
        tasks.recover()
        jobs.tidy_after_update()
        tasks.catch_up()
        llm.backfill_refs()
        retrieval.backfill_chunks()
        if retrieval.embeddings_available():
            tasks.enqueue("embed_index", {}, ref="index")
    stop = asyncio.Event()
    background = [asyncio.create_task(tasks.worker_loop(stop)), asyncio.create_task(tasks.scheduler_loop(stop))]
    yield
    stop.set()
    for t in background:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
# Changes whenever the CSS or JS changes, so browsers never keep an old copy after an update.
STATIC_V = int(max((HERE / "static" / f).stat().st_mtime for f in ("app.css", "app.js")))

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}


@app.middleware("http")
async def local_guard(request: Request, call_next):
    """Only answer requests addressed to this Mac, and refuse cross-site form posts."""
    host = (request.headers.get("host") or "").lower()
    host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    if host not in _LOCAL_HOSTS:
        return PlainTextResponse("Forbidden host", status_code=403)
    # The Collect jobs bookmark posts from the job site's own tab; /collect/take checks the bookmark's key instead.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path != "/collect/take":
        origin = request.headers.get("origin")
        if origin and origin != "null" and (urlparse(origin).hostname or "") not in ("127.0.0.1", "localhost", "::1", "testserver"):
            return PlainTextResponse("Cross-site request blocked", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def record_changes(request: Request, call_next):
    """Everything one click changes is one entry in History. After a change you made, the page you're sent to
    offers to undo it."""
    if request.url.path.startswith("/static"):
        return await call_next(request)
    mine = request.method not in ("GET", "HEAD", "OPTIONS")
    with history.op(f"{request.method} {request.url.path}", source="you" if mine else "automatic") as o:
        response = await call_next(request)
    loc = response.headers.get("location")
    if mine and o.touched and loc and response.status_code in (302, 303) and history.get_op(o.id):
        base, hash_, fragment = loc.partition("#")
        response.headers["location"] = base + ("&" if "?" in base else "?") + f"undo={o.id}" + hash_ + fragment
    return response


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------

NAV = [("/", "Today"), ("/jobs", "Jobs"), ("/tracker", "Applications"), ("/companies", "Companies"),
       ("/profile", "Profile"), ("/insights", "Insights"), ("/history", "History"), ("/settings", "Settings")]


def render(request: Request, template: str, **ctx):
    path = request.url.path
    active = next((href for href, _ in NAV[1:] if path.startswith(href)), "/")
    if path.startswith("/applications"):
        active = "/tracker"
    if path.startswith("/documents"):
        active = "/profile"
    undo_id = request.query_params.get("undo", "")
    undo_op = history.get_op(int(undo_id)) if undo_id.isdigit() else None
    here = request.url.path + ("?" + urlencode([(k, v) for k, v in request.query_params.multi_items()
                                                if k not in ("msg", "err", "undo")]) if request.url.query else "")
    ctx.update(request=request, nav=NAV, active=active, msg=request.query_params.get("msg"),
               err=request.query_params.get("err"), app_name=config.APP_NAME, todo_count=todos.count_now(), static_v=STATIC_V,
               on_hold=tasks.on_hold(), undo_op=undo_op if undo_op and not undo_op["undone_by"] else None,
               here=here.rstrip("?"))
    return templates.TemplateResponse(request, template, ctx)


def local_path(value: str | None, fallback: str) -> str:
    """A path on this app to send you back to, never another site."""
    v = (value or "").strip()
    return v if v.startswith("/") and not v.startswith("//") and "\\" not in v else fallback


def go(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    url, hash_, fragment = url.partition("#")
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    return RedirectResponse(url + hash_ + fragment, status_code=303)


def back(request: Request, fallback: str = "/") -> str:
    ref = request.headers.get("referer") or ""
    p = urlparse(ref)
    if p.hostname in ("127.0.0.1", "localhost", "testserver") and p.path:
        return p.path + (f"?{p.query}" if p.query else "")
    return fallback


async def form(request: Request) -> dict:
    data = await request.form()
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}


def done_response() -> Response:
    return Response("", headers={"X-Done": "1"})


def need(row):
    if not row:
        raise HTTPException(404, "Not found")
    return row


def llm_ready() -> tuple[bool, str]:
    cfg = settings.get("llm")
    for tier in ("writing", "analysis"):
        t = cfg.get(tier) or {}
        if not t.get("provider") or not t.get("model"):
            return False, "Choose an AI model in Settings."
        if t["provider"] == "claude_code" and not llm.find_claude_binary(cfg.get("claude_code_path") or None):
            return False, "Claude Code wasn't found on this Mac. See Settings → Claude through Claude Code."
    return True, ""


# ---------------------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------------------

# Today lists jobs ranked at least this high that have no draft yet (it was 15 under the old drafting threshold of 80).
MATCH_FLOOR = 65


@app.get("/")
def today(request: Request):
    p = settings.get("profile")
    ai_ok, ai_msg = llm_ready()
    n_docs = db.scalar("SELECT COUNT(*) FROM documents") or 0
    n_facts = db.scalar("SELECT COUNT(*) FROM facts WHERE hidden = 0") or 0
    n_sources = db.scalar("SELECT COUNT(*) FROM sources") or 0
    n_jobs = db.scalar("SELECT COUNT(*) FROM jobs") or 0
    steps = [
        {"done": ai_ok, "title": "Choose your AI model", "text": ai_msg or "Ready.", "href": "/settings#models"},
        {"done": bool(p.get("name")), "title": "Tell it about you", "text": "Name, contact details, what you're looking for.", "href": "/profile?tab=about"},
        {"done": n_docs > 0, "title": "Import your documents", "text": "CVs, past applications, emails, references.", "href": "/profile?tab=documents"},
        {"done": n_facts > 0 and bool(settings.get("style_guide")), "title": "Check your career database and style", "text": "Built automatically from your documents.", "href": "/profile?tab=facts"},
        {"done": n_sources > 0 or n_jobs > 0, "title": "Add jobs", "text": "Paste a job link, or add a job-search feed to watch.", "href": "/jobs"},
    ]
    drafts = rank.urgent_first(db.q(
        "SELECT a.*, j.title AS job_title, j.company_name, j.fit_score, j.deadline, j.details, j.posted_at, "
        "j.created_at AS job_created_at, j.gone_at, j.open_said_at, j.checked_at, j.check_note "
        "FROM applications a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.status IN ('drafting', 'draft', 'ready') ORDER BY a.updated_at DESC"))
    for a in drafts:
        a["claims_d"] = db.jload(a["claims"], {}) or {}
        a["voice_d"] = db.jload(a["voice"], {}) or {}
    matches = [j for j in rank.best_first(db.q(  # the next best after the drafts, worth a look
        "SELECT j.* FROM jobs j WHERE j.fit_score >= ? AND j.status IN ('new', 'shortlisted') "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id) "
        f"AND {jobs.open_sql()}", (MATCH_FLOOR, date.today().isoformat())))
        if j["rank"]["score"] >= MATCH_FLOOR][:8]
    for j in matches:
        j["urgency"] = rank.urgency(j)
    interviews = db.q("SELECT a.*, j.title AS job_title, j.company_name FROM applications a JOIN jobs j ON j.id = a.job_id "
                      "WHERE a.status IN ('interview', 'offer') ORDER BY a.last_event_at DESC")
    errors = db.q("SELECT * FROM tasks WHERE status = 'error' AND finished_at >= ? ORDER BY id DESC LIMIT 5",
                  (db.days_ago(2),))
    counts = {label: db.scalar(f"SELECT COUNT(*) FROM applications WHERE status IN ({','.join('?' * len(sts))})", sts) or 0
              for label, sts in tracker.BOARD}
    return render(request, "today.html", steps=steps, show_steps=not all(s["done"] for s in steps),
                  drafts=drafts, matches=matches, deadlines=tracker.deadlines_soon(7),
                  followups=tracker.followups_due(), interviews=interviews, errors=errors, counts=counts,
                  name=p.get("name"), task_labels=tasks.TASK_LABELS,
                  followup_days=settings.get("automation").get("followup_days"), todo=todos.lists(),
                  needs_text=len(jobs.needing_text()))


# ---------------------------------------------------------------------------------------
# Your to-do list
# ---------------------------------------------------------------------------------------

@app.post("/todos/add")
async def todos_add(request: Request):
    f = await form(request)
    try:
        todos.add(f.get("title", ""), f.get("detail", ""), show_from=f.get("show_from") or None)
    except ValueError as e:
        return go("/#todo", err=str(e))
    return go("/#todo", msg="Added to your to-do list.")


@app.post("/todos/{todo_id}/done")
def todos_done(request: Request, todo_id: int):
    todos.set_done(todo_id, True)
    collect.remind()  # ticking off the collect reminder dates the next one
    return go("/#todo", msg="Done. Nice.")


@app.post("/todos/{todo_id}/undo")
def todos_undo(request: Request, todo_id: int):
    todos.set_done(todo_id, False)
    return go("/#todo")


@app.post("/todos/{todo_id}/move")
async def todos_move(request: Request, todo_id: int):
    f = await form(request)
    todos.move(todo_id, -1 if f.get("dir") == "up" else 1)
    return go("/#todo")


@app.post("/todos/{todo_id}/delete")
def todos_delete(request: Request, todo_id: int):
    todos.delete(todo_id)
    return go("/#todo", msg="Removed from your to-do list.")


# ---------------------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------------------

@app.post("/jobs/add")
async def jobs_add(request: Request):
    f = await form(request)
    url, text = f.get("url", ""), f.get("text", "")
    if text and len(text) > 80:
        job_id = jobs.add_from_text(text, url or None)
        return go(f"/jobs/{job_id}", msg="Reading the ad…")
    if url.startswith(("http://", "https://")):
        job_id, created = jobs.add_from_url(url)
        return go(f"/jobs/{job_id}", msg="Reading the ad…" if created else "You already have this job.")
    return go(back(request, "/jobs"), err="Paste a job link (https://…) or the full text of the ad.")


JOB_SORTS = [("best", "Best for you first"), ("score", "Best fit first"), ("urgent", "Most urgent first"),
             ("newest", "Newest first")]


@app.get("/jobs")
def jobs_list(request: Request, view: str = "inbox", sort: str = "best", q: str = ""):
    where, params = [], []
    today_s = date.today().isoformat()
    sort = {"deadline": "urgent"}.get(sort, sort)
    if sort not in dict(JOB_SORTS):
        sort = "best"
    if view == "inbox":
        where.append(f"j.status IN ('new', 'shortlisted') AND a.id IS NULL AND {jobs.open_sql()}")
        params.append(today_s)
    elif view == "same-ad":  # set aside by the AI repeat check (the to-do on Today links here)
        where.append("j.notes LIKE ?")
        params.append(jobs.SAME_JOB_NOTE_LIKE)
    elif view == "needs-text":
        where.append(f"{jobs.NEEDS_TEXT_SQL} AND {jobs.open_sql()}")
        params.append(today_s)
    elif view == "shortlisted":
        where.append("j.status = 'shortlisted'")
    elif view == "applying":
        where.append("a.id IS NOT NULL")
    elif view in ("filtered", "dismissed"):
        where.append("j.status = ?")
        params.append(view)
    elif view == "expired":
        where.append(f"NOT {jobs.open_sql()} AND a.id IS NULL")
        params.append(today_s)
    if q:
        where.append("(j.title LIKE ? OR j.company_name LIKE ? OR j.location LIKE ?)")
        params += [f"%{q}%"] * 3
    order = {"score": "j.fit_score IS NULL, j.fit_score DESC, j.id DESC"}.get(sort, "j.id DESC")
    sql = ("SELECT j.*, a.id AS app_id, a.status AS app_status FROM jobs j LEFT JOIN applications a ON a.job_id = j.id"
           + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order}")
    rows = db.q(sql, params)  # ranked here, not in SQL: distance and employer size count too (rank.py)
    if view == "needs-text":
        rows = [r for r in rows if jobs.needs_text(r)]
    rows = (rank.best_first(rows) if sort == "best" else rank.urgent_first(rows) if sort == "urgent"
            else rank.annotate(rows))[:300]
    today_d = date.today()
    grades: dict = {}
    for r in rows:
        r["urgency"] = r.get("urgency") or rank.urgency(r, today_d)
        if r["company_id"] and r["company_id"] not in grades:
            grades[r["company_id"]] = companies.badge(r["company_id"])
        r["badge"] = grades.get(r["company_id"])
        r["f"] = db.jload(r["fit"], {}) or {}
        r["needs_text"] = jobs.needs_text(r)
    counts = {
        "inbox": db.scalar("SELECT COUNT(*) FROM jobs j LEFT JOIN applications a ON a.job_id = j.id WHERE j.status IN ('new','shortlisted') "
                           f"AND a.id IS NULL AND {jobs.open_sql()}", (today_s,)),
        "shortlisted": db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'shortlisted'"),
        "applying": db.scalar("SELECT COUNT(*) FROM applications"),
        "filtered": db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'filtered'"),
        "dismissed": db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'dismissed'"),
        "expired": db.scalar("SELECT COUNT(*) FROM jobs j LEFT JOIN applications a ON a.job_id = j.id "
                             f"WHERE NOT {jobs.open_sql()} AND a.id IS NULL", (today_s,)),
        "all": db.scalar("SELECT COUNT(*) FROM jobs"),
        "needs-text": len(jobs.needing_text()),
    }
    busy = db.scalar("SELECT COUNT(*) FROM tasks WHERE kind IN ('parse_job', 'score_job', 'poll_source', 'collect_jobs') "
                     "AND status IN ('queued', 'running')") or 0
    return render(request, "jobs.html", rows=rows, view=view, sort=sort, q=q, counts=counts, busy=busy,
                  sources=db.q("SELECT * FROM sources WHERE kind != 'collect' AND enabled = 1 ORDER BY id"), sorts=JOB_SORTS,
                  searches=collect.searches(), searches_at=collect.last_collected())


@app.get("/jobs/progress")
def jobs_progress(request: Request, n: int = 0):
    busy = db.scalar("SELECT COUNT(*) FROM tasks WHERE kind IN ('parse_job', 'score_job', 'poll_source', 'collect_jobs') "
                     "AND status IN ('queued', 'running')") or 0
    if busy == 0:
        return done_response()
    return templates.TemplateResponse(request, "partials/jobs_progress.html", {"busy": busy})


@app.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int):
    job = need(jobs.get(job_id))
    rank.annotate([job])
    job["urgency"] = rank.urgency(job)
    adcheck.queue_if_stale(job)
    app_row = db.q1("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    company = db.q1("SELECT * FROM companies WHERE id = ?", (job["company_id"],)) if job["company_id"] else None
    fact_ids = {i for m in (job["f"].get("matched") or []) for i in (m.get("fact_ids") or [])}
    facts = {f["id"]: f for f in db.q(f"SELECT id, title, organization FROM facts WHERE id IN ({','.join('?' * len(fact_ids))})",
                                       list(fact_ids))} if fact_ids else {}
    made = {"score": llm.made_by(f"job:{job_id}", ("fit_score",)), "parse": llm.made_by(f"job:{job_id}", ("parse_job",))}
    return render(request, "job.html", job=job, app=app_row, company=company, facts=facts, made=made,
                  badge=companies.badge(job["company_id"]), busy=tasks.active(ref=f"job:{job_id}"),
                  error=tasks.last_error(f"job:{job_id}"), expired=jobs.is_expired(job), task_labels=tasks.TASK_LABELS,
                  source=db.q1("SELECT name FROM sources WHERE id = ?", (job["source_id"],)) if job["source_id"] else None,
                  repeats=jobs.repeats_of(job_id),
                  original=jobs.get(job["duplicate_of"]) if job["duplicate_of"] else None)


@app.get("/jobs/{job_id}/progress")
def job_progress(request: Request, job_id: int):
    busy = tasks.active(ref=f"job:{job_id}")
    if not busy:
        return done_response()
    return templates.TemplateResponse(request, "partials/progress.html",
                                      {"busy": busy, "url": f"/jobs/{job_id}/progress", "labels": tasks.TASK_LABELS})


@app.post("/jobs/{job_id}/check")
async def job_check(request: Request, job_id: int):
    need(db.q1("SELECT id FROM jobs WHERE id = ?", (job_id,)))
    tasks.enqueue("check_ads", {"job_ids": [job_id]}, ref=f"job:{job_id}")
    return go(back(request, f"/jobs/{job_id}"), msg="Checking the ad… reload in a moment to see what was found.")


@app.post("/jobs/{job_id}/still-open")
async def job_still_open(request: Request, job_id: int):
    need(db.q1("SELECT id FROM jobs WHERE id = ?", (job_id,)))
    db.update("jobs", job_id, {"gone_at": None, "open_said_at": db.now(), "check_note": "you said the ad is still open"})
    return go(back(request, f"/jobs/{job_id}"), msg="Marked as still open. The regular checks won't mark it gone again.")


@app.post("/jobs/{job_id}/remove-gone")
async def job_remove_gone(request: Request, job_id: int):
    f = await form(request)
    nxt = local_path(f.get("next"), "/jobs")
    gone = adcheck.remove(job_id)
    if not gone:
        return go(back(request, f"/jobs/{job_id}"),
                  err="Only a job whose ad is gone, with nothing sent for it, can be removed this way.")
    what = "The job and your draft" if gone["application"] else "The job"
    return go(nxt, msg=f"{what} for {gone['title']} at {gone['company'] or '?'} were removed. "
                       "Undo it here, or later under History.")


@app.post("/jobs/{job_id}/status")
async def job_status(request: Request, job_id: int):
    f = await form(request)
    nxt = local_path(f.get("next"), back(request, f"/jobs/{job_id}"))
    if f.get("status") != "dismissed":
        jobs.set_status(job_id, f.get("status", ""))
        return go(nxt)
    gone = jobs.dismiss(job_id)
    if not gone:
        return go(nxt)
    what = [w for w, n in (("your draft was removed", gone["draft_removed"]),
                           (f"{gone['copies']} cop{'y' if gone['copies'] == 1 else 'ies'} of the ad set aside",
                            gone["copies"])) if n]
    return go(nxt, msg=f"{gone['title'] or 'The job'} is not for you" + (": " + " and ".join(what) if what else "")
                       + ". New copies of the ad will be set aside too. Undo it here, or later under History.")


@app.post("/jobs/{job_id}/rescore")
async def job_rescore(request: Request, job_id: int):
    f = await form(request)
    tasks.enqueue("score_job", {"job_id": job_id, **chosen_model(f)}, ref=f"job:{job_id}")
    return go(f"/jobs/{job_id}", msg=with_model_msg(f, "Scoring again") + "…")


@app.post("/jobs/{job_id}/reparse")
async def job_reparse(request: Request, job_id: int):
    f = await form(request)
    job = need(jobs.get(job_id))
    change = {"parse_status": "pending", "parse_error": None}
    if job["status"] == "filtered":  # you looked and want it: never set aside automatically again
        change.update(status="new", keep=1, duplicate_of=None)
    db.update("jobs", job_id, change)
    tasks.enqueue("parse_job", {"job_id": job_id, "use_text": not job["url"], **chosen_model(f)}, ref=f"job:{job_id}")
    return go(f"/jobs/{job_id}", msg=with_model_msg(f, "Reading the ad again") + "…")


@app.post("/jobs/{job_id}/text")
async def job_text(request: Request, job_id: int):
    f = await form(request)
    text = f.get("text", "")
    if len(text) < 80:
        return go(f"/jobs/{job_id}", err="Paste the full text of the ad.")
    db.update("jobs", job_id, {"description": text, "parse_status": "pending", "parse_error": None})
    tasks.enqueue("parse_job", {"job_id": job_id, "use_text": True}, ref=f"job:{job_id}")
    return go(f"/jobs/{job_id}", msg="Reading the pasted text…")


@app.post("/jobs/{job_id}/write")
async def job_write(request: Request, job_id: int):
    f = await form(request)
    need(jobs.get(job_id))
    if f.get("mode") == "claude":  # you write it in Claude.ai: no model needed here
        app_id = jobs.create_application(job_id, instructions=f.get("instructions") or None, draft=False)
        return go(f"/applications/{app_id}?tab=claude")
    ok, why = llm_ready()
    if not ok:
        return go(f"/jobs/{job_id}", err=why)
    app_id = jobs.create_application(job_id, instructions=f.get("instructions") or None)
    return go(f"/applications/{app_id}", msg="Writing your application…")


@app.post("/jobs/{job_id}/notes")
async def job_notes(request: Request, job_id: int):
    f = await form(request)
    db.update("jobs", job_id, {"notes": f.get("notes") or None})
    return go(f"/jobs/{job_id}", msg="Notes saved.")


@app.post("/jobs/{job_id}/delete")
def job_delete(request: Request, job_id: int):
    db.ex("DELETE FROM jobs WHERE id = ?", (job_id,))
    return go("/jobs", msg="Job deleted.")


# ---------------------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------------------

APP_TABS = [("review", "Review"), ("letter", "Letter"), ("claude", "Claude.ai"), ("cv", "CV"), ("check", "Fact check"), ("voice", "Voice"),
            ("send", "Send"), ("timeline", "Timeline"), ("interview", "Interview prep"), ("history", "Versions")]


@app.get("/applications/{app_id}")
def application(request: Request, app_id: int, tab: str = ""):
    a = need(tracker.app_with_job(app_id))
    job = jobs.get(a["job_id"])
    if job and a["status"] in ("drafting", "draft", "ready"):
        adcheck.queue_if_stale(job)
        job["urgency"] = rank.urgency(job)
    if tab not in dict(APP_TABS):  # a draft opens on the review screen; nothing to review yet opens on the letter
        tab = "review" if (a["letter"] or a["cv"]) else "letter"
    look = {}
    if tab == "review":
        waiting = [r["id"] for r in rank.urgent_first(db.q(  # the same order as Today's "Waiting for your review"
            "SELECT a.id, a.updated_at, j.title AS job_title, j.fit_score, j.deadline, j.details, j.posted_at, "
            "j.created_at AS job_created_at, j.gone_at, j.open_said_at FROM applications a JOIN jobs j ON j.id = a.job_id "
            "WHERE a.status IN ('draft', 'ready') AND a.letter IS NOT NULL ORDER BY a.updated_at DESC"))]
        at = waiting.index(app_id) if app_id in waiting else -1
        look = {"base_open": len(review.base_questions(cvdata.base_for(a["language"]))), "questions": review.questions(a), "letter_html": review.marked(a["letter"], a),
                "placed": review.last_placed(f"app:{app_id}"),
                "decided": db.jload(a.get("review"), {}) or {},
                "siblings": db.q("SELECT a.id, j.title, a.status FROM applications a JOIN jobs j ON j.id = a.job_id "
                                 "WHERE j.company_id = ? AND a.id != ? ORDER BY a.id DESC", (a["company_id"], app_id)) if a["company_id"] else [],
                "waiting": {"total": len(waiting), "position": at + 1, "prev": waiting[at - 1] if at > 0 else None,
                            "next": waiting[at + 1] if 0 <= at < len(waiting) - 1 else None}}
    claims = db.jload(a["claims"], {}) or {}
    ids = {i for c in claims.get("claims", []) for i in c.get("fact_ids", [])}
    facts = {f["id"]: f for f in db.q(f"SELECT id, title, organization, evidence FROM facts WHERE id IN ({','.join('?' * len(ids))})",
                                       list(ids))} if ids else {}
    prompt = writer.manual_prompt(app_id) if tab == "claude" else ""
    ref = f"app:{app_id}"
    made = {"letter": llm.made_by(ref, ("write_letter", "revise_letter")), "cv": llm.made_by(ref, ("write_cv", "revise_cv")),
            "check": llm.made_by(ref, ("claim_check",)), "prep": llm.made_by(ref, ("interview_prep",)),
            "voice": llm.made_by(ref, ("voice_read",))}
    cv_data = db.jload(a.get("cv_data"), None)
    cv_pages, cv_error = 0, ""
    if cv_data and tab in ("cv", "review"):
        try:
            cv_pages = cvrender.pages(cv_data, a["language"])
        except Exception as e:  # noqa: BLE001 - a CV that can't be typeset still has to be editable
            cv_error = str(e)[:300]
    return render(request, "application.html", a=a, job=job, tab=tab, tabs=APP_TABS, claims=claims, facts=facts, made=made,
                  cv_data=cv_data, cv_json=json.dumps(cv_data, ensure_ascii=False, indent=2) if cv_data else "",
                  cv_pages=cv_pages, cv_error=cv_error, cv_base=cvdata.base_for(a["language"]), look=look, design_primary=cvrender.design()["primary"],
                  prompt=prompt, prompt_words=len(prompt.split()),
                  v=db.jload(a["voice"], {}) or {}, read=voice.read_notes(a), notes=db.jload(a["writer_notes"], []) or [],
                  busy=tasks.active(ref=f"app:{app_id}"), error=tasks.last_error(f"app:{app_id}"),
                  versions=db.q("SELECT * FROM app_versions WHERE application_id = ? ORDER BY id DESC", (app_id,)),
                  timeline=tracker.timeline(app_id), statuses=tracker.APP_STATUSES, event_types=tracker.EVENT_TYPES,
                  event_labels=tracker.EVENT_LABELS, status_labels=tracker.STATUS_LABELS,
                  badge=companies.badge(a["company_id"]), task_labels=tasks.TASK_LABELS, today=date.today().isoformat(),
                  apply_mail=emails.application(a) if tab == "send" else None,
                  followup_mail=emails.followup(a) if a["applied_at"] else None, followup_due=tracker.followup_due(a))


@app.get("/applications/{app_id}/progress")
def application_progress(request: Request, app_id: int):
    busy = tasks.active(ref=f"app:{app_id}")
    if not busy:
        return done_response()
    return templates.TemplateResponse(request, "partials/progress.html",
                                      {"busy": busy, "url": f"/applications/{app_id}/progress", "labels": tasks.TASK_LABELS})


@app.post("/applications/{app_id}/save")
async def application_save(request: Request, app_id: int):
    f = await form(request)
    a = need(db.q1("SELECT * FROM applications WHERE id = ?", (app_id,)))
    patch = {"updated_at": db.now()}
    tab = "letter"
    if "letter" in f:
        patch.update({"letter": f["letter"], "subject": f.get("subject") or a["subject"]})
    if "cv" in f:
        patch["cv"] = f["cv"]
        tab = "cv"
    if "cv_data" in f:  # the CV edited as data: it has to be valid before it replaces what's there
        tab = "cv"
        try:
            data = cvdata.clean(json.loads(f["cv_data"]))
            cvrender.render(data, a["language"])
        except Exception as e:  # noqa: BLE001 - bad JSON or a CV that can't be typeset
            return go(f"/applications/{app_id}?tab=cv", err=f"That isn't a CV I can use, so nothing was saved: {str(e)[:160]}")
        patch.update({"cv_data": db.jdump(data), "cv": cvdata.to_markdown(data)})
    db.insert("app_versions", {"application_id": app_id, "label": "Before your edit", "subject": a["subject"],
                               "letter": a["letter"], "cv": a["cv"], "cv_data": a["cv_data"], "created_at": db.now()})
    db.update("applications", app_id, patch)
    v = voice.check(patch.get("letter", a["letter"]) or "", a["language"])
    kept = voice.read_notes({**a, **patch})  # the read for your voice stays while the letter is the one it read
    if kept:
        v["read"] = kept
    db.update("applications", app_id, {"voice": db.jdump(v)})
    return go(f"/applications/{app_id}?tab={tab}", msg="Saved. Run the fact check again when you're done editing.")


@app.post("/applications/{app_id}/revise")
async def application_revise(request: Request, app_id: int):
    f = await form(request)
    instructions = f.get("instructions", "")
    part = f.get("part", "letter")
    if not instructions:
        return go(f"/applications/{app_id}?tab={part}", err="Write what you'd like changed.")
    tasks.enqueue("revise_application", {"application_id": app_id, "instructions": instructions, "part": part, **chosen_model(f)},
                  ref=f"app:{app_id}")
    tab = "review" if f.get("from") == "review" else ("cv" if part == "cv" else "letter")
    return go(f"/applications/{app_id}?tab={tab}", msg=with_model_msg(f, "Revising") + "…")


@app.post("/applications/{app_id}/cv-reset")
def application_cv_reset(app_id: int):
    """The CV back to your base CV, word for word: a fallback when the tailoring went wrong."""
    need(db.q1("SELECT id FROM applications WHERE id = ?", (app_id,)))
    result = writer.reset_cv(app_id)
    if not result:
        return go(f"/applications/{app_id}?tab=cv", err="You have no base CV yet. Add one under Profile → Your CV.")
    tasks.enqueue("check_application", {"application_id": app_id}, ref=f"app:{app_id}")
    msg = (f"The CV is your base CV again ({result['base']['title']}). The previous one is under Versions. Lines your "
           "base CV's own check has judged keep their verdict, so only anything new is checked…")
    if not result["same_language"]:
        msg += " Your base CV isn't in this application's language, so the CV is now in the base's language."
    return go(f"/applications/{app_id}?tab=cv", msg=msg)


@app.post("/applications/{app_id}/cv-colour")
async def application_cv_colour(request: Request, app_id: int):
    """This job's CV in the employer's colour, your usual colour, or one you pick. Only the look changes."""
    f = await form(request)
    a = need(tracker.app_with_job(app_id))
    data = db.jload(a.get("cv_data"), None)
    back = f"/applications/{app_id}?tab=cv"
    if not data:
        return go(back, err="This CV is kept as text, so it has no colour of its own.")
    choice, msg = f.get("choice"), ""
    if choice == "company":
        found = brand.company_colour(jobs.get(a["job_id"]), refresh=True)
        if not found["colour"]:
            where = f" on {found['website']}" if found["website"] else " (no website for it was found in the ad)"
            return go(back, err=f"Couldn't find {a.get('company_name') or 'the employer'}'s colour{where}. Pick one instead.")
        data["color"], data["color_by"] = found["colour"], "company"
        msg = f"The CV is in {a.get('company_name') or 'the employer'}'s colour, from {found['website']}."
    elif choice == "own":
        data["color"], data["color_by"] = "own", "you"
        msg = "The CV is in your usual colour."
    else:
        picked = (f.get("colour") or "").lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", picked):
            return go(back, err="Pick a colour first.")
        data["color"], data["color_by"] = brand.readable(picked), "you"
        msg = "Colour changed." + (" It's a little darker than you picked, so the white text on it can be read."
                                   if data["color"] != picked else "")
    db.update("applications", app_id, {"cv_data": db.jdump(cvdata.clean(data)), "updated_at": db.now()})
    return go(back, msg=msg)


@app.post("/applications/{app_id}/voice-revise")
async def application_voice_revise(request: Request, app_id: int):
    """The sentences you ticked on the Voice tab, rewritten the plainer way the voice read suggested."""
    f = await form(request)
    a = need(db.q1("SELECT * FROM applications WHERE id = ?", (app_id,)))
    read = voice.read_notes(a)
    chosen = [n for n in (read or {}).get("notes") or [] if f.get(f"use_{n['n']}")]
    if not chosen:
        return go(f"/applications/{app_id}?tab=voice", err="Tick at least one sentence first.")
    instructions = ("Change only these sentences, so they sound like the candidate. Use the plainer wording, or "
                    "something close to it that fits the letter; keep every fact and add none:\n"
                    + "\n".join(f"- “{n['text']}” → “{n['plainer']}”" for n in chosen))
    voice.settle(app_id, [n["plainer"] for n in chosen])  # later reads leave these sentences alone
    voice.leave(app_id, [n["text"] for n in read["notes"] if n not in chosen])  # and the ones you kept as they are
    tasks.enqueue("revise_application", {"application_id": app_id, "instructions": instructions, "part": "letter",
                                         **chosen_model(f)}, ref=f"app:{app_id}", dedupe=False)
    return go(f"/applications/{app_id}?tab=voice", msg=with_model_msg(f, "Revising") + "…")


@app.post("/applications/{app_id}/voice-keep")
def application_voice_keep(request: Request, app_id: int):
    """Keep the sentences the voice read pointed at as they are: later reads don't suggest them again."""
    a = need(db.q1("SELECT * FROM applications WHERE id = ?", (app_id,)))
    notes = (voice.read_notes(a) or {}).get("notes") or []
    if not notes:
        return go(f"/applications/{app_id}?tab=voice")
    voice.leave(app_id, [n["text"] for n in notes])
    n = len(notes)
    return go(f"/applications/{app_id}?tab=voice",
              msg=f"Kept {'it' if n == 1 else f'all {n}'} as you wrote {'it' if n == 1 else 'them'}. Later reads leave "
                  f"{'it' if n == 1 else 'them'} alone.")


@app.post("/applications/{app_id}/answers")
async def application_answers(request: Request, app_id: int):
    """Your answers to the review screen's questions: what you confirmed is suggested for your career database (it
    waits for your approval there), and the draft is revised to match."""
    f = await form(request)
    a = need(tracker.app_with_job(app_id))
    answers = {q["id"]: {"verdict": f.get(f"v_{q['id']}") or "", "text": f.get(f"t_{q['id']}") or ""} for q in review.questions(a)}
    result = review.apply_answers(a, answers, chosen_model(f))
    if not (result["placing"] or result["confirmed"] or result["revised"]):
        return go(f"/applications/{app_id}?tab=review", err="Answer at least one question first.")
    n = result["placing"]
    said = []
    if n:
        said.append(f"Suggesting {n} answer{'s' if n != 1 else ''} for your career database, where you approve "
                    "them under Pending changes. Meanwhile "
                    + ("the draft is revised to match…" if result["revised"] else "the facts are checked again…"))
    elif result["revised"]:
        said.append("Revising the draft to match…")
    if result["confirmed"]:
        said.append(f"{result['confirmed']} kept as true for this application only.")
    return go(f"/applications/{app_id}?tab=review", msg=" ".join(said))


@app.post("/applications/{app_id}/redraft")
async def application_redraft(request: Request, app_id: int):
    f = await form(request)
    if f.get("instructions") is not None:
        db.update("applications", app_id, {"instructions": f.get("instructions") or None})
    part = f.get("part") if f.get("part") in ("letter", "cv") else "both"
    payload = {"application_id": app_id, **({"part": part} if part != "both" else {}), **chosen_model(f)}
    tasks.enqueue("draft_application", payload, ref=f"app:{app_id}", dedupe=True)
    what = {"letter": "a fresh letter", "cv": "a fresh CV"}.get(part, "a fresh draft")
    tab = "review" if f.get("from") == "review" else ("cv" if part == "cv" else "letter")
    return go(f"/applications/{app_id}?tab={tab}", msg=with_model_msg(f, f"Writing {what}") + ". The current one stays under Versions.")


@app.post("/applications/{app_id}/manual-import")
async def application_manual_import(request: Request, app_id: int):
    f = await form(request)
    need(db.q1("SELECT id FROM applications WHERE id = ?", (app_id,)))
    try:
        result = writer.import_manual(app_id, f.get("text", ""))
    except ValueError as e:
        return go(f"/applications/{app_id}?tab=claude", err=str(e))
    msg = f"Claude's version is in ({result['letter_words']} words). The previous draft is under Versions. Fact-checking now…"
    return go(f"/applications/{app_id}?tab=letter", msg=msg)


@app.post("/applications/{app_id}/check")
async def application_check(request: Request, app_id: int):
    """Fact-check what changed since the last check; everything with `full`, or when you picked another model."""
    f = await form(request)
    model = chosen_model(f)
    full = bool(f.get("full") or model.get("model"))
    tasks.enqueue("check_application", {"application_id": app_id, **model, **({"full": True} if full else {})},
                  ref=f"app:{app_id}")
    return go(f"/applications/{app_id}?tab=check", msg=with_model_msg(f, "Fact-checking " + ("everything again" if full
                                                                                            else "what changed")) + "…")


@app.get("/applications/{app_id}/export/{name}")
def application_export(request: Request, app_id: int, name: str):
    a = need(tracker.app_with_job(app_id))
    kind, _, fmt = name.partition(".")
    if (kind, fmt) == ("cv", "json"):  # the open JSON Resume format, for other résumé tools
        data = db.jload(a.get("cv_data"), None)
        if not data:
            return go(f"/applications/{app_id}?tab=cv", err="This CV is kept as text, so there is no data file for it.")
        body = json.dumps(cvdata.to_json_resume(data), ensure_ascii=False, indent=2)
        filename = exporter.filename("cv", "json", a.get("company_name"), a.get("language"))
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})
    if kind not in ("letter", "cv") or fmt not in ("pdf", "docx"):
        raise HTTPException(404)
    if not a.get(kind):
        return go(f"/applications/{app_id}?tab=send", err="There's nothing to export yet.")
    data, filename = exporter.render(a, kind, fmt)
    media = "application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    disposition = "inline" if fmt == "pdf" and request.query_params.get("view") else "attachment"
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}"})


def _png(data: dict | None, language: str | None, page: int) -> Response:
    image = cvrender.png(data, language, page) if data else None
    if image is None:
        raise HTTPException(404)
    return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/applications/{app_id}/cv-page/{page}.png")
def application_cv_page(app_id: int, page: int):
    a = need(db.q1("SELECT cv_data, language FROM applications WHERE id = ?", (app_id,)))
    return _png(db.jload(a["cv_data"], None), a["language"], page)


# ---- Your CV: the base that tailored CVs start from, and how CVs look ----

@app.post("/cv/base")
async def cv_base_build(request: Request):
    f = await form(request)
    doc = need(db.q1("SELECT id FROM documents WHERE id = ? AND kind = 'cv'", (int(f.get("doc_id") or 0),)))
    tasks.enqueue("build_cv_base", {"doc_id": doc["id"], **chosen_model(f)}, ref="cv-base", dedupe=False)
    return go("/profile?tab=cv", msg="Reading that CV. It becomes your base when it's done.")


@app.post("/cv/base/{base_id}/use")
def cv_base_use(base_id: int):
    need(db.q1("SELECT id FROM cv_bases WHERE id = ?", (base_id,)))
    settings.update("cv", {"base_id": base_id})
    return go("/profile?tab=cv", msg="That is now the CV new applications start from.")


@app.post("/cv/base/{base_id}/save")
async def cv_base_save(request: Request, base_id: int):
    f = await form(request)
    need(db.q1("SELECT id FROM cv_bases WHERE id = ?", (base_id,)))
    try:
        data = cvdata.clean(json.loads(f.get("data") or ""))
        cvrender.render(data)
    except Exception as e:  # noqa: BLE001
        return go(f"/profile?tab=cv&base={base_id}", err=f"That isn't a CV I can use, so nothing was saved: {str(e)[:160]}")
    cvdata.save_base(base_id, data)
    return go(f"/profile?tab=cv&base={base_id}", msg="Base CV saved.")


@app.post("/cv/base/{base_id}/check")
def cv_base_check(base_id: int):
    need(db.q1("SELECT id FROM cv_bases WHERE id = ?", (base_id,)))
    tasks.enqueue("check_cv_base", {"base_id": base_id}, ref="cv-base-check")
    return go(f"/profile?tab=cv&base={base_id}", msg="Checking your base CV against your career database…")


@app.post("/cv/base/{base_id}/answers")
async def cv_base_answers(request: Request, base_id: int):
    """Your answers about base CV lines the career database doesn't back: suggested for the database (and counted as
    true meanwhile), or the CV is changed."""
    f = await form(request)
    base = need(cvdata.base(base_id))
    answers = {q["id"]: {"verdict": f.get(f"v_{q['id']}") or "", "text": f.get(f"t_{q['id']}") or ""}
               for q in review.base_questions(base)}
    result = review.apply_base_answers(base, answers)
    if not (result["placing"] or result["changed"] or result["missed"]):
        return go(f"/profile?tab=cv&base={base_id}#cv-check", err="Answer at least one question first.")
    said = []
    if result["placing"]:
        said.append(f"{result['placing']} kept as true and suggested for your career database (approve under Pending changes)")
    if result["changed"]:
        said.append(f"{result['changed']} line{'s' if result['changed'] != 1 else ''} changed in the base CV")
    line = ", ".join(said)
    msg = (line[:1].upper() + line[1:] + ". Checking again…") if said else None
    err = ("Couldn't find this in the CV data, so change it by hand under Correct the base CV: "
           + "; ".join(t[:80] for t in result["missed"])) if result["missed"] else None
    return go(f"/profile?tab=cv&base={base_id}#cv-check", msg=msg, err=err)


@app.post("/cv/base/redo-drafts")
def cv_base_redo_drafts():
    """Make the CVs of the applications you haven't sent again, from the base CV as it is now."""
    ids = review.open_drafts()
    for app_id in ids:
        tasks.enqueue("draft_application", {"application_id": app_id, "part": "cv"}, ref=f"app:{app_id}")
    return go("/profile?tab=cv", msg=f"Making the CV again for {len(ids)} application{'s' if len(ids) != 1 else ''}. "
                                     "The letters stay as they are.")


@app.post("/cv/design")
async def cv_design(request: Request):
    f = await form(request)
    patch: dict = {"primary": (f.get("primary") or "").strip(), "font": (f.get("font") or "").strip(),
                   "company_colour": bool(f.get("company_colour"))}
    for key, cast in (("size", float), ("sidebar", float)):
        try:
            patch[key] = cast(f.get(key) or 0) or settings.DEFAULTS["cv"][key]
        except ValueError:
            pass
    for key in ("sidebar_order", "main_order"):
        wanted = [x.strip() for x in (f.get(key) or "").replace(",", " ").split() if x.strip()]
        if wanted:
            patch[key] = wanted
    settings.update("cv", patch)
    return go("/profile?tab=cv", msg="CV design saved. Every CV uses it from now on.")


@app.post("/cv/photo")
async def cv_photo(request: Request):
    data = await request.form()
    up = data.get("photo")
    if not getattr(up, "filename", None):
        return go("/profile?tab=cv#cv-photo", err="Choose a picture first.")
    try:
        cvrender.save_photo(await up.read())
    except ValueError as e:
        return go("/profile?tab=cv#cv-photo", err=str(e))
    return go("/profile?tab=cv#cv-photo", msg="Photo saved. It is on every CV, unless a job ad asks for applications without one.")


@app.post("/cv/photo/remove")
def cv_photo_remove():
    cvrender.remove_photo()
    return go("/profile?tab=cv#cv-photo", msg="Photo removed from your CVs.")


@app.get("/cv/photo.jpg")
def cv_photo_file():
    path = cvrender.photo_path()
    if not path:
        raise HTTPException(404)
    return Response(path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/cv/base/{base_id}/page/{page}.png")
def cv_base_page(base_id: int, page: int):
    b = need(cvdata.base(base_id))
    return _png(b["data"], b["language"], page)


@app.get("/cv/base/{base_id}/cv.{fmt}")
def cv_base_export(base_id: int, fmt: str):
    b = need(cvdata.base(base_id))
    if fmt == "pdf":
        return Response(cvrender.pdf(b["data"], b["language"]), media_type="application/pdf",
                        headers={"Content-Disposition": "inline; filename=cv.pdf"})
    if fmt == "json":
        return Response(json.dumps(cvdata.to_json_resume(b["data"]), ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=cv.json"})
    raise HTTPException(404)


@app.post("/applications/{app_id}/mail")
async def application_mail(request: Request, app_id: int):
    a = need(tracker.app_with_job(app_id))
    paths = await asyncio.to_thread(exporter.save_exports, a)
    mail = emails.application(a)  # the same text the Send tab shows
    try:
        await asyncio.to_thread(mailer.create_draft, mail["subject"], mail["body"], mail["to"], paths)
    except mailer.MailError as e:
        return go(f"/applications/{app_id}?tab=send", err=str(e))
    return go(f"/applications/{app_id}?tab=send", msg="A draft is open in Mail with both PDFs attached. Check it and press Send yourself.")


@app.post("/applications/{app_id}/mail-followup")
async def application_mail_followup(request: Request, app_id: int):
    """The follow-up email as a draft in Mail. You press Send, then "I sent it"."""
    mail = emails.followup(need(tracker.app_with_job(app_id)))
    try:
        await asyncio.to_thread(mailer.create_draft, mail["subject"], mail["body"], mail["to"], [])
    except mailer.MailError as e:
        return go(f"/applications/{app_id}?tab=timeline", err=str(e))
    return go(f"/applications/{app_id}?tab=timeline", msg="The follow-up is open in Mail. Check it, press Send yourself, then mark it as sent here.")


@app.post("/jobs/{job_id}/mail-ask")
async def job_mail_ask(request: Request, job_id: int):
    """The email asking whether the job is still open, as a draft in Mail. You press Send."""
    mail = emails.still_open(need(jobs.get(job_id)))
    try:
        await asyncio.to_thread(mailer.create_draft, mail["subject"], mail["body"], mail["to"], [])
    except mailer.MailError as e:
        return go(back(request, f"/jobs/{job_id}"), err=str(e))
    return go(back(request, f"/jobs/{job_id}"), msg="The email is open in Mail. Check it and press Send yourself.")


@app.post("/applications/{app_id}/status")
async def application_status(request: Request, app_id: int):
    f = await form(request)
    tracker.set_status(app_id, f.get("status", ""))
    return go(back(request, f"/applications/{app_id}"), msg="Status updated.")


@app.post("/applications/{app_id}/applied")
async def application_applied(request: Request, app_id: int):
    f = await form(request)
    when = f.get("date") or date.today().isoformat()
    a = need(db.q1("SELECT * FROM applications WHERE id = ?", (app_id,)))
    if a["applied_at"]:
        return go(f"/applications/{app_id}?tab=timeline", msg="Already marked as sent.")
    tracker.add_event(app_id, "applied", occurred_at=f"{when}T12:00:00", summary="Application sent")
    return go(f"/applications/{app_id}?tab=timeline", msg="Marked as sent. I'll remind you to follow up, and track their response.")


@app.post("/applications/{app_id}/reply")
async def application_reply(request: Request, app_id: int):
    f = await form(request)
    text = f.get("text", "")
    if len(text) < 15:
        return go(f"/applications/{app_id}?tab=timeline", err="Paste the message you received.")
    tasks.enqueue("classify_response", {"application_id": app_id, "text": text, **chosen_model(f)}, ref=f"app:{app_id}", dedupe=False)
    return go(f"/applications/{app_id}?tab=timeline", msg="Reading their reply…")


@app.post("/applications/{app_id}/event")
async def application_event(request: Request, app_id: int):
    f = await form(request)
    type_ = f.get("type", "note")
    if type_ not in tracker.EVENT_LABELS:
        return go(f"/applications/{app_id}?tab=timeline", err="Unknown event type.")
    when = f.get("date") or date.today().isoformat()
    tracker.add_event(app_id, type_, occurred_at=f"{when}T12:00:00", summary=f.get("summary") or None,
                      feedback=f.get("feedback") or None)
    return go(f"/applications/{app_id}?tab=timeline", msg="Added to the timeline.")


@app.post("/applications/{app_id}/event/{event_id}/delete")
def application_event_delete(request: Request, app_id: int, event_id: int):
    db.ex("DELETE FROM events WHERE id = ? AND application_id = ?", (event_id, app_id))
    return go(f"/applications/{app_id}?tab=timeline", msg="Event removed. (The status isn't changed automatically.)")


@app.post("/applications/{app_id}/prep")
async def application_prep(request: Request, app_id: int):
    f = await form(request)
    tasks.enqueue("interview_prep", {"application_id": app_id, **chosen_model(f)}, ref=f"app:{app_id}")
    return go(f"/applications/{app_id}?tab=interview", msg=with_model_msg(f, "Preparing your interview notes") + "…")


@app.post("/applications/{app_id}/followup")
async def application_followup(request: Request, app_id: int):
    f = await form(request)
    tasks.enqueue("followup", {"application_id": app_id, **chosen_model(f)}, ref=f"app:{app_id}")
    return go(f"/applications/{app_id}?tab=timeline", msg=with_model_msg(f, "Writing a follow-up") + "…")


@app.post("/applications/{app_id}/followup-sent")
def application_followup_sent(request: Request, app_id: int):
    tracker.add_event(app_id, "followup_sent", summary="Follow-up sent")
    return go(f"/applications/{app_id}?tab=timeline", msg="Noted.")


@app.post("/applications/{app_id}/restore/{version_id}")
def application_restore(request: Request, app_id: int, version_id: int):
    v = need(db.q1("SELECT * FROM app_versions WHERE id = ? AND application_id = ?", (version_id, app_id)))
    a = db.q1("SELECT * FROM applications WHERE id = ?", (app_id,))
    db.insert("app_versions", {"application_id": app_id, "label": "Before restore", "subject": a["subject"],
                               "letter": a["letter"], "cv": a["cv"], "cv_data": a["cv_data"], "created_at": db.now()})
    db.update("applications", app_id, {"subject": v["subject"], "letter": v["letter"], "cv": v["cv"],
                                       "cv_data": v["cv_data"], "updated_at": db.now()})
    return go(f"/applications/{app_id}", msg="Version restored.")


@app.post("/applications/{app_id}/delete")
def application_delete(request: Request, app_id: int):
    db.ex("DELETE FROM applications WHERE id = ?", (app_id,))
    return go("/tracker", msg="Application deleted.")


@app.get("/tracker")
def tracker_page(request: Request):
    rows = db.q("SELECT a.*, j.title AS job_title, j.company_name, j.company_id, j.fit_score, j.deadline FROM applications a "
                "JOIN jobs j ON j.id = a.job_id ORDER BY COALESCE(a.last_event_at, a.updated_at, a.created_at) DESC")
    columns = [(label, [r for r in rows if r["status"] in sts]) for label, sts in tracker.BOARD]
    return render(request, "tracker.html", columns=columns, followups=tracker.followups_due(),
                  status_labels=tracker.STATUS_LABELS)


# ---------------------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------------------

@app.get("/companies")
def companies_page(request: Request, show: str = "history"):
    rows = companies.all_with_metrics()
    if show == "history":
        rows = [r for r in rows if r["m"]["applications"] or r["user_rating"] or r["notes"]]
    rows.sort(key=lambda r: (r["m"]["score"] is None, -(r["m"]["score"] or 0), r["name"].lower()))
    return render(request, "companies.html", rows=rows, show=show,
                  total=db.scalar("SELECT COUNT(*) FROM companies") or 0)


@app.get("/companies/{company_id}")
def company_page(request: Request, company_id: int):
    c = need(db.q1("SELECT * FROM companies WHERE id = ?", (company_id,)))
    return render(request, "company.html", c=c, m=companies.metrics(company_id),
                  jobs=db.q("SELECT j.*, a.id AS app_id, a.status AS app_status FROM jobs j LEFT JOIN applications a "
                            "ON a.job_id = j.id WHERE j.company_id = ? ORDER BY j.id DESC", (company_id,)),
                  events=db.q("SELECT e.*, j.title AS job_title FROM events e JOIN applications a ON a.id = e.application_id "
                              "JOIN jobs j ON j.id = a.job_id WHERE e.company_id = ? ORDER BY e.occurred_at DESC", (company_id,)),
                  event_labels=tracker.EVENT_LABELS, status_labels=tracker.STATUS_LABELS,
                  sizes=companies.SIZES, size_labels=companies.SIZE_LABELS)


@app.post("/companies/{company_id}")
async def company_save(request: Request, company_id: int):
    f = await form(request)
    rating = f.get("user_rating")
    db.update("companies", company_id, {
        "website": f.get("website") or None, "notes": f.get("notes") or None,
        "user_rating": int(rating) if rating and rating.isdigit() else None,
        "blocked": 1 if f.get("blocked") else 0,
    })
    c = db.q1("SELECT size, size_by FROM companies WHERE id = ?", (company_id,))
    size = f.get("size") or None
    if size and (c["size_by"] != "you" or c["size"] != size):
        companies.set_size(company_id, size)
    elif not size and c["size_by"] == "you":
        companies.set_size(company_id, None)
        tasks.enqueue("size_companies", ref="companies")
    return go(f"/companies/{company_id}", msg="Saved.")


# ---------------------------------------------------------------------------------------
# Profile, documents, facts, style
# ---------------------------------------------------------------------------------------

@app.get("/profile")
def profile_page(request: Request, tab: str = "about"):
    docs = db.q("SELECT d.*, (SELECT COUNT(*) FROM facts f WHERE f.document_id = d.id) AS n_facts FROM documents d "
                "ORDER BY COALESCE(d.doc_date, '') DESC, d.id DESC")
    facts = profile.visible_facts() + db.q("SELECT * FROM facts WHERE hidden = 1 ORDER BY category, id")
    index = profile.places(facts)
    grouped = []
    for c, label in profile.CATEGORIES:
        items = [f for f in facts if f["category"] == c]
        sections = profile.by_area(items) if c == "skill" else profile.by_place(items, index)
        grouped.append((c, label, items, sections if len(sections) > 1 else [("", "", items)]))
    deleted = history.recently_deleted("facts", 20) if tab == "facts" else []
    changed, changed_total = profile.recently_changed() if tab == "facts" else ([], 0)
    pending = suggestions.waiting() if tab == "facts" else []
    active_rows = db.q("SELECT kind FROM tasks WHERE kind IN ('analyze_document', 'build_style', 'consolidate_facts') "
                       "AND status IN ('queued', 'running')")
    n_docs = sum(1 for r in active_rows if r["kind"] == "analyze_document")
    style_busy = any(r["kind"] == "build_style" for r in active_rows)
    busy = len(active_rows)
    cv = {}
    if tab == "cv":
        in_use = cvdata.base()
        shown = (request.query_params.get("base") or "").strip()
        main = (cvdata.base(int(shown)) if shown.isdigit() else None) or in_use  # the base shown, checked and edited on the page
        others = {}  # language → the base used for applications in it, where that isn't the one in use
        for b in cvdata.bases():
            if in_use and (b["language"] or "")[:2] != (in_use["language"] or "")[:2]:
                others.setdefault((b["language"] or "")[:2], b["id"])
        pages, cv_error, spilled = 0, "", []
        if main:
            try:
                pages = cvrender.pages(main["data"], main["language"])
                spilled = cvrender.spills(main["data"], main["language"])
            except Exception as e:  # noqa: BLE001
                cv_error = str(e)[:300]
        cv = {"main": main, "in_use": in_use["id"] if in_use else None, "other_languages": {v: k for k, v in others.items()},
              "bases": cvdata.bases(), "docs": cvdata.cv_documents(), "pages": pages, "error": cv_error,
              "design": settings.get("cv"), "photo": bool(cvrender.photo_path()), "spills": spilled, "json": json.dumps(main["data"], ensure_ascii=False, indent=2) if main else "",
              "busy": tasks.active(ref="cv-base"), "task_error": tasks.last_error("cv-base"),
              "questions": review.base_questions(main), "checked": db.jload((main or {}).get("claims"), None),
              "checking": tasks.active(ref="cv-base-check"), "check_error": tasks.last_error("cv-base-check"),
              "placed": review.last_placed("cv-base-check"),
              "open_drafts": len(review.open_drafts())}
    return render(request, "profile.html", tab=tab, n_docs=n_docs, style_busy=style_busy, levels=profile.LEVELS, cv=cv,
                  job_kinds=jobkinds.KINDS, kinds_wanted=jobkinds.wanted_kinds(),
                  made_style=llm.made_by("style", ("style_guide",)),
                  tidy_busy=any(r["kind"] == "consolidate_facts" for r in active_rows), p=settings.get("profile"), docs=docs, grouped=grouped, deleted=deleted,
                  changed=changed, changed_total=changed_total, recent_days=profile.RECENT_DAYS,
                  pending=pending, n_pending=suggestions.count(), category_labels=profile.CATEGORY_LABELS,
                  kinds=profile.KINDS, kind_labels=profile.KIND_LABELS, outcomes=profile.OUTCOMES,
                  categories=profile.CATEGORIES, areas=profile.AREAS, depths=profile.DEPTHS, depth_labels=profile.DEPTH_LABELS,
                  style_guide=settings.get("style_guide"),
                  baseline=settings.get("style_stats") or {}, exemplars=voice.exemplars(None, 99999),
                  style_locked=settings.get("state").get("style_locked"), busy=busy, metrics=voice.METRICS,
                  style_error=tasks.last_error("style"), outcome_labels=profile.OUTCOME_LABELS,
                  folders=db.q("SELECT * FROM watched_folders ORDER BY id"),
                  nextcloud=watch.nextcloud_candidates() if tab == "documents" else [],
                  search=retrieval.status() if tab == "documents" else None,
                  agent=knowledge.status() if tab == "documents" else None)


@app.get("/profile/progress")
def profile_progress(request: Request):
    rows = db.q("SELECT kind, status FROM tasks WHERE kind IN ('analyze_document', 'build_style', 'consolidate_facts') "
                "AND status IN ('queued', 'running')")
    if not rows:
        return done_response()
    return templates.TemplateResponse(request, "partials/profile_progress.html",
                                      {"n_docs": sum(1 for r in rows if r["kind"] == "analyze_document"),
                                       "style_busy": any(r["kind"] == "build_style" for r in rows),
                                       "tidy_busy": any(r["kind"] == "consolidate_facts" for r in rows)})


PROFILE_FIELDS = ["name", "email", "phone", "city", "linkedin", "website", "headline", "level", "target_roles",
                  "target_industries", "locations", "max_km", "preferred_km", "big_employers", "min_salary", "availability", "deal_breakers",
                  "always_mention", "never_mention", "about"]


@app.post("/profile/about")
async def profile_about(request: Request):
    data = await request.form()
    patch = {k: (data.get(k) or "").strip() for k in PROFILE_FIELDS}
    if patch["big_employers"] not in ("off", "moderate", "strong"):
        patch["big_employers"] = "moderate"
    patch["work_modes"] = [m for m in ("onsite", "hybrid", "remote") if data.get(f"mode_{m}")]
    patch["job_kinds"] = {k: bool(data.get(f"kind_{k}")) for k, _ in jobkinds.KINDS}
    current = settings.get("profile")
    current.update(patch)
    settings.set("profile", current)
    if companies.sizes_wanted() and companies.unsized():  # just switched on: judge the employers already here
        tasks.enqueue("size_companies", ref="companies")
    return go("/profile?tab=about", msg="Saved.")


@app.post("/documents/upload")
async def documents_upload(request: Request):
    data = await request.form()
    files = [f for f in data.getlist("files") if getattr(f, "filename", None)]
    added, skipped, errors = [], 0, []
    for up in files:
        try:
            doc_id, created = ingest.import_bytes(up.filename, await up.read())
            if created:
                added.append(doc_id)
            else:
                skipped += 1
        except ingest.ImportErrorUser as e:
            errors.append(f"{up.filename}: {e}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{up.filename}: couldn't read ({e.__class__.__name__})")
    for doc_id in ingest.cv_first(added):
        tasks.enqueue("analyze_document", {"doc_id": doc_id}, ref=f"doc:{doc_id}")
    if added:
        tasks.enqueue("embed_index", {}, ref="index")
    msg = f"Imported {len(added)} file{'s' if len(added) != 1 else ''}" + (f", {skipped} already imported" if skipped else "") + "."
    return go("/profile?tab=documents", msg=msg, err="; ".join(errors)[:900] or None)


@app.post("/documents/import")
async def documents_import(request: Request):
    f = await form(request)
    path = Path(f.get("path", "")).expanduser()
    if not f.get("path"):
        return go("/profile?tab=documents", err="Enter a folder or file path.")
    try:
        result = await asyncio.to_thread(ingest.import_folder, path)
    except ingest.ImportErrorUser as e:
        return go("/profile?tab=documents", err=str(e))
    except PermissionError:
        return go("/profile?tab=documents", err="macOS didn't allow reading that folder. Give your Terminal app Full Disk Access, or use the upload button.")
    for doc_id in ingest.cv_first(result["imported"]):
        tasks.enqueue("analyze_document", {"doc_id": doc_id}, ref=f"doc:{doc_id}")
    if result["imported"]:
        tasks.enqueue("embed_index", {}, ref="index")
    msg = f"Imported {len(result['imported'])} document(s) from {path.name or path}" + \
          (f", {result['skipped']} already imported" if result["skipped"] else "") + "."
    err = "; ".join(f"{n}: {e}" for n, e in result["errors"][:8]) or None
    if err and len(result["errors"]) > 8:
        err += f" … and {len(result['errors']) - 8} more"
    return go("/profile?tab=documents", msg=msg, err=err)


@app.post("/documents/analyze-pending")
def documents_analyze_pending(request: Request):
    rows = db.q("SELECT id FROM documents WHERE status IN ('new', 'error')")
    for r in rows:
        tasks.enqueue("analyze_document", {"doc_id": r["id"]}, ref=f"doc:{r['id']}")
    return go("/profile?tab=documents", msg=f"Analysing {len(rows)} document(s)…")


@app.get("/documents/{doc_id}")
def document_page(request: Request, doc_id: int):
    d = need(db.q1("SELECT * FROM documents WHERE id = ?", (doc_id,)))
    return render(request, "document.html", d=d, kinds=profile.KINDS, outcomes=profile.OUTCOMES,
                  made=llm.made_by(f"doc:{doc_id}", ("analyze_document",)),
                  kind_labels=profile.KIND_LABELS, category_labels=profile.CATEGORY_LABELS,
                  facts=db.q("SELECT * FROM facts WHERE document_id = ? ORDER BY category, id", (doc_id,)),
                  waiting=db.scalar("SELECT COUNT(*) FROM fact_suggestions WHERE document_id = ? AND status = 'pending'", (doc_id,)) or 0,
                  busy=tasks.active(ref=f"doc:{doc_id}"), error=tasks.last_error(f"doc:{doc_id}"))


@app.post("/documents/{doc_id}")
async def document_save(request: Request, doc_id: int):
    f = await form(request)
    patch = {k: f.get(k) or None for k in ("kind", "language", "doc_date", "outcome", "target_company", "target_role", "title")
             if k in f}
    if "kind" in patch and patch["kind"] is None:
        patch["kind"] = "unknown"
    if "outcome" in patch and patch["outcome"] is None:
        patch["outcome"] = "unknown"
    if "flags" in f:
        patch["use_for_style"] = 1 if f.get("use_for_style") else 0
        patch["authored"] = 1 if f.get("authored") else 0
    db.update("documents", doc_id, patch)
    if "flags" in f or "outcome" in patch:
        voice.compute_baseline()
    return go(back(request, f"/documents/{doc_id}"), msg="Saved.")


@app.post("/documents/{doc_id}/reanalyze")
async def document_reanalyze(request: Request, doc_id: int):
    f = await form(request)
    model = chosen_model(f)
    tasks.enqueue("analyze_document", {"doc_id": doc_id, "replace": bool(model), **model}, ref=f"doc:{doc_id}")
    if model:
        return go(f"/documents/{doc_id}", msg=with_model_msg(f, "Reading it again") + ": what its last reading suggested "
                                                                                     "and you haven't decided is replaced.")
    return go(f"/documents/{doc_id}", msg="Analysing again. What it finds waits for you under Career database → Pending changes.")


@app.post("/redo")
async def redo_work(request: Request):
    f = await form(request)
    kinds = [k for k in redo.KINDS if f.get(f"what_{k}")]
    if not kinds:
        return go("/settings#redo", err="Tick at least one kind of work to redo.")
    counts = redo.enqueue(kinds, chosen_model(f).get("model"), f.get("only_model") or None)
    total = sum(counts.values())
    if not total:
        return go("/settings#redo", msg="Nothing matched, so nothing was queued.")
    parts = ", ".join(f"{n} × {k}" for k, n in counts.items() if n)
    return go("/tasks", msg=with_model_msg(f, f"Queued {total} tasks ({parts})") + ". Earlier drafts stay under Versions.")


@app.post("/documents/{doc_id}/delete")
async def document_delete(request: Request, doc_id: int):
    f = await form(request)
    d = db.q1("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if d:
        if f.get("with_facts"):
            db.ex("DELETE FROM facts WHERE document_id = ? AND verified = 0", (doc_id,))
        db.ex("DELETE FROM documents WHERE id = ?", (doc_id,))
        # The stored copy of the file stays, so the document can be restored from History.
        voice.compute_baseline()
    return go("/profile?tab=documents", msg="Document deleted.")


FACT_FIELDS = ["category", "title", "organization", "location", "start_date", "end_date", "detail", "evidence", "tags",
               "area", "depth"]


@app.post("/facts/add")
async def fact_add(request: Request):
    f = await form(request)
    if not f.get("title"):
        return go("/profile?tab=facts", err="A fact needs a title.")
    row = {k: f.get(k) or None for k in FACT_FIELDS}
    if (f.get("document_id") or "").isdigit() and db.q1("SELECT 1 FROM documents WHERE id = ?", (int(f["document_id"]),)):
        row["document_id"] = int(f["document_id"])
    row.update({"source": "manual", "verified": 1, "created_at": db.now()})
    fid = db.insert("facts", row)
    nxt = f.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return go(nxt, msg="Added to your career database. Future drafts can use it.")
    return go(f"/profile?tab=facts#fact-{fid}", msg="Added to your career database.")


@app.post("/facts/{fact_id}")
async def fact_save(request: Request, fact_id: int):
    f = await form(request)
    patch = {k: f.get(k) or None for k in FACT_FIELDS if k in f}
    patch.update({"verified": 1, "updated_at": db.now()})
    db.update("facts", fact_id, patch)
    return go(f"/profile?tab=facts#fact-{fact_id}", msg="Saved and marked as checked.")


@app.post("/facts/{fact_id}/toggle/{field}")
def fact_toggle(request: Request, fact_id: int, field: str):
    if field in ("verified", "hidden"):
        db.ex(f"UPDATE facts SET {field} = 1 - {field}, updated_at = ? WHERE id = ?", (db.now(), fact_id))
    return go(f"/profile?tab=facts#fact-{fact_id}")


@app.post("/facts/{fact_id}/delete")
def fact_delete(request: Request, fact_id: int):
    db.ex("DELETE FROM facts WHERE id = ?", (fact_id,))
    return go("/profile?tab=facts", msg="Fact deleted.")


@app.post("/facts/tidy")
async def facts_tidy(request: Request):
    f = await form(request)
    tasks.enqueue("consolidate_facts", {**chosen_model(f)}, ref="facts")
    return go("/profile?tab=facts", msg=with_model_msg(f, "Looking for duplicates")
              + "… Any it finds wait for you under Pending changes.")


def _name_change(what: str, title: str) -> None:
    """History names the click after what it did, not after the address it was sent to."""
    o = history.current()
    if o is not None:
        o.context = f"{what}: “{title[:120]}”"


@app.post("/suggestions/{sid}/approve")
async def suggestion_approve(request: Request, sid: int):
    """Approve a suggested change, as it is or as you edited it in the form first."""
    f = await form(request)
    s = need(suggestions.get(sid))
    _name_change("Kept a change" if s["status"] == "applied" else "Approved a suggested change", s["title"])
    try:
        r = suggestions.approve(sid, f if f.get("edited") else None)
    except suggestions.Problem as e:
        return go(f"/profile?tab=facts#suggestion-{sid}", err=str(e))
    return go("/profile?tab=facts#pending", msg=r["said"])


@app.post("/suggestions/{sid}/refuse")
def suggestion_refuse(request: Request, sid: int):
    s = need(suggestions.get(sid))
    _name_change("Undid a change" if s["status"] == "applied" else "Refused a suggested change", s["title"])
    try:
        r = suggestions.refuse(sid)
    except suggestions.Problem as e:
        return go(f"/profile?tab=facts#suggestion-{sid}", err=str(e))
    return go("/profile?tab=facts#pending", msg=r["said"])


@app.post("/suggestions/document/{doc_id}/{choice}")
def suggestions_for_document(request: Request, doc_id: int, choice: str):
    """Everything one document's reading suggested, approved or refused in one go."""
    if choice not in ("approve", "refuse"):
        raise HTTPException(404)
    d = need(db.q1("SELECT title, filename FROM documents WHERE id = ?", (doc_id,)))
    name = d["title"] or d["filename"]
    _name_change("Approved the suggestions from" if choice == "approve" else "Refused the suggestions from", name)
    n, problems = suggestions.decide_all(doc_id, choice == "approve")
    said = (f"{n} fact{'s' if n != 1 else ''} from {name} " + ("added to your career database." if choice == "approve"
                                                                else "left out. They won't be suggested again."))
    return go("/profile?tab=facts#pending", msg=said, err="; ".join(problems)[:600] or None)


@app.post("/style")
async def style_save(request: Request):
    f = await form(request)
    settings.set("style_guide", f.get("guide", ""))
    settings.update("state", {"style_locked": True})
    return go("/profile?tab=style", msg="Saved. Your edits are kept until you rebuild the guide.")


@app.post("/style/rebuild")
async def style_rebuild(request: Request):
    f = await form(request)
    settings.update("state", {"style_locked": False})
    tasks.enqueue("build_style", {**chosen_model(f)}, ref="style")
    return go("/profile?tab=style", msg=with_model_msg(f, "Studying your writing") + "…")


# ---------------------------------------------------------------------------------------
# Insights, settings, sources, tasks
# ---------------------------------------------------------------------------------------

@app.get("/insights")
def insights_page(request: Request):
    return render(request, "insights.html", funnel=insights.funnel(), r=insights.response_stats(),
                  skills=insights.skills_demand(), ranks=insights.company_rankings(), spend=insights.spend())


@app.get("/settings")
def settings_page(request: Request):
    st = llm.status()
    bk = backup.status()
    bk_error = tasks.last_error("backup")  # an automatic backup that failed, unless one has worked since
    if bk_error and (bk_error["finished_at"] or "") <= bk["last_at"]:
        bk_error = None
    return render(request, "settings.html", st=st, cfg=settings.get("llm"), auto=settings.get("automation"),
                  w=settings.get("writing"),
                  sources=db.q("SELECT * FROM sources ORDER BY id"), source_kinds=sources.KINDS,
                  collect_bookmark=collect.bookmark(str(request.base_url)),
                  collect_sites=sorted({name for name, _ in collect.SITES.values()}),
                  collect_searches=collect.searches(), remind_days=collect.REMIND_DAYS,
                  data_dir=str(config.DATA_DIR), efforts=["low", "medium", "high", "xhigh", "max"],
                  search=retrieval.status(), letter_max=writer.LETTER_MAX,
                  steps=[{"key": k, "label": label, "tier": {"check": writer.check_tier()}.get(tier, tier),
                          "own": llm.step_settings(k)} for k, label, tier, _ in llm.STEPS],
                  check_tier=writer.check_tier(), model_label=llm.model_label,
                  model_groups=llm.model_choices(),
                  claude_code_models=llm.plan.choices()[0], claude_code_earlier=llm.plan.choices()[1],
                  claude_plan=llm.plan.status(), claude_plan_busy=tasks.active(ref="claude-models"), redo_kinds=redo.KINDS, redo_counts={k: len(v) for k, v in redo.candidates().items()},
                  models_used=redo.models_used(), backup=bk, backup_busy=tasks.active(ref="backup"), backup_error=bk_error)


def _save_models(f: dict) -> None:
    patch: dict = {}
    for tier in ("writing", "analysis"):
        choice = f.get(f"{tier}_model", "")
        provider, _, model = choice.partition("|")
        if provider in llm.PROVIDERS and model:
            patch[tier] = {"provider": provider, "model": model, "effort": f.get(f"{tier}_effort") or "medium"}
    patch["ollama_url"] = f.get("ollama_url") or "http://localhost:11434"
    try:
        patch["ollama_num_ctx"] = max(4096, min(262144, int(f.get("ollama_num_ctx") or 32768)))
        patch["concurrency"] = max(0, min(8, int(f.get("concurrency") or 0)))
    except ValueError:
        pass
    if "claude_code_path" in f:
        patch["claude_code_path"] = (f.get("claude_code_path") or "").strip()
    patch["local_only"] = bool(f.get("local_only"))
    patch["redact_for_cloud"] = bool(f.get("redact_for_cloud"))
    settings.update("llm", patch)


@app.post("/settings/models")
async def settings_models(request: Request):
    _save_models(await form(request))
    return go("/settings#models", msg="Model settings saved.")


@app.post("/settings/steps")
async def settings_steps(request: Request):
    """A model and an effort for each step; a step left on "the usual" follows the Writing or Analysis model."""
    f = await form(request)
    steps = {}
    for key, _, _, _ in llm.STEPS:
        choice = llm.parse_choice(f.get(f"{key}_model")) or {}
        effort = f.get(f"{key}_effort") if f.get(f"{key}_effort") in llm.EFFORTS else ""
        entry = {**({"provider": choice["provider"], "model": choice["model"]} if choice.get("provider") else {}),
                 **({"effort": effort} if effort else {})}
        if entry:
            steps[key] = entry
    cfg = settings.get("llm")
    cfg["steps"] = steps  # replaced whole, so a step set back to "the usual" is really cleared
    settings.set("llm", cfg)
    return go("/settings#steps", msg="Saved. Each step uses its model and effort from the next task on.")


@app.post("/settings/claude-models")
async def settings_claude_models(request: Request):
    tasks.enqueue("check_claude_models", {}, ref="claude-models")
    return go("/settings#claude-code", msg="Checking which Claude models your plan offers. It takes a minute or two.")


@app.post("/settings/test/{tier}")
async def settings_test(request: Request, tier: str):
    if tier not in ("writing", "analysis"):
        raise HTTPException(404)
    f = await form(request)
    if f.get("writing_model") or f.get("analysis_model"):
        _save_models(f)
    try:
        result = await asyncio.to_thread(llm.test_tier, tier)
        return go("/settings#models", msg=f"{tier.title()} model works: {result}")
    except llm.LLMError as e:
        return go("/settings#models", err=f"{tier.title()} model: {e}")


@app.post("/settings/automation")
async def settings_automation(request: Request):
    f = await form(request)

    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(f.get(key) or default)))
        except ValueError:
            return default

    settings.update("automation", {
        "auto_score": bool(f.get("auto_score")), "auto_draft": bool(f.get("auto_draft")),
        "auto_draft_daily_cap": num("auto_draft_daily_cap", 5, 0, 50),
        "followup_days": num("followup_days", 10, 1, 90), "ghost_days": num("ghost_days", 30, 7, 365),
        "notifications": bool(f.get("notifications")), "on_hold": bool(f.get("on_hold")),
    })
    if not tasks.on_hold():
        tasks.catch_up()
    return go("/settings#automation", msg="Automation settings saved.")


@app.post("/settings/hold")
async def settings_hold(request: Request):
    """Put automatic work on hold, or let it run again (the banner at the top of every page)."""
    f = await form(request)
    hold = f.get("on") == "1"
    settings.update("automation", {"on_hold": hold})
    if hold:
        return go(f.get("next") or "/", msg="Automatic work is on hold. Nothing starts on its own until you resume.")
    tasks.catch_up()
    tasks.queue_due()
    return go(f.get("next") or "/", msg="Automatic work is running again.")


@app.post("/settings/writing")
async def settings_writing(request: Request):
    f = await form(request)
    try:
        words = int(f.get("letter_words") or 0)
        words = 0 if words <= 0 else max(120, min(900, words))
    except ValueError:
        words = 0
    settings.update("writing", {
        "letter_words": words, "language": f.get("language") or "auto",
        "pdf_font": f.get("pdf_font") if f.get("pdf_font") in ("sans", "serif") else "sans",
        "accent_color": f.get("accent_color") or "#1f4e79",
        "extra_banned_phrases": f.get("extra_banned_phrases", ""),
    })
    return go("/settings#writing", msg="Writing settings saved.")


@app.post("/settings/search")
async def settings_search(request: Request):
    f = await form(request)
    settings.update("llm", {"embed_model": f.get("embed_model") or retrieval.DEFAULT_EMBED_MODEL,
                            "check_with": f.get("check_with") if f.get("check_with") in ("writing", "analysis") else "auto"})
    retrieval.embeddings_available(force=True)
    if retrieval.embeddings_available():
        tasks.enqueue("embed_index", {}, ref="index")
    return go("/settings#search", msg="Search settings saved.")


@app.post("/settings/pull-model")
def settings_pull_model(request: Request):
    tasks.enqueue("pull_model", {"model": retrieval.embed_model()}, ref="index")
    return go("/settings#search", msg=f"Downloading {retrieval.embed_model()} in the background (about 1 GB)…")


@app.post("/settings/reindex")
def settings_reindex(request: Request):
    retrieval.backfill_chunks()
    tasks.enqueue("embed_index", {}, ref="index")
    return go("/settings#search", msg="Indexing your documents…")


# ---------------------------------------------------------------------------------------
# Watched folders and search
# ---------------------------------------------------------------------------------------

@app.post("/knowledge/import")
async def knowledge_import(request: Request):
    try:
        r = await asyncio.to_thread(knowledge.import_facts)
    except FileNotFoundError as e:
        return go("/profile?tab=documents#agents", err=str(e))
    tasks.after_import()
    def n(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"

    msg = (f"From the agent folder: {n(r['facts_added'], 'new fact')} suggested, {r['facts_merged']} you already had, "
           f"{n(r['documents'], 'document')} added.")
    if r["unreadable"]:
        msg += f" {n(len(r['unreadable']), 'source file')} couldn't be read (scans or files inside archives)."
    return go("/profile?tab=facts#pending", msg=msg + " Approve the ones that are right under Pending changes.")


@app.post("/knowledge/settings")
async def knowledge_settings(request: Request):
    f = await form(request)
    path = (f.get("folder") or "").strip()
    if path and not Path(path).expanduser().is_dir():
        return go("/profile?tab=documents#agents", err=f"Folder not found: {path}")
    settings.update("knowledge", {"folder": path, "use": bool(f.get("use"))})
    return go("/profile?tab=documents#agents", msg="Agent folder settings saved.")


@app.post("/watch/add")
async def watch_add(request: Request):
    f = await form(request)
    try:
        folder_id = await asyncio.to_thread(watch.add_local, f.get("path", ""))
    except watch.WatchError as e:
        return go("/profile?tab=documents#watch", err=str(e))
    tasks.enqueue("scan_folder", {"folder_id": folder_id}, ref=f"watch:{folder_id}")
    return go("/profile?tab=documents#watch", msg="Watching the folder. Reading what's in it now…")


@app.post("/watch/add-nextcloud")
async def watch_add_nextcloud(request: Request):
    f = await form(request)
    try:
        folder_id = await asyncio.to_thread(watch.add_webdav, f.get("url", ""), f.get("username", ""), f.get("password", ""))
    except watch.WatchError as e:
        return go("/profile?tab=documents#watch", err=str(e))
    except Exception as e:  # noqa: BLE001 - Keychain problems
        return go("/profile?tab=documents#watch", err=f"Couldn't store the app password: {e}")
    tasks.enqueue("scan_folder", {"folder_id": folder_id}, ref=f"watch:{folder_id}")
    return go("/profile?tab=documents#watch", msg="Connected. Fetching your documents from Nextcloud…")


@app.post("/watch/{folder_id}/scan")
def watch_scan(request: Request, folder_id: int):
    tasks.enqueue("scan_folder", {"folder_id": folder_id}, ref=f"watch:{folder_id}")
    return go("/profile?tab=documents#watch", msg="Checking for new and changed files…")


@app.post("/watch/{folder_id}/toggle")
def watch_toggle(request: Request, folder_id: int):
    db.ex("UPDATE watched_folders SET enabled = 1 - enabled WHERE id = ?", (folder_id,))
    return go("/profile?tab=documents#watch")


@app.post("/watch/{folder_id}/delete")
def watch_delete(request: Request, folder_id: int):
    watch.remove(folder_id)
    return go("/profile?tab=documents#watch", msg="Stopped watching. Documents already imported are kept.")


@app.get("/search")
def search_page(request: Request, q: str = ""):
    hits = retrieval.search_chunks(q, k=12, exclude_kinds=()) if q.strip() else []
    return render(request, "search.html", q=q, hits=hits, kind_labels=profile.KIND_LABELS, search=retrieval.status())


@app.post("/sources/add")
async def sources_add(request: Request):
    f = await form(request)
    url = f.get("url", "")
    if not url.startswith(("http://", "https://")):
        return go("/settings#sources", err="Paste the address of a job search (https://…).")
    try:
        interval = int(f.get("interval") or 360)
    except ValueError:
        interval = 360
    try:
        sid = await asyncio.to_thread(sources.add, f.get("name", ""), url, f.get("kind") or "auto", interval)
    except sources.NotAllowed as e:
        return go("/settings#sources", err=f"Not added: {e}")
    tasks.enqueue("poll_source", {"source_id": sid}, ref=f"source:{sid}")
    return go("/settings#sources", msg="Source added. Checking it now…")


# --- Jobs collected from a page you have open (the "Collect jobs" bookmark) ---

@app.get("/collect")
def collect_page(request: Request):
    return render(request, "collect.html", result=None)


@app.get("/collect/take")
def collect_take_without_page(request: Request):
    # A browser that opened the new tab without sending the page along (or a reload of the result).
    return render(request, "collect.html", result={"ok": False, "message": "The page didn't come along. Go back to "
                  "the job site and click the Collect jobs bookmark again."})


@app.post("/collect/take")
async def collect_take(request: Request):
    """The bookmark posts the page you're on here, from the job site's own tab. It's the one address other sites
    may post to (see local_guard), so it takes nothing without the bookmark's key."""
    # A long results page is several MB, past the 1 MB a form field may be by default; collect.receive has the limit.
    f = await request.form(max_part_size=collect.MAX_POST)
    if not collect.token_ok(f.get("token") if isinstance(f.get("token"), str) else None):
        return render(request, "collect.html", result={"ok": False, "message": "This bookmark is out of date. "
                      "Remove it, and drag the Collect jobs button from Settings → Job sources to your Favourites bar again."})
    html, page_url = f.get("html"), f.get("url")
    try:
        if not isinstance(html, str) or not isinstance(page_url, str):
            raise ValueError("The page didn't arrive in one piece. Click the bookmark again.")
        pages = f.get("pages")
        pages = int(pages) if isinstance(pages, str) and pages.isdigit() else 1
        result = await asyncio.to_thread(collect.receive, html, page_url, min(max(pages, 1), 100))
    except ValueError as e:
        result = {"ok": False, "message": str(e)}
    return render(request, "collect.html", result=result)


@app.post("/collect/confirm")
async def collect_confirm(request: Request):
    f = await form(request)
    try:
        result = collect.confirm(f.get("page_id", ""))
    except ValueError as e:
        result = {"ok": False, "message": str(e)}
    return render(request, "collect.html", result=result)


@app.get("/collect/run")
def collect_run(request: Request):
    """Collect now: a page with the steps and the bookmark to drag, and the first of your saved searches to open;
    after each Send, the page that opens offers the next."""
    first = collect.start_run()
    if not first:
        return go("/settings#collect-searches", err="Save a search first: open a job search, click Collect jobs, "
                                                    "then press Save this search.")
    return render(request, "collect_run.html", first=first, searches=collect.searches(), remind_days=collect.REMIND_DAYS,
                  collect_bookmark=collect.bookmark(str(request.base_url)))


@app.post("/collect/searches/add")
async def collect_search_add(request: Request):
    f = await form(request)
    try:
        row = collect.add_search(f.get("name", ""), f.get("url", ""))
    except ValueError as e:
        return go("/settings#collect-searches", err=str(e))
    return go("/settings#collect-searches", msg=f"Saved “{row['name']}”. Today reminds you when it's a week since you collected it.")


@app.post("/collect/searches/remove")
async def collect_search_remove(request: Request):
    f = await form(request)
    collect.remove_search(f.get("url", ""))
    return go("/settings#collect-searches", msg="Search removed.")


@app.post("/collect/new-key")
def collect_new_key(request: Request):
    collect.new_token()
    return go("/settings#collect", msg="New key made. The old bookmark no longer works: drag the button to your Favourites bar again.")


@app.post("/sources/{source_id}/run")
async def sources_run(request: Request, source_id: int):
    f = await form(request)
    tasks.enqueue("poll_source", {"source_id": source_id, **chosen_model(f)}, ref=f"source:{source_id}")
    return go(back(request, "/settings#sources"), msg=with_model_msg(f, "Checking for new jobs") + "…")


@app.post("/sources/{source_id}/toggle")
def sources_toggle(request: Request, source_id: int):
    db.ex("UPDATE sources SET enabled = 1 - enabled WHERE id = ?", (source_id,))
    return go("/settings#sources")


@app.post("/sources/{source_id}/delete")
def sources_delete(request: Request, source_id: int):
    db.ex("DELETE FROM sources WHERE id = ?", (source_id,))
    db.ex("DELETE FROM settings WHERE key = ?", (f"alerts_seen:{source_id}",))
    return go("/settings#sources", msg="Source removed. Jobs it found are kept.")


@app.get("/settings/backup")
def settings_backup(request: Request):
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "backup.zip"
        backup.write_zip(target)
        data = target.read_bytes()
    name = f"career-agent-backup-{datetime.now().strftime('%Y-%m-%d')}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _save_backup(f: dict) -> tuple[bool, str]:
    """Save the backup settings from the form. Returns (whether the folder changed, a problem or "")."""
    folder = (f.get("folder") or "").strip()
    if folder:
        try:
            folder = str(backup.check_folder(folder))
        except backup.BackupError as e:
            return False, str(e)

    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(f.get(key) or default)))
        except ValueError:
            return default

    before = settings.get("backup")
    settings.update("backup", {"enabled": bool(f.get("enabled")), "folder": folder, "every_days": num("every_days", 7, 1, 365),
                               "mode": f.get("mode") if f.get("mode") in backup.MODES else "accumulate",
                               "keep": num("keep", 0, 0, 1000)})
    return folder != (before.get("folder") or ""), ""


@app.post("/settings/backup/save")
async def settings_backup_save(request: Request):
    changed, problem = _save_backup(await form(request))
    if problem:
        return go("/settings#backup", err=f"Not saved: {problem}")
    cfg = settings.get("backup")
    if changed and cfg["folder"] and cfg["enabled"]:
        tasks.enqueue("backup", {}, ref="backup")
        return go("/settings#backup", msg="Saved. Making the first backup in that folder now…")
    return go("/settings#backup", msg="Backup settings saved.")


@app.post("/settings/backup/run")
async def settings_backup_run(request: Request):
    """Save what's in the form, then back up right away (not behind a long model task in the queue)."""
    _, problem = _save_backup(await form(request))
    if problem:
        return go("/settings#backup", err=f"Not backed up: {problem}")
    try:
        result = await asyncio.to_thread(backup.run)
    except backup.BackupError as e:
        return go("/settings#backup", err=str(e))
    gone = f" Removed {len(result['removed'])} older backup{'s' if len(result['removed']) != 1 else ''}." if result["removed"] else ""
    return go("/settings#backup", msg=f"Backed up to {result['file']} ({result['size']}).{gone}")


@app.get("/tasks")
def tasks_page(request: Request):
    return render(request, "tasks.html", rows=db.q("SELECT * FROM tasks ORDER BY id DESC LIMIT 200"),
                  labels=tasks.TASK_LABELS)


@app.get("/tasks/indicator")
def tasks_indicator(request: Request):
    rows = tasks.active()
    running = [r for r in rows if r["status"] == "running"]
    return templates.TemplateResponse(request, "partials/indicator.html",
                                      {"n": len(rows), "running": running, "labels": tasks.TASK_LABELS,
                                       "waiting_for": ", ".join(tasks.queue_state()["holds"])})


@app.get("/api/queue")
def api_queue():
    """What the background queue is doing (used by `./agent run` to know when the work is done)."""
    return tasks.queue_state()


@app.post("/tasks/{task_id}/retry")
def tasks_retry(request: Request, task_id: int):
    tasks.retry(task_id)
    return go(back(request, "/tasks"), msg="Trying again…")


@app.post("/tasks/{task_id}/cancel")
def tasks_cancel(request: Request, task_id: int):
    tasks.cancel(task_id)
    return go(back(request, "/tasks"), msg="Cancelled.")


@app.post("/tasks/cancel-queued")
def tasks_cancel_queued(request: Request):
    n = tasks.cancel_queued()
    return go(back(request, "/tasks"), msg=f"Cancelled {n} waiting task{'s' if n != 1 else ''}. What is running now finishes.")


@app.post("/tasks/clear")
def tasks_clear(request: Request):
    db.ex("DELETE FROM tasks WHERE status IN ('done', 'cancelled') OR (status = 'error' AND finished_at < ?)", (db.days_ago(1),))
    return go("/tasks", msg="Cleared finished tasks.")


# ---------------------------------------------------------------------------------------
# History: undo anything
# ---------------------------------------------------------------------------------------

HISTORY_SHOW = [("all", "Everything"), ("you", "Your changes"), ("automatic", "Automatic"), ("deleted", "Deletions")]
HISTORY_PAGE = 50


@app.get("/history")
def history_page(request: Request, show: str = "all", before: int | None = None, conflict: int | None = None, force_ok: int = 0):
    show = show if show in dict(HISTORY_SHOW) else "all"
    entries = history.ops(show, before=before, limit=HISTORY_PAGE)
    for e in entries:
        e["details"] = [history.describe_change(c) for c in e["changes"][:40]]
        e["more"] = max(0, len(e["changes"]) - 40)
        undoer = history.get_op(e["undone_by"]) if e["undone_by"] else None
        e["undone"] = bool(undoer and not undoer["undone_by"])  # undoing the undo puts it back in force
    return render(request, "history.html", entries=entries, show=show, shows=HISTORY_SHOW, conflict=conflict,
                  force_ok=force_ok, next_before=entries[-1]["id"] if len(entries) == HISTORY_PAGE else None)


@app.post("/history/{op_id}/undo")
async def history_undo(request: Request, op_id: int):
    f = await form(request)
    nxt = local_path(f.get("next"), "/history")
    try:
        await asyncio.to_thread(history.undo, op_id, bool(f.get("force")))
    except history.Conflict as e:
        return go(f"/history?conflict={op_id}&force_ok={int(e.forceable)}#op-{op_id}",
                  err="Not undone: " + "; ".join(e.problems) + "." + (" Undo anyway puts it back regardless." if e.forceable else ""))
    return go(nxt, msg="Undone.")


@app.post("/history/change/{change_id}/restore")
async def history_restore(request: Request, change_id: int):
    f = await form(request)
    nxt = local_path(f.get("next"), "/history?show=deleted")
    try:
        await asyncio.to_thread(history.restore, change_id)
    except history.Conflict as e:
        return go(nxt, err="Not restored: " + "; ".join(e.problems) + ".")
    return go(nxt, msg="Restored.")


@app.get("/health")
def health():
    return {"ok": True}
