"""What changed between two drafts of an application: the letter word by word, the CV line by line.

Every new draft, revision, edit, reset and restore keeps the draft it replaces under Versions (app_versions). The
review screen compares the draft as it is now with one of those, by default the newest one that differs from it,
so you see what a revision actually changed, and whether it changed anything you didn't ask for.

No model is involved: it's a plain text comparison. The letter is matched sentence by sentence first, so a
rewritten sentence shows as one change and not as scattered words; within a sentence that was only reworded, the
changed words are marked. The CV is matched section by section and line by line, with lines that only moved told
apart from lines that changed, and its look (colour, photo) compared too.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from markupsafe import Markup, escape

from . import db

REWRITTEN = 0.7  # this share of the letter's sentences new or changed: it was written again, not revised
SIMILAR = 0.4  # sentences (or CV lines) at least this alike are the same one reworded; less is a replacement

_WORD = re.compile(r"\w+", re.U)
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]", re.U)
# A sentence ends at . ! ? … before a word that doesn't start in lower case ("1. marts", "bl.a. at" go on), or at a
# line break.
_UNIT_END = re.compile(r"((?<=[.!?…])[\"'”’)]*[ \t]+(?![a-zæøåäöüéèêàáâíóôúûñç])|[ \t]*\n\s*)")
_BREAK = re.compile(r"[ \t]*\n[ \t]*\n\s*")


# ---------------------------------------------------------------------------------------
# Words within a sentence or a line
# ---------------------------------------------------------------------------------------

def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _words(text: str) -> int:
    return len(_WORD.findall(text))


def _token_key(t: str) -> str:
    """Whitespace compares by what it does: a space, a line break or a new paragraph."""
    if t.isspace():
        return "¶" if t.count("\n") >= 2 else "\n" if "\n" in t else " "
    return t


def _alike(a: str, b: str) -> float:
    """How alike two texts are, 0 to 1: by their words in order, or by the words they share when the order changed."""
    wa, wb = _WORD.findall(a.lower()), _WORD.findall(b.lower())
    if not wa and not wb:
        return 1.0
    shared = sum(min(wa.count(w), wb.count(w)) for w in set(wa))
    return max(SequenceMatcher(None, wa, wb, autojunk=False).ratio(), 2 * shared / (len(wa) + len(wb)))


def _same_words(a: str, b: str) -> bool:
    return sorted(_WORD.findall(a.lower())) == sorted(_WORD.findall(b.lower()))


def _lf(text: str | None) -> str:
    """Line breaks as \\n: text saved from the browser comes with \\r\\n."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def word_segments(old: str, new: str) -> list[tuple[str, str]]:
    """(kind, text) pieces that turn `old` into `new`: "eq" kept, "del" taken out, "ins" put in. A word or two kept
    between two changes is counted into the change, so a rewritten phrase reads as one change, not as confetti."""
    ta, tb = _TOKEN.findall(old), _TOKEN.findall(new)
    ops: list[list[str]] = []  # ["eq", text] or ["chg", old text, new text]
    sm = SequenceMatcher(None, [_token_key(t) for t in ta], [_token_key(t) for t in tb], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ops.append(["eq", "".join(tb[j1:j2])])
        elif ops and ops[-1][0] == "chg":
            ops[-1][1] += "".join(ta[i1:i2])
            ops[-1][2] += "".join(tb[j1:j2])
        else:
            ops.append(["chg", "".join(ta[i1:i2]), "".join(tb[j1:j2])])
    merged = True
    while merged:  # fold short kept bits between two changes into one change
        merged = False
        for k in range(1, len(ops) - 1):
            if ops[k][0] == "eq" and ops[k - 1][0] == "chg" and ops[k + 1][0] == "chg":
                kept = ops[k][1]
                around = _words(ops[k - 1][1] + ops[k + 1][1]) + _words(ops[k - 1][2] + ops[k + 1][2])
                if "\n" not in kept and (_words(kept) <= 1 or (_words(kept) <= 2 and around >= 12)):
                    a, b = ops[k - 1], ops[k + 1]
                    ops[k - 1: k + 2] = [["chg", a[1] + kept + b[1], a[2] + kept + b[2]]]
                    merged = True
                    break
    out: list[tuple[str, str]] = []
    for op in ops:
        if op[0] == "eq":
            out.append(("eq", op[1]))
            continue
        if op[1]:
            out.append(("del", op[1]))
        if op[2]:
            out.append(("ins", op[2]))
    return out


def _wrap(tag: str, text: str, breaks: bool) -> str:
    """`text` in <ins> or <del>, with the space around it left outside, so words on either side don't run together."""
    core = text.strip()
    lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    html = str(escape(core if tag == "ins" and breaks else _norm(core)))
    if breaks:
        html = html.replace("\n", "<br>")
    return (" " if lead else "") + f'<{tag} class="chg">{html}</{tag}>' + (" " if trail else "")


def render(segments: list[tuple[str, str]], paragraphs: bool = True) -> Markup:
    """The pieces as HTML: what was put in underlined in green, what was taken out struck through in red. With
    `paragraphs`, blank lines start a new paragraph (a letter); otherwise it's one line (a CV line)."""
    paras: list[list[str]] = [[]]
    for kind, text in segments:
        if kind == "del":
            if text.strip():
                paras[-1].append(_wrap("del", text, False))
            elif paragraphs and _BREAK.search(text):
                paras[-1].append('<del class="chg para" title="A paragraph break was taken out here">¶</del>')
            continue
        pieces = _BREAK.split(text) if paragraphs else [text]
        for n, piece in enumerate(pieces):
            if n:
                if kind == "ins" and not text.strip():  # only a paragraph break was put in
                    paras[-1].append('<ins class="chg para" title="A new paragraph starts here">¶</ins>')
                paras.append([])
            if kind == "ins" and piece.strip():
                paras[-1].append(_wrap("ins", piece, paragraphs))
            else:
                paras[-1].append(str(escape(piece)).replace("\n", "<br>" if paragraphs else " "))
    if not paragraphs:
        return Markup("".join(paras[0]))
    return Markup("".join(f"<p>{''.join(p)}</p>" for p in paras if "".join(p).strip()))


# ---------------------------------------------------------------------------------------
# The letter
# ---------------------------------------------------------------------------------------

def _units(text: str) -> list[str]:
    """The text cut into sentences and lines, each with the space after it, so joined they give the text back."""
    parts = _UNIT_END.split(text or "")
    units: list[str] = []
    for n in range(0, len(parts), 2):
        unit = parts[n] + (parts[n + 1] if n + 1 < len(parts) else "")
        if not unit:
            continue
        if units and not unit.strip():
            units[-1] += unit
        else:
            units.append(unit)
    return units


def letter(old: str | None, new: str | None) -> dict:
    """The letter now, with what changed since `old` marked; and how many sentences were reworded, added and
    taken out."""
    old, new = _lf(old), _lf(new)
    ua, ub = _units(old), _units(new)
    na, nb = [_norm(u) for u in ua], [_norm(u) for u in ub]
    counts = {"reworded": 0, "added": 0, "removed": 0}
    segments: list[tuple[str, str]] = []
    sm = SequenceMatcher(None, na, nb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a, b = "".join(ua[i1:i2]), "".join(ub[j1:j2])
        if tag == "equal":
            segments.append(("eq", b))
            continue
        real_a = [u for u in na[i1:i2] if u]
        real_b = [u for u in nb[j1:j2] if u]
        if tag == "replace" and _alike(a, b) >= SIMILAR:
            segments += word_segments(a, b)
            pairs = min(len(real_a), len(real_b))
            counts["reworded"] += pairs
            counts["added"] += len(real_b) - pairs
            counts["removed"] += len(real_a) - pairs
            continue
        if a:
            segments.append(("del", a))
        if b:
            segments.append(("ins", b))
        counts["added"] += len(real_b)
        counts["removed"] += len(real_a)
    kept = sum(1 for tag, _, _, j1, j2 in sm.get_opcodes() if tag == "equal" for u in nb[j1:j2] if u)
    total = len([u for u in nb if u]) or 1
    same = na == nb
    return {"same": same, "html": render(segments), "counts": counts, "words": (len(old.split()), len(new.split())),
            "rewritten": bool(old.strip()) and not same and 1 - kept / total >= REWRITTEN,
            "new": not old.strip() and bool(new.strip()), "gone": bool(old.strip()) and not new.strip()}


# ---------------------------------------------------------------------------------------
# The CV
# ---------------------------------------------------------------------------------------

def _sections(md: str | None) -> list[tuple[str, list[str]]]:
    """The CV's Markdown as (heading, lines) per section; the top (headline and profile) has the heading ""."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in _lf(md).splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("## "):
            out.append((s[3:].strip(), []))
        else:
            out[-1][1].append(s)
    return [sec for sec in out if sec[0] or sec[1]]


def _key(heading: str) -> str:
    return re.sub(r"[\W_]+", "", heading.lower())


def _plain(line: str) -> tuple[str, str]:
    """A CV line as (kind, text): headline, entry (a job, an education, a project), point, or text."""
    if line.startswith("# "):
        return "headline", line[2:].strip()
    if line.startswith("### "):
        return "entry", line[4:].strip()
    if line.startswith(("- ", "* ")):
        return "point", line[2:].strip()
    return "text", line


def _entries(lines: list[str]) -> list[str]:
    """For each line, the job, education or project it belongs to ("" before the first)."""
    out, cur = [], ""
    for line in lines:
        if line.startswith("### "):
            cur = line[4:].strip()
        out.append(cur)
    return out


def _line_changes(old: list[str], new: list[str]) -> list[dict]:
    """What changed in one section, in the order of the CV now. A line that is still there word for word but in
    another place moved; one that is much like a line that went is that line reworded."""
    na, nb = [_norm(x).lower() for x in old], [_norm(x).lower() for x in new]
    ea, eb = _entries(old), _entries(new)
    removed: list[tuple[int, int]] = []  # (old index, where it was in the CV now)
    added: list[int] = []
    place: dict[int, int] = {}  # an added line: where the lines it may have replaced were
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        removed += [(i, j1) for i in range(i1, i2)]
        added += list(range(j1, j2))
        place.update({j: j1 for j in range(j1, j2)})
    items: list[dict] = []
    for i, at in list(removed):  # the same line in another place
        j = next((j for j in added if nb[j] == na[i]), None)
        if j is not None:
            removed.remove((i, at))
            added.remove(j)
            items.append({"kind": "moved", "at": j, "entry": eb[j], "line": new[j]})
    pairs = sorted(((_alike(old[i], new[j]), i, at, j) for i, at in removed for j in added), reverse=True)
    for score, i, at, j in pairs:  # the most alike first
        if score < SIMILAR or (i, at) not in removed or j not in added:
            continue
        removed.remove((i, at))
        added.remove(j)
        items.append({"kind": "changed", "at": j, "entry": eb[j], "line": new[j], "old": old[i], "moved": place[j] != at})
    items += [{"kind": "added", "at": j, "entry": eb[j], "line": new[j]} for j in added]
    items += [{"kind": "removed", "at": at - 0.5, "entry": ea[i], "line": old[i]} for i, at in removed]
    items.sort(key=lambda x: x["at"])
    for it in items:
        kind, text = _plain(it["line"])
        it["line_kind"] = kind
        if it["kind"] == "changed" and _same_words(it["old"], it["line"]):  # the same items, in another order
            it["kind"] = "reordered"
        if it["kind"] == "changed":
            it["html"] = render(word_segments(_plain(it["old"])[1], text), paragraphs=False)
        else:
            it["html"] = Markup(escape(text))
    return items


def _colour(value: str | None) -> str | None:
    return value if value and value != "own" else None


def look(old: dict | None, new: dict | None) -> list[dict]:
    """What changed in how the CV looks: its colour, and whether your photo is on it."""
    out = []
    if old is None or new is None:
        return out
    if _colour(old.get("color")) != _colour(new.get("color")):
        out.append({"what": "colour", "old": _colour(old.get("color")), "new": _colour(new.get("color")),
                    "by": new.get("color_by")})
    if (old.get("photo") is False) != (new.get("photo") is False):
        out.append({"what": "photo", "new": new.get("photo") is not False})
    return out


def cv(old_md: str | None, new_md: str | None, old_data: dict | None = None, new_data: dict | None = None) -> dict:
    """The CV's changes since `old_md`, grouped by section and by job, education or project."""
    sa, sb = _sections(old_md), _sections(new_md)
    a_by, b_by = {_key(h): (h, lines) for h, lines in sa}, {_key(h): (h, lines) for h, lines in sb}
    groups: list[dict] = []
    counts = {"changed": 0, "reordered": 0, "added": 0, "removed": 0, "moved": 0}

    def add(section: str, items: list[dict]) -> None:
        for it in items:
            counts[it["kind"]] += 1
            if groups and groups[-1]["section"] == section and groups[-1]["entry"] == it["entry"]:
                groups[-1]["lines"].append(it)
            else:
                groups.append({"section": section, "entry": it["entry"], "lines": [it]})

    for k, (heading, lines) in b_by.items():
        before = a_by.get(k, (heading, []))[1]
        add(heading, _line_changes(before, lines))
    for k, (heading, lines) in a_by.items():
        if k not in b_by:
            add(heading, _line_changes(lines, []))
    order_a = [h for h, _ in sa if h and _key(h) in b_by]
    order_b = [h for h, _ in sb if h and _key(h) in a_by]
    reordered = [_key(h) for h in order_a] != [_key(h) for h in order_b]
    looks = look(old_data, new_data)
    same = not any(counts.values()) and not reordered
    return {"same": same and not looks, "groups": groups, "counts": counts, "look": looks,
            "order": (order_a, order_b) if reordered else None,
            "new": not (old_md or "").strip() and bool((new_md or "").strip()),
            "gone": bool((old_md or "").strip()) and not (new_md or "").strip()}


# ---------------------------------------------------------------------------------------
# An application against one of its versions
# ---------------------------------------------------------------------------------------

_SINCE = {"Before revision": "the draft before your revision", "Before a new draft": "the draft before the new one",
          "Before your edit": "the draft before your edit", "Before restore": "the draft before you restored a version",
          "Before reset to base CV": "the draft before the CV was reset to your base CV",
          "Before the Claude.ai version": "the draft before the Claude.ai version",
          "First draft": "the first draft", "New draft": "the new draft as it was written"}


def since(label: str | None) -> str:
    return _SINCE.get(label or "", f"the version “{label}”" if label else "an earlier version")


def _state(row: dict) -> tuple:
    data = db.jload(row.get("cv_data"), None)
    looks = (_colour(data.get("color")), data.get("photo") is False) if data else None
    return _norm(row.get("subject")), _norm(row.get("letter")), _norm(row.get("cv")), looks


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def summary_letter(d: dict) -> str:
    if d["new"]:
        return "written for the first time"
    if d["gone"]:
        return "taken out"
    if d["same"]:
        return "unchanged"
    c = d["counts"]
    parts = [x for x in (c["reworded"] and _count(c["reworded"], "sentence") + " reworded",
                         c["added"] and _count(c["added"], "new sentence"),
                         c["removed"] and _count(c["removed"], "sentence") + " taken out") if x]
    words = f"{d['words'][0]} → {d['words'][1]} words"
    if d["rewritten"]:
        return f"mostly written again ({', '.join(parts)}), {words}"
    return (", ".join(parts) or "only spacing changed") + f", {words}"


def summary_cv(d: dict) -> str:
    if d["new"]:
        return "written for the first time"
    if d["gone"]:
        return "taken out"
    if d["same"]:
        return "unchanged"
    c = d["counts"]
    parts = [x for x in (c["changed"] and _count(c["changed"], "line") + " reworded",
                         c["reordered"] and _count(c["reordered"], "line") + " with the same words in a new order",
                         c["added"] and _count(c["added"], "line") + " added",
                         c["removed"] and _count(c["removed"], "line") + " taken out",
                         c["moved"] and _count(c["moved"], "line") + " moved",
                         d["order"] and "sections in a new order") if x]
    for lk in d["look"]:
        parts.append("new colour" if lk["what"] == "colour" else "photo back on" if lk["new"] else "photo left out")
    return ", ".join(parts) or "only spacing changed"


def for_app(app: dict, versions: list[dict], against: int | None = None) -> dict | None:
    """The application now against one of its versions: the one you picked, else the newest that differs from it.
    None when there's nothing earlier to compare with. `versions` are newest first."""
    if not versions:
        return None
    now = _state(app)
    same_as_now = {v["id"] for v in versions if _state(v) == now}
    differs = [v for v in versions if v["id"] not in same_as_now]
    picked = next((v for v in versions if v["id"] == against), None) if against else None
    base = picked or (differs[0] if differs else None)
    # A revision that changed nothing: the newest version is the draft as it was before it, word for word.
    newest = versions[0]
    idle = None
    if newest.get("asked") and _state(newest) == now and newest["label"] == "Before revision":
        idle = {"asked": newest["asked"], "at": newest["created_at"]}
    if not base:
        return {"base": None, "versions": versions, "same_as_now": same_as_now, "idle": idle, "changed": False}
    lt = letter(base.get("letter"), app.get("letter"))
    cd = cv(base.get("cv"), app.get("cv"), db.jload(base.get("cv_data"), None), db.jload(app.get("cv_data"), None))
    subject = None
    if _norm(base.get("subject")) != _norm(app.get("subject")) and (base.get("subject") or app.get("subject")):
        subject = render(word_segments(base.get("subject") or "", app.get("subject") or ""), paragraphs=False)
    asked = [{"text": v["asked"], "at": v["created_at"]} for v in sorted(versions, key=lambda v: v["id"])
             if v["id"] >= base["id"] and v.get("asked") and not (idle and v is newest)]
    changed = not (lt["same"] and cd["same"]) or subject is not None
    return {"base": base, "since": since(base["label"]), "versions": versions, "same_as_now": same_as_now,
            "letter": lt, "cv": cd, "subject": subject, "asked": asked, "idle": idle, "changed": changed,
            "letter_line": summary_letter(lt), "cv_line": summary_cv(cd),
            # Shown with its changes marked unless the letter was written again from scratch: then it reads clean.
            "show": changed and not lt["rewritten"]}
