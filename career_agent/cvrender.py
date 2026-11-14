"""Render a CV (see cvdata.py) to PDF and page images, on this Mac, with Typst.

The look follows a two-column résumé with a coloured sidebar. Colour, font, sizes and the order of the sections
are settings (Settings key "cv"); the words come from the CV data and your contact details from About you, which
never go to a model.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from . import config, settings

TEMPLATE = Path(__file__).parent / "cv_templates" / "sidebar.typ"
FALLBACK_FONTS = ["Charter", "Georgia", "Libertinus Serif"]
TITLES = {
    "en": {"summary": "Profile", "work": "Work Experience", "education": "Education", "projects": "Projects",
           "skills": "Skills", "languages": "Languages", "interests": "Interests", "certificates": "Certifications"},
    "da": {"summary": "Profil", "work": "Erhvervserfaring", "education": "Uddannelse", "projects": "Projekter",
           "skills": "Kompetencer", "languages": "Sprog", "interests": "Fritidsinteresser", "certificates": "Certificeringer"},
}
SIDEBAR_SECTIONS = ("skills", "certificates", "languages", "interests")
MAIN_SECTIONS = ("work", "education", "projects")


def design() -> dict:
    d = settings.get("cv")
    primary = d.get("primary") if re.fullmatch(r"#[0-9a-fA-F]{6}", d.get("primary") or "") else "#2254bf"
    r, g, b = (int(primary[i:i + 2], 16) for i in (1, 3, 5))
    tint = "#%02x%02x%02x" % tuple(round(c * 0.2 + 255 * 0.8) for c in (r, g, b))  # the colour, mostly white
    return {"primary": primary, "tint": tint, "sidebar": max(24, min(42, float(d.get("sidebar") or 34))),
            "size": max(8.0, min(11.5, float(d.get("size") or 9.6))),
            "fonts": [f for f in dict.fromkeys([(d.get("font") or "").strip(), *FALLBACK_FONTS]) if f],
            "sidebar_order": [s for s in (d.get("sidebar_order") or SIDEBAR_SECTIONS) if s in SIDEBAR_SECTIONS],
            "main_order": [s for s in (d.get("main_order") or MAIN_SECTIONS) if s in MAIN_SECTIONS]}


def _dates(e: dict) -> str:
    return " – ".join(x for x in ((e.get("start") or "").strip(), (e.get("end") or "").strip()) if x)


def _title(data: dict, key: str, lang: str) -> str:
    return ((data.get("titles") or {}).get(key) or "").strip() or TITLES.get(lang, TITLES["en"])[key]


def model(data: dict, language: str | None = None, profile: dict | None = None, look: dict | None = None) -> dict:
    """Everything the template prints, in the order it prints it."""
    p = profile if profile is not None else settings.get("profile")
    look = look or design()
    lang = (data.get("language") or language or "en")[:2]
    lines = []
    if p.get("email"):
        lines.append({"text": p["email"], "url": f"mailto:{p['email']}"})
    if p.get("phone"):
        lines.append({"text": p["phone"], "url": ""})
    if p.get("city"):
        lines.append({"text": p["city"], "url": ""})
    if p.get("linkedin"):
        lines.append({"text": "LinkedIn", "url": p["linkedin"]})
    if p.get("website"):
        lines.append({"text": re.sub(r"^https?://(www\.)?", "", p["website"]).rstrip("/"), "url": p["website"]})
    lines += [{"text": x, "url": ""} for x in (data.get("personal") or []) if x.strip()]

    def entries(key: str) -> list[dict]:
        out = []
        for e in data.get(key) or []:
            if key == "work":
                a, b, c, d = e.get("name"), e.get("location"), e.get("position"), _dates(e)
            elif key == "education":
                a, b, c = e.get("institution"), e.get("degree"), e.get("area")
                d = " • ".join(x for x in ((e.get("location") or "").strip(), _dates(e)) if x)
            else:
                a, b, c, d = e.get("name"), _dates(e), "", ""
            out.append({"a": a or "", "b": b or "", "c": c or "", "d": d or "", "text": (e.get("description") or "").strip(),
                        "bullets": [h.strip() for h in e.get("highlights") or [] if h and h.strip()]})
        return out

    def items(key: str) -> list[dict]:
        if key == "skills":
            return [{"name": s.get("name") or "", "text": ", ".join(s.get("keywords") or [])} for s in data.get("skills") or []]
        if key == "languages":
            return [{"name": x.get("language") or "", "text": x.get("fluency") or ""} for x in data.get("languages") or []]
        if key == "certificates":
            return [{"name": c.get("name") or "", "text": ", ".join(x for x in (c.get("issuer"), c.get("date")) if x)}
                    for c in data.get("certificates") or []]
        return [{"name": x, "text": ""} for x in data.get("interests") or [] if x.strip()]

    sidebar = [{"title": _title(data, k, lang), "items": items(k)} for k in look["sidebar_order"]]
    main = [{"title": _title(data, k, lang), "entries": entries(k)} for k in look["main_order"]]
    return {"lang": lang, "design": look,
            "header": {"name": p.get("name") or "", "label": (data.get("label") or "").strip(), "lines": lines},
            "profile": {"title": _title(data, "summary", lang), "text": (data.get("summary") or "").strip()},
            "sidebar": [s for s in sidebar if s["items"]], "main": [s for s in main if s["entries"]]}


def render(data: dict, language: str | None = None) -> Path:
    """The folder with cv.pdf and page-1.png, page-2.png… for this CV; made once and kept until anything changes."""
    import typst

    m = model(data, language)
    key = hashlib.sha1((json.dumps(m, sort_keys=True, ensure_ascii=False) + TEMPLATE.read_text(encoding="utf-8"))
                       .encode("utf-8")).hexdigest()[:20]
    folder = config.DATA_DIR / "cache" / "cv" / key
    if (folder / "cv.pdf").exists() and (folder / "page-1.png").exists():
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "data.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(TEMPLATE, folder / "main.typ")
    typst.compile(str(folder / "main.typ"), output=str(folder / "cv.pdf"), root=str(folder))
    typst.compile(str(folder / "main.typ"), output=str(folder / "page-{p}.png"), root=str(folder), format="png", ppi=130.0)
    return folder


def pdf(data: dict, language: str | None = None) -> bytes:
    return (render(data, language) / "cv.pdf").read_bytes()


def pages(data: dict, language: str | None = None) -> int:
    return len(list(render(data, language).glob("page-*.png")))


def png(data: dict, language: str | None = None, page: int = 1) -> bytes | None:
    path = render(data, language) / f"page-{page}.png"
    return path.read_bytes() if path.exists() else None
