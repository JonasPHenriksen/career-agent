"""Test setup: a throwaway data folder and a fake model that returns canned, schema-shaped answers."""
from __future__ import annotations

import json
import os
import tempfile

os.environ["CAREER_AGENT_SECRETS"] = "file"
os.environ["CAREER_AGENT_NO_NOTIFY"] = "1"
os.environ["CAREER_AGENT_NO_OLLAMA_START"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402

from career_agent import config, cvdata, db, jobs, llm, profile, settings, sources, tracker, writer  # noqa: E402
from career_agent.llm.providers import ProviderReply  # noqa: E402

from career_agent import fetch as _fetch  # noqa: E402

_fetch.SITE_GAP = 0  # tests ask their pretend sites as fast as they like

CV_TEXT = """Sara Lund
Projektleder og product owner

Erfaring
Product Owner, Danske Bank, København, 2022 – nu
Ansvarlig for mobilbankens betalingsfunktioner med 2 mio. brugere. Ledte et team på 8 udviklere.
Reducerede antallet af supporthenvendelser om betalinger med 30 % på et år.

Projektleder, Novo Nordisk, Bagsværd, 2018 – 2022
Ledte digitaliseringsprojekter med et samlet budget på 12 mio. kr.

Uddannelse
Cand.merc.it, Copenhagen Business School, 2016 – 2018

Kompetencer
Scrum, Jira, stakeholder management, SQL, Figma
Sprog: dansk (modersmål), engelsk (flydende)
"""

APPLICATION_TEXT = """Kære Mette

Jeg søger stillingen som product owner hos jer, fordi jeg gerne vil arbejde med produkter, der gør hverdagen lettere for rigtige mennesker.

I mit nuværende job i Danske Bank har jeg ansvaret for betalingsfunktionerne i mobilbanken. Vi har fået supporthenvendelserne ned med 30 %, og det er jeg stolt af. Jeg kan lide at være tæt på brugerne. Jeg kan også lide at få et team til at lykkes.

Før det var jeg projektleder i Novo Nordisk i fire år. Her lærte jeg at holde styr på store budgetter og mange interessenter.

Jeg håber, vi kan tage en snak.

Med venlig hilsen
Sara Lund
"""

JOB_TEXT = """Product Owner til vores betalingsteam – FinPay A/S, København
Vi søger en erfaren product owner, der kan lede vores team af udviklere og forbedre betalingsoplevelsen for vores kunder.
Du har erfaring med Scrum og agile teams, og du har arbejdet med betalinger eller finans.
Det er en fordel, hvis du kender SQL.
Ansøgningsfrist: 1. december 2030. Send ansøgning til job@finpay.dk.
"""


def _facts_for(user: str) -> list[dict]:
    if "Danske Bank" in user and "Uddannelse" in user:  # the CV
        return [
            {"category": "experience", "title": "Product Owner", "organization": "Danske Bank", "location": "København",
             "start_date": "2022", "end_date": "present", "detail": "Ansvarlig for mobilbankens betalingsfunktioner med 2 mio. brugere.",
             "evidence": "Ansvarlig for mobilbankens betalingsfunktioner", "tags": ["payments", "mobile"]},
            {"category": "experience", "title": "Projektleder", "organization": "Novo Nordisk", "location": "Bagsværd",
             "start_date": "2018", "end_date": "2022", "detail": "Ledte digitaliseringsprojekter med budget på 12 mio. kr.",
             "evidence": "Ledte digitaliseringsprojekter", "tags": ["project management"]},
            {"category": "achievement", "title": "Reducerede supporthenvendelser om betalinger med 30 %",
             "organization": "Danske Bank", "location": "", "start_date": "", "end_date": "",
             "detail": "Reducerede antallet af supporthenvendelser om betalinger med 30 % på et år.",
             "evidence": "Reducerede antallet af supporthenvendelser om betalinger med 30 %", "tags": []},
            {"category": "skill", "title": "Scrum", "organization": "", "location": "", "start_date": "", "end_date": "",
             "detail": "", "evidence": "Scrum, Jira", "tags": []},
            {"category": "skill", "title": "SQL", "organization": "", "location": "", "start_date": "", "end_date": "",
             "detail": "", "evidence": "SQL", "tags": []},
            {"category": "education", "title": "Cand.merc.it", "organization": "Copenhagen Business School", "location": "",
             "start_date": "2016", "end_date": "2018", "detail": "", "evidence": "Cand.merc.it", "tags": []},
            {"category": "language", "title": "Danish", "organization": "", "location": "", "start_date": "", "end_date": "",
             "detail": "native", "evidence": "dansk (modersmål)", "tags": []},
        ]
    # the application repeats two facts (should merge) and adds a value
    return [
        {"category": "experience", "title": "Product Owner", "organization": "Danske Bank A/S", "location": "",
         "start_date": "2022", "end_date": "", "detail": "Ansvar for betalingsfunktionerne i mobilbanken.",
         "evidence": "ansvaret for betalingsfunktionerne", "tags": ["payments"]},
        {"category": "skill", "title": "scrum", "organization": "", "location": "", "start_date": "", "end_date": "",
         "detail": "", "evidence": "", "tags": []},
        {"category": "value", "title": "Vil arbejde med produkter, der gør hverdagen lettere", "organization": "",
         "location": "", "start_date": "", "end_date": "", "detail": "", "evidence": "gør hverdagen lettere", "tags": []},
    ]


class FakeProvider:
    name = "fake"
    is_cloud = False
    model = "fake-model"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, schema, max_tokens):
        self.calls.append((system, user, schema))
        data = self.answer(system, user, schema)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return ProviderReply(text=text, input_tokens=len(system + user) // 4, output_tokens=len(text) // 4, model=self.model)

    def answer(self, system, user, schema):
        if schema is profile.ANALYZE_SCHEMA:
            if "Forelæsning" in user:  # lecture slides: the model classifies them as course material
                return {"kind": "course_material", "language": "da", "title": "Forelæsning", "doc_date": "",
                        "summary": "Lecture slides.", "written_by_candidate": False, "target_company": "",
                        "target_role": "", "facts": [{"category": "skill", "title": "Kvantemekanik",
                                                      "organization": "", "location": "", "start_date": "",
                                                      "end_date": "", "detail": "Taught in the lecture.",
                                                      "evidence": "Forelæsning", "tags": []}]}
            is_cv = "Uddannelse" in user
            return {"kind": "cv" if is_cv else "application", "language": "da",
                    "title": "CV" if is_cv else "Ansøgning til product owner", "doc_date": "2024-05",
                    "summary": "Test document.", "written_by_candidate": True,
                    "target_company": "" if is_cv else "Lunar", "target_role": "" if is_cv else "Product owner",
                    "facts": _facts_for(user)}
        if schema is cvdata.CV_SCHEMA:  # the CV read into data, as written (and never with contact details)
            assert "sara@example.com" not in user, "contact details must not reach the model"
            return {"language": "da", "label": "Projektleder og product owner", "personal": [], "summary": "",
                    "titles": {"work": "Erfaring", "education": "Uddannelse", "skills": "Kompetencer"},
                    "work": [{"name": "Danske Bank", "position": "Product Owner", "location": "København", "start": "2022", "end": "nu",
                              "highlights": ["Ansvarlig for mobilbankens betalingsfunktioner med 2 mio. brugere.",
                                             "Reducerede antallet af supporthenvendelser om betalinger med 30 % på et år."]},
                             {"name": "Novo Nordisk", "position": "Projektleder", "location": "Bagsværd", "start": "2018", "end": "2022",
                              "highlights": ["Ledte digitaliseringsprojekter med et samlet budget på 12 mio. kr."]}],
                    "education": [{"institution": "Copenhagen Business School", "degree": "Cand.merc.it", "area": "", "location": "",
                                   "start": "2016", "end": "2018", "highlights": []}],
                    "projects": [], "skills": [{"name": "", "keywords": ["Scrum", "Jira", "stakeholder management", "SQL", "Figma"]}],
                    "languages": [{"language": "dansk", "fluency": "modersmål"}, {"language": "engelsk", "fluency": "flydende"}],
                    "interests": [], "certificates": []}
        if schema is cvdata.TAILOR_SCHEMA:  # the base with one small change: the second point of the first job goes first
            cv = json.loads(user.split("<cv>\n", 1)[1].split("\n</cv>", 1)[0])
            no_photo = "vedlægge foto" in user.lower()  # the ad (not the rules above it) asks for applications without a photo
            if "Shorter" in user or "Shorten it" in user:
                cv["work"][0]["highlights"] = cv["work"][0]["highlights"][:1]
                return {"cv": cv, "changes": ["Kept one point per job."], "used_fact_ids": [], "notes": [], "leave_out_photo": no_photo}
            cv["work"][0]["highlights"].reverse()
            return {"cv": cv, "changes": ["Put the support result first: the ad is about payments."], "used_fact_ids": [1], "notes": [],
                    "leave_out_photo": no_photo}
        if schema is profile.CONSOLIDATE_SCHEMA:
            import re
            ids = {m.group(2): int(m.group(1)) for m in re.finditer(r"\[#(\d+)\] (\w+) \| ", user)}
            return {"groups": []}
        if schema is writer.VERIFY_SCHEMA:
            # a claim is supported when its words appear verbatim in one of its passages
            import re
            verdicts = []
            for block in user.split("\n\n---\n\n"):
                m = re.match(r"CLAIM (\d+): (.*?)\nFLAGGED BECAUSE", block, re.S)
                claim = m.group(2).strip()
                found = None
                for pid, text in re.findall(r"\[(p\d+)\] from [^\n]*\n(.*?)(?=\n\n\[p\d+\]|\Z)", block, re.S):
                    if claim.lower() in text.lower():
                        found = pid
                verdicts.append({"n": int(m.group(1)), "status": "supported" if found else "unsupported",
                                 "passage": found or "", "quote": claim if found else "", "note": ""})
            return {"verdicts": verdicts}
        if schema is jobs.PARSE_SCHEMA:
            return {"is_job_ad": True, "title": "Product Owner", "company": "FinPay A/S", "company_website": "",
                    "location": "København", "work_mode": "hybrid", "employment_type": "full-time", "seniority": "senior",
                    "language": "da", "posted_at": "", "deadline": "2030-12-01", "deadline_note": "", "apply_url": "",
                    "apply_method": "email", "contact_name": "Mette Holm", "contact_email": "job@finpay.dk",
                    "contact_phone": "", "salary": "", "summary": "Lead the payments team.",
                    "responsibilities": ["Lede udviklingsteamet"], "requirements": ["Erfaring med Scrum", "Betalinger eller finans"],
                    "nice_to_have": ["SQL"], "benefits": [], "keywords": ["Scrum", "payments", "SQL", "Kubernetes"],
                    "company_blurb": "FinPay builds payment software.", "red_flags": [],
                    "ai_policy": "AI-skrevne ansøgninger læses ikke." if "ChatGPT" in user else ""}
        if schema is jobs.FIT_SCHEMA:
            ids = [int(x) for x in __import__("re").findall(r"\[#(\d+)\]", system)][:2]
            return {"score": 86, "verdict": "strong", "summary": "You lead payments products today.",
                    "matched": [{"requirement": "Erfaring med Scrum", "evidence": "Scrum in your skills", "fact_ids": ids + [99999]}],
                    "gaps": [], "dealbreakers": [], "angle": "Payments product owner with measurable results.", "questions": []}
        if schema is writer.LETTER_SCHEMA:
            return {"subject": "Ansøgning: Product Owner", "main_gap": "Kubernetes",
                    "gap_line": "Kubernetes har jeg ikke arbejdet med endnu.",
                    "letter": "Kære Mette\n\nJeg søger stillingen som product owner hos FinPay. I Danske Bank har jeg ansvaret for "
                              "betalingsfunktionerne i mobilbanken, og vi har fået supporthenvendelserne ned med 30 %.\n\n"
                              "Jeg har ledet et team på 12 udviklere. Ledte et team på 8 udviklere.\n\nMed venlig hilsen\nSara Lund",
                    "used_fact_ids": [1, 3], "assumed": ["You want to work with payments again."],
                    "angle": "Payments product owner with a measurable result.", "notes": ["Check the team size."]}
        if schema is writer.CV_SCHEMA:
            return {"cv_markdown": "# Product Owner, betalinger\nErfaren product owner.\n\n## Erfaring\n### Product Owner — Danske Bank\n"
                                   "2022 – nu · København\n- Reducerede supporthenvendelser med **30 %**\n\n## Kompetencer\nScrum · SQL",
                    "used_fact_ids": [1], "notes": []}
        if schema is writer.CHECK_SCHEMA:
            # a verdict per numbered item: team sizes aren't in the fake career database
            import re
            verdicts = []
            for n, where, text in re.findall(r"^\[(\d+)\] \((letter|cv)\) (.*)$", user, re.M):
                if text.startswith(("Kære", "Med venlig hilsen")):
                    verdicts.append({"n": int(n), "kind": "other", "status": "not_a_claim", "fact_ids": [], "problem": ""})
                elif "udviklere" in text:
                    verdicts.append({"n": int(n), "kind": "fact", "status": "unsupported", "fact_ids": [], "problem": "team size"})
                else:
                    verdicts.append({"n": int(n), "kind": "fact", "status": "supported", "fact_ids": [1, 99999], "problem": ""})
            return {"verdicts": verdicts}
        if schema is tracker.RESPONSE_SCHEMA:
            return {"type": "rejected_generic", "summary": "Standard rejection.", "feedback": "", "personalized": False,
                    "date": "", "interview_at": "", "next_step": ""}
        if schema is sources.TRIAGE_SCHEMA:
            return {"keep": [{"n": 0, "reason": "fits"}]}
        if schema is sources.PAGE_SCHEMA:
            return {"jobs": [{"n": 0, "title": "Job A", "company": "X", "location": ""}]}
        if schema is writer.FOLLOWUP_SCHEMA:
            return {"subject": "Opfølgning", "body": "Hej Mette, jeg følger lige op."}
        if schema is None:
            if "style guide" in system.lower():
                return "## Voice in brief\nShort, warm sentences."
            return "## Your 60-second pitch\nHello [#1]."
        raise AssertionError(f"FakeProvider got an unexpected schema: {list(schema.get('properties', {}))}")


@pytest.fixture()
def fake(tmp_path):
    config.configure(tmp_path / "data")
    db.init()
    settings.update("llm", {"writing": {"provider": "ollama", "model": "fake"}, "analysis": {"provider": "ollama", "model": "fake"}})
    settings.update("profile", {"name": "Sara Lund", "email": "sara@example.com", "city": "København"})
    # Never pick up a real ~/Nextcloud/Agents folder on the machine running the tests.
    settings.update("knowledge", {"folder": str(tmp_path / "no-agent-folder")})
    provider = FakeProvider()
    llm.set_test_provider(provider)
    yield provider
    llm.set_test_provider(None)


@pytest.fixture(autouse=True)
def no_robots_rules(monkeypatch):
    """Tests never fetch a real robots.txt: every site allows everything unless a test sets its own rules."""
    from career_agent import robots

    monkeypatch.setattr(robots, "rules_for", lambda url: [])
