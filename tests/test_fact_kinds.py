"""The reorganised career database: results live inside their project, old kinds are filed under new ones, merged
facts don't come back, and skills carry an area and how well you know them."""
from __future__ import annotations

from fastapi.testclient import TestClient

from career_agent import db, profile, settings
from career_agent.web.app import app
from .conftest import approve_waiting


def _fact(category: str, title: str, **kw) -> int:
    return db.insert("facts", {"category": category, "title": title, "created_at": db.now(), "verified": 1, **kw})


def test_old_kinds_are_filed_under_the_new_ones(fake):
    profile.save_facts(None, [
        {"category": "value", "title": "Likes clear plans"},
        {"category": "personal", "title": "Driving licence B"},
        {"category": "volunteering", "title": "Board member, chess club"},
        {"category": "achievement", "title": "Won the hackathon", "organization": "Acme"},
        {"category": "utility", "title": "Display switcher script"},
        {"category": "no_such_kind", "title": "Ignored"},
    ])
    approve_waiting()
    kinds = {f["title"]: f["category"] for f in db.q("SELECT title, category FROM facts")}
    assert kinds == {"Likes clear plans": "about", "Driving licence B": "about", "Board member, chess club": "about",
                     "Won the hackathon": "project", "Display switcher script": "utility"}


def test_a_result_the_project_already_states_adds_nothing(fake):
    _fact("project", "Transport booking module", organization="Acme P/S",
          detail="Replaced two systems. Result: saves the customer approx. DKK 100,000 a year.")
    added, merged = profile.save_facts(None, [{"category": "achievement", "title": "Saves DKK 100,000 annually",
                                               "organization": "Acme"}])
    approve_waiting()
    assert (added, merged) == (0, 1)
    added, _ = profile.save_facts(None, [{"category": "achievement", "title": "Cut build time by 40%", "organization": "Acme"}])
    approve_waiting()
    assert added == 1, "a result no entry states yet is kept, as a project for you to file"


def test_merged_and_removed_facts_do_not_come_back(fake):
    keep = _fact("project", "EDI setup agent", organization="Acme")
    profile.remember_absorbed(keep, ["X++ action tools for the EDI setup agent", "Demo of the EDI setup agent"])
    profile.remember_absorbed(keep, ["Demo of the EDI setup agent"])  # twice is harmless
    assert len(db.jload(db.q1("SELECT absorbed FROM facts WHERE id = ?", (keep,))["absorbed"])) == 2
    settings.set("dismissed_facts", [profile._norm("Interest in green solutions")])
    added, merged = profile.save_facts(None, [
        {"category": "project", "title": "X++ Action Tools for the EDI Setup Agent", "organization": "Acme"},
        {"category": "achievement", "title": "Demo of the EDI setup agent", "organization": "Acme"},
        {"category": "value", "title": "Interest in green solutions"},
        {"category": "project", "title": "Something new", "organization": "Acme"},
    ])
    approve_waiting()
    assert (added, merged) == (1, 3)
    assert db.scalar("SELECT COUNT(*) FROM facts") == 2


def test_skills_are_grouped_by_area_and_say_how_well_you_know_them(fake):
    go = _fact("skill", "Go", area="Languages", depth="work", detail="Microservices in an internship.")
    k8s = _fact("skill", "Kubernetes", area="DevOps and infrastructure", depth="insight")
    _fact("skill", "Python", area="Languages", depth="project")
    _fact("skill", "Juggling")
    sections = profile.by_area(db.q("SELECT * FROM facts WHERE category = 'skill'"))
    assert [(area, [f["title"] for f in group]) for area, _, group in sections] == [
        ("Languages", ["Go", "Python"]), ("DevOps and infrastructure", ["Kubernetes"]), ("Other", ["Juggling"])]
    digest = profile.facts_digest(10**6)
    assert f"[#{go}] Go [used at work]: Microservices" in digest and f"[#{k8s}] Kubernetes [insight only]" in digest
    page = TestClient(app).get("/profile?tab=facts").text
    assert "DevOps and infrastructure" in page and "How well you know it" in page
    r = TestClient(app).post(f"/facts/{k8s}", data={"title": "Kubernetes", "detail": "", "area": "DevOps and infrastructure",
                                                     "depth": "coursework"}, follow_redirects=False)
    assert r.status_code in (302, 303) and db.q1("SELECT depth FROM facts WHERE id = ?", (k8s,))["depth"] == "coursework"


def test_a_project_is_shown_whole_to_the_writer(fake):
    long = "What it was. " * 30 + "Your part. " * 20 + "Result: 51/51 checks passed."
    pid = _fact("project", "EDI setup agent", detail=long)
    uid = _fact("utility", "Display switcher", detail="x" * 600)
    digest = profile.facts_digest(10**6)
    assert "Result: 51/51 checks passed." in digest, "a project's result at the end of its text isn't cut off"
    assert "x" * 400 in digest and "x" * 401 not in digest, "a smaller program gets a shorter text"
    assert digest.index("## Projects") < digest.index("## Smaller programs and utilities") and f"[#{pid}]" in digest and f"[#{uid}]" in digest
