"""Fetch web pages and turn them into text, JSON-LD and links."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx2
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "da,en;q=0.8",
}


class FetchError(Exception):
    """Fetching failed; message is for the user. `connection` is True when the site couldn't be reached at all
    (no answer, no network): worth trying again later, unlike a page that's gone or blocked."""

    def __init__(self, message: str, connection: bool = False):
        super().__init__(message)
        self.connection = connection


def get(url: str, timeout: float = 25) -> httpx2.Response:
    if urlparse(url).scheme not in ("http", "https"):
        raise FetchError("Only http(s) links can be fetched.")
    try:
        r = httpx2.get(url, headers=HEADERS, follow_redirects=True, timeout=timeout)
    except httpx2.TimeoutException as e:
        raise FetchError("The site took too long to answer.", connection=True) from e
    except httpx2.TransportError as e:
        raise FetchError(f"Couldn't load the page ({e.__class__.__name__}).", connection=True) from e
    except httpx2.HTTPError as e:
        raise FetchError(f"Couldn't load the page ({e.__class__.__name__}).") from e
    if r.status_code in (401, 403, 429, 999):
        raise FetchError(f"The site blocked automatic access (HTTP {r.status_code}). "
                         "Open the ad in your browser and paste its text instead.")
    if r.status_code in (404, 410):
        raise FetchError("Page not found. The ad may have been taken down.")
    if r.status_code >= 400:
        raise FetchError(f"The site answered HTTP {r.status_code}.")
    return r


# Is this Mac online at all? Asked only after a fetch fails, to tell "the site is down" from "no internet".
# These are the connectivity checks macOS and Android use themselves; nothing about you is sent.
_PROBES = ("http://captive.apple.com/hotspot-detect.html", "https://www.gstatic.com/generate_204")
_online: dict = {"at": 0.0, "ok": True}


def online(force: bool = False) -> bool:
    if not force and time.monotonic() - _online["at"] < 20:
        return _online["ok"]
    ok = False
    for url in _PROBES:
        try:
            if httpx2.get(url, timeout=4, follow_redirects=False).status_code < 500:
                ok = True
                break
        except httpx2.HTTPError:
            continue
    _online.update(at=time.monotonic(), ok=ok)
    return ok


_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|mc_.*|trk.*|trackingid|refid|ref_src|_hs.*|cmp|campaign)$", re.I)


def canonical_url(url: str) -> str:
    p = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, p.params, query, ""))


def _walk_jsonld(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk_jsonld(x)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _walk_jsonld(node[key])


def jobposting_jsonld(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except ValueError:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except ValueError:
                continue
        for node in _walk_jsonld(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "JobPosting" in types:
                return node
    return None


def compact_jobposting(jp: dict) -> dict:
    """Keep the useful JSON-LD fields, with the HTML description flattened."""
    org = jp.get("hiringOrganization") or {}
    loc = jp.get("jobLocation") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") or {} if isinstance(loc, dict) else {}
    if isinstance(addr, str):
        addr = {"streetAddress": addr}
    desc = jp.get("description") or ""
    if "<" in desc:
        desc = BeautifulSoup(desc, "html.parser").get_text("\n")
    return {
        "title": jp.get("title"),
        "company": org.get("name") if isinstance(org, dict) else org,
        "company_url": org.get("sameAs") or org.get("url") if isinstance(org, dict) else None,
        "datePosted": jp.get("datePosted"),
        "validThrough": jp.get("validThrough"),
        "employmentType": jp.get("employmentType"),
        "location": ", ".join(str(x) for x in (addr.get("addressLocality"), addr.get("addressRegion"),
                                                addr.get("addressCountry")) if x and isinstance(x, str)),
        "remote": jp.get("jobLocationType"),
        "salary": jp.get("baseSalary"),
        "description": re.sub(r"\n{3,}", "\n\n", desc).strip()[:20000],
    }


# Forms themselves stay: ASP.NET and some job systems wrap the whole page, ad included, in one <form>.
# Their controls go (a <select> of 98 municipalities isn't ad text).
_BOILERPLATE = ["script", "style", "noscript", "svg", "iframe", "nav", "footer", "header", "aside", "button",
                "input", "select", "textarea"]


def html_to_text(html: str) -> tuple[str, str]:
    """Return (page title, readable text)."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    for tag in soup(_BOILERPLATE):
        tag.decompose()
    for el in soup.select('[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i]'):
        el.decompose()
    root = None
    for sel in ("main", "article", '[role="main"]'):
        cand = soup.select_one(sel)
        if cand and len(cand.get_text(" ", strip=True)) > 400:
            root = cand
            break
    root = root or soup.body or soup
    text = root.get_text("\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def page_links(html: str, base_url: str, limit: int = 300) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "footer"]):
        tag.decompose()
    seen, out = set(), []
    base_host = urlparse(base_url).netloc
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = canonical_url(urljoin(base_url, href))
        text = " ".join(a.get_text(" ", strip=True).split())[:160]
        parent = a.find_parent(["li", "article", "div", "tr"])
        context = " ".join(parent.get_text(" ", strip=True).split())[:200] if parent else ""
        if not text:  # a card whose link covers it without words of its own: the card's text names the link
            text = context[:160]
        if not text or len(text) < 4 or url in seen or url.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(url)
        out.append({"text": text, "url": url, "context": context if context != text else "",
                    "same_site": urlparse(url).netloc == base_host})
        if len(out) >= limit:
            break
    return out


def looks_like_feed(resp: httpx2.Response) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    head = resp.content[:400].lstrip().lower()
    return any(t in ctype for t in ("rss", "atom", "xml")) or head.startswith((b"<?xml", b"<rss", b"<feed"))


# --- Finding the full ad -------------------------------------------------------------------------------------
# Job boards (Jobindex, IT-jobbank) show a teaser page and keep the real ad one step further on, on the employer's
# site or on their own /jobannonce/ page. The feeds link to the teaser, so reading it gives 1-2k characters.
_WRAPPER_HOSTS = ("jobindex.dk", "it-jobbank.dk")
_SHORT_PAGE = 2500  # below this many characters of text, a page may just be a pointer to the real ad
_MAX_HOPS = 2
_POINTS_TO_AD = re.compile(
    r"^(se jobbet|se (hele )?(job)?annonce(n)?|se hele jobbet|læs (hele )?(jobbet|annoncen)|"
    r"view (the )?(full )?(job|ad|advert|vacancy)|see (the )?(full )?(job|ad)|read (the )?full (job|ad)|"
    r"go to (the )?(job|ad)|gå til (job|annonce)\w*)\s*[»›>→]?$", re.I)


def _is_wrapper(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.lower().endswith(_WRAPPER_HOSTS) and "/vis-job/" in p.path


def _next_hop(html: str, url: str, text_len: int) -> str | None:
    """The one link that leads on to the full ad, or None when the page is (or may be) the ad itself."""
    soup = BeautifulSoup(html, "html.parser")
    if _is_wrapper(url):
        # "Se jobbet" button first (employer's page), else the headline link (the board's own full-ad page).
        for sel in ("a.seejobdesktop[href]", "a.seejobmobil[href]", ".job-title a[href]"):
            a = soup.select_one(sel)
            if a and a["href"].startswith(("http", "/")):
                return urljoin(url, a["href"])
        return None
    if text_len >= _SHORT_PAGE:
        return None
    host = urlparse(url).netloc.lower()
    targets = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"].strip())
        if (urlparse(href).scheme in ("http", "https") and urlparse(href).netloc.lower() != host
                and _POINTS_TO_AD.match(" ".join(a.get_text(" ", strip=True).split()))):
            targets.add(href)
    # Two different "view job" links means a list of ads, not a pointer to one.
    return targets.pop() if len(targets) == 1 else None


def _is_html(resp: httpx2.Response) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    return not ctype or "html" in ctype


def ad_page(url: str) -> httpx2.Response:
    """Fetch the page that holds the full ad: follows a job board's teaser page on to the real ad (at most two
    hops). If following fails or gives less text, the page we started from is returned. The caller keeps its own
    URL; only the text comes from here."""
    best = cur = get(url)  # a failure on the first page is a real failure and is raised
    best_len = len(html_to_text(best.text)[1]) if _is_html(best) else 0
    seen = {url, str(best.url)}
    for _ in range(_MAX_HOPS):
        if not _is_html(cur):
            break
        cur_len = len(html_to_text(cur.text)[1])
        nxt = _next_hop(cur.text, str(cur.url), cur_len)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        try:
            cur = get(nxt)
        except FetchError:
            break
        seen.add(str(cur.url))
        if not _is_html(cur):
            break
        n = len(html_to_text(cur.text)[1])
        if n > best_len:
            best, best_len = cur, n
    return best
