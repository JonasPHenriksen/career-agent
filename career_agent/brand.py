"""An employer's own colour, read from its website, so the CV for that job can wear it.

The colour is looked for the way a person would spot it: the colour the site names for itself (theme-color, the
pinned-tab icon), the colours in its icon, then the colour its own stylesheets use most. Greys, near-white and
near-black don't count, and neither do the stock palettes WordPress and Bootstrap put on every site. The colour
found is darkened if needed, so the white name and contact lines on the CV's header stay readable.

The website is the company's own when the app knows it; otherwise it is taken from the ad: the contact email's
domain, or a link in the ad to a site named like the company. Nothing personal goes anywhere: only the company's
public pages are read, and only when robots.txt allows it.
"""
from __future__ import annotations

import colorsys
import io
import re
from collections import Counter
from urllib.parse import urljoin, urlparse

from . import companies, db, fetch, robots

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
MIN_CONTRAST = 3.5  # white text on the colour; the CV's header has the name and contact lines in white
RECHECK_DAYS = 60

# Colours every site of a kind carries whatever its brand: WordPress's block palette and admin blue, Bootstrap's
# defaults. Counted, they would win on half the web.
STOCK = {
    "#cf2e2e", "#ff6900", "#fcb900", "#7bdcb5", "#00d084", "#8ed1fc", "#0693e3", "#abb8c3", "#eb144c", "#f78da7",
    "#9b51e0", "#007cba", "#006ba1", "#005a87", "#7a00df", "#00a0d2", "#0073aa", "#2271b1", "#d63638", "#dba617",
    "#0d6efd", "#007bff", "#6610f2", "#6f42c1", "#d63384", "#dc3545", "#fd7e14", "#ffc107", "#198754", "#28a745",
    "#20c997", "#0dcaf0", "#17a2b8", "#6c757d", "#343a40", "#e83e8c",
    "#337ab7", "#286090", "#23527c", "#2e6da4", "#5cb85c", "#5bc0de", "#f0ad4e", "#d9534f", "#449d44", "#31b0d5",
}
FREE_MAIL = {"gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com", "me.com", "mail.dk",
             "jubii.dk", "hotmail.dk", "live.dk", "protonmail.com", "proton.me"}
# Job boards and other sites that carry many employers: never the employer's own site.
BOARDS = ("jobbank", "jobindex", "jobnet", "linkedin", "facebook", "instagram", "twitter", "x.com", "youtube",
          "google", "qwant", "bing", "datatilsynet", "thehub", "glassdoor", "indeed", "stepstone", "ofir",
          "jobteaser", "graduateland", "akademikernes", "it-jobbank", "careerjet", "jooble", "tiktok", "apple.com",
          "microsoft.com", "cookiebot", "cookieinformation", "w3.org", "wordpress", "wp.com", "gravatar")
# Hiring systems: a company's career pages there wear its colours, but a link to one doesn't name the company.
NOT_COMPANY = BOARDS + ("emply", "hr-manager", "hrmanager", "teamtailor", "workday", "successfactors", "varbi",
                        "recruitee", "talent-soft")


# ---------------------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------------------

def _hex(text: str) -> str | None:
    t = (text or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", t):
        t = "#" + "".join(c * 2 for c in t[1:])
    if re.fullmatch(r"#[0-9a-f]{6}", t):
        return t
    m = re.fullmatch(r"rgba?\(\s*(\d{1,3})[ ,]+(\d{1,3})[ ,]+(\d{1,3})\s*(?:[,/]\s*([\d.]+%?)\s*)?\)", t)
    if m and (m.group(4) is None or m.group(4) not in ("0", "0%", "0.0")):
        return "#%02x%02x%02x" % tuple(min(255, int(m.group(i))) for i in (1, 2, 3))
    return None


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def is_brandlike(color: str) -> bool:
    """A colour a brand could be: not a grey, not near-white or near-black."""
    r, g, b = (c / 255 for c in _rgb(color))
    h, light, sat = colorsys.rgb_to_hls(r, g, b)
    chroma = max(r, g, b) - min(r, g, b)
    return chroma >= 0.22 and 0.12 <= light <= 0.88 and sat >= 0.3


def _luminance(color: str) -> float:
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(color)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_with_white(color: str) -> float:
    return 1.05 / (_luminance(color) + 0.05)


def readable(color: str) -> str:
    """The same colour, darkened just enough for white text on it to read."""
    r, g, b = (c / 255 for c in _rgb(color))
    h, light, s = colorsys.rgb_to_hls(r, g, b)
    out = color
    while contrast_with_white(out) < MIN_CONTRAST and light > 0.05:
        light -= 0.01
        out = "#%02x%02x%02x" % tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, light, s))
    return out


def _colours_in(text: str) -> list[str]:
    found = re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\([^)]{5,40}\)", text or "")
    return [c for c in (_hex(x) for x in found) if c]


def _icon_colours(raw: bytes, svg: bool) -> list[str]:
    """The brand-like colours in the site's icon, most common first."""
    if svg:
        return [c for c in _colours_in(raw.decode("utf-8", "ignore")) if is_brandlike(c)]
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        img.thumbnail((64, 64))
        counts = Counter("#%02x%02x%02x" % (r // 8 * 8, g // 8 * 8, b // 8 * 8) for r, g, b, a in img.getdata() if a > 200)
    except Exception:  # noqa: BLE001 - an icon we can't read just doesn't count
        return []
    return [c for c, _ in counts.most_common() if is_brandlike(c)]


def from_page(html: str, base_url: str, get=None) -> tuple[str | None, str]:
    """The brand colour of the page at `base_url`, and a few words on where it came from. `get(url) -> (bytes, type)`
    fetches the page's icon and stylesheets; None reads only the page itself."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    for meta in soup.find_all("meta"):  # what the site says its colour is
        if (meta.get("name") or "").lower() in ("theme-color", "msapplication-tilecolor"):
            c = _hex(meta.get("content") or "")
            if c and is_brandlike(c) and c not in STOCK:
                return c, "the site's theme colour"
    for link in soup.find_all("link"):
        c = _hex(link.get("color") or "")
        if c and is_brandlike(c) and c not in STOCK:
            return c, "the site's pinned-tab icon"

    score: Counter = Counter()
    inline = " ".join(t.get_text() for t in soup.find_all("style")) + " " + \
             " ".join(t.get("style", "") for t in soup.find_all(style=True))
    for c in _colours_in(inline):
        score[c] += 1
    host = urlparse(base_url).netloc.lower().removeprefix("www.")
    if get:
        sheets = [urljoin(base_url, l.get("href") or "") for l in soup.find_all("link")
                  if "stylesheet" in [x.lower() for x in (l.get("rel") or [])] and l.get("href")]
        own = [u for u in dict.fromkeys(sheets) if urlparse(u).netloc.lower().removeprefix("www.").endswith(host)]
        for url in own[:4]:
            body = get(url)
            if body:
                for c in _colours_in(body[0][:2_000_000].decode("utf-8", "ignore")):
                    score[c] += 1
        icons = [l for l in soup.find_all("link") if "icon" in " ".join(l.get("rel") or []).lower() and l.get("href")]
        icons.sort(key=lambda l: (not (l.get("href") or "").lower().endswith(".svg"), "apple" not in " ".join(l.get("rel"))))
        for link in icons[:2]:
            body = get(urljoin(base_url, link["href"]))
            if body:
                svg = "svg" in body[1] or link["href"].lower().split("?")[0].endswith(".svg")
                for n, c in enumerate(_icon_colours(body[0], svg)[:3]):
                    score[c] += 15 - 5 * n  # the icon is the logo: its colour weighs as much as a stylesheet's
    picks = [(c, n) for c, n in score.most_common() if is_brandlike(c) and c not in STOCK]
    if not picks:
        return None, ""
    return picks[0][0], "the colour the site uses most"


# ---------------------------------------------------------------------------------------
# Which website, and the colour per company
# ---------------------------------------------------------------------------------------

def _name_tokens(name: str) -> list[str]:
    return [t for t in companies.norm_name(name).split() if len(t) >= 4 and t not in ("international", "group", "danmark",
                                                                                       "denmark", "nordic", "holding")]


def _is_company_host(host: str, name: str) -> bool:
    host = host.lower().removeprefix("www.")
    if not host or any(x in host for x in NOT_COMPANY):
        return False
    label = host.split(".")[-2] if host.count(".") >= 1 else host
    flat = re.sub(r"[^a-z0-9]", "", label)
    return any(t in flat or flat in t for t in _name_tokens(name) if len(flat) >= 3)


def website_for(job: dict, ad_html: str | None = None) -> str | None:
    """The employer's own website: known, from the contact email, or from a link in the ad to a site named like
    the company."""
    if job.get("company_id"):
        row = db.q1("SELECT website FROM companies WHERE id = ?", (job["company_id"],))
        site = (row or {}).get("website") or ""
        if site.startswith("http") and not any(x in urlparse(site).netloc.lower() for x in BOARDS):
            return site
    name = job.get("company_name") or ""
    email = (job.get("contact_email") or "").lower()
    if "@" in email:
        domain = email.split("@", 1)[1].strip()
        if domain and domain not in FREE_MAIL and not any(x in domain for x in NOT_COMPANY):
            return f"https://{domain}"
    for url in (job.get("apply_url"), job.get("url")):
        if url and _is_company_host(urlparse(url).netloc, name):
            return f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    if ad_html is None and job.get("url"):
        page = _get(job["url"])
        ad_html = page[0].decode("utf-8", "ignore") if page else None
    if ad_html:
        for link in fetch.page_links(ad_html, job.get("url") or "", 500):
            p = urlparse(link["url"])
            if p.scheme in ("http", "https") and _is_company_host(p.netloc, name):
                return f"{p.scheme}://{p.netloc}"
    return None


def _get(url: str) -> tuple[bytes, str] | None:
    try:
        if not robots.allowed(url):
            return None
        r = fetch.get(url, timeout=15)
    except fetch.FetchError:
        return None
    return r.content[:3_000_000], (r.headers.get("content-type") or "").lower()


def look_up(site: str) -> tuple[str | None, str]:
    """The colour on this website, made readable, and where it was found."""
    page = _get(site)
    if not page:
        return None, ""
    colour, where = from_page(page[0].decode("utf-8", "ignore"), site, _get)
    return (readable(colour), where) if colour else (None, "")


def company_colour(job: dict, refresh: bool = False) -> dict:
    """{colour, website, where} for this job's employer, read once and kept on the company. colour is None when
    none was found; a miss is tried again after a while."""
    cid = job.get("company_id")
    row = db.q1("SELECT * FROM companies WHERE id = ?", (cid,)) if cid else None
    if row and not refresh and row.get("brand_checked_at") and (row.get("brand_color") or
                                                                row["brand_checked_at"] > db.days_ago(RECHECK_DAYS)):
        return {"colour": row.get("brand_color"), "website": row.get("website"), "where": row.get("brand_note") or ""}
    site = website_for(job)
    colour, where = look_up(site) if site else (None, "")
    if row:
        patch = {"brand_color": colour, "brand_note": where or None, "brand_checked_at": db.now()}
        if site and not row.get("website"):
            patch["website"] = site
        db.update("companies", row["id"], patch)
    return {"colour": colour, "website": site, "where": where}
