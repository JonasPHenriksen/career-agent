"""Provider-agnostic model access.

Two tiers, each mapped to a provider + model in Settings:
  - "writing":  cover letters, CVs, interview prep, style guide (quality matters most)
  - "analysis": document extraction, job parsing, fit scoring, claim checks (volume work)
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from dataclasses import dataclass
from typing import Any

from .. import db, keychain, privacy, settings
from . import jsonutil
from .errors import LLMConfigError, LLMError, LLMRefusal, LLMTransient
from . import plan
from .providers import installed_ollama_models as installed_ollama
from .providers import (CLAUDE_CODE_ALIASES, claude_name, ClaudeCodeProvider, OllamaProvider,
                        ProviderReply, find_claude_binary, is_local_url, list_ollama_models,
                        ollama_models)

__all__ = ["run", "LLMResponse", "LLMError", "LLMConfigError", "LLMRefusal", "LLMTransient", "jsonutil", "using",
           "made_by", "model_label", "model_choices", "parse_choice", "backfill_refs", "PROVIDERS",
           "set_test_provider", "status", "tier_provider", "ollama_running", "EFFORTS", "thinks", "effort_kind", "STEPS", "STEP_OF", "step_provider", "step_settings", "detect_defaults", "bootstrap", "test_tier",
           "ProviderReply", "list_ollama_models"]

CLAUDE_CODE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
PROVIDERS = ("claude_code", "ollama")

# What the current task is about ("app:12", "doc:3"…) and, for a redo, which model to use instead of Settings.
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("llm_context", default={})


@contextlib.contextmanager
def using(ref: str | None = None, model: str | None = None):
    """Run model calls for `ref` (recorded with each call, so you can see which model made what) and, if `model`
    is a choice like "claude_code|claude-sonnet-5-5" or "ollama|gemma4:31b", with that model instead of the configured ones."""
    token = _ctx.set({"ref": ref, "override": parse_choice(model)})
    try:
        yield
    finally:
        _ctx.reset(token)


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def thinks(effort: str | None) -> bool:
    """For a local model, effort only says whether it thinks before answering."""
    return effort in ("high", "xhigh", "max")


def effort_kind(provider: str | None) -> str:
    """What the effort choice means for this provider: "claude" (five levels) or "local" (thinking on/off)."""
    return "local" if provider == "ollama" else "claude"

# What the app does, step by step: (key, label, the tier whose model it uses by default, its model calls).
# Settings lets you pick another model and effort for any step; a redo can pick one for a single run.
STEPS = [
    ("screen", "Screen new job listings", "analysis", ("triage", "page_jobs", "company_size")),
    ("read_ad", "Read a job ad", "analysis", ("parse_job",)),
    ("score", "Score how well a job fits", "analysis", ("fit_score",)),
    ("read_document", "Read your documents into facts", "analysis", ("analyze_document", "consolidate_facts", "place_answers")),
    ("reply", "Read an employer's reply", "analysis", ("classify_response",)),
    ("letter", "Write and revise the letter", "writing", ("write_letter", "revise_letter", "followup")),
    ("cv", "Tailor and revise the CV", "writing", ("write_cv", "revise_cv", "cv_base")),
    ("fact_check", "Fact-check a draft", "check", ("claim_check", "verify_claims")),
    ("voice", "Read a letter for your voice", "writing", ("voice_read",)),
    ("style", "Write your style guide", "writing", ("style_guide",)),
    ("prep", "Prepare you for an interview", "writing", ("interview_prep",)),
]
STEP_OF = {purpose: key for key, _, _, purposes in STEPS for purpose in purposes}
STEP_TIER = {key: tier for key, _, tier, _ in STEPS}


def parse_choice(value: str | None) -> dict | None:
    """A choice made next to a button: "provider|model", "provider|model|effort", or "||effort" for the usual
    model at another effort. None when nothing was chosen."""
    parts = (value or "").strip().split("|") + ["", "", ""]
    provider, model, effort = parts[0], parts[1], parts[2]
    out: dict = {}
    if provider in PROVIDERS and model:
        out.update(provider=provider, model=model)
    if effort in EFFORTS:
        out["effort"] = effort
    return out or None


def step_settings(step: str | None) -> dict:
    """Your choice for one step under Settings ({} = use the tier's model and the usual effort)."""
    return dict((settings.get("llm").get("steps") or {}).get(step or "") or {})


def step_provider(step: str, choice: str | None = None) -> str:
    """Which provider a step would use right now: a one-off choice, else the step's own model, else its tier's."""
    if _test_provider is not None:
        return getattr(_test_provider, "name", "")
    override = parse_choice(choice) or {}
    if override.get("provider"):
        return override["provider"]
    own = step_settings(step)
    if own.get("provider") and own.get("model"):
        return own["provider"]
    tier = STEP_TIER.get(step, "analysis")
    if tier == "check":
        from ..writer import check_tier

        tier = check_tier()
    return (settings.get("llm").get(tier) or {}).get("provider") or ""

@dataclass
class LLMResponse:
    text: str
    data: Any = None
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0


_test_provider = None


def set_test_provider(provider) -> None:
    """Tests inject a fake provider object with .name, .is_cloud and .complete(...)."""
    global _test_provider
    _test_provider = provider


def _build(tier: str, effort: str | None, purpose: str | None = None):
    if _test_provider is not None:
        return _test_provider
    cfg = settings.get("llm")
    t = dict(cfg.get(tier) or {})
    own = step_settings(STEP_OF.get(purpose or ""))  # this step's own model and effort, if you set one
    if own.get("provider") and own.get("model"):
        t.update(provider=own["provider"], model=own["model"])
    override = _ctx.get().get("override") or {}
    if override.get("provider"):  # a redo with another model: same step, different model
        t.update(provider=override["provider"], model=override["model"])
    # Effort: the one picked for this run, else the step's setting, else what the step usually uses, else the tier's.
    effort = override.get("effort") or own.get("effort") or effort
    provider, model = t.get("provider"), t.get("model")
    if not provider or not model:
        raise LLMConfigError("No AI model is set up yet. Open Settings → AI models.")
    if provider == "ollama":
        # A local model has no effort levels, only thinking first or not: on at high or above, on any step.
        level = effort or t.get("effort") or "high"
        return OllamaProvider(model=model, base_url=cfg.get("ollama_url") or "http://localhost:11434",
                              num_ctx=int(cfg.get("ollama_num_ctx") or 32768), think=thinks(level))
    if provider == "claude_code":
        if cfg.get("local_only"):
            raise LLMConfigError("Local-only mode is on, so cloud models (Claude Code too) are blocked.")
        binary = find_claude_binary(cfg.get("claude_code_path") or None)
        if not binary:
            raise LLMConfigError("Claude Code wasn't found on this Mac. Install it, or set its path in Settings → AI models.")
        return ClaudeCodeProvider(model=model, binary=binary, token=keychain.get(CLAUDE_CODE_TOKEN) or "",
                                  effort=effort or t.get("effort"))
    raise LLMConfigError(f"Unknown AI provider '{provider}'.")


def tier_provider(tier: str, choice: str | None = None) -> str:
    """Which provider a task on this tier would use ("ollama", "claude_code"…): the redo choice if there is
    one, else Settings."""
    if _test_provider is not None:
        return getattr(_test_provider, "name", "")
    override = parse_choice(choice)
    if override:
        return override["provider"]
    return (settings.get("llm").get(tier) or {}).get("provider") or ""


def ollama_running() -> bool:
    return _ollama_up(settings.get("llm").get("ollama_url") or "http://localhost:11434")


def _log(purpose: str, provider: str, model: str, r: ProviderReply | None, ms: int, error: str | None = None) -> None:
    try:
        db.insert("llm_calls", {
            "purpose": purpose, "provider": provider, "model": model,
            "input_tokens": r.input_tokens if r else 0, "output_tokens": r.output_tokens if r else 0,
            "cache_read_tokens": r.cache_read if r else 0, "cache_write_tokens": r.cache_write if r else 0,
            "duration_ms": ms, "ok": 0 if error else 1, "error": error,
            "ref": _ctx.get().get("ref"), "created_at": db.now(),
        })
    except Exception:  # logging must never break a task
        pass


def run(tier: str, purpose: str, system: str, user: str, schema: dict | None = None,
        max_tokens: int = 8000, effort: str | None = None) -> LLMResponse:
    """Call the model configured for `tier`. With a schema, returns parsed + coerced JSON in .data."""
    prov = _build(tier, effort, purpose)
    if getattr(prov, "is_cloud", False) and settings.get("llm").get("redact_for_cloud", True):
        system, user = privacy.redact(system), privacy.redact(user)
    model = getattr(prov, "model", "")
    t0 = time.monotonic()
    reply: ProviderReply | None = None
    try:
        reply = prov.complete(system=system, user=user, schema=schema, max_tokens=max_tokens)
        data = None
        if schema is not None:
            data = jsonutil.parse(reply.text)
            if data is None:
                # One retry: longer budget if the answer was cut off, firmer instruction otherwise.
                retry_tokens = min(max_tokens * 2, 64000) if reply.truncated else max_tokens
                reply = prov.complete(system=system,
                                      user=user + "\n\nReturn only the JSON object, nothing else.",
                                      schema=schema, max_tokens=retry_tokens)
                data = jsonutil.parse(reply.text)
                if data is None:
                    raise LLMError("The model's answer wasn't valid JSON, even after a retry.")
            data = jsonutil.coerce(data, schema)
        elif reply.truncated:
            reply.text = reply.text.rstrip() + "\n\n[…cut off: the model hit its output limit]"
        ms = int((time.monotonic() - t0) * 1000)
        used_model = reply.model or model
        _log(purpose, prov.name, used_model, reply, ms)
        return LLMResponse(text=reply.text, data=data, provider=prov.name, model=used_model,
                           input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                           cache_read_tokens=reply.cache_read, cache_write_tokens=reply.cache_write,
                           duration_ms=ms)
    except LLMError as e:
        _log(purpose, prov.name, model, reply, int((time.monotonic() - t0) * 1000), str(e))
        e.provider = prov.name  # so the task queue knows what to wait for
        raise
    except Exception as e:
        _log(purpose, prov.name, model, reply, int((time.monotonic() - t0) * 1000), repr(e))
        err = LLMError(f"Unexpected error from the model provider: {e}")
        err.provider = prov.name
        raise err from e


# ---------------------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------------------

# Local model choice per tier, from a blind test on this kind of work (November 2026): the dense
# Gemma 4 31B writes better letters; the mixture-of-experts 26B (3.8B active) is ~5x faster for
# reading, scoring and fact-checking, and checked as well as the 31B.
_PREFER_WRITING = ("gemma4:31b", "gemma4:26b", "qwen3:32b", "gemma3:27b", "mistral-small", "gemma4", "qwen3",
                   "gpt-oss", "gemma3", "llama3.3", "qwen2.5", "llama3")
_PREFER_ANALYSIS = ("gemma4:26b", "qwen3:30b", "gpt-oss:20b", "gemma4:31b", "gemma4", "qwen3", "gemma3",
                    "mistral-small", "gpt-oss", "llama3.3", "qwen2.5", "llama3")
_EMBEDDING_FAMILIES = ("bert", "nomic-bert", "xlm-roberta", "jina-bert-v2")


def _chat_models(models: list[dict]) -> list[dict]:
    out = []
    for m in models:
        name = m.get("name", "")
        family = ((m.get("details") or {}).get("family") or "").lower()
        caps = m.get("capabilities") or []  # known when read from disk: an embedding model can't complete text
        if "embed" in name or name.startswith(("bge", "all-minilm", "granite-embedding")) or family in _EMBEDDING_FAMILIES \
                or (caps and "completion" not in caps):
            continue
        out.append(m)
    return out


def _pick_local(models: list[dict], prefs: tuple[str, ...] = _PREFER_WRITING) -> str:
    chat = _chat_models(models)
    names = [m.get("name", "") for m in chat]
    for pref in prefs:
        for n in names:
            if n.startswith(pref):
                return n
    by_size = sorted(chat, key=lambda m: m.get("size") or 0, reverse=True)
    return by_size[0]["name"] if by_size else ""


def detect_defaults() -> dict:
    cfg = settings.get("llm")
    models = ollama_models(cfg.get("ollama_url") or "http://localhost:11434")
    writing, analysis = _pick_local(models, _PREFER_WRITING), _pick_local(models, _PREFER_ANALYSIS)
    if writing and analysis:
        return {"writing": {"provider": "ollama", "model": writing, "effort": "high"},
                "analysis": {"provider": "ollama", "model": analysis, "effort": "medium"}}
    return {}


def bootstrap() -> None:
    """Fill in a working model choice for any tier that isn't configured yet."""
    cfg = settings.get("llm")
    if all((cfg.get(t) or {}).get("provider") for t in ("writing", "analysis")):
        return
    found = detect_defaults()
    patch = {t: found[t] for t in ("writing", "analysis") if t in found and not (cfg.get(t) or {}).get("provider")}
    if patch:
        settings.update("llm", patch)


def status() -> dict:
    cfg = settings.get("llm")
    url = cfg.get("ollama_url") or "http://localhost:11434"
    live = list_ollama_models(url)
    models = live or (installed_ollama() if is_local_url(url) else [])
    return {
        "ollama_running": bool(live) or _ollama_up(url),
        "ollama_models": [m.get("name") for m in models],
        "ollama_chat_models": [m.get("name") for m in _chat_models(models)],  # without embedding models
        "local_only": bool(cfg.get("local_only")),
        "writing": cfg.get("writing"),
        "analysis": cfg.get("analysis"),
        "claude_code": find_claude_binary(cfg.get("claude_code_path") or None) or "",
        "claude_code_token": bool(keychain.get(CLAUDE_CODE_TOKEN)),
    }


# ---------------------------------------------------------------------------------------
# Which model made what, and which models you can redo work with
# ---------------------------------------------------------------------------------------

def model_label(provider: str | None, model: str | None) -> str:
    model = model or "?"
    if provider == "claude_code":
        short = model.removeprefix("claude-code/")
        return f"{claude_name(CLAUDE_CODE_ALIASES.get(short, short))} (Claude Code)"
    if provider == "ollama":
        return f"{model} (local)"
    return model


def made_by(ref: str, purposes: tuple[str, ...] | None = None) -> dict | None:
    """The model behind the latest successful call for `ref` (optionally for these purposes)."""
    sql = "SELECT provider, model, created_at FROM llm_calls WHERE ref = ? AND ok = 1"
    args: list = [ref]
    if purposes:
        sql += f" AND purpose IN ({','.join('?' * len(purposes))})"
        args += list(purposes)
    row = db.q1(sql + " ORDER BY id DESC LIMIT 1", args)
    if not row:
        return None
    return {"provider": row["provider"], "model": row["model"], "at": row["created_at"],
            "label": model_label(row["provider"], row["model"])}


def backfill_refs() -> int:
    """Calls logged before refs were recorded: give each the ref of the task that was running when it ended."""
    before = db.scalar("SELECT COUNT(*) FROM llm_calls WHERE ref IS NULL") or 0
    db.ex("UPDATE llm_calls SET ref = (SELECT t.ref FROM tasks t WHERE t.ref IS NOT NULL AND t.started_at IS NOT NULL "
          "AND t.finished_at IS NOT NULL AND llm_calls.created_at >= t.started_at AND llm_calls.created_at <= t.finished_at "
          "ORDER BY t.id DESC LIMIT 1) WHERE ref IS NULL")
    return before - (db.scalar("SELECT COUNT(*) FROM llm_calls WHERE ref IS NULL") or 0)


_choices_cache: dict = {"at": 0.0, "value": []}


def model_choices() -> list[tuple[str, list[tuple[str, str]]]]:
    """Models you can pick for a redo, grouped: [(group, [(value, label)])]. Cached for a minute."""
    if time.monotonic() - _choices_cache["at"] < 60 and _choices_cache["value"]:
        return _choices_cache["value"]
    cfg = settings.get("llm")
    groups = []
    if not cfg.get("local_only") and find_claude_binary(cfg.get("claude_code_path") or None):
        newest, earlier = plan.choices()
        groups.append(("Claude through Claude Code (your subscription)", [(f"claude_code|{m}", label) for m, label in newest]))
        if earlier:
            groups.append(("Claude Code: earlier versions", [(f"claude_code|{m}", label) for m, label in earlier]))
    local = [m.get("name") for m in _chat_models(ollama_models(cfg.get("ollama_url") or "http://localhost:11434"))]
    if local:
        groups.append(("On this Mac (Ollama)", [(f"ollama|{m}", m) for m in local]))
    _choices_cache.update(at=time.monotonic(), value=groups)
    return groups


def _ollama_up(url: str) -> bool:
    import httpx2
    try:
        return httpx2.get(f"{url.rstrip('/')}/api/version", timeout=2).status_code == 200
    except httpx2.HTTPError:
        return False


def test_tier(tier: str) -> str:
    r = run(tier, "connection_test", "You are a connection test.",
            "Reply with the single word OK.", max_tokens=2000, effort="low")
    return f"{r.provider} · {r.model} answered “{r.text.strip()[:40]}” in {r.duration_ms / 1000:.1f}s"
