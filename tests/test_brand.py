"""The employer's own colour on its job's CV: found on its website, kept per job, changed on the CV tab."""
from __future__ import annotations

from fastapi.testclient import TestClient

from career_agent import brand, db, settings, tracker, writer
from career_agent.web.app import app

from .test_cv import _draft

SITE = """<html><head><link rel="stylesheet" href="/main.css"><link rel="stylesheet" href="https://cdn.example.net/x.css">
<link rel="icon" href="/favicon.svg"><style>a{color:#cf2e2e} .x{color:#cf2e2e} .y{color:#cf2e2e}</style></head>
<body style="background:#ffffff;color:#222222">FinPay</body></html>"""
PAGES = {
    "https://finpay.dk": (SITE.encode(), "text/html"),
    "https://finpay.dk/main.css": (b"h1{color:#e05a10} .b{background:#e05a10} .c{border-color:#e05a10} p{color:#333}", "text/css"),
    "https://cdn.example.net/x.css": (b".z{color:#00ff00}" * 50, "text/css"),
    "https://finpay.dk/favicon.svg": (b'<svg><path fill="#e05a10"/><path fill="#ffffff"/></svg>', "image/svg+xml"),
}


def _site(monkeypatch, pages=PAGES):
    asked = []
    monkeypatch.setattr(brand, "_get", lambda url: asked.append(url) or pages.get(url.rstrip("/") if url.count("/") == 3 else url))
    return asked


def test_the_colour_a_site_names_for_itself_comes_first():
    html = '<meta name="theme-color" content="#0a7c5a"><style>a{color:#e05a10}</style>'
    assert brand.from_page(html, "https://x.dk") == ("#0a7c5a", "the site's theme colour")
    assert brand.from_page('<meta name="theme-color" content="#ffffff"><style>a{color:#e05a10}</style>', "https://x.dk")[0] == "#e05a10", \
        "white isn't a brand colour"


def test_own_stylesheets_and_icon_count_but_stock_palettes_and_other_sites_dont():
    colour, where = brand.from_page(SITE, "https://finpay.dk", lambda url: PAGES.get(url))
    assert colour == "#e05a10" and where == "the colour the site uses most", "not WordPress red, not the CDN's green"
    assert brand.from_page("<p>nothing</p>", "https://finpay.dk") == (None, "")


def test_a_colour_too_light_for_white_text_is_darkened_but_keeps_its_hue():
    out = brand.readable("#fc5220")
    assert out != "#fc5220" and brand.contrast_with_white(out) >= brand.MIN_CONTRAST
    assert brand.readable("#123456") == "#123456"
    r, g, b = brand._rgb(out)
    assert r > g > b, "still orange"


def test_the_website_comes_from_the_company_the_email_or_a_link_in_the_ad(fake):
    assert brand.website_for({"company_name": "FinPay A/S", "contact_email": "job@finpay.dk"}) == "https://finpay.dk"
    assert brand.website_for({"company_name": "FinPay A/S", "contact_email": "lise@gmail.com"}, ad_html="") is None
    ad = ('<a href="https://www.linkedin.com/company/finpay">LinkedIn</a><a href="https://jobbank.dk/other">x</a>'
          '<a href="https://zensai.com/careers">Søg via vores websider</a>')
    job = {"company_name": "Zensai International ApS", "url": "https://jobbank.dk/job/1"}
    assert brand.website_for(job, ad_html=ad) == "https://zensai.com"
    assert brand.website_for({"company_name": "Init Group ApS", "url": "https://jobbank.dk/job/2"}, ad_html=ad) is None
    cid = db.insert("companies", {"name": "Board Co", "website": "https://jobbank.dk/job/9", "created_at": db.now()})
    assert brand.website_for({"company_id": cid, "company_name": "Board Co", "url": "https://jobbank.dk/job/9"}, ad_html="") is None, \
        "a job board saved as the website isn't the company's site"


def test_a_job_cv_wears_the_employers_colour_and_keeps_it(fake, monkeypatch):
    _site(monkeypatch)
    a = _draft()
    data = db.jload(a["cv_data"])
    assert (data["color"], data["color_by"]) == (brand.readable("#e05a10"), "company")
    assert any("colour" in n and "finpay.dk" in n for n in db.jload(a["writer_notes"]))
    company = db.q1("SELECT * FROM companies WHERE id = (SELECT company_id FROM jobs WHERE id = ?)", (a["job_id"],))
    assert company["brand_color"] == data["color"] and company["website"] == "https://finpay.dk"

    asked = _site(monkeypatch, {})
    writer.revise(a["id"], "Shorter bullets.", "cv")
    assert db.jload(tracker.app_with_job(a["id"])["cv_data"])["color"] == data["color"], "a revision keeps the colour"
    writer.reset_cv(a["id"])
    assert db.jload(tracker.app_with_job(a["id"])["cv_data"])["color"] == data["color"], "so does a reset"
    assert not asked, "the colour was read once and kept on the company"


def test_off_in_settings_means_your_usual_colour(fake, monkeypatch):
    _site(monkeypatch)
    settings.update("cv", {"company_colour": False})
    assert "color" not in db.jload(_draft()["cv_data"])


def test_the_cv_tab_changes_only_the_colour(fake, monkeypatch):
    a = _draft()  # the site couldn't be read: your usual colour
    assert "color" not in db.jload(a["cv_data"])
    c = TestClient(app)
    page = c.get(f"/applications/{a['id']}?tab=cv").text
    assert "/cv-colour" in page and "your usual colour" in page

    r = c.post(f"/applications/{a['id']}/cv-colour", data={"choice": "company"}, follow_redirects=False)
    assert "Couldn" in r.headers["location"], "nothing found says so"
    _site(monkeypatch)
    c.post(f"/applications/{a['id']}/cv-colour", data={"choice": "company"}, follow_redirects=False)
    after = tracker.app_with_job(a["id"])
    assert db.jload(after["cv_data"])["color_by"] == "company" and after["cv"] == a["cv"], "the words stay"

    c.post(f"/applications/{a['id']}/cv-colour", data={"choice": "pick", "colour": "#123456"}, follow_redirects=False)
    assert db.jload(tracker.app_with_job(a["id"])["cv_data"])["color"] == "#123456"
    assert c.get(f"/applications/{a['id']}/cv-page/1.png").status_code == 200
    writer.draft(a["id"], "cv")
    assert db.jload(tracker.app_with_job(a["id"])["cv_data"])["color"] == "#123456", "a colour you picked outlasts a new draft"
    c.post(f"/applications/{a['id']}/cv-colour", data={"choice": "own"}, follow_redirects=False)
    assert db.jload(tracker.app_with_job(a["id"])["cv_data"])["color"] == "own"
