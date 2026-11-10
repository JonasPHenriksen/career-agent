"""Background work: a small persistent task queue, a worker, and a scheduler.

Everything slow (model calls, fetching) runs here so the UI stays responsive and work
survives restarts. Tasks are rows in the `tasks` table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import traceback
from datetime import datetime

from . import db, settings

log = logging.getLogger("career_agent.tasks")

TASK_LABELS = {
    "analyze_document": "Reading document",
    "build_style": "Learning your writing style",
    "consolidate_facts": "Tidying up your career database",
    "parse_job": "Reading job ad",
    "score_job": "Scoring fit",
    "draft_application": "Writing application",
    "revise_application": "Revising application",
    "check_application": "Fact-checking application",
    "interview_prep": "Preparing interview notes",
    "followup": "Writing follow-up",
    "classify_response": "Reading employer's reply",
    "poll_source": "Checking job source",
    "maintenance": "Housekeeping",
    "scan_folder": "Checking a watched folder",
    "embed_index": "Indexing documents for search",
    "pull_model": "Downloading the search model",
}

_wake = threading.Event()


def enqueue(kind: str, payload: dict | None = None, ref: str | None = None, dedupe: bool = True) -> int:
    payload = payload or {}
    if dedupe:
        existing = db.q1("SELECT id FROM tasks WHERE kind = ? AND payload = ? AND status IN ('queued', 'running')",
                         (kind, json.dumps(payload, sort_keys=True)))
        if existing:
            return existing["id"]
    task_id = db.insert("tasks", {"kind": kind, "payload": json.dumps(payload, sort_keys=True), "ref": ref,
                                  "status": "queued", "created_at": db.now()})
    _wake.set()
    return task_id


def _handlers() -> dict:
    from . import jobs, profile, retrieval, sources, tracker, voice, watch, writer

    def wait_for_analyses() -> None:
        """Let in-flight document analyses finish so we see everything."""
        for _ in range(300):
            if not db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'analyze_document' AND status = 'running'"):
                return
            time.sleep(2)

    def consolidate(_: dict) -> dict:
        wait_for_analyses()
        return profile.consolidate()

    def build_style(payload: dict) -> dict:
        wait_for_analyses()
        voice.compute_baseline()
        if payload.get("auto") and settings.get("state").get("style_locked"):
            return {"skipped": "you edited the style guide yourself; rebuild it from Profile → Writing style"}
        voice.build_style_guide()
        settings.update("state", {"style_built_at": db.now()})
        return {"ok": True}

    def maintenance(_: dict) -> dict:
        return tracker.maintenance()

    return {
        "analyze_document": lambda p: _after_analysis(profile.analyze_document(p["doc_id"])),
        "build_style": build_style,
        "consolidate_facts": consolidate,
        "parse_job": lambda p: jobs.parse(p["job_id"], use_text=p.get("use_text", False)),
        "score_job": lambda p: jobs.score(p["job_id"]),
        "draft_application": lambda p: writer.draft(p["application_id"]),
        "revise_application": lambda p: writer.revise(p["application_id"], p["instructions"], p.get("part", "letter")),
        "check_application": lambda p: writer.run_checks(p["application_id"]),
        "interview_prep": lambda p: writer.interview_prep(p["application_id"]),
        "followup": lambda p: writer.followup(p["application_id"]),
        "classify_response": lambda p: tracker.classify_response(p["application_id"], p["text"]),
        "poll_source": lambda p: sources.poll(p["source_id"]),
        "maintenance": maintenance,
        "scan_folder": lambda p: watch.scan(p["folder_id"]),
        "embed_index": lambda p: retrieval.embed_pending(),
        "pull_model": lambda p: _pull_model(p["model"]),
    }


def _pull_model(model: str) -> dict:
    import httpx2

    from . import retrieval

    url = (settings.get("llm").get("ollama_url") or "http://localhost:11434").rstrip("/")
    try:
        r = httpx2.post(f"{url}/api/pull", json={"model": model, "stream": False}, timeout=httpx2.Timeout(3600.0, connect=5.0))
    except httpx2.ConnectError as e:
        from .llm import LLMConfigError

        raise LLMConfigError("Ollama isn't running. Open the Ollama app and try again.") from e
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama couldn't download {model}: {r.text[:200]}")
    retrieval.embeddings_available(force=True)
    enqueue("embed_index", {}, ref="index")
    return {"pulled": model}


def after_import() -> None:
    """Facts and documents arrived without the local model reading them (the agent folder's import):
    run the same follow-ups as after reading documents."""
    _after_analysis({})


def _after_analysis(result: dict) -> dict:
    """When no more documents are waiting, refresh the writing baseline and style guide."""
    queued = db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'analyze_document' AND status = 'queued'")
    if not queued:
        enqueue("consolidate_facts", {}, ref="facts")
        enqueue("embed_index", {}, ref="index")
        # Rebuild the style guide only when a text you wrote yourself was (re)read since the last build.
        newest = db.scalar("SELECT MAX(analyzed_at) FROM documents WHERE authored = 1 AND status = 'analyzed' "
                           "AND kind IN ('application', 'email', 'report', 'other')")
        built = settings.get("state").get("style_built_at") or ""
        if newest and newest > built:
            enqueue("build_style", {"auto": True}, ref="style")
    return result


def run_task(task: dict) -> None:
    handlers = _handlers()
    db.update("tasks", task["id"], {"status": "running", "started_at": db.now(), "error": None})
    try:
        fn = handlers.get(task["kind"])
        if fn is None:
            raise RuntimeError(f"Unknown task type {task['kind']}")
        result = fn(json.loads(task["payload"] or "{}"))
        db.update("tasks", task["id"], {"status": "done", "finished_at": db.now(),
                                        "progress": json.dumps(result, ensure_ascii=False)[:2000] if result else None})
    except Exception as e:  # noqa: BLE001 - every failure is recorded on the task
        from .fetch import FetchError
        from .llm import LLMError

        msg = str(e) if isinstance(e, (LLMError, FetchError)) else f"{e.__class__.__name__}: {e}"
        if not isinstance(e, (LLMError, FetchError)):
            log.error("Task %s failed:\n%s", task["id"], traceback.format_exc())
        db.update("tasks", task["id"], {"status": "error", "finished_at": db.now(), "error": msg[:2000]})
        if task["kind"] == "draft_application":
            app_id = json.loads(task["payload"]).get("application_id")
            db.ex("UPDATE applications SET status = 'draft' WHERE id = ? AND status = 'drafting' AND letter IS NOT NULL", (app_id,))


def concurrency() -> int:
    cfg = settings.get("llm")
    if int(cfg.get("concurrency") or 0) > 0:
        return int(cfg["concurrency"])
    local = any((cfg.get(t) or {}).get("provider") == "ollama" for t in ("writing", "analysis"))
    return 1 if local else 3


def recover() -> None:
    """Tasks that were running when the app stopped go back in the queue."""
    db.ex("UPDATE tasks SET status = 'queued', started_at = NULL WHERE status = 'running'")
    db.ex("UPDATE applications SET status = 'draft' WHERE status = 'drafting' AND letter IS NOT NULL "
          "AND id NOT IN (SELECT CAST(substr(ref, 5) AS INTEGER) FROM tasks WHERE ref LIKE 'app:%' AND status = 'queued')")


async def worker_loop(stop: asyncio.Event) -> None:
    running: set[asyncio.Task] = set()
    while not stop.is_set():
        try:
            slots = concurrency() - len(running)
            if slots > 0:
                rows = db.q("SELECT * FROM tasks WHERE status = 'queued' ORDER BY id LIMIT ?", (slots,))
                for row in rows:
                    db.update("tasks", row["id"], {"status": "running", "started_at": db.now()})
                    t = asyncio.create_task(asyncio.to_thread(run_task, row))
                    running.add(t)
                    t.add_done_callback(running.discard)
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
        _wake.clear()
        try:
            await asyncio.wait_for(asyncio.to_thread(_wake.wait, 2.0), timeout=3.0)
        except asyncio.TimeoutError:
            pass
    for t in running:
        t.cancel()


async def scheduler_loop(stop: asyncio.Event) -> None:
    """Checks sources when due, marks ghosted applications, sends a morning digest."""
    from . import sources, tracker, watch

    while not stop.is_set():
        try:
            for src in sources.due():
                enqueue("poll_source", {"source_id": src["id"]}, ref=f"source:{src['id']}")
            for folder in watch.due():
                enqueue("scan_folder", {"folder_id": folder["id"]}, ref=f"watch:{folder['id']}")
            state = settings.get("state")
            now_local = datetime.now()
            last_maint = db.parse_ts(state.get("last_maintenance"))
            if not last_maint or (db.parse_ts(db.now()) - last_maint).total_seconds() > 6 * 3600:
                enqueue("maintenance", {}, ref="maintenance")
                settings.update("state", {"last_maintenance": db.now()})
            today = now_local.date().isoformat()
            if now_local.hour >= 8 and state.get("last_digest") != today:
                settings.update("state", {"last_digest": today})
                await asyncio.to_thread(tracker.daily_digest)
        except Exception:  # noqa: BLE001
            log.exception("scheduler error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


def active(ref: str | None = None) -> list[dict]:
    if ref:
        return db.q("SELECT * FROM tasks WHERE ref = ? AND status IN ('queued', 'running') ORDER BY id", (ref,))
    return db.q("SELECT * FROM tasks WHERE status IN ('queued', 'running') ORDER BY id")


def last_error(ref: str) -> dict | None:
    row = db.q1("SELECT * FROM tasks WHERE ref = ? ORDER BY id DESC LIMIT 1", (ref,))
    return row if row and row["status"] == "error" else None


def retry(task_id: int) -> None:
    db.ex("UPDATE tasks SET status = 'queued', error = NULL, started_at = NULL, finished_at = NULL WHERE id = ? "
          "AND status IN ('error', 'cancelled')", (task_id,))
    _wake.set()


def cancel(task_id: int) -> None:
    db.ex("UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'", (db.now(), task_id))
