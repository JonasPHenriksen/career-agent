"""Your writing voice: stylometrics, the style guide, example selection, and the voice check."""
from __future__ import annotations

import re
from statistics import mean, pstdev

from . import db, llm, settings
from .profile import OUTCOME_LABELS, OUTCOME_RANK, limits

# Phrases that make letters sound generic or machine-written. A phrase is only flagged in a
# draft if *you* don't use it in your own documents - if it's yours, it's your voice.
CLICHES = {
    "en": [
        "i am writing to express", "i am excited to apply", "i am thrilled", "passionate about",
        "proven track record", "dynamic team", "fast-paced environment", "hit the ground running",
        "leverage my", "synergy", "delve", "tapestry", "testament to", "in today's", "unique blend",
        "make a meaningful impact", "drive innovation", "cutting-edge", "seamlessly", "furthermore,",
        "moreover,", "additionally,", "aligns perfectly", "navigate the complexities", "ever-evolving",
        "spearheaded", "honed", "pivotal", "invaluable", "embark", "realm", "showcase",
        "i am confident that my", "i would welcome the opportunity", "eager to contribute",
        "perfect fit", "wealth of experience", "results-driven", "self-starter", "think outside the box",
    ],
    "da": [
        "brænder for", "brænder virkelig for", "passion for", "i en verden hvor", "jeg er overbevist om",
        "spændende muligheder", "drevet af", "yderst motiveret", "unik kombination", "stærk kombination",
        "værdiskabende", "den rette kandidat", "perfekt match", "dynamisk", "omskiftelig verden",
        "jeg er sikker på, at jeg", "i dagens", "skabe værdi", "gøre en forskel", "holistisk",
        "proaktiv tilgang", "jeg vil være et stærkt aktiv", "gå-på-mod", "synergi",
    ],
}

# Relative time words go stale when old letters are reused; history openers are a typical invention.
RISKY_TIME_WORDS = ["sidste år", "i år", "for nylig", "sidste måned", "last year", "this year", "recently", "last month",
                    # status that goes stale: true in one season's letters, wrong a few months later
                    "nyuddannet", "lige blevet færdiguddannet", "just graduated", "newly graduated", "recent graduate"]
INVENTED_HISTORY = ["har længe beundret", "har altid drømt", "har altid ønsket", "har længe fulgt", "long admired",
                    "always dreamed", "have always wanted", "have long followed"]

ABBREVIATIONS = ["f.eks.", "bl.a.", "ca.", "dvs.", "mht.", "inkl.", "evt.", "osv.", "mv.", "jf.", "pga.",
                 "e.g.", "i.e.", "etc.", "vs.", "Mr.", "Mrs.", "Dr.", "nr.", "kl.", "cand.", "merc.",
                 "scient.", "polit.", "jur.", "mag.", "techn.", "ph.d.", "Ph.D.", "St.", "a.m.", "p.m."]

WORD = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ][0-9A-Za-zÀ-ÖØ-öø-ÿ'’\-]*")
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'“(\[]?[A-ZÆØÅÄÖÜ0-9])|\n{2,}|\n(?=[-•*] )")

METRICS = [
    # key, label, kind, tolerance
    ("avg_sentence", "Average sentence length (words)", "rel", 0.25),
    ("sd_sentence", "Sentence length variation", "rel", 0.40),
    ("avg_word", "Average word length", "rel", 0.08),
    ("commas_per_sentence", "Commas per sentence", "rel", 0.45),
    ("words_per_paragraph", "Paragraph length (words)", "rel", 0.45),
    ("i_start_pct", "Sentences starting with I / Jeg (%)", "abs", 12.0),
    ("exclamations_per_1k", "Exclamation marks per 1,000 words", "abs", 3.0),
]


def sentences(text: str) -> list[str]:
    t = text
    for a in ABBREVIATIONS:
        t = t.replace(a, a.replace(".", "§"))
    parts = _SPLIT.split(t)
    return [p.replace("§", ".").strip() for p in parts if len(WORD.findall(p)) >= 2]


def paragraphs(text: str) -> list[str]:
    """Paragraphs; for PDF text without blank lines, a short line ending a sentence ends a paragraph."""
    paras = [p for p in re.split(r"\n\s*\n", text) if len(WORD.findall(p)) >= 3]
    if len(paras) <= 1:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        lens = sorted(len(ln) for ln in lines)
        if len(lines) >= 2 and lens[len(lens) // 2] > 140:
            # Long lines are whole paragraphs (Word, plain text), not a PDF's wrapped lines.
            found = [ln.strip() for ln in lines if len(WORD.findall(ln)) >= 3]
            if len(found) > 1:
                return found
        if len(lines) >= 6:
            lens = sorted(len(ln) for ln in lines)
            width = lens[int(0.9 * (len(lens) - 1))]  # a full line's width
            out, cur = [], []
            for n, ln in enumerate(lines):
                cur.append(ln.strip())
                nxt = lines[n + 1].strip() if n + 1 < len(lines) else ""
                if re.search(r"[.!?:,]\s*$", ln) and len(ln) < 0.8 * width and (not nxt or nxt[:1].isupper()):
                    out.append(" ".join(cur))
                    cur = []
            if cur:
                out.append(" ".join(cur))
            found = [p for p in out if len(WORD.findall(p)) >= 3]
            if len(found) > 1:
                paras = found
    return paras or [text]


def text_stats(text: str) -> dict:
    words = WORD.findall(text or "")
    n = len(words)
    if n < 20:
        return {}
    sents = sentences(text)
    lens = [len(WORD.findall(s)) for s in sents] or [n]
    paras = paragraphs(text)

    def per1k(c: int) -> float:
        return round(c * 1000 / n, 2)

    i_starts = sum(1 for s in sents if re.match(r"^[\"'“(]?(I|Jeg)\b", s))
    return {
        "words": n,
        "sentences": len(sents),
        "avg_sentence": round(mean(lens), 1),
        "sd_sentence": round(pstdev(lens), 1),
        "avg_word": round(mean(len(w) for w in words), 2),
        "words_per_paragraph": round(n / len(paras), 1),
        "commas_per_sentence": round(text.count(",") / max(len(sents), 1), 2),
        "em_dash_per_1k": per1k(text.count("—")),
        "exclamations_per_1k": per1k(text.count("!")),
        "i_start_pct": round(100 * i_starts / max(len(sents), 1), 1),
    }


def _avg(stats_list: list[dict]) -> dict:
    stats_list = [s for s in stats_list if s]
    if not stats_list:
        return {}
    keys = stats_list[0].keys()
    out = {k: round(mean(s[k] for s in stats_list), 2) for k in keys}
    out["samples"] = len(stats_list)
    return out


_SIGNOFF = re.compile(r"^[ \t]*(med venlig hilsen|venlig hilsen|de bedste hilsner|bedste hilsner|mvh\.?|"
                      r"kind regards|best regards|warm regards|sincerely|yours sincerely|yours faithfully)[ \t]*,?[ \t]*$",
                      re.I | re.M)


def letter_part(text: str) -> str:
    """A letter up to its sign-off and the name under it. Google Docs exports with tabs, and notes pasted under a
    letter, put other material after it (research notes, the job ad, private notes) that isn't the letter."""
    m = _SIGNOFF.search(text or "")
    if not m:
        return text
    lines = text[m.end():].split("\n")
    name_at = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if name_at is None:
        return text
    tail = "\n".join(lines[name_at + 1:])
    if len(WORD.findall(tail)) < 40:  # only a short P.S. or a title after the name: keep it all
        return text
    return (text[:m.end()] + "\n".join(lines[:name_at + 1])).rstrip()


def style_documents(kinds: tuple[str, ...] = ("application",)) -> list[dict]:
    marks = ",".join("?" for _ in kinds)
    rows = db.q(
        f"SELECT * FROM documents WHERE authored = 1 AND use_for_style = 1 AND status = 'analyzed' "
        f"AND kind IN ({marks}) ORDER BY COALESCE(doc_date, created_at) DESC", kinds)
    for d in rows:  # examples, statistics and the style guide use the letter itself, nothing after it
        if d.get("kind") == "application":
            d["text"] = letter_part(d["text"] or "")
            d["word_count"] = len(d["text"].split())
    return rows


def compute_baseline() -> dict:
    """Average stats of your own applications (or other prose if you have none), per language."""
    docs = style_documents(("application",)) or style_documents(("application", "email", "report", "other"))
    by_lang: dict[str, list[dict]] = {}
    for d in docs:
        st = text_stats(d["text"])
        if st:
            by_lang.setdefault(d.get("language") or "other", []).append(st)
            by_lang.setdefault("all", []).append(st)
    baseline = {lang: _avg(v) for lang, v in by_lang.items()}
    settings.set("style_stats", baseline)
    return baseline


def _corpus_lower() -> str:
    rows = db.q("SELECT text FROM documents WHERE authored = 1")
    return "\n".join(r["text"] for r in rows).lower()


def check(text: str, language: str | None) -> dict:
    """Compare a draft with your own writing. Returns a score, metric table and flagged phrases."""
    lang = (language or "").lower()[:2]
    baseline_all = settings.get("style_stats") or {}
    base = baseline_all.get(lang) or baseline_all.get("all") or {}
    draft = text_stats(text)
    metrics, penalty = [], 0
    for key, label, kind, tol in METRICS:
        if key not in draft:
            continue
        yours = base.get(key)
        flag = ""
        if yours is not None:
            if kind == "rel" and yours > 0:
                ratio = draft[key] / yours
                if ratio > 1 + tol:
                    flag = "higher than usual"
                elif ratio < 1 - tol:
                    flag = "lower than usual"
            elif kind == "abs" and abs(draft[key] - yours) > tol:
                flag = "higher than usual" if draft[key] > yours else "lower than usual"
        if flag:
            penalty += 7
        metrics.append({"key": key, "label": label, "draft": draft[key], "yours": yours, "flag": flag})

    notes = []
    if draft.get("em_dash_per_1k", 0) > 0 and (base.get("em_dash_per_1k") or 0) < 0.5:
        penalty += 8
        notes.append("Uses em dashes (—), which you normally don't.")

    corpus = _corpus_lower()
    lowered = (text or "").lower()
    extra = [p.strip().lower() for p in settings.get("writing").get("extra_banned_phrases", "").split("\n") if p.strip()]
    phrases = []
    candidates = CLICHES.get(lang, []) + (CLICHES["en"] if lang not in CLICHES else []) + extra
    for phrase in dict.fromkeys(candidates):
        count = lowered.count(phrase)
        if count and (phrase in extra or corpus.count(phrase) == 0):
            phrases.append({"phrase": phrase, "count": count})
    penalty += min(30, 5 * sum(p["count"] for p in phrases))
    stale = [w for w in RISKY_TIME_WORDS if re.search(rf"\b{re.escape(w)}\b", lowered)]
    if stale:
        notes.append("Time words that may be out of date (check the dates in your career database): "
                     + ", ".join(f"“{w}”" for w in stale) + ".")
    invented = [w for w in INVENTED_HISTORY if w in lowered]
    if invented:
        penalty += 10
        notes.append("Possibly invented history or feelings, check they're true: " + ", ".join(f"“{w}”" for w in invented) + ".")
    if not base:
        notes.append("No baseline yet: import some of your own applications so I can compare against your writing.")
    return {"score": max(0, 100 - penalty), "metrics": metrics, "phrases": phrases, "notes": notes,
            "baseline_samples": base.get("samples", 0), "words": draft.get("words", 0)}


# ---------------------------------------------------------------------------------------
# Examples of your writing for prompts
# ---------------------------------------------------------------------------------------

def _trim_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    cut = " ".join(words[:max_words])
    return cut + " […]"


_OUTCOME_BUCKET = {"offer": 0, "interview": 0, "unknown": 1, "rejected": 2, "no_response": 2}


def exemplars(language: str | None, budget_words: int | None = None, query: str | None = None) -> list[dict]:
    """Your best past applications: same language first, then the ones that worked, and among
    those the most similar to this job (when a `query` is given)."""
    budget = budget_words or limits()["exemplar_words"]
    docs = style_documents(("application",))
    lang = (language or "").lower()[:2]
    rel: dict[int, int] = {}
    if query and len(docs) > 1:
        from . import retrieval

        rel = {doc_id: n for n, doc_id in enumerate(retrieval.rank_documents(query, docs))}
    docs.sort(key=lambda d: (0 if (d.get("language") or "")[:2] == lang else 1,
                             _OUTCOME_BUCKET.get(d.get("outcome") or "unknown", 1),
                             rel.get(d["id"], 0)))
    out, used = [], 0
    for d in docs:
        if used >= budget or len(out) >= 3:
            break
        take = min(900, budget - used)
        if take < 150:
            break
        out.append({"id": d["id"], "language": d.get("language"), "outcome": d.get("outcome"),
                    "title": d.get("title"), "text": _trim_words(d["text"], take)})
        used += min(take, d["word_count"])
    return out


_NEUTRAL = {"sidste år": "tidligere", "i år": "for nylig", "sidste måned": "tidligere",
            "last year": "previously", "this year": "recently", "last month": "previously"}


def neutral_time_words(text: str) -> str:
    """Example letters are years old: 'sidste år' in them is no longer true, and models copy it.
    Replace relative time words with neutral ones before a model sees the examples."""
    def swap(m: re.Match) -> str:
        word = _NEUTRAL[m.group(0).lower()]
        return word[0].upper() + word[1:] if m.group(0)[0].isupper() else word
    pattern = r"\b(" + "|".join(re.escape(w) for w in _NEUTRAL) + r")\b"
    return re.sub(pattern, swap, text, flags=re.IGNORECASE)


def exemplars_block(language: str | None, query: str | None = None) -> str:
    ex = exemplars(language, query=query)
    if not ex:
        return "(No past applications imported yet. Follow the style guide.)"
    parts = []
    for n, e in enumerate(ex, 1):
        outcome = OUTCOME_LABELS.get(e["outcome"] or "unknown", "")
        parts.append(f'<example n="{n}" language="{e["language"] or "?"}" outcome="{outcome}">\n{neutral_time_words(e["text"])}\n</example>')
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------------------
# Style guide
# ---------------------------------------------------------------------------------------

STYLE_SYSTEM = """You are a writing-style analyst. You will read texts written by one person and write a style guide that lets a ghostwriter produce new job applications that read as if this person wrote them.

Describe the person's actual habits, with short verbatim examples in the original language. Do not give generic writing advice and do not describe how they *should* write.

Write the guide in English, in Markdown, at most about 700 words, with these sections:
## Voice in brief
## Tone and formality
## How their applications are built
(openings, structure, how they connect their experience to the job, closings and sign-offs, typical length)
## Sentences and paragraphs
## Vocabulary and phrasing
(words and expressions they actually use, quoted; words and constructions they avoid)
## How they talk about achievements
## How they express motivation
## Do / Don't for the ghostwriter
(concrete bullet lists)

If they write in several languages, note differences per language."""


def build_style_guide() -> str:
    baseline = compute_baseline()
    budget = limits()["style_words"]
    docs = style_documents(("application",))
    docs.sort(key=lambda d: OUTCOME_RANK.get(d.get("outcome") or "unknown", 2))
    docs += style_documents(("email", "report", "other"))
    samples, used = [], 0
    for d in docs:
        if used >= budget:
            break
        take = min(1200, budget - used)
        if take < 120:
            break
        meta = f"{d['kind']}, language {d.get('language') or '?'}, {d.get('doc_date') or 'undated'}"
        if d["kind"] == "application":
            meta += f", outcome: {OUTCOME_LABELS.get(d.get('outcome') or 'unknown')}"
        samples.append(f'<sample n="{len(samples) + 1}" meta="{meta}">\n{_trim_words(d["text"], take)}\n</sample>')
        used += min(take, d["word_count"])
    if not samples:
        raise llm.LLMError("Import and analyse some documents you wrote yourself (applications, emails, reports) first.")
    stats_lines = []
    for lang, st in baseline.items():
        if lang == "all":
            continue
        stats_lines.append(
            f"- {lang}: {st.get('samples')} texts, avg sentence {st.get('avg_sentence')} words "
            f"(variation {st.get('sd_sentence')}), avg paragraph {st.get('words_per_paragraph')} words, "
            f"{st.get('commas_per_sentence')} commas/sentence, {st.get('i_start_pct')}% of sentences start with I/Jeg, "
            f"{st.get('em_dash_per_1k')} em dashes per 1,000 words")
    user = ("Measured statistics of their applications:\n" + ("\n".join(stats_lines) or "- (not enough data)")
            + "\n\nTheir texts:\n\n" + "\n\n".join(samples))
    r = llm.run("writing", "style_guide", STYLE_SYSTEM, user, max_tokens=12000)
    guide = r.text.strip()
    settings.set("style_guide", guide)
    return guide
