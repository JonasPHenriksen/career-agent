"""A site's rules for automated access (robots.txt), checked before the app reads a job source on its own.

Matching follows the standard (RFC 9309, as Google reads it): the rules for "User-agent: *", `*` matches anything,
`$` ends the address, and the longest matching rule wins, with Allow winning a tie."""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qsl, urlparse

from . import fetch

CACHE_SECONDS = 24 * 3600
_cache: dict[str, tuple[float, list[tuple[bool, str]]]] = {}


def parse(text: str) -> list[tuple[bool, str]]:
    """The (allow, pattern) rules that apply to every robot. A run of User-agent lines opens one group."""
    rules: list[tuple[bool, str]] = []
    in_star = False
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            in_star = (value == "*") or (last_was_agent and in_star)
            last_was_agent = True
            continue
        last_was_agent = False
        if in_star and key in ("allow", "disallow") and value:
            rules.append((key == "allow", value))
    return rules


def _matches(pattern: str, path: str) -> bool:
    rx = re.escape(pattern).replace(r"\*", ".*")
    if rx.endswith(r"\$"):
        rx = rx[:-2] + "$"
    return re.match(rx, path) is not None


def blocking_rule(rules: list[tuple[bool, str]], url: str) -> str | None:
    """The Disallow pattern that rules out this address, or None if it's allowed."""
    p = urlparse(url)
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    best: tuple[bool, str] | None = None
    for allow, pattern in rules:
        if _matches(pattern, path) and (best is None or len(pattern) > len(best[1])
                                        or (len(pattern) == len(best[1]) and allow)):
            best = (allow, pattern)
    return best[1] if best and not best[0] else None


def rules_for(url: str) -> list[tuple[bool, str]]:
    """The site's rules, read at most once a day. No robots.txt (or an unreadable one) means no rules. Raises
    fetch.FetchError only when the site can't be reached, so the caller tries again later instead of guessing."""
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc.lower()}"
    hit = _cache.get(origin)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        resp = fetch.get(origin + "/robots.txt", timeout=15)
        text = resp.text if "html" not in resp.headers.get("content-type", "").lower() else ""
    except fetch.FetchError as e:
        if e.connection:
            raise
        text = ""
    rules = parse(text)
    _cache[origin] = (time.monotonic(), rules)
    return rules


def check(url: str) -> str | None:
    """None if the site lets robots read this address, otherwise a sentence saying why not and what to do."""
    rule = blocking_rule(rules_for(url), url)
    return explain(url, rule) if rule else None


def allowed(url: str) -> bool:
    try:
        return check(url) is None
    except fetch.FetchError:
        return False


def explain(url: str, rule: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    hint = ""
    if rule.strip("/") == "" or rule == "/":
        hint = (f" {host} doesn't allow automated access at all. Set up the site's own job alert by email "
                "instead and add it under Job alerts by email.")
    elif "page=" in rule:
        hint = " Take out the page number; to see older jobs, use the site's job alert by email."
    elif "+*+" in rule:
        hint = " Use at most two search words (one + between them), or split it into two searches."
    elif any(k in rule for k in ("radius=", "geoareaid=")):
        hint = (" Distance and municipality filters aren't allowed for robots; pick a region instead "
                "(on Jobindex: area=storkbh, for example), or use the site's job alert by email.")
    elif "&*&" in rule or rule.endswith("?*&") or ("&" in url and len(query) > 2):
        hint = " Use fewer filters in the address: a search word and a region, for example."
    return f"{host} doesn't let robots read this address (rule: Disallow: {rule}).{hint}"
