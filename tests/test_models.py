"""Switching and redoing with other models: one-off model choice, Claude through Claude Code, retries, who made
what, and redoing work (one item or in bulk)."""
from __future__ import annotations

import json
import stat
import sys
from urllib.parse import unquote_plus

import pytest
from fastapi.testclient import TestClient

from career_agent import db, llm, redo, settings, tasks, writer
from career_agent.llm import providers
from career_agent.llm.errors import LLMConfigError, LLMTransient
from career_agent.web.app import app

from .conftest import approve_waiting

FAKE_CLAUDE = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
user = sys.stdin.read()
with open(os.environ["FAKE_OUT"], "w", encoding="utf-8") as fh:
    json.dump({"args": args, "system": system, "user": user, "api_key_seen": "ANTHROPIC_API_KEY" in os.environ,
               "claude_vars": sorted(k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC"))),
               "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")}, fh)
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "login":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}))
elif mode == "limit":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached"}))
else:
    out = {"type": "result", "subtype": "success", "is_error": False, "result": "Hello from Claude",
           "usage": {"input_tokens": 3, "cache_read_input_tokens": 7, "output_tokens": 2},
           "modelUsage": {"claude-haiku-5-5": {"outputTokens": 14}, "claude-sonnet-5-5": {"outputTokens": 63}}}
    if "--json-schema" in args:
        out["structured_output"] = {"ok": True}
    print(json.dumps(out))
'''


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE.replace("PYTHON", sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_OUT", str(tmp_path / "out.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-reach-claude-code")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_VERSION", "0.3.325")  # what a terminal inside the Claude app passes on
    return path, tmp_path / "out.json"


def test_claude_code_provider_runs_without_tools_and_without_the_api_key(fake_claude, monkeypatch):
    path, out = fake_claude
    prov = providers.ClaudeCodeProvider("sonnet", str(path), token="tok-123", effort="high")
    reply = prov.complete("SYSTEM PROMPT", "USER PROMPT", None, 4000)
    seen = json.loads(out.read_text())
    args = seen["args"]
    assert reply.text == "Hello from Claude" and reply.model == "claude-code/claude-sonnet-5-5"
    assert (reply.input_tokens, reply.cache_read, reply.output_tokens) == (3, 7, 2), "fresh and cached input kept apart"
    assert seen["system"] == "SYSTEM PROMPT" and seen["user"] == "USER PROMPT"
    assert args[args.index("--tools") + 1] == "" and "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--setting-sources") + 1] == "" and args[args.index("--model") + 1] == "claude-sonnet-5-5", \
        "a short name runs on the exact version it stood for"
    assert args[args.index("--effort") + 1] == "high"
    assert not seen["api_key_seen"] and seen["token"] == "tok-123"  # the subscription, never per-call billing
    assert seen["claude_vars"] == ["CLAUDE_CODE_OAUTH_TOKEN"], "nothing else from a Claude terminal's environment is passed on"

    structured = prov.complete("S", "U", {"type": "object", "properties": {"ok": {"type": "boolean"}}}, 4000)
    assert json.loads(structured.text) == {"ok": True} and "--json-schema" in json.loads(out.read_text())["args"]

    monkeypatch.setenv("FAKE_MODE", "login")
    with pytest.raises(LLMConfigError, match="logged in"):
        prov.complete("S", "U", None, 100)
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(LLMTransient):
        prov.complete("S", "U", None, 100)


def test_a_redo_can_use_another_model_than_settings(fake, fake_claude, monkeypatch):
    monkeypatch.setattr(llm, "_test_provider", None)
    path, _ = fake_claude
    settings.update("llm", {"claude_code_path": str(path)})
    assert isinstance(llm._build("writing", None), providers.OllamaProvider)
    with llm.using(model="ollama|gemma4:31b-mlx"):
        prov = llm._build("analysis", None)
        assert isinstance(prov, providers.OllamaProvider) and prov.model == "gemma4:31b-mlx"
    with llm.using(model="claude_code|opus"):
        prov = llm._build("writing", None)
        assert isinstance(prov, providers.ClaudeCodeProvider) and prov.model == "claude-opus-5-5"
    with llm.using(model="nonsense"):
        assert llm._build("writing", None).model == "fake"
    settings.update("llm", {"local_only": True})
    with llm.using(model="claude_code|opus"), pytest.raises(LLMConfigError, match="Local-only"):
        llm._build("writing", None)
    assert llm.parse_choice("claude_code|sonnet") == {"provider": "claude_code", "model": "sonnet"}
    assert llm.parse_choice("") is None and llm.parse_choice("evil|x") is None


def test_each_call_records_what_it_was_for(fake):
    with llm.using(ref="app:7"):
        llm.run("writing", "write_letter", "s", "u")
    made = llm.made_by("app:7", ("write_letter",))
    assert made["model"] == "fake-model" and made["label"] == "fake-model"
    assert llm.made_by("app:7", ("claim_check",)) is None and llm.made_by("app:8") is None
    assert llm.model_label("claude_code", "claude-code/claude-opus-5-5") == "Claude Opus 5.5 (Claude Code)"
    assert llm.model_label("ollama", "gemma4:31b") == "gemma4:31b (local)"

    db.insert("tasks", {"kind": "score_job", "payload": "{}", "ref": "job:3", "status": "done", "created_at": "2026-01-01T10:00:00",
                        "started_at": "2026-01-01T10:00:00", "finished_at": "2026-01-01T10:05:00"})
    db.insert("llm_calls", {"purpose": "fit_score", "provider": "ollama", "model": "gemma4:26b", "ok": 1,
                            "created_at": "2026-01-01T10:03:00"})
    assert llm.backfill_refs() == 1 and llm.made_by("job:3")["model"] == "gemma4:26b"


def test_passing_errors_are_retried_later_then_reported(fake, monkeypatch):
    calls = []

    def boom(payload):
        calls.append(payload)
        raise LLMTransient("Server disconnected without sending a response.")

    monkeypatch.setattr(tasks, "_handlers", lambda: {"boom": boom, "config": lambda p: (_ for _ in ()).throw(
        LLMConfigError("Claude Code wasn't found on this Mac."))})
    tid = db.insert("tasks", {"kind": "boom", "payload": "{}", "ref": "x", "status": "queued", "created_at": db.now()})
    for attempt in range(1, len(tasks.RETRY_MINUTES) + 1):
        tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (tid,)))
        row = db.q1("SELECT * FROM tasks WHERE id = ?", (tid,))
        assert (row["status"], row["attempts"]) == ("queued", attempt) and row["run_after"] > db.now()
        assert "trying again at" in row["error"]
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (tid,)))
    assert db.q1("SELECT status FROM tasks WHERE id = ?", (tid,))["status"] == "error" and len(calls) == 4

    cid = db.insert("tasks", {"kind": "config", "payload": "{}", "status": "queued", "created_at": db.now()})
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (cid,)))
    assert db.q1("SELECT status, attempts FROM tasks WHERE id = ?", (cid,)) == {"status": "error", "attempts": 0}
    assert tasks.is_transient(LLMConfigError("Ollama isn't running. Open the Ollama app."))


def test_the_model_choice_reaches_the_task(fake, monkeypatch):
    seen = []
    monkeypatch.setattr(tasks, "_handlers", lambda: {"probe": lambda p: seen.append(llm._ctx.get())})
    tid = db.insert("tasks", {"kind": "probe", "payload": json.dumps({"model": "ollama|bigger"}), "ref": "doc:1",
                              "status": "queued", "created_at": db.now()})
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (tid,)))
    assert seen == [{"ref": "doc:1", "override": {"provider": "ollama", "model": "bigger"}}]
    assert llm._ctx.get() == {}  # nothing leaks to the next task


def _app_with_draft(letter="Kære FinPay\n\nOld letter.\n\nMed venlig hilsen\nSara Lund"):
    from career_agent import jobs

    from .conftest import JOB_TEXT

    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)
    app_id = db.insert("applications", {"job_id": job_id, "status": "draft", "letter": letter, "cv": "# CV",
                                        "created_at": db.now(), "updated_at": db.now()})
    return job_id, app_id


def test_a_new_draft_keeps_the_old_one(fake):
    from career_agent import ingest, profile

    from .conftest import CV_TEXT

    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    profile.analyze_document(doc_id)
    approve_waiting()
    _, app_id = _app_with_draft()
    writer.draft(app_id)
    labels = [r["label"] for r in db.q("SELECT label FROM app_versions WHERE application_id = ? ORDER BY id", (app_id,))]
    assert labels == ["Before a new draft", "New draft"]
    assert "Old letter" in db.q1("SELECT letter FROM app_versions WHERE label = 'Before a new draft'")["letter"]


def test_redo_in_bulk_and_per_item(fake):
    from career_agent import ingest

    read_doc = ingest.import_bytes("a.txt", b"Product owner in a bank for many years, payments and Scrum teams.")[0]
    folder_doc = ingest.import_bytes("b.txt", b"Imported from the agent folder: facts came with it, no model read it.")[0]
    db.ex("UPDATE documents SET status = 'analyzed'")
    db.insert("llm_calls", {"purpose": "analyze_document", "provider": "ollama", "model": "gemma4:26b", "ok": 1,
                            "ref": f"doc:{read_doc}", "created_at": db.now()})
    job_id, app_id = _app_with_draft()
    db.insert("llm_calls", {"purpose": "write_letter", "provider": "ollama", "model": "gemma4:31b", "ok": 1,
                            "ref": f"app:{app_id}", "created_at": db.now()})

    found = redo.candidates()
    assert found["documents"] == [f"doc:{read_doc}"]  # the agent folder's documents aren't re-read
    assert found["drafts"] == [f"app:{app_id}"] and redo.candidates("gemma4:26b")["drafts"] == []
    assert redo.models_used()[0][0] in ("gemma4:26b", "gemma4:31b")

    counts = redo.enqueue(["documents", "drafts", "checks"], model="claude_code|sonnet")
    assert counts == {"documents": 1, "drafts": 1, "checks": 0}  # a new draft is checked as it's written
    doc_task = db.q1("SELECT payload FROM tasks WHERE kind = 'analyze_document' AND ref = ?", (f"doc:{read_doc}",))
    assert json.loads(doc_task["payload"]) == {"doc_id": read_doc, "replace": True, "model": "claude_code|sonnet"}

    c = TestClient(app)
    r = c.post(f"/jobs/{job_id}/rescore", data={"model": "ollama|gemma4:31b-mlx"}, follow_redirects=False)
    assert "Scoring again with gemma4:31b-mlx (local)" in unquote_plus(r.headers["location"])
    task = db.q1("SELECT payload FROM tasks WHERE kind = 'score_job' ORDER BY id DESC LIMIT 1")
    assert json.loads(task["payload"]) == {"job_id": job_id, "model": "ollama|gemma4:31b-mlx"}
    c.post(f"/documents/{folder_doc}/reanalyze", data={"model": ""}, follow_redirects=False)
    plain = db.q1("SELECT payload FROM tasks WHERE kind = 'analyze_document' AND ref = ?", (f"doc:{folder_doc}",))
    assert json.loads(plain["payload"]) == {"doc_id": folder_doc, "replace": False}
    r = c.post("/redo", data={"model": "ollama|gemma4:31b-mlx", "what_jobs": "1"}, follow_redirects=False)
    assert r.headers["location"].startswith("/tasks")
    assert "Tick at least one" in unquote_plus(c.post("/redo", data={"model": ""}, follow_redirects=False).headers["location"])

    for url in ("/settings", f"/applications/{app_id}", f"/jobs/{job_id}", f"/documents/{read_doc}", "/profile?tab=style"):
        assert c.get(url).status_code == 200, url
    assert "Claude through Claude Code" in c.get("/settings").text


def test_analyze_with_replace_keeps_ticked_and_hidden_facts(fake):
    from career_agent import ingest, profile

    from .conftest import CV_TEXT

    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    profile.analyze_document(doc_id)
    approve_waiting()
    first = db.q("SELECT id FROM facts WHERE document_id = ? ORDER BY id", (doc_id,))
    keep, hide = first[0]["id"], first[1]["id"]
    db.update("facts", keep, {"verified": 1})
    db.update("facts", hide, {"hidden": 1})
    db.insert("facts", {"document_id": doc_id, "category": "skill", "title": "Made-up skill from a weak reading",
                        "created_at": db.now()})
    profile.analyze_document(doc_id, replace=True)
    approve_waiting()
    titles = {r["title"] for r in db.q("SELECT title FROM facts WHERE document_id = ?", (doc_id,))}
    assert "Made-up skill from a weak reading" not in titles
    assert db.q1("SELECT verified FROM facts WHERE id = ?", (keep,))["verified"] == 1
    assert db.q1("SELECT hidden FROM facts WHERE id = ?", (hide,))["hidden"] == 1


def test_each_step_can_have_its_own_model_and_effort(fake, fake_claude, monkeypatch):
    monkeypatch.setattr(llm, "_test_provider", None)
    path, _ = fake_claude
    settings.update("llm", {"claude_code_path": str(path), "writing": {"provider": "claude_code", "model": "claude-opus-5-5", "effort": "high"},
                            "analysis": {"provider": "ollama", "model": "gemma4:26b-mlx", "effort": "medium"}})

    def built(tier, purpose, effort=None):
        p = llm._build(tier, effort, purpose)
        return type(p).__name__.replace("Provider", ""), p.model, getattr(p, "effort", None)

    assert built("writing", "write_letter") == ("ClaudeCode", "claude-opus-5-5", "high"), "the tier's model and effort"
    assert built("writing", "revise_letter", "medium") == ("ClaudeCode", "claude-opus-5-5", "medium"), "the step's usual effort"
    assert built("analysis", "fit_score", "medium")[:2] == ("Ollama", "gemma4:26b-mlx")

    cfg = settings.get("llm")
    cfg["steps"] = {"score": {"provider": "claude_code", "model": "claude-sonnet-5-5", "effort": "low"}, "letter": {"effort": "xhigh"}}
    settings.set("llm", cfg)
    assert built("analysis", "fit_score", "medium") == ("ClaudeCode", "claude-sonnet-5-5", "low"), "its own model and effort"
    assert built("analysis", "parse_job", "low")[:2] == ("Ollama", "gemma4:26b-mlx"), "other steps are untouched"
    assert built("writing", "write_letter") == ("ClaudeCode", "claude-opus-5-5", "xhigh"), "only the effort was set for the letter"
    assert built("writing", "revise_letter", "medium") == ("ClaudeCode", "claude-opus-5-5", "xhigh"), "your setting beats the usual effort"
    assert built("writing", "write_cv") == ("ClaudeCode", "claude-opus-5-5", "high")
    with llm.using(model="||max"):  # one run at another effort, same model
        assert built("writing", "write_letter") == ("ClaudeCode", "claude-opus-5-5", "max")
    with llm.using(model="claude_code|claude-haiku-5-5|medium"):
        assert built("analysis", "fit_score", "medium") == ("ClaudeCode", "claude-haiku-5-5", "medium")

    assert llm.step_provider("score") == "claude_code" and llm.step_provider("read_ad") == "ollama"
    assert llm.step_provider("fact_check") == "claude_code", "a cloud writer also checks the facts"
    assert built("writing", "voice_read", "medium") == ("ClaudeCode", "claude-opus-5-5", "medium"), \
        "the voice read is its own step: left on the usual, the Writing model at medium"
    cfg["steps"]["fact_check"] = {"provider": "claude_code", "model": "claude-sonnet-5-5"}
    settings.set("llm", cfg)
    assert built("check", "claim_check")[1] == "claude-sonnet-5-5" and built("writing", "voice_read")[1] == "claude-opus-5-5", \
        "a model for the fact check isn't the voice read's"
    cfg["steps"]["voice"] = {"provider": "claude_code", "model": "claude-sonnet-5-5", "effort": "high"}
    settings.set("llm", cfg)
    assert built("writing", "voice_read", "medium") == ("ClaudeCode", "claude-sonnet-5-5", "high")
    assert tasks.needs({"kind": "read_voice", "payload": "{}"}) == {"claude_code"}
    assert llm.step_provider("read_ad", "claude_code|claude-haiku-5-5") == "claude_code"
    assert tasks.needs({"kind": "score_job", "payload": "{}"}) == {"claude_code"}
    assert tasks.needs({"kind": "parse_job", "payload": "{}"}) == {"ollama", "network"}
    assert tasks.concurrency() == 1, "a local model anywhere means one task at a time"
    assert llm.parse_choice("claude_code|claude-opus-5-5|high") == {"provider": "claude_code", "model": "claude-opus-5-5", "effort": "high"}
    assert llm.parse_choice("||high") == {"effort": "high"} and llm.parse_choice("||silly") is None
    assert {p for _, _, _, ps in llm.STEPS for p in ps} >= {"triage", "parse_job", "fit_score", "write_letter", "write_cv",
                                                            "claim_check", "style_guide", "interview_prep", "classify_response",
                                                            "voice_read", "place_answers"}


def test_effort_reaches_claude_and_can_be_picked_everywhere(fake, fake_claude, monkeypatch):
    from career_agent.web import app as web

    assert web.chosen_model({"model": "claude_code|claude-opus-5-5", "effort": "high"}) == {"model": "claude_code|claude-opus-5-5|high"}
    assert web.chosen_model({"model": "", "effort": "xhigh"}) == {"model": "||xhigh"}
    assert web.chosen_model({"model": "claude_code|claude-opus-5-5", "effort": ""}) == {"model": "claude_code|claude-opus-5-5"}
    assert web.chosen_model({"model": "", "effort": ""}) == {} and web.chosen_model({"effort": "bogus"}) == {}
    assert web.with_model_msg({"model": "claude_code|claude-opus-5-5", "effort": "high"}, "Writing") == "Writing with Claude Opus 5.5 (Claude Code) at high effort"
    assert web.with_model_msg({"effort": "low"}, "Writing") == "Writing with low effort"

    path, out = fake_claude
    monkeypatch.setattr(llm, "_test_provider", None)
    settings.update("llm", {"claude_code_path": str(path), "writing": {"provider": "claude_code", "model": "claude-opus-5-5", "effort": "high"}})
    with llm.using(ref="app:1", model="||medium"):
        llm.run("writing", "write_letter", "S", "U")
    args = json.loads(out.read_text())["args"]
    assert args[args.index("--model") + 1] == "claude-opus-5-5" and args[args.index("--effort") + 1] == "medium"

    c = TestClient(app)
    page = c.get("/settings")
    assert page.status_code == 200 and "Model and effort for each step" in page.text and 'name="letter_effort"' in page.text
    c.post("/settings/steps", data={"letter_effort": "xhigh", "score_model": "claude_code|claude-sonnet-5-5", "score_effort": "low",
                                    "cv_model": "", "cv_effort": ""}, follow_redirects=False)
    assert settings.get("llm")["steps"] == {"letter": {"effort": "xhigh"},
                                            "score": {"provider": "claude_code", "model": "claude-sonnet-5-5", "effort": "low"}}
    c.post("/settings/steps", data={"letter_effort": ""}, follow_redirects=False)
    assert settings.get("llm")["steps"] == {}, "a step set back to the usual is cleared"


def test_the_letter_or_the_cv_can_be_written_again_alone(fake):
    from .test_cv import _draft

    a = _draft()
    writer.draft(a["id"], "letter")
    after = db.q1("SELECT * FROM applications WHERE id = ?", (a["id"],))
    assert after["cv_data"] == a["cv_data"] and after["cv"] == a["cv"], "the CV is left alone"
    assert any("Only the letter was written again" in n for n in db.jload(after["writer_notes"]))
    db.update("applications", a["id"], {"letter": "Min egen tekst."})
    writer.draft(a["id"], "cv")
    assert db.q1("SELECT letter FROM applications WHERE id = ?", (a["id"],))["letter"] == "Min egen tekst."

    c = TestClient(app)
    c.post(f"/applications/{a['id']}/redraft", data={"part": "cv", "model": "", "effort": "high"}, follow_redirects=False)
    queued = db.q1("SELECT payload FROM tasks WHERE kind = 'draft_application' AND status = 'queued'")
    assert json.loads(queued["payload"]) == {"application_id": a["id"], "part": "cv", "model": "||high"}
    c.post(f"/applications/{a['id']}/revise", data={"part": "letter", "instructions": "Kortere.", "model": "claude_code|claude-opus-5-5", "effort": "low"},
           follow_redirects=False)
    assert json.loads(db.q1("SELECT payload FROM tasks WHERE kind = 'revise_application'")["payload"])["model"] == "claude_code|claude-opus-5-5|low"


def test_claude_code_models_saved_by_short_name_get_their_exact_version(fake):
    settings.update("llm", {"writing": {"provider": "claude_code", "model": "opus", "effort": "high"},
                            "analysis": {"provider": "claude_code", "model": "sonnet", "effort": "high"},
                            "steps": {"score": {"provider": "claude_code", "model": "haiku", "effort": "low"},
                                      "screen": {"provider": "ollama", "model": "opus", "effort": "low"}}})
    db.connect().executescript(db.MIGRATIONS[10])  # migration 11 (later ones add columns, so can't run twice)
    cfg = settings.get("llm")
    assert cfg["writing"]["model"] == "claude-opus-5-5" and cfg["analysis"]["model"] == "claude-sonnet-5-5"
    assert cfg["steps"]["score"]["model"] == "claude-haiku-5-5"
    assert cfg["steps"]["screen"]["model"] == "opus", "only Claude Code models are touched"
    assert all(m.startswith("claude-") for m, _ in providers.CLAUDE_CODE_MODELS), "every choice names its version"


def test_every_claude_code_version_is_named_in_full():
    assert llm.model_label("claude_code", "claude-code/claude-opus-4-8") == "Claude Opus 4.8 (Claude Code)"
    assert llm.model_label("ollama", "gemma4:31b") == "gemma4:31b (local)"
    assert providers.claude_name("claude-opus-5") == "Claude Opus 5" and providers.claude_name("gemma4:31b") == "gemma4:31b"
    every = providers.CLAUDE_CODE_MODELS + providers.CLAUDE_CODE_EARLIER
    assert all(label.startswith(providers.claude_name(m)) for m, label in every), "each label shows its exact version"
    assert len({m for m, _ in every}) == len(every)
