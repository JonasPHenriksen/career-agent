"""Template filters and small view helpers."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timezone

import markdown as mdlib
from markupsafe import Markup, escape

from .. import db


def local_dt(ts: str | None, fmt: str = "%-d %b %Y, %H:%M") -> str:
    dt = db.parse_ts(ts)
    return dt.astimezone().strftime(fmt) if dt else ""


def local_date(ts: str | None) -> str:
    return local_dt(ts, "%-d %b %Y")


def ago(ts: str | None) -> str:
    dt = db.parse_ts(ts)
    if not dt:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def days_since(ts: str | None) -> int | None:
    dt = db.parse_ts(ts)
    return (datetime.now(timezone.utc) - dt).days if dt else None


def duration(start: str | None, end: str | None) -> str:
    a, b = db.parse_ts(start), db.parse_ts(end)
    if not a or not b:
        return ""
    secs = int((b - a).total_seconds())
    return f"{secs} s" if secs < 120 else f"{secs // 60} min"


def nice_date(d: str | None) -> str:
    if not d:
        return ""
    try:
        return date.fromisoformat(d[:10]).strftime("%-d %b %Y")
    except ValueError:
        return d


def days_until(d: str | None) -> int | None:
    if not d:
        return None
    try:
        return (date.fromisoformat(d[:10]) - date.today()).days
    except ValueError:
        return None


def deadline_text(d: str | None) -> str:
    n = days_until(d)
    if n is None:
        return ""
    if n < 0:
        return f"closed {nice_date(d)}"
    if n == 0:
        return "deadline today"
    if n == 1:
        return "deadline tomorrow"
    return f"{n} days left" if n <= 14 else f"deadline {nice_date(d)}"


def score_class(score) -> str:
    if score is None:
        return "score-none"
    s = int(score)
    return "score-high" if s >= 80 else "score-good" if s >= 65 else "score-mid" if s >= 50 else "score-low"


def md(text: str | None) -> Markup:
    """Render Markdown from the model safely: raw HTML is escaped first."""
    if not text:
        return Markup("")
    safe = html.escape(text, quote=False)
    out = mdlib.markdown(safe, extensions=["sane_lists", "nl2br"])
    out = re.sub(r"\[#(\d+)\]", r'<a class="fact-ref" href="/profile?tab=facts#fact-\1">#\1</a>', out)
    return Markup(out)


def paras(text: str | None) -> Markup:
    if not text:
        return Markup("")
    chunks = [str(escape(p.strip())).replace("\n", "<br>") for p in re.split(r"\n\s*\n", text) if p.strip()]
    return Markup("".join(f"<p>{c}</p>" for c in chunks))


def highlight_claims(text: str | None, claims: list[dict] | None, where: str = "letter") -> Markup:
    """Letter text as paragraphs, with fact-checked claims marked up."""
    esc = str(escape(text or ""))
    spans: list[tuple[int, int, dict]] = []
    lower = esc.lower()
    for c in sorted(claims or [], key=lambda c: -len(c.get("text") or "")):
        if c.get("where") != where:
            continue
        needle = str(escape((c.get("text") or "").strip().strip('"“”')))
        if len(needle) < 8:
            continue
        i = esc.find(needle)
        if i < 0:
            i = lower.find(needle.lower())
        if i < 0:
            continue
        j = i + len(needle)
        if any(not (j <= s or i >= e) for s, e, _ in spans):
            continue
        spans.append((i, j, c))
    out, pos = [], 0
    for i, j, c in sorted(spans, key=lambda x: x[0]):
        out.append(esc[pos:i])
        tip = c.get("note") or {"supported": "Backed by your career database",
                                 "partly": "Partly backed: check the wording",
                                 "unsupported": "Not backed by your career database"}.get(c.get("status"), "")
        out.append(f'<mark class="claim claim-{c.get("status")}" title="{escape(tip)}">{esc[i:j]}</mark>')
        pos = j
    out.append(esc[pos:])
    body = "".join(out)
    parts = [p.strip().replace("\n", "<br>") for p in re.split(r"\n\s*\n", body) if p.strip()]
    return Markup("".join(f"<p>{p}</p>" for p in parts))


def install(env) -> None:
    env.filters.update({
        "local_dt": local_dt, "local_date": local_date, "ago": ago, "nice_date": nice_date,
        "days_until": days_until, "deadline_text": deadline_text, "score_class": score_class, "md": md,
        "paras": paras, "days_since": days_since, "duration": duration, "jload": db.jload,
    })
    env.globals.update({"highlight_claims": highlight_claims})
