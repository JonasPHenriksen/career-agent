"""The Claude models your subscription offers through Claude Code: found in the program, checked through the plan,
and checked again when Claude Code is updated or a week has passed."""
from __future__ import annotations

import stat
import sys

import pytest
from fastapi.testclient import TestClient

from career_agent import db, settings, tasks
from career_agent.llm import plan
from career_agent.llm.errors import LLMTransient
from career_agent.web.app import app

# A pretend Claude Code. Its own text holds the model IDs, like the real program does; how each one answers is
# in ANSWERS: "self" = the model itself answers, "other" = another model answers in its place, missing = not on the plan.
FAKE = r'''#!PYTHON
import json, os, sys
ANSWERS = {"claude-opus-6": "self", "claude-opus-5-5": "self", "claude-sonnet-5-5": "self", "claude-sonnet-5": "self",
           "claude-haiku-5-5": "self", "claude-mythos-5-2": "self", "claude-opus-4-1": "other", "claude-code-2": "self",
           "claude-sonnet-3-7": "self", "claude-opus-4-1-20250805": "self"}
args = sys.argv[1:]
model = args[args.index("--model") + 1]
if os.environ.get("FAKE_MODE") == "limit":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached"}))
elif ANSWERS.get(model) is None:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": f"There's an issue with the selected model ({model}). It may not exist or you may not have access to it."}))
else:
    used = model if ANSWERS[model] == "self" else "claude-opus-5-5"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "OK",
                      "usage": {"input_tokens": 3, "output_tokens": 1}, "modelUsage": {used: {"outputTokens": 1}}}))
'''


@pytest.fixture()
def fake_code(tmp_path, fake):  # `fake`: a throwaway data folder, never your own
    path = tmp_path / "claude"
    path.write_text(FAKE.replace("PYTHON", sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    settings.update("llm", {"claude_code_path": str(path),
                            "writing": {"provider": "claude_code", "model": "claude-opus-5-5", "effort": "high"}})
    return path


def test_the_program_names_the_candidates(fake_code):
    found = plan.candidates(str(fake_code))
    assert {"claude-opus-6", "claude-mythos-5-2", "claude-opus-4-1"} <= set(found)
    assert "claude-code-2" not in found and "claude-sonnet-3-7" not in found, "not models, or retired"
    assert "claude-opus-4-1-20250805" not in found, "dated IDs are the same models"
    assert "claude-fable-5-2" in found, "today's known versions are always asked"


def test_the_plan_keeps_what_answers_as_itself(fake_code, monkeypatch):
    sent = []
    monkeypatch.setattr("career_agent.notify.send", lambda title, msg: sent.append((title, msg)))
    result = plan.check()
    assert result["available"] == ["claude-haiku-5-5", "claude-mythos-5-2", "claude-opus-5-5", "claude-opus-6",
                                   "claude-sonnet-5", "claude-sonnet-5-5"]
    assert "claude-opus-4-1" not in result["available"], "answered by another model, so not what you'd pick"
    assert sent == [], "the first check only sets the list"
    newest, earlier = plan.choices()
    assert newest == [("claude-opus-6", "Claude Opus 6 (best writing)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5 (fast, for analysis)"),
                      ("claude-haiku-5-5", "Claude Haiku 5.5 (fastest, lightest on your plan)"), ("claude-mythos-5-2", "Claude Mythos 5.2")]
    assert earlier == [("claude-opus-5-5", "Claude Opus 5.5"), ("claude-sonnet-5", "Claude Sonnet 5")]
    assert plan.status()["count"] == 6

    # The plan changes: Opus 5.5 (in use) goes, Fable 5.2 comes.
    text = fake_code.read_text().replace('"claude-opus-5-5": "self"', '"claude-fable-5-2": "self"')
    fake_code.write_text(text)
    result = plan.check()
    assert result["new"] == ["claude-fable-5-2"] and result["gone"] == ["claude-opus-5-5"]
    assert [t for t, _ in sent] == ["New Claude model on your plan", "A Claude model you use is gone"]


def test_a_usage_limit_keeps_the_last_list(fake_code, monkeypatch):
    plan.check()
    before = settings.get("state")["claude_models"]
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(LLMTransient):
        plan.check()
    assert settings.get("state")["claude_models"] == before


def test_checked_again_when_claude_code_is_updated_or_a_week_has_passed(fake_code):
    assert plan.due(), "never checked"
    plan.check()
    assert not plan.due(), "just checked"
    settings.update("state", {"claude_models_tried_at": "2000-01-01T00:00:00"})
    assert not plan.due()
    with open(fake_code, "a") as fh:
        fh.write("# a newer version\n")
    assert plan.due(), "Claude Code was updated"
    plan.check()
    settings.update("state", {"claude_models_tried_at": "2000-01-01T00:00:00"})
    last = settings.get("state")["claude_models"]
    settings.update("state", {"claude_models": {**last, "checked_at": db.days_ago(8)}})
    assert plan.due(), "a week has passed"
    settings.update("state", {"claude_models_tried_at": db.now()})
    assert not plan.due(), "a check that couldn't finish waits a few hours"
    settings.update("llm", {"writing": {"provider": "ollama", "model": "gemma4:31b"}})
    settings.update("state", {"claude_models_tried_at": "2000-01-01T00:00:00"})
    assert not plan.due(), "no check when Claude Code isn't used"


def test_settings_shows_the_plan_and_checks_again_on_request(fake_code):
    plan.check()
    c = TestClient(app)
    page = c.get("/settings").text
    assert "Claude Opus 6 (best writing)" in page and "Claude Code: earlier versions" in page
    assert "Models on your plan:" in page and "6 versions" in page
    assert "claude-opus-5-5 (not on your plan now)" not in page, "Opus 5.5 is still on the plan, as an earlier version"
    c.post("/settings/claude-models", follow_redirects=False)
    queued = db.q("SELECT kind FROM tasks WHERE status = 'queued'")
    assert [t["kind"] for t in queued] == ["check_claude_models"]
    assert tasks.needs({"kind": "check_claude_models", "payload": "{}"}) == {"claude_code"}
