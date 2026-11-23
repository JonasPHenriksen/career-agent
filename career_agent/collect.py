"""Jobs collected from a results page you have open in your own browser (the "Collect jobs" bookmark).

A job board's filtered search (category, distance, page 2 …) is closed to robots, so the app can't watch it. You
can open it, though. The bookmark posts the page you're looking at to the app: the app fetches nothing from the
site, it reads what was on your screen. Jobindex and Jobnet are read by their layout and taken at once; for any
other page you're asked first, and it then goes through the same link-picking as a watched results page. New
listings are screened, stored and read like a source's.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from . import config, db, fetch, robots, settings, sources, tasks, todos

KIND = "collect"
MAX_HTML = 8_000_000  # characters: a results page is 0.1-2 MB
MAX_POST = 4 * MAX_HTML  # bytes the page may take as it's posted: Danish letters grow when a form encodes them
MAX_ITEMS = 1000  # a whole search: the bookmark reads up to 50 pages of 20
TEASER = 600
KEEP_PAGE_SECONDS = 24 * 3600
LOCKED_NOTE = ("Collected from {site}. The site behind the link doesn't let the app open it on its own, so only the "
               "short text from the results page was read. Open it and paste the ad text to have it read and scored.")
_JOBINDEX_ID = re.compile(r"[hr]\d{5,9}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _txt(node) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).replace("​", "").split())


def _date(node) -> str:
    value = (node.get("datetime") or "") if node is not None else ""
    return value[:10] if _DATE.match(value) else ""


def _jobindex(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Jobindex and IT-Jobbank: one <div id="jobad-wrapper-<id>"> per ad. The ad's own page /vis-job/<id> is open
    to robots and is the address the RSS feeds use, so a job seen both ways is one job. IT-Jobbank's cards have
    their own class names (job-title, job-company, job-location, job-body) and show no posted date."""
    home = "https://www.it-jobbank.dk" if urlparse(page_url).netloc.endswith("it-jobbank.dk") else "https://www.jobindex.dk"
    items = []
    for w in soup.select('div[id^="jobad-wrapper-"]'):
        tid = w["id"].removeprefix("jobad-wrapper-")
        inner = w.select_one(".PaidJob-inner, .jix_robotjob-inner, .job-body") or w
        head = w.select_one(".PaidJob-inner h4 a, .jix_robotjob-inner h4 a, h3.job-title a") or inner.select_one("h4 a")
        share = w.select_one("[data-share-title]")
        title = _txt(head) or (share["data-share-title"].strip() if share else "")
        if not title:
            continue
        if _JOBINDEX_ID.fullmatch(tid):
            url = f"{home}/vis-job/{tid}"
        elif head and head.get("href"):
            url = urljoin(page_url, head["href"])
        else:
            continue
        teaser = " ".join(t for t in (_txt(p) for p in inner.find_all(["p", "li"])) if t)
        items.append({"url": url, "title": title, "company": _txt(w.select_one(".jix-toolbar-top__company, .job-company")),
                      "location": _txt(w.select_one(".jix_robotjob--area, .job-location")), "text": teaser[:TEASER],
                      "posted_at": _date(w.select_one(".jix-toolbar__pubdate time"))})
    return items


def _class_starts(prefix: str):
    return lambda value: bool(value) and value.startswith(prefix)


def _jobnet(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Jobnet's find-job: cards whose class names start with jobadcard_ (the endings change with each release).
    Most ads live on the employer's site or on LinkedIn; the card links straight there."""
    items = []
    for card in soup.find_all(class_=_class_starts("jobadcard_job-ad-card__")):
        link = card.find("a", id=re.compile(r"^job-.+-headline$"), href=True) or card.find("a", href=True)
        title = _txt(card.find(["h3", "h4"]))
        if not link or not title:
            continue
        posted = deadline = ""
        for t in card.find_all("time"):
            if "frist" in _txt(t).lower():
                deadline = _date(t)
            else:
                posted = _date(t)
        items.append({"url": urljoin(page_url, link["href"]), "title": title,
                      "company": _txt(card.find(class_=_class_starts("jobadcard_sub-header__"))),
                      "location": _txt(card.find("address")),
                      "text": _txt(card.find(class_=_class_starts("jobadcard_description-paragraph__")))[:TEASER],
                      "posted_at": posted, "deadline": deadline})
    return items


# Sites read by their layout: host ending -> (name, reader). Their pages are taken as soon as they arrive; any
# other page asks you first.
SITES = {"jobindex.dk": ("Jobindex", _jobindex), "it-jobbank.dk": ("IT-Jobbank", _jobindex),
         "jobnet.dk": ("Jobnet", _jobnet)}


def site_for(page_url: str) -> tuple[str, object] | None:
    host = (urlparse(page_url).hostname or "").lower()
    for ending, site in SITES.items():
        if host == ending or host.endswith("." + ending):
            return site
    return None


def stash_cards(html: str) -> str | None:
    """Jobindex and IT-Jobbank build the job list in the browser from the page's own data (var Stash = {...}).
    The cards for the jobs on the page, as one HTML document the layout readers take, or None when the page has no
    such data. The same data the bookmark reads from your tab."""
    a = html.find("var Stash = ")
    b = html.find("</script>", a) if a >= 0 else -1
    if a < 0 or b < 0:
        return None
    data = html[a + len("var Stash = "):b]
    try:
        stash = json.loads(data[:data.rfind("}") + 1])
    except ValueError:
        return None
    todo = [stash]
    while todo:
        o = todo.pop()
        if isinstance(o, dict):
            found = o.get("searchResponse")
            if isinstance(found, dict) and isinstance(found.get("results"), list):
                cards = [r.get("html") or "" for r in found["results"] if isinstance(r, dict)]
                return "<html><body>" + "\n".join(cards) + "</body></html>"
            todo.extend(o.values())
        elif isinstance(o, list):
            todo.extend(o)
    return None


def extract(html: str, page_url: str) -> list[dict] | None:
    """The job cards on a known site's results page; None for a site the app has no layout for."""
    site = site_for(page_url)
    if not site:
        return None
    seen, out = set(), []
    for it in site[1](BeautifulSoup(html, "html.parser"), page_url):
        key = fetch.canonical_url(it["url"])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out[:MAX_ITEMS]


def _unwrapped(url: str) -> list[str]:
    """Addresses a tracking link carries inside it: a full address in its query, or (on Jobindex) the ad's id,
    whose own page /vis-job/<id> is open to robots."""
    p = urlparse(url)
    out = []
    for _, value in parse_qsl(p.query, keep_blank_values=True):
        v = unquote(value)
        if v.startswith(("http://", "https://")):
            out.append(v)
        elif p.netloc.lower().endswith("jobindex.dk") and re.fullmatch(r"[hr]\d{5,9}", v):
            out.append(f"https://www.jobindex.dk/vis-job/{v}")
    return out


def readable_link(url: str) -> str | None:
    """The address to read this ad from without breaking the site's rules, or None if there isn't one."""
    if robots.allowed(url):
        return url
    for inner in _unwrapped(url):
        if robots.allowed(inner):
            return inner
    return None


def _site_name(page_url: str) -> str:
    site = site_for(page_url)
    return site[0] if site else (urlparse(page_url).hostname or "").removeprefix("www.")


def source_for(page_url: str) -> int:
    """One row under Job sources per site you collect from, so its jobs say where they came from."""
    p = urlparse(page_url)
    root = f"{p.scheme}://{p.netloc.lower()}/"
    row = db.q1("SELECT id FROM sources WHERE kind = ? AND url = ?", (KIND, root))
    if row:
        return row["id"]
    return db.insert("sources", {"name": f"Collected from {_site_name(page_url)}", "kind": KIND, "url": root,
                                 "created_at": db.now()})


def _pages_dir() -> Path:
    d = config.DATA_DIR / "cache" / "collect"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _page_no(page_url: str) -> str:
    page = parse_qs(urlparse(page_url).query).get("page", [""])[0]
    return f", page {page}" if page.isdigit() and int(page) > 1 else ""


def token() -> str:
    """The key the bookmark carries. Only a page you clicked the bookmark on can hand the app a page, so a website
    can't feed it listings behind your back. Made once; Settings → Job sources can make a new one."""
    value = settings.get("state").get("collect_token") or ""
    if not value:
        value = new_token()
    return value


def new_token() -> str:
    value = secrets.token_urlsafe(18)
    settings.update("state", {"collect_token": value})
    return value


def token_ok(given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), token().encode())


def _keep_page(html: str, page_url: str) -> str:
    """Put a page aside until you've said yes to reading it (or the queue has read it). Returns its id."""
    folder = _pages_dir()
    for old in folder.glob("*.json"):  # pages nobody said yes to
        if time.time() - old.stat().st_mtime > KEEP_PAGE_SECONDS:
            old.unlink(missing_ok=True)
    page_id = hashlib.sha256((page_url + html).encode("utf-8", "replace")).hexdigest()[:24]
    (folder / f"{page_id}.json").write_text(json.dumps({"url": page_url, "html": html}), encoding="utf-8")
    return page_id


def _kept_page(page_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{24}", page_id or ""):
        return None
    path = _pages_dir() / f"{page_id}.json"
    return path if path.exists() else None


def receive(html: str, page_url: str, pages: int = 1) -> dict:
    """Take a page from the bookmark: one results page, or every page of a search joined (`pages` of them). Answers
    at once with what was found; screening runs in the queue. A page from a site the app has no layout for is put
    aside and you're asked first (`ask`, then `confirm`)."""
    page_url = (page_url or "").strip()
    if not page_url.startswith(("http://", "https://")) or not urlparse(page_url).hostname:
        raise ValueError("That isn't a web page the app can read jobs from.")
    if not html or len(html) > MAX_HTML:
        raise ValueError("The page is empty or too large to read.")
    name = _site_name(page_url)
    items = extract(html, page_url)
    if items is None:
        return {"ok": True, "site": name, "found": None, "new": None, "ask": _keep_page(html, page_url),
                "message": f"This page is from {name}. The app has no layout for that site, but it can pick out "
                           "the links that look like job ads."}
    where, on = (f"{name}, {pages} pages", "in the search") if pages > 1 else (f"{name}{_page_no(page_url)}", "on the page")
    if not items:
        return {"ok": False, "site": name, "found": 0, "new": 0,
                "message": f"No jobs found on this {name} page. Is it a search results page with jobs showing? "
                           "If it is, the site has changed its layout and the collector needs adjusting."}
    fresh = [it for it in items if not db.q1("SELECT 1 FROM jobs WHERE url = ?", (fetch.canonical_url(it["url"]),))]
    n, k = len(items), len(fresh)
    saved = mark_collected(page_url)
    chain = _chain(page_url) if saved else {}
    if not fresh:
        return {"ok": True, "site": name, "found": n, "new": 0, "saved": saved, "page_url": page_url, **chain,
                "message": f"{where}: {n} job{'s' if n != 1 else ''} {on}, all already in Career Agent."}
    sid = source_for(page_url)
    step = sources.MAX_NEW_PER_RUN  # a whole search can be more than one queue task screens
    for i in range(0, k, step):
        tasks.enqueue("collect_jobs", {"source_id": sid, "page_url": page_url, "items": fresh[i:i + step]}, ref=f"source:{sid}")
    return {"ok": True, "site": name, "found": n, "new": k, "saved": saved, "page_url": page_url, **chain,
            "message": f"{where}: {n} job{'s' if n != 1 else ''} {on}, {k} new. The new ones are being "
                       "screened now; the ones that fit show up under Jobs."}


def confirm(page_id: str) -> dict:
    """You said yes to a page from a site without a layout: the queue asks the model which links are job ads."""
    path = _kept_page(page_id)
    if not path:
        raise ValueError("That page is no longer kept. Click the bookmark on it again.")
    page_url = json.loads(path.read_text(encoding="utf-8"))["url"]
    sid = source_for(page_url)
    tasks.enqueue("collect_jobs", {"source_id": sid, "page_url": page_url, "page_id": page_id}, ref=f"source:{sid}")
    return {"ok": True, "site": _site_name(page_url), "found": None, "new": None,
            "message": f"Looking for job ads on the {_site_name(page_url)} page. What fits shows up under Jobs."}


def store(payload: dict) -> dict:
    """The queued half: pick the job links (unknown sites), then screen and store what's new."""
    sid, page_url = payload["source_id"], payload["page_url"]
    items = payload.get("items")
    if items is None:
        path = _kept_page(payload.get("page_id", ""))
        if not path:
            return {"skipped": "page no longer kept"}
        html = json.loads(path.read_text(encoding="utf-8"))["html"]
        items = sources.pick_jobs(fetch.page_links(html, page_url), f"Page: {page_url}")
        for it in items:
            it["text"] = (it["text"] or "")[:TEASER]
        path.unlink(missing_ok=True)
    note = LOCKED_NOTE.format(site=_site_name(page_url))
    for it in items:
        good = readable_link(it["url"])
        if good:
            it["url"] = good
        else:
            it["readable"] = False
            it["note"] = note
    fresh = sources._unseen(items, set())[:sources.MAX_NEW_PER_RUN]
    kept = 0
    for i in range(0, len(fresh), sources.TRIAGE_BATCH):
        kept += sources._screen_and_store(fresh[i:i + sources.TRIAGE_BATCH], sid)
    src = db.q1("SELECT jobs_found FROM sources WHERE id = ?", (sid,))
    if src:
        db.update("sources", sid, {"last_run_at": db.now(), "last_error": None,
                                   "last_status": f"{len(items)} listings, {len(fresh)} new, {kept} kept for review",
                                   "jobs_found": (src["jobs_found"] or 0) + kept})
    return {"listings": len(items), "new": len(fresh), "kept": kept}


# --- Your saved searches and the reminder to collect them ---
# The app may not check a filtered search on its own, so new jobs there only arrive when you collect it. The searches
# you run are saved here with when you last collected them, and a to-do on Today reminds you when one hasn't been
# collected for REMIND_DAYS days. "Collect bookmark jobs" on the Jobs page starts a round whenever you like.

SEARCHES = "collect_searches"
REMIND_DAYS = 7
REMIND_TITLE = "Collect new jobs from your saved searches"
REMIND_DETAIL = ("The job sites don't let the app check these searches on its own. Open each one and click the "
                 "*Collect jobs* bookmark there. Your searches are under [Settings → Job sources](/settings#collect).")


def search_key(url: str) -> str:
    """The same search whatever page you're on and however the site orders the address."""
    p = urlparse((url or "").strip())
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "page")
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}?{urlencode(query)}"


def searches() -> list[dict]:
    return [dict(s) for s in (settings.get(SEARCHES) or []) if isinstance(s, dict) and s.get("url")]


def _save(rows: list[dict]) -> None:
    settings.set(SEARCHES, rows)
    remind()


def add_search(name: str, url: str) -> dict:
    url = " ".join((url or "").split())
    if not url.startswith(("http://", "https://")) or not urlparse(url).hostname:
        raise ValueError("Paste the address of the search results page, starting with https://.")
    rows = searches()
    if any(search_key(s["url"]) == search_key(url) for s in rows):
        raise ValueError("That search is already saved.")
    row = {"name": (" ".join((name or "").split()) or f"{_site_name(url)} search")[:120], "url": url[:2000],
           "last_at": None}
    _save(rows + [row])
    return row


def remove_search(url: str) -> None:
    _save([s for s in searches() if search_key(s["url"]) != search_key(url)])


def mark_collected(page_url: str) -> bool:
    """Note that a saved search was just collected. True when the page is one of your saved searches."""
    rows, hit = searches(), False
    for s in rows:
        if search_key(s["url"]) == search_key(page_url):
            s["last_at"], hit = db.now(), True
    if hit:
        _save(rows)
    return hit


def _collected_on(row: dict) -> date | None:
    ts = db.parse_ts(row.get("last_at"))
    return ts.date() if ts else None


def due_searches(today: date | None = None) -> list[dict]:
    """Saved searches not collected in the last REMIND_DAYS days, oldest first (never collected first of all)."""
    today = today or date.today()
    out = [s for s in searches() if not _collected_on(s) or _collected_on(s) <= today - timedelta(days=REMIND_DAYS)]
    return sorted(out, key=lambda s: s.get("last_at") or "")


def remind(today: date | None = None) -> None:
    """Keep one to-do on Today for collecting your saved searches, dated the day the first of them is REMIND_DAYS
    days old: it waits under Coming up until then and is shown (with one notification) on the day. Collecting them
    all moves it REMIND_DAYS days on; ticking it off without collecting waits as long before reminding again."""
    today = today or date.today()
    rows = searches()
    open_ = db.q1("SELECT * FROM todos WHERE check_kind = 'collect' AND done_at IS NULL ORDER BY id")
    if not rows:
        if open_:
            todos.delete(open_["id"])
        return
    due = min((_collected_on(s) + timedelta(days=REMIND_DAYS)) if _collected_on(s) else today for s in rows)
    if not open_:
        last_done = db.parse_ts(db.scalar("SELECT MAX(done_at) FROM todos WHERE check_kind = 'collect'"))
        if last_done:
            due = max(due, last_done.date() + timedelta(days=REMIND_DAYS))
        todos.add(REMIND_TITLE, REMIND_DETAIL, show_from=max(due, today).isoformat(), check_kind="collect")
        return
    if due > today:
        show_from = due.isoformat()
    else:  # due now: keep the day it first showed, or show it today if it was still waiting
        show_from = min(open_["show_from"] or today.isoformat(), today.isoformat())
    if show_from != open_["show_from"]:
        db.update("todos", open_["id"], {"show_from": show_from, "notified_at": None})


def next_search(today: date | None = None) -> dict | None:
    """The next saved search that is due, in the order of your list."""
    due = {search_key(s["url"]) for s in due_searches(today)}
    return next((s for s in searches() if search_key(s["url"]) in due), None)


RUN_HOURS = 3  # a round you start with Collect now is forgotten after this, and the reminder's order applies again
_run: dict = {}  # that round, in memory only: the searches still to collect and when it started


def start_run() -> dict | None:
    """You pressed Collect now: all your saved searches, in the order of your list, whether or not they were
    collected lately. The first one to open, or None when there are none."""
    rows = searches()
    _run.clear()
    if not rows:
        return None
    _run.update(keys=[search_key(s["url"]) for s in rows[1:]], at=time.monotonic())
    return rows[0]


def _chain(page_url: str = "") -> dict:
    """After collecting a saved search: the next one to collect, so a run is one click after another, and how many
    are left. In a round you started with Collect now: the rest of that round; otherwise the ones that are due."""
    if _run and time.monotonic() - _run["at"] < RUN_HOURS * 3600:
        _run["keys"] = [k for k in _run["keys"] if k != search_key(page_url)]
        by_key = {search_key(s["url"]): s for s in searches()}
        left = [by_key[k] for k in _run["keys"] if k in by_key]
        if not left:
            _run.clear()
        return {"next": left[0] if left else None, "left": len(left), "round": True}
    _run.clear()
    return {"next": next_search(), "left": len(due_searches())}


def last_collected() -> str | None:
    """When the saved search collected longest ago was collected (None if one never was, or there are none)."""
    rows = searches()
    if not rows or any(not s.get("last_at") for s in rows):
        return None
    return min(s["last_at"] for s in rows)


def search_links(today: date | None = None) -> list[dict]:
    """The saved searches for the reminder on Today: name, address and when you last collected each."""
    due = {search_key(s["url"]) for s in due_searches(today)}
    return [{**s, "due": search_key(s["url"]) in due} for s in searches()]


_BOOKMARK_JS = Path(__file__).with_name("collect_bookmark.js")


def bookmark(app_url: str) -> str:
    """The bookmark's address: collect_bookmark.js on one line, with the app's address and key filled in. It reads
    every page of a Jobindex or IT-Jobbank search (or presses a "load more" button until all jobs show) from your
    own tab, then posts them to the app in a new tab, as an ordinary form. A form needs nothing from the site
    (Jobindex cuts the tie between its pages and the tabs they open, so one tab can't hand a page to another)."""
    js = re.sub(r"/\*.*?\*/", "", _BOOKMARK_JS.read_text(encoding="utf-8"), count=1, flags=re.S)
    js = "".join(line.strip() for line in js.splitlines())
    return "javascript:" + js.replace("__APP__", app_url.rstrip("/")).replace("__KEY__", token())
