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
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from . import alerts, config, db, fetch, settings, sources, tasks

KIND = "collect"
MAX_HTML = 8_000_000  # characters: a results page is 0.1-2 MB
MAX_ITEMS = 300
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
    to robots and is the address the RSS feeds use, so a job seen both ways is one job."""
    home = "https://www.it-jobbank.dk" if urlparse(page_url).netloc.endswith("it-jobbank.dk") else "https://www.jobindex.dk"
    items = []
    for w in soup.select('div[id^="jobad-wrapper-"]'):
        tid = w["id"].removeprefix("jobad-wrapper-")
        inner = w.select_one(".PaidJob-inner, .jix_robotjob-inner") or w
        head = inner.select_one("h4 a")
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
        items.append({"url": url, "title": title, "company": _txt(w.select_one(".jix-toolbar-top__company")),
                      "location": _txt(w.select_one(".jix_robotjob--area")), "text": teaser[:TEASER],
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


def receive(html: str, page_url: str) -> dict:
    """Take a page from the bookmark. Answers at once with what was found; screening runs in the queue. A page
    from a site the app has no layout for is put aside and you're asked first (`ask`, then `confirm`)."""
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
    where = f"{name}{_page_no(page_url)}"
    if not items:
        return {"ok": False, "site": name, "found": 0, "new": 0,
                "message": f"No jobs found on this {name} page. Is it a search results page with jobs showing? "
                           "If it is, the site has changed its layout and the collector needs adjusting."}
    fresh = [it for it in items if not db.q1("SELECT 1 FROM jobs WHERE url = ?", (fetch.canonical_url(it["url"]),))]
    n, k = len(items), len(fresh)
    if not fresh:
        return {"ok": True, "site": name, "found": n, "new": 0,
                "message": f"{where}: {n} job{'s' if n != 1 else ''} on the page, all already in Career Agent."}
    sid = source_for(page_url)
    tasks.enqueue("collect_jobs", {"source_id": sid, "page_url": page_url, "items": fresh}, ref=f"source:{sid}")
    return {"ok": True, "site": name, "found": n, "new": k,
            "message": f"{where}: {n} job{'s' if n != 1 else ''} on the page, {k} new. The new ones are being "
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
        good = alerts.readable_link(it["url"])
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


def bookmark(app_url: str) -> str:
    """The bookmark's address: a script that posts the page you're on to the app, in a new tab, as an ordinary
    form. A form needs nothing from the site (Jobindex cuts the tie between its pages and the tabs they open, so
    one tab can't hand a page to another). It carries no site-specific code, so a site changing its layout never
    means making the bookmark again."""
    app_url = app_url.rstrip("/")
    js = ("(function(){var A='%s',d=document;"
          "if(location.origin===A){alert('Drag this button to your Favourites bar. Then click it there while a "
          "job search results page is open.');return;}"
          "var h=d.documentElement.outerHTML,f=d.createElement('form');"
          "f.method='post';f.action=A+'/collect/take';f.target='_blank';f.acceptCharset='UTF-8';"
          "f.style.display='none';"
          "function a(n,v){var i=d.createElement('input');i.type='hidden';i.name=n;i.value=v;f.appendChild(i);}"
          "a('token','%s');a('url',location.href);a('html',h);"
          "(d.body||d.documentElement).appendChild(f);f.submit();f.remove();"
          "})();") % (app_url, token())
    return "javascript:" + js
