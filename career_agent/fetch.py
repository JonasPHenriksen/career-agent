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
        if not text or len(text) < 4 or url in seen or url.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(url)
        parent = a.find_parent(["li", "article", "div", "tr"])
        context = " ".join(parent.get_text(" ", strip=True).split())[:200] if parent else ""
        out.append({"text": text, "url": url, "context": context if context != text else "",
                    "same_site": urlparse(url).netloc == base_host})
        if len(out) >= limit:
            break
    return out


def looks_like_feed(resp: httpx2.Response) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    head = resp.content[:400].lstrip().lower()
    return any(t in ctype for t in ("rss", "atom", "xml")) or head.startswith((b"<?xml", b"<rss", b"<feed"))
