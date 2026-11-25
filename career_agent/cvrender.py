"""Render a CV (see cvdata.py) to PDF and page images, on this Mac, with Typst.

The look follows a two-column résumé with a coloured sidebar. Colour, font, sizes and the order of the sections
are settings (Settings key "cv"); the words come from the CV data and your contact details from About you, which
never go to a model.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
from pathlib import Path

from . import config, cvdata, settings

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


def design(colour: str | None = None) -> dict:
    """The look every CV shares (Profile → Your CV). `colour` is one CV's own colour, such as the employer's."""
    d = settings.get("cv")
    primary = d.get("primary") if re.fullmatch(r"#[0-9a-fA-F]{6}", d.get("primary") or "") else "#2254bf"
    if re.fullmatch(r"#[0-9a-fA-F]{6}", colour or ""):
        primary = colour
    r, g, b = (int(primary[i:i + 2], 16) for i in (1, 3, 5))
    tint = "#%02x%02x%02x" % tuple(round(c * 0.2 + 255 * 0.8) for c in (r, g, b))  # the colour, mostly white
    return {"primary": primary, "tint": tint, "sidebar": max(24, min(42, float(d.get("sidebar") or 34))),
            "size": max(8.0, min(11.5, float(d.get("size") or 9.6))),
            "fonts": [f for f in dict.fromkeys([(d.get("font") or "").strip(), *FALLBACK_FONTS]) if f],
            "sidebar_order": [s for s in (d.get("sidebar_order") or SIDEBAR_SECTIONS) if s in SIDEBAR_SECTIONS],
            "main_order": [s for s in (d.get("main_order") or MAIN_SECTIONS) if s in MAIN_SECTIONS]}


PHOTO_FILE = "cv-photo.jpg"
PHOTO_SIDE = 600  # pixels: sharp in print at the size it's shown, without making every PDF heavy


def photo_path() -> Path | None:
    """Your photo for the CV, if you've added one."""
    name = (settings.get("cv").get("photo") or "").strip()
    path = config.FILES_DIR / name if name else None
    return path if path and path.is_file() else None


def save_photo(raw: bytes) -> None:
    """Keep a picture as your CV photo: turned upright, cropped to a square around its centre and made smaller."""
    from PIL import Image, ImageOps

    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception as e:  # noqa: BLE001 - whatever Pillow can't read isn't a photo we can use
        raise ValueError("That file isn't a picture I can read. Use a JPEG or PNG.") from e
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    if side > PHOTO_SIDE:
        img = img.resize((PHOTO_SIDE, PHOTO_SIDE), Image.LANCZOS)
    config.ensure_dirs()
    img.save(config.FILES_DIR / PHOTO_FILE, "JPEG", quality=90)
    settings.update("cv", {"photo": PHOTO_FILE})


def remove_photo() -> None:
    settings.update("cv", {"photo": ""})  # the file stays, so undoing this in History brings the photo back




def _title(data: dict, key: str, lang: str) -> str:
    return ((data.get("titles") or {}).get(key) or "").strip() or TITLES.get(lang, TITLES["en"])[key]


def model(data: dict, language: str | None = None, profile: dict | None = None, look: dict | None = None) -> dict:
    """Everything the template prints, in the order it prints it."""
    p = profile if profile is not None else settings.get("profile")
    look = look or design(data.get("color"))
    lang = (data.get("language") or language or "en")[:2]
    lines = []
    if p.get("email"):
        lines.append({"text": p["email"], "url": f"mailto:{p['email']}"})
    if p.get("phone"):
        lines.append({"text": p["phone"], "url": ""})
    place = (data.get("location") or "").strip() or p.get("city")  # as your CV writes it ("Horsens, Denmark"), else your city
    if place:
        lines.append({"text": place, "url": ""})
    if p.get("linkedin"):  # the address itself, so it can be read on paper too
        lines.append({"text": re.sub(r"^https?://(www\.)?", "", p["linkedin"]).rstrip("/"), "url": p["linkedin"]})
    if p.get("website"):
        lines.append({"text": re.sub(r"^https?://(www\.)?", "", p["website"]).rstrip("/"), "url": p["website"]})
    lines += [{"text": x, "url": ""} for x in (data.get("personal") or []) if x.strip()]

    def entries(key: str) -> list[dict]:
        out = []
        for e in data.get(key) or []:
            if key == "work":
                a, b, c, d = e.get("name"), e.get("location"), e.get("position"), cvdata.period(e, lang)
            elif key == "education":
                a, b, c = e.get("institution"), e.get("degree"), e.get("area")
                d = " • ".join(x for x in ((e.get("location") or "").strip(), cvdata.period(e, lang)) if x)
            else:
                a, b, c, d = e.get("name"), cvdata.period(e, lang), "", ""
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

    # The photo is on every CV unless this one says no (the job ad asked for applications without a photo).
    photo = photo_path() if data.get("photo") is not False else None
    sidebar = [{"title": _title(data, k, lang), "items": items(k)} for k in look["sidebar_order"]]
    order = [k for k in data.get("main_order") or [] if k in MAIN_SECTIONS] or look["main_order"]  # this CV's own, for its job
    main = [{"title": _title(data, k, lang), "entries": entries(k)} for k in order]
    return {"lang": lang, "design": look,
            "photo": "photo.jpg" if photo else "", "photo_hash": hashlib.sha1(photo.read_bytes()).hexdigest()[:12] if photo else "",
            "header": {"name": p.get("name") or "", "label": (data.get("label") or "").strip(), "lines": lines},
            "profile": {"title": _title(data, "summary", lang), "text": (data.get("summary") or "").strip()},
            "sidebar": [s for s in sidebar if s["items"]], "main": [s for s in main if s["entries"]]}


SPACINGS = [round(1 + 0.03 * n, 2) for n in range(21)]  # from the tightest (1.0) to very open (1.6)


def _page_count(folder: Path, m: dict, spacing: float) -> int:
    import typst
    from pypdf import PdfReader

    m["design"]["spacing"] = spacing
    (folder / "data.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return len(PdfReader(io.BytesIO(typst.compile(str(folder / "main.typ"), root=str(folder), format="pdf"))).pages)


def fit_spacing(folder: Path, m: dict) -> float:
    """The most open line spacing that keeps the CV on as few pages as the tightest one does, so the last page is
    filled instead of ending in a blank band. Leaves `m` and data.json set to it."""
    least = _page_count(folder, m, SPACINGS[0])
    if _page_count(folder, m, SPACINGS[-1]) == least:  # a short CV: as open as the layout goes
        return SPACINGS[-1]
    lo, hi = 0, len(SPACINGS) - 2
    while lo < hi:  # more open never takes fewer pages, so search for the last value that still fits
        mid = (lo + hi + 1) // 2
        if _page_count(folder, m, SPACINGS[mid]) == least:
            lo = mid
        else:
            hi = mid - 1
    _page_count(folder, m, SPACINGS[lo])
    return SPACINGS[lo]


def _measure(folder: Path, m: dict) -> tuple[list[str], list[dict], list[dict]]:
    """Lay the CV out once in measuring mode (see the template): the texts to set a touch tighter, because that saves
    a line; the texts whose last line still holds only a word or so ({kind, text}), to be shortened; and every text
    with the number of lines it is printed on ({kind, text, lines}, and for the headline whether it fits at full size)."""
    import typst

    (folder / "data.json").write_text(json.dumps({**m, "measure": True}, ensure_ascii=False), encoding="utf-8")
    found = json.loads(typst.query(str(folder / "main.typ"), "<cvtext>", field="value", root=str(folder)))
    tight = sorted({f["text"] for f in found if f["tighten"]})
    short = [{"kind": f["kind"], "text": f["text"]} for f in found if f["short"] and not f["tighten"]]
    texts = [{"kind": f["kind"], "text": f["text"], "lines": f["lines"] - (1 if f["tighten"] else 0),
              **({"fits": f["fits"]} if "fits" in f else {})} for f in found]
    return tight, list({s["text"]: s for s in short}.values()), list({t["text"]: t for t in texts}.values())


def _room(folder: Path, m: dict, spacing: float | None = None) -> dict:
    """{"side", "main"}: how many lines of text each column has left on the page; negative is how far a column runs
    over. At `spacing`, or else at the spacing the CV is printed with (and at the tightest one when a column runs over
    even there, the most it can hold)."""
    import typst

    def heights(at: float) -> dict:
        look = {**m["design"], "spacing": at}
        (folder / "data.json").write_text(json.dumps({**m, "design": look, "room": True}, ensure_ascii=False), encoding="utf-8")
        return json.loads(typst.query(str(folder / "main.typ"), "<cvroom>", field="value", root=str(folder)))[0]

    h = heights(spacing or m["design"].get("spacing", SPACINGS[0]))
    if spacing is None and max(h["side"], h["main"]) > h["page"]:
        h = heights(SPACINGS[0])
    (folder / "data.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")  # back to the CV as printed
    return {"side": round((h["page"] - h["side"]) / h["side_line"], 1),
            "main": round((h["page"] - h["main"]) / h["main_line"], 1)}


def room(data: dict, language: str | None = None, spacing: float | None = None) -> dict:
    """How many lines of text the sidebar and the main column have left on the first page: {"side", "main"},
    negative where a column runs over the page. At the spacing the CV is printed with, or at `spacing` (such as your
    base CV's, to tell content from lines set further apart). Empty if it couldn't be measured."""
    folder = render(data, language)
    path = folder / ("room.json" if spacing is None else f"room-{spacing:.2f}.json")
    if not path.exists() and spacing is not None:
        path.write_text(json.dumps(_room(folder, json.loads((folder / "data.json").read_text(encoding="utf-8")), spacing)),
                        encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def spacing(data: dict, language: str | None = None) -> float:
    """The line spacing this CV is printed with (see fit_spacing)."""
    return json.loads((render(data, language) / "data.json").read_text(encoding="utf-8"))["design"].get("spacing", SPACINGS[0])


def measured(data: dict, language: str | None = None) -> list[dict]:
    """Every text on the CV with the number of lines it is printed on: [{kind, text, lines}], where kind is bullet,
    text, profile, side or headline (which also says whether it fits on its line at full size)."""
    path = render(data, language) / "measure.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def spills(data: dict, language: str | None = None) -> list[dict]:
    """The texts on this CV whose last line holds only a word or so, even set tighter: [{kind, text}], where kind is
    bullet, text (an entry's description), profile or side (a sidebar line, such as a group of skills)."""
    path = render(data, language) / "spills.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


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
    shutil.copyfile(TEMPLATE, folder / "main.typ")
    if m["photo"]:
        shutil.copyfile(photo_path(), folder / m["photo"])
    m["tight"], short, texts = _measure(folder, m)
    (folder / "spills.json").write_text(json.dumps(short, ensure_ascii=False), encoding="utf-8")
    (folder / "measure.json").write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    fit_spacing(folder, m)
    (folder / "room.json").write_text(json.dumps(_room(folder, m)), encoding="utf-8")
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
