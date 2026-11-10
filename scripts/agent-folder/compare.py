#!/usr/bin/env python3
"""Letters written by the app's writing model with and without the agent folder, for a blind comparison.

Usage: uv run --project <repo> python -I scripts/agent-folder/compare.py OUTDIR AD1.txt URL1 [AD2.txt URL2 ...] [--data DIR]

For each ad: add the job, read it with the analysis model, then write the letter twice with the same model and
settings, once with the agent folder switched off and once on, and fact-check and voice-check both. Automatic
scoring is paused while it runs; the settings are restored and the test jobs deleted afterwards. Takes a while with
local models (minutes per letter). Results: OUTDIR/results.json, one file per letter, and a shuffled
OUTDIR/review-input.md (labels X/Y) with the key in OUTDIR/key.json, for a reviewer who doesn't know which is which.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

os.environ.setdefault("CAREER_AGENT_NO_NOTIFY", "1")
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from career_agent import config, db, jobs, llm, settings, voice, writer  # noqa: E402


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def wait_idle(max_minutes: int = 25) -> None:
    for _ in range(max_minutes * 6):
        if not db.scalar("SELECT COUNT(*) FROM tasks WHERE status IN ('queued', 'running')"):
            return
        time.sleep(10)
    log("the app is still busy after waiting; going ahead")


def run(out: Path, pairs: list[tuple[str, str]]) -> list[dict]:
    knowledge_before, automation_before = settings.get("knowledge"), settings.get("automation")
    made_jobs: list[int] = []
    results: list[dict] = []
    try:
        settings.update("automation", {"auto_score": False, "auto_draft": False})
        log("waiting for the app's background tasks to finish")
        wait_idle()
        for ad_path, url in pairs:
            text = Path(ad_path).read_text(encoding="utf-8")
            job_id = db.insert("jobs", {"url": url, "description": text, "status": "new", "parse_status": "pending",
                                        "created_at": db.now()})
            made_jobs.append(job_id)
            jobs.parse(job_id, use_text=True)
            job = jobs.get(job_id)
            log(f"job {job_id}: {job['title']} at {job['company_name']}")
            for variant, use in (("without", False), ("with", True)):
                settings.update("knowledge", {"use": use})
                language = writer.choose_language(job)
                system = writer.writer_system(language, job)
                t1 = time.time()
                r = llm.run("writing", "write_letter", system, writer._letter_task(job, language, None),
                            writer.LETTER_SCHEMA, max_tokens=16000)
                letter = writer.strip_citations(r.data["letter"])
                t2 = time.time()
                claims = writer.check_claims(letter, "")
                v = voice.check(letter, language)
                rec = {"job_id": job_id, "job": f"{job['title']} at {job['company_name']}", "ad": ad_path, "url": url,
                       "variant": variant, "language": language, "prompt_chars": len(system), "letter": letter,
                       "words": len(letter.split()), "main_gap": r.data.get("main_gap"), "counts": claims["counts"],
                       "flagged": [c for c in claims["claims"] if c["status"] in ("partly", "unsupported")],
                       "voice_score": v.get("score"), "seconds_write": round(t2 - t1),
                       "seconds_check": round(time.time() - t2)}
                results.append(rec)
                (out / f"job{job_id}-{variant}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                                                                 encoding="utf-8")
                log(f"  {variant}: {rec['words']} words in {rec['seconds_write']}s, check {rec['counts']}")
    finally:
        settings.set("knowledge", knowledge_before)
        settings.set("automation", automation_before)
        for job_id in made_jobs:
            company_id = db.scalar("SELECT company_id FROM jobs WHERE id = ?", (job_id,))
            db.ex("DELETE FROM tasks WHERE ref = ?", (f"job:{job_id}",))
            db.ex("DELETE FROM applications WHERE job_id = ?", (job_id,))
            db.ex("DELETE FROM jobs WHERE id = ?", (job_id,))
            if company_id and not db.scalar("SELECT COUNT(*) FROM jobs WHERE company_id = ?", (company_id,)):
                db.ex("DELETE FROM companies WHERE id = ?", (company_id,))
    return results


def blind_input(out: Path, results: list[dict]) -> None:
    rng = random.SystemRandom()
    key, parts = {}, ["# Blind review: two letters per job ad", ""]
    for n, job_id in enumerate(dict.fromkeys(r["job_id"] for r in results), 1):
        pair = [r for r in results if r["job_id"] == job_id]
        rng.shuffle(pair)
        key[f"job {n}"] = {label: r["variant"] for label, r in zip("XY", pair)}
        parts += [f"## Job {n}: {pair[0]['job']}", "", "### The job ad", "",
                  Path(pair[0]["ad"]).read_text(encoding="utf-8").strip(), ""]
        for label, r in zip("XY", pair):
            parts += [f"### Letter {label}", "", r["letter"].strip(), ""]
    (out / "review-input.md").write_text("\n".join(parts), encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("pairs", nargs="+", help="AD.txt URL [AD.txt URL ...]")
    ap.add_argument("--data", type=Path, default=REPO / "data", help="Career Agent's data folder")
    a = ap.parse_args()
    if len(a.pairs) % 2:
        raise SystemExit("Give each ad file followed by its URL.")
    config.configure(a.data.expanduser())
    db.init()
    a.out.mkdir(parents=True, exist_ok=True)
    results = run(a.out, list(zip(a.pairs[::2], a.pairs[1::2])))
    (a.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    if results:
        blind_input(a.out, results)
    log(f"done: {len(results)} letters; settings restored and test jobs removed")


if __name__ == "__main__":
    main()
