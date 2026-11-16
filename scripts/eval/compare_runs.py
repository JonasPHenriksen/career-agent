#!/usr/bin/env python
"""Compare two runs of the whole pipeline, step by step: what each model kept at screening, read in the ads,
scored, and wrote.

    uv run python scripts/eval/compare_runs.py --before data/runs/2026-11-14-local/career.db --name cloud

`--before` is a frozen copy of the database after the first run; the second run is the app's own database (or
`--after`). Results go to data/runs/<name>-vs-<before folder>/:
    comparison.md   screening, reading, scoring, drafting, time and tokens
    drafts.md       the letters for the same jobs side by side as A and B (which is which is at the bottom)
Only jobs both runs worked on from the same ad text are compared directly; the rest are listed separately.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def rows(path: Path, sql: str, args: tuple = ()) -> list[dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def jl(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    if len(xs) < 3:
        return float("nan")
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else float("nan")


def bands(scores: list[int]) -> str:
    c = Counter("80+" if s >= 80 else "70–79" if s >= 70 else "55–69" if s >= 55 else "35–54" if s >= 35 else "0–34" for s in scores)
    return " · ".join(f"{b}: {c.get(b, 0)}" for b in ("80+", "70–79", "55–69", "35–54", "0–34"))


def models(path: Path, since: str, until: str = "9999") -> list[dict]:
    return rows(path, "SELECT purpose, model, COUNT(*) n, SUM(ok) ok, ROUND(AVG(input_tokens)) tokens_in, ROUND(AVG(output_tokens)) "
                      "tokens_out, ROUND(AVG(duration_ms) / 1000.0) seconds, ROUND(SUM(duration_ms) / 60000.0) minutes, "
                      "SUM(input_tokens) total_in, SUM(output_tokens) total_out FROM llm_calls WHERE created_at >= ? AND created_at < ? "
                      "GROUP BY purpose, model ORDER BY minutes DESC", (since, until))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--before", required=True, help="Database of the first run (a frozen copy)")
    p.add_argument("--after", default=str(ROOT / "data" / "career.db"), help="Database of the second run (default: the app's)")
    p.add_argument("--name", default="second", help="What to call the second run, e.g. cloud")
    p.add_argument("--before-name", default="local")
    p.add_argument("--before-since", default="2026-11-14T08:48", help="When the first run's model calls start (UTC)")
    p.add_argument("--after-since", required=True, help="When the second run's model calls start (UTC), e.g. 2026-11-16T10:00")
    args = p.parse_args()
    before, after = Path(args.before), Path(args.after)
    A, B = args.before_name, args.name
    out = ROOT / "data" / "runs" / f"{B}-vs-{before.parent.name}"
    out.mkdir(parents=True, exist_ok=True)

    cols = ("id, url, title, company_name, status, parse_status, fit_score, seniority, employment_type, work_mode, deadline, "
            "source_id, notes, LENGTH(description) ad_chars, details, fit")
    jb = {r["id"]: r for r in rows(before, f"SELECT {cols} FROM jobs")}
    ja = {r["id"]: r for r in rows(after, f"SELECT {cols} FROM jobs")}
    both = sorted(set(jb) & set(ja))
    md = [f"# {A} against {B}, step by step", "",
          f"First run ({A}): {before}. Second run ({B}): {after}. {len(both)} jobs are in both; {len(set(ja) - set(jb))} are new in the second.", ""]

    # ---- Screening
    def kept(r):
        return r["status"] != "filtered"

    screened = [i for i in both if jb[i]["source_id"]]
    agree_keep = [i for i in screened if kept(jb[i]) and kept(ja[i])]
    agree_drop = [i for i in screened if not kept(jb[i]) and not kept(ja[i])]
    only_b = [i for i in screened if not kept(jb[i]) and kept(ja[i])]
    only_a = [i for i in screened if kept(jb[i]) and not kept(ja[i])]
    md += ["## Screening: which listings each kept", "",
           f"Of {len(screened)} listings both screened: both kept {len(agree_keep)}, both dropped {len(agree_drop)}, "
           f"only {B} kept {len(only_b)}, only {A} kept {len(only_a)}.", ""]
    if only_b:
        md += [f"Dropped by {A}, kept by {B} (with {B}'s score where it got that far):", "",
               f"| Job | Company | {B} score | Level |", "|---|---|---|---|"]
        for i in sorted(only_b, key=lambda i: -(ja[i]["fit_score"] or -1)):
            r = ja[i]
            md.append(f"| {r['title']} | {r['company_name'] or ''} | {r['fit_score'] if r['fit_score'] is not None else '–'} | {r['seniority'] or ''} |")
        md.append("")
    if only_a:
        md += [f"Kept by {A}, dropped by {B} (with {A}'s score, and {B}'s reason):", "", f"| Job | Company | {A} score | Why {B} dropped it |", "|---|---|---|---|"]
        for i in sorted(only_a, key=lambda i: -(jb[i]["fit_score"] or -1)):
            md.append(f"| {jb[i]['title']} | {jb[i]['company_name'] or ''} | {jb[i]['fit_score'] if jb[i]['fit_score'] is not None else '–'} "
                      f"| {(ja[i]['notes'] or '')[:90]} |")
        md.append("")

    # ---- Reading
    read = [i for i in both if jb[i]["parse_status"] == "parsed" and ja[i]["parse_status"] == "parsed"]
    longer = [i for i in read if (ja[i]["ad_chars"] or 0) > 1.5 * (jb[i]["ad_chars"] or 1)]
    md += ["## Reading the ads", "",
           f"{len(read)} ads were read in both runs. In {len(longer)} of them the second run had much more ad text "
           "(the full ad instead of a job board's teaser), so their scores aren't comparable and are left out of the direct comparison below.", ""]
    same_text = [i for i in read if i not in longer]
    for field, label in (("seniority", "Level"), ("employment_type", "Kind of employment"), ("work_mode", "Where the work happens"), ("deadline", "Deadline")):
        diff = [i for i in same_text if (jb[i][field] or "") != (ja[i][field] or "")]
        md.append(f"* {label}: differs in {len(diff)} of {len(same_text)}"
                  + (". E.g. " + "; ".join(f"{ja[i]['title'][:40]}: {jb[i][field] or '–'} → {ja[i][field] or '–'}" for i in diff[:4]) if diff else "") + ".")
    n_req = [(len(jl(jb[i]["details"], {}).get("requirements") or []), len(jl(ja[i]["details"], {}).get("requirements") or [])) for i in same_text]
    if n_req:
        md.append(f"* Must-haves found per ad: {sum(a for a, _ in n_req) / len(n_req):.1f} ({A}) against {sum(b for _, b in n_req) / len(n_req):.1f} ({B}).")
    md.append("")

    # ---- Scoring
    scored = [i for i in same_text if jb[i]["fit_score"] is not None and ja[i]["fit_score"] is not None]
    sa, sb = [jb[i]["fit_score"] for i in scored], [ja[i]["fit_score"] for i in scored]
    md += ["## Scoring", "", f"{len(scored)} jobs were scored by both from the same ad text.", "",
           f"* {A}: {bands(sa)}", f"* {B}: {bands(sb)}"]
    if scored:
        top_a, top_b = Counter(sa).most_common(1)[0], Counter(sb).most_common(1)[0]
        md += [f"* Most common exact score: {A} gave {top_a[0]} to {top_a[1]} jobs; {B} gave {top_b[0]} to {top_b[1]}.",
               f"* Agreement on the order (1 = same order, 0 = unrelated): {spearman(sa, sb):.2f}. Average difference: "
               f"{sum(abs(a - b) for a, b in zip(sa, sb)) / len(scored):.0f} points.", ""]
        for who, mine, other, jm, jo in ((A, sa, sb, jb, ja), (B, sb, sa, ja, jb)):
            ranked = sorted(scored, key=lambda i: -jm[i]["fit_score"])[:15]
            md += [f"Top 15 by {who} (with the other's score):", "", f"| Job | Company | Level | {who} | other |", "|---|---|---|---|---|"]
            md += [f"| {jm[i]['title'][:60]} | {(jm[i]['company_name'] or '')[:24]} | {jm[i]['seniority'] or ''} | {jm[i]['fit_score']} | {jo[i]['fit_score']} |"
                   for i in ranked] + [""]
        apart = sorted(scored, key=lambda i: -abs(jb[i]["fit_score"] - ja[i]["fit_score"]))[:12]
        md += ["Where they disagree most:", ""]
        for i in apart:
            md += [f"* **{ja[i]['title'][:70]}** ({ja[i]['company_name'] or '?'}): {A} {jb[i]['fit_score']}, {B} {ja[i]['fit_score']}",
                   f"  * {A}: {(jl(jb[i]['fit'], {}).get('summary') or '')[:260]}",
                   f"  * {B}: {(jl(ja[i]['fit'], {}).get('summary') or '')[:260]}"]
        md.append("")
    new_scored = sorted((i for i in ja if ja[i]["fit_score"] is not None and (i not in jb or jb[i]["fit_score"] is None or i in longer)),
                        key=lambda i: -ja[i]["fit_score"])
    if new_scored:
        md += [f"Scored only by {B}, or by {B} on a fuller ad (top 20 of {len(new_scored)}):", "", f"| Job | Company | Level | {B} | {A} on the teaser |", "|---|---|---|---|---|"]
        md += [f"| {ja[i]['title'][:60]} | {(ja[i]['company_name'] or '')[:24]} | {ja[i]['seniority'] or ''} | {ja[i]['fit_score']} | "
               f"{jb[i]['fit_score'] if i in jb and jb[i]['fit_score'] is not None else '–'} |" for i in new_scored[:20]] + [""]

    # ---- Drafts
    acols = "a.id, a.job_id, a.letter, a.cv, a.claims, a.voice, a.review, a.language, j.title, j.company_name"
    ab = {r["job_id"]: r for r in rows(before, f"SELECT {acols} FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.letter IS NOT NULL")}
    aa = {r["job_id"]: r for r in rows(after, f"SELECT {acols} FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.letter IS NOT NULL")}
    shared = [j for j in sorted(set(ab) & set(aa)) if (ab[j]["letter"] or "").strip() != (aa[j]["letter"] or "").strip()]
    md += ["## Drafts", "", f"{len(aa)} applications in the second run, {len(ab)} in the first; {len(shared)} for the same job with a new letter.", "",
           f"| Job | Words {A} / {B} | Flagged by each run's own fact check: {A} | {B} | Questions about you: {A} / {B} |", "|---|---|---|---|---|"]

    def flagged(a):
        c = jl(a["claims"], {}).get("counts", {})
        return f"{c.get('partly', 0) + c.get('unsupported', 0)} of {sum(c.values())}" if c else "not checked"

    for j in shared:
        qa, qb = len(jl(ab[j]["review"], {}).get("assumed") or []), len(jl(aa[j]["review"], {}).get("assumed") or [])
        md.append(f"| {aa[j]['title'][:50]} ({aa[j]['company_name'] or '?'}) | {len(ab[j]['letter'].split())} / {len(aa[j]['letter'].split())} "
                  f"| {flagged(ab[j])} | {flagged(aa[j])} | {qa} / {qb} |")
    md += ["", "Each run's fact check was done by that run's own checker, so the two columns are not judged by the same eye. "
               "The letters themselves are in drafts.md.", ""]
    blind = ["# The letters for the same jobs, side by side", "",
             "A and B are in a different order for every job; which run wrote which is at the bottom.", ""]
    key = []
    for j in shared:
        pair = [(A, ab[j]["letter"]), (B, aa[j]["letter"])]
        random.Random(f"{j}").shuffle(pair)
        blind += [f"## {aa[j]['title']} ({aa[j]['company_name'] or '?'})", ""]
        for label, (who, text) in zip("AB", pair):
            blind += [f"### Letter {label}", "", "```text", text.strip(), "```", ""]
            key.append(f"{aa[j]['title'][:40]}: {label} = {who}")
    blind += ["", "<details><summary>Which is which</summary>", "", *[f"* {k}" for k in key], "", "</details>", ""]

    # ---- Time and tokens
    md += ["## Time and tokens", ""]
    for who, path, since in ((A, before, args.before_since), (B, after, args.after_since)):
        m = models(path, since) if who == B else models(path, since, args.after_since)
        total_min = sum(r["minutes"] or 0 for r in m)
        md += [f"{who}: {sum(r['n'] for r in m)} model calls, {total_min / 60:.1f} hours of model time, "
               f"{sum(r['total_in'] or 0 for r in m) / 1e6:.1f}M tokens in, {sum(r['total_out'] or 0 for r in m) / 1e6:.2f}M out.", "",
               "| Step | Model | Calls | Failed | Tokens in / out per call | Seconds per call | Minutes in all |", "|---|---|---|---|---|---|---|"]
        md += [f"| {r['purpose']} | {r['model']} | {r['n']} | {r['n'] - (r['ok'] or 0)} | {int(r['tokens_in'] or 0):,} / {int(r['tokens_out'] or 0):,} "
               f"| {int(r['seconds'] or 0)} | {int(r['minutes'] or 0)} |" for r in m] + [""]

    (out / "comparison.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out / "drafts.md").write_text("\n".join(blind) + "\n", encoding="utf-8")
    print(f"Wrote {out / 'comparison.md'} and {out / 'drafts.md'}")


if __name__ == "__main__":
    main()
