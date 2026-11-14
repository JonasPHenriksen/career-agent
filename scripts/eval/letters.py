#!/usr/bin/env python
"""A small benchmark: the same cover letters written by a few models, to see which one is needed.

For each job and each model, the app's own letter prompt is run (each model gets what it would get in the app:
a local model the short career digest, Claude the whole database). Every letter is then fact-checked by one
fixed checker and compared with your own writing, and the letters are laid out blind for you to rank.

    uv run python scripts/eval/letters.py --jobs 194,199,169
    uv run python scripts/eval/letters.py --jobs 194 --configs opus-medium,gemma31-local

Results go to data/eval/<run>/ (your data folder, never the repository):
    blind.md      the letters per job as A, B, C…: rank them before you look at anything else
    summary.md    which model wrote which, tokens, time, voice match and what the fact check flagged
    results.json  everything, for comparing runs later

Cloud models are used through Claude Code (your plan), never the pay-per-call API. Run it with the app stopped
or running; a run that is interrupted continues where it left off when started again with the same --run.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import random
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from career_agent import config, db, jobs, llm, settings, tasks, voice, writer  # noqa: E402

# name: (provider, model, effort). Effort on a local model only switches thinking on (high) or off.
CONFIGS = {
    "haiku-xhigh": ("claude_code", "haiku", "xhigh"),
    "sonnet-high": ("claude_code", "sonnet", "high"),
    "sonnet-xhigh": ("claude_code", "sonnet", "xhigh"),
    "opus-low": ("claude_code", "opus", "low"),
    "opus-medium": ("claude_code", "opus", "medium"),
    "opus-high": ("claude_code", "opus", "high"),
    "opus-xhigh": ("claude_code", "opus", "xhigh"),
    "gemma31-local": ("ollama", "gemma4:31b-mlx", "high"),
}
CHECKER = ("claude_code", "opus", "medium")  # one checker for every letter, so the counts are comparable

_real_get, _real_set = settings.get, settings.set


@contextlib.contextmanager
def writing_model(provider: str, model: str, effort: str):
    """Make this the Writing model for the calls inside, without touching the saved settings."""
    def patched_get(key):
        value = _real_get(key)
        if key == "llm":
            value["writing"] = {"provider": provider, "model": model, "effort": effort}
            value["local_only"] = False
        return value

    def guarded_set(key, value):
        if key == "llm":
            raise RuntimeError("the benchmark must not change the model settings")
        _real_set(key, value)

    settings.get, settings.set = patched_get, guarded_set
    try:
        yield
    finally:
        settings.get, settings.set = _real_get, _real_set


def usage(ref: str) -> dict:
    row = db.q1("SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS tokens_in, COALESCE(SUM(output_tokens), 0) "
                "AS tokens_out, COALESCE(SUM(duration_ms), 0) AS ms FROM llm_calls WHERE ref = ? AND ok = 1", (ref,))
    return {"calls": row["calls"], "tokens_in": row["tokens_in"], "tokens_out": row["tokens_out"],
            "seconds": round(row["ms"] / 1000)}


def write_letter(run: str, name: str, job: dict) -> dict:
    provider, model, effort = CONFIGS[name]
    ref = f"eval:{run}:{name}:{job['id']}"
    with writing_model(provider, model, effort):
        language = writer.choose_language(job)
        system = writer.writer_system(language, job)
        task = writer._letter_task(job, language, None)
        with llm.using(ref=ref):
            r = llm.run("writing", "eval_letter", system, task, writer.LETTER_SCHEMA, max_tokens=16000, effort=effort)
    d = r.data
    letter = writer.strip_citations(d["letter"])
    gap_note = writer._gap_note({**d, "letter": letter})
    return {"config": name, "job_id": job["id"], "language": language, "model": r.model, "effort": effort,
            "prompt_chars": len(system) + len(task), "tokens_in": r.input_tokens, "tokens_out": r.output_tokens,
            "seconds": round(r.duration_ms / 1000), "subject": d["subject"], "letter": letter,
            "words": len(letter.split()), "angle": d.get("angle", ""), "assumed": d.get("assumed", []), "main_gap": d["main_gap"], "gap_in_letter": bool(gap_note) and "doesn't mention" not in gap_note[0],
            "notes": d["notes"], "voice": voice.check(letter, language)["score"]}


def check_letter(run: str, cell: dict) -> dict:
    ref = f"eval:{run}:check:{cell['config']}:{cell['job_id']}"
    with writing_model(*CHECKER), llm.using(ref=ref):
        result = writer.check_claims(cell["letter"], "", tier="writing")
    flagged = [{"status": c["status"], "text": c["text"], "note": c["note"]} for c in result["claims"]
               if c["status"] in ("partly", "unsupported")]
    return {"counts": result["counts"], "flagged": flagged, "usage": usage(ref)}


def ensure_ollama() -> None:
    """The local model needs it, and every prompt uses it to pick the most relevant facts and example letters."""
    if llm.ollama_running():
        return
    tasks.start_ollama()
    for _ in range(30):
        if llm.ollama_running():
            return
        time.sleep(1)
    print("Ollama didn't start: local models will fail, and facts are picked by keywords only.", flush=True)


def labels(run: str, job_id: int, names: list[str]) -> dict[str, str]:
    """A, B, C… per job, in an order that's fixed for this run but different for every job."""
    order = sorted(names)
    random.Random(f"{run}:{job_id}").shuffle(order)
    return {name: "ABCDEFGH"[n] for n, name in enumerate(order)}


def write_reports(out: Path, run: str, job_rows: list[dict], names: list[str], cells: dict) -> None:
    blind = [f"# Blind review: {run}", "",
             "For each job, rank the letters from the one you'd send with the least editing to the one you'd throw away "
             "(e.g. `194: C > A > D > B`). Do this before opening summary.md: it says which model wrote which.", ""]
    summary = [f"# Benchmark {run}", "",
               f"Fact check by one fixed checker for all letters: {CHECKER[1]} at {CHECKER[2]} effort (through Claude Code). "
               "One letter per model and job: a direction, not a measurement.", ""]
    totals: dict[str, list[dict]] = {n: [] for n in names}
    for job in job_rows:
        lab = labels(run, job["id"], names)
        blind += [f"## Job {job['id']}: {job['title']} ({job['company_name'] or '?'})", "", job.get("url") or "", ""]
        summary += [f"## Job {job['id']}: {job['title']} ({job['company_name'] or '?'})", "",
                    "| Letter | Model | Words | Voice | Backed | Partly | Unbacked | Gap named | Tokens in / out | Seconds |",
                    "|---|---|---|---|---|---|---|---|---|---|"]
        details, angles = [], []
        for name in sorted(names, key=lambda n: lab[n]):
            cell = cells.get(f"{name}|{job['id']}")
            if not cell or cell.get("error"):
                blind += [f"### Letter {lab[name]}", "", "(no letter: this model failed)", ""]
                summary.append(f"| {lab[name]} | {name} | failed: {(cell or {}).get('error', 'not run')[:80]} | | | | | | | |")
                continue
            blind += [f"### Letter {lab[name]}", "", "```text", cell["letter"], "```", ""]
            chk = cell.get("check") or {}
            c = chk.get("counts", {})
            gap = ("yes: " + cell["main_gap"]) if cell["main_gap"] and cell["gap_in_letter"] else \
                  ("spotted, not in letter: " + cell["main_gap"]) if cell["main_gap"] else "none found"
            checked = (f"{c.get('supported', 0)} | {c.get('partly', 0)} | {c.get('unsupported', 0)}" if chk and not chk.get("error")
                       else "check failed | |")
            summary.append(f"| {lab[name]} | {name} | {cell['words']} | {cell['voice']} | {checked} | {gap} | "
                           f"{cell['tokens_in']:,} / {cell['tokens_out']:,} | {cell['seconds']} |")
            totals[name].append(cell)
            for f in chk.get("flagged", []):
                details.append(f"* **{lab[name]} ({name}), {f['status']}:** “{f['text']}” — {f['note']}")
            if cell.get("angle"):
                angles.append(f"* **{lab[name]} ({name}):** {cell['angle']}"
                              + ("".join(f"\n  * assumed: {a}" for a in cell.get("assumed") or [])))
        summary += [""] + (["The angle each writer chose, and what it assumed about you:", ""] + angles + [""] if angles else [])
        summary += (["What the checker flagged:", ""] + details + [""] if details else [])
    summary += ["## Per model, over all jobs", "",
                "| Model | Letters | Avg words | Avg voice | Backed | Partly | Unbacked | Avg tokens in / out | Avg seconds |",
                "|---|---|---|---|---|---|---|---|---|"]
    for name in names:
        got = totals[name]
        if not got:
            summary.append(f"| {name} | 0 | | | | | | | |")
            continue
        n = len(got)

        def total(status: str) -> int:
            return sum((c.get("check") or {}).get("counts", {}).get(status, 0) for c in got)

        summary.append(f"| {name} | {n} | {sum(c['words'] for c in got) // n} | {sum(c['voice'] for c in got) // n} | "
                       f"{total('supported')} | {total('partly')} | {total('unsupported')} | "
                       f"{sum(c['tokens_in'] for c in got) // n:,} / {sum(c['tokens_out'] for c in got) // n:,} | "
                       f"{sum(c['seconds'] for c in got) // n} |")
    (out / "blind.md").write_text("\n".join(blind) + "\n", encoding="utf-8")
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--jobs", required=True, help="Job ids from the app, e.g. 194,199,169")
    parser.add_argument("--configs", default="haiku-xhigh,sonnet-high,opus-medium,gemma31-local", help=f"Which of: {', '.join(CONFIGS)}")
    parser.add_argument("--run", default=date.today().isoformat(), help="Name of the run (default: today's date)")
    parser.add_argument("--baseline", default=None, metavar="RUN:CONFIG",
                        help="Also lay out the letters an earlier run made, e.g. 2026-11-13:sonnet-high, to compare blind")
    parser.add_argument("--data", default=None, help="Data folder (default ./data)")
    args = parser.parse_args()

    config.configure(args.data)
    db.init()
    names = [n.strip() for n in args.configs.split(",") if n.strip()]
    unknown = [n for n in names if n not in CONFIGS]
    if unknown:
        sys.exit(f"Unknown config(s): {', '.join(unknown)}")
    job_rows = []
    for jid in (int(x) for x in args.jobs.split(",")):
        job = jobs.get(jid)
        if not job or job["parse_status"] != "parsed":
            sys.exit(f"Job {jid} doesn't exist or hasn't been read yet.")
        job_rows.append(job)

    out = config.DATA_DIR / "eval" / args.run
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.json"
    cells: dict = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {}

    if args.baseline:  # letters from an earlier run, shown as one more anonymous entry per job
        old_run, _, old_config = args.baseline.partition(":")
        earlier = json.loads((config.DATA_DIR / "eval" / old_run / "results.json").read_text(encoding="utf-8"))
        base = f"earlier-{old_run}-{old_config}"
        for job in job_rows:
            cell = earlier.get(f"{old_config}|{job['id']}")
            if cell and not cell.get("error"):
                cells[f"{base}|{job['id']}"] = {**cell, "config": base}
        names.append(base)

    def save() -> None:
        results_path.write_text(json.dumps(cells, ensure_ascii=False, indent=1), encoding="utf-8")
        write_reports(out, args.run, job_rows, names, cells)

    ensure_ollama()
    stop = False
    for name in names:  # cloud models first (quick), the local model last (slow)
        if name not in CONFIGS:
            continue  # the baseline's letters already exist
        for job in job_rows:
            key = f"{name}|{job['id']}"
            if key in cells and not cells[key].get("error"):
                continue
            print(f"[{time.strftime('%H:%M')}] writing: {name}, job {job['id']} ({job['title'][:40]})", flush=True)
            try:
                cells[key] = write_letter(args.run, name, job)
                print(f"          {cells[key]['words']} words in {cells[key]['seconds']} s", flush=True)
            except llm.LLMError as e:
                cells[key] = {"config": name, "job_id": job["id"], "error": str(e)}
                print(f"          failed: {e}", flush=True)
                if isinstance(e, llm.LLMConfigError):  # not logged in, model missing: the rest would fail the same way
                    stop = name
            save()
            if stop:
                break
        if stop:
            print(f"Skipping the rest of {stop}.", flush=True)
            stop = False
    for key, cell in cells.items():
        if cell.get("error") or (cell.get("check") and not cell["check"].get("error")):
            continue
        if cell["config"] not in names or cell["job_id"] not in [j["id"] for j in job_rows]:
            continue
        print(f"[{time.strftime('%H:%M')}] checking: {cell['config']}, job {cell['job_id']}", flush=True)
        try:
            cell["check"] = check_letter(args.run, cell)
        except llm.LLMError as e:
            cell["check"] = {"error": str(e)}
            print(f"          check failed: {e}", flush=True)
        save()
    save()
    print(f"Done. Rank the letters in {out / 'blind.md'}, then see {out / 'summary.md'}.", flush=True)


if __name__ == "__main__":
    main()
