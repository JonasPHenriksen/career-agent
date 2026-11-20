"""Ollama's models in Settings follow what's installed, also while Ollama isn't running; and there's no Claude API."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from career_agent import llm, settings
from career_agent.llm import providers
from career_agent.web.app import app


def _install(root, name: str, tag: str, config: dict, namespace: str = "library", size: int = 1000) -> None:
    digest = f"sha256:{abs(hash((name, tag))):064x}"[:71]
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    (root / "blobs" / digest.replace(":", "-")).write_text(json.dumps(config))
    folder = root / "manifests" / "registry.ollama.ai" / namespace / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / tag).write_text(json.dumps({"config": {"digest": digest}, "layers": [{"size": size}, {"size": 5}]}))


def test_installed_models_are_read_from_ollamas_folder(tmp_path, monkeypatch):
    _install(tmp_path, "gemma4", "26b-mlx", {"capabilities": ["completion", "vision"]}, size=18_000)
    _install(tmp_path, "bge-m3", "latest", {"model_family": "bert"})
    _install(tmp_path, "qwen9", "latest", {"capabilities": ["completion"]}, namespace="someone")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    models = providers.installed_ollama_models()
    assert [m["name"] for m in models] == ["bge-m3:latest", "gemma4:26b-mlx", "someone/qwen9:latest"]
    assert models[1]["size"] == 18_005 and models[0]["details"]["family"] == "bert"
    assert [m["name"] for m in llm._chat_models(models)] == ["gemma4:26b-mlx", "someone/qwen9:latest"], \
        "the search model isn't offered for writing"


def test_settings_lists_installed_models_while_ollama_is_stopped(fake, tmp_path, monkeypatch):
    _install(tmp_path, "gemma4", "31b-mlx", {"capabilities": ["completion"]})
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    settings.update("llm", {"ollama_url": "http://127.0.0.1:9"})  # nothing answers there: Ollama is stopped
    llm._choices_cache["at"] = 0.0
    st = llm.status()
    assert not st["ollama_running"] and st["ollama_chat_models"] == ["gemma4:31b-mlx"]
    page = TestClient(app).get("/settings").text
    assert 'value="ollama|gemma4:31b-mlx"' in page and "1 model installed" in page
    assert "No Ollama models found" not in page

    # Removed from disk: gone from the list at once.
    (tmp_path / "manifests" / "registry.ollama.ai" / "library" / "gemma4" / "31b-mlx").unlink()
    llm._choices_cache["at"] = 0.0
    assert llm.status()["ollama_chat_models"] == []
    assert all(group != "On this Mac (Ollama)" for group, _ in llm.model_choices())


def test_there_is_no_claude_api_any_more(fake):
    page = TestClient(app).get("/settings").text
    assert "Anthropic API key" not in page and "Claude API" not in page and "anthropic|" not in page and "/settings/key" not in page
    assert "anthropic" not in llm.PROVIDERS
    assert TestClient(app).post("/settings/key", data={"provider": "anthropic", "key": "x" * 30}).status_code in (404, 405)


def test_old_short_names_are_shown_as_the_version_they_ran_on():
    assert llm.model_label("claude_code", "sonnet") == "Claude Sonnet 5.5 (Claude Code)"


def test_a_local_model_thinks_when_you_switch_it_on_on_any_step(fake, monkeypatch):
    monkeypatch.setattr(llm, "_test_provider", None)
    settings.update("llm", {"writing": {"provider": "ollama", "model": "gemma4:31b-mlx", "effort": "high"},
                            "analysis": {"provider": "ollama", "model": "gemma4:26b-mlx", "effort": "medium"}})
    assert llm._build("writing", None, "write_letter").think, "writing thinks by default, as before"
    assert not llm._build("writing", "medium", "revise_letter").think, "a step that usually goes quicker doesn't"
    assert not llm._build("analysis", "medium", "fit_score").think
    cfg = settings.get("llm")
    cfg["steps"] = {"score": {"effort": "high"}, "letter": {"effort": "low"}}
    settings.set("llm", cfg)
    assert llm._build("analysis", "medium", "fit_score").think, "thinking switched on for an analysis step works now"
    assert not llm._build("writing", None, "write_letter").think, "and switched off for a writing step"
    assert [llm.effort_kind(p) for p in ("claude_code", "ollama", "openai")] == ["claude", "local", "none"]


def test_effort_choices_fit_each_steps_model(fake, tmp_path, monkeypatch):
    _install(tmp_path, "gemma4", "26b-mlx", {"capabilities": ["completion"]})
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    settings.update("llm", {"ollama_url": "http://127.0.0.1:9",
                            "writing": {"provider": "claude_code", "model": "claude-opus-5-5", "effort": "high"},
                            "analysis": {"provider": "ollama", "model": "gemma4:26b-mlx", "effort": "medium"},
                            "steps": {"letter": {"provider": "ollama", "model": "gemma4:26b-mlx", "effort": "max"},
                                      "cv": {"provider": "openai", "model": "qwen"}}})
    page = TestClient(app).get("/settings").text

    def effort_select(step):
        start = page.index(f'id="effort_{step}"')
        return page[start:page.index("</select>", start)]

    score = effort_select("score")  # the Analysis model is local
    assert "thinking off" in score and "usual (thinking off)" in score and ">xhigh<" not in score
    letter = effort_select("letter")  # its own local model, effort max = thinking on
    assert 'value="high" selected>thinking on' in letter
    assert "disabled" in effort_select("cv") and "no effort setting" in effort_select("cv")
    prep = effort_select("prep")  # the Writing model is Claude
    options = prep[prep.index("<option"):]
    assert ">xhigh<" in options and "usual (high)" in options and "thinking" not in options
    assert 'data-usual-local="usual (thinking on)"' in prep, "ready for when you pick a local model"
    import re
    assert all("autocomplete=\"off\"" in s for s in re.findall(r'<select[^>]*name="\w+_(?:model|effort)"[^>]*>', page)), \
        "a reload never brings back old choices"
