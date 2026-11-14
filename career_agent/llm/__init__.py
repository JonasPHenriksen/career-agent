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
from .providers import (CLAUDE_CODE_MODELS, AnthropicProvider, ClaudeCodeProvider, OllamaProvider,
                        OpenAICompatProvider, ProviderReply, find_claude_binary, is_local_url, list_ollama_models)

__all__ = ["run", "LLMResponse", "LLMError", "LLMConfigError", "LLMRefusal", "LLMTransient", "jsonutil", "using",
           "made_by", "model_label", "model_choices", "parse_choice", "backfill_refs", "PROVIDERS",
           "set_test_provider", "status", "tier_provider", "ollama_running", "EFFORTS", "STEPS", "STEP_OF", "step_provider", "step_settings", "detect_defaults", "bootstrap", "test_tier",
           "ANTHROPIC_KEY", "OPENAI_KEY", "ProviderReply", "list_ollama_models"]

ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"
CLAUDE_CODE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
PROVIDERS = ("anthropic", "ollama", "openai", "claude_code")

# What the current task is about ("app:12", "doc:3"…) and, for a redo, which model to use instead of Settings.
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("llm_context", default={})


@contextlib.contextmanager
def using(ref: str | None = None, model: str | None = None):
    """Run model calls for `ref` (recorded with each call, so you can see which model made what) and, if `model`
    is a choice like "claude_code|sonnet" or "ollama|gemma4:31b", with that model instead of the configured ones."""
    token = _ctx.set({"ref": ref, "override": parse_choice(model)})
    try:
        yield
    finally:
        _ctx.reset(token)


EFFORTS = ("low", "medium", "high", "xhigh", "max")

# What the app does, step by step: (key, label, the tier whose model it uses by default, its model calls).
# Settings lets you pick another model and effort for any step; a redo can pick one for a single run.
STEPS = [
    ("screen", "Screen new job listings", "analysis", ("triage", "page_jobs")),
    ("read_ad", "Read a job ad", "analysis", ("parse_job",)),
    ("score", "Score how well a job fits", "analysis", ("fit_score",)),
    ("read_document", "Read your documents into facts", "analysis", ("analyze_document", "consolidate_facts")),
    ("reply", "Read an employer's reply", "analysis", ("classify_response",)),
    ("letter", "Write and revise the letter", "writing", ("write_letter", "revise_letter", "followup")),
    ("cv", "Tailor and revise the CV", "writing", ("write_cv", "revise_cv", "cv_base")),
    ("fact_check", "Fact-check a draft", "check", ("claim_check", "verify_claims")),
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

# USD per million tokens: input, output, cache read, cache write (5-minute TTL). Estimates.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-2": (8.0, 40.0, 0.20, 10.0),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
}

ANTHROPIC_MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5 (best writing, recommended)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 (cheaper, fast)"),
    ("claude-haiku-5-5", "Claude Haiku 5.5 (cheapest, for bulk analysis)"),
    ("claude-fable-5-2", "Claude Fable 5.2 (most capable, most expensive)"),
]


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
    cost_usd: float = 0.0


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
    if provider == "anthropic":
        if cfg.get("local_only"):
            raise LLMConfigError("Local-only mode is on, so cloud models are blocked. Pick a local model for this task in Settings.")
        key = keychain.get(ANTHROPIC_KEY)
        if not key:
            raise LLMConfigError("Add your Anthropic API key in Settings → AI models.")
        return AnthropicProvider(model=model, api_key=key, effort=effort or t.get("effort") or "medium")
    if provider == "ollama":
        level = effort or t.get("effort") or "high"
        return OllamaProvider(model=model, base_url=cfg.get("ollama_url") or "http://localhost:11434",
                              num_ctx=int(cfg.get("ollama_num_ctx") or 32768),
                              think=(tier == "writing" and level in ("high", "xhigh", "max")))
    if provider == "openai":
        base = cfg.get("openai_base_url") or ""
        if cfg.get("local_only") and not is_local_url(base):
            raise LLMConfigError("Local-only mode is on, and the OpenAI-compatible server isn't on this Mac.")
        return OpenAICompatProvider(model=model, base_url=base, api_key=keychain.get(OPENAI_KEY))
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


def _cost(model: str, r: ProviderReply) -> float:
    p = PRICES.get(model) or next((v for k, v in PRICES.items() if model.startswith(k)), None)
    if not p:
        return 0.0
    return (r.input_tokens * p[0] + r.output_tokens * p[1] + r.cache_read * p[2] + r.cache_write * p[3]) / 1_000_000


def _log(purpose: str, provider: str, model: str, r: ProviderReply | None, ms: int, cost: float,
         error: str | None = None) -> None:
    try:
        db.insert("llm_calls", {
            "purpose": purpose, "provider": provider, "model": model,
            "input_tokens": r.input_tokens if r else 0, "output_tokens": r.output_tokens if r else 0,
            "cache_read_tokens": r.cache_read if r else 0, "cache_write_tokens": r.cache_write if r else 0,
            "cost_usd": cost, "duration_ms": ms, "ok": 0 if error else 1, "error": error,
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
        cost = _cost(used_model, reply)
        _log(purpose, prov.name, used_model, reply, ms, cost)
        return LLMResponse(text=reply.text, data=data, provider=prov.name, model=used_model,
                           input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                           cache_read_tokens=reply.cache_read, cache_write_tokens=reply.cache_write,
                           duration_ms=ms, cost_usd=cost)
    except LLMError as e:
        _log(purpose, prov.name, model, reply, int((time.monotonic() - t0) * 1000), 0.0, str(e))
        e.provider = prov.name  # so the task queue knows what to wait for
        raise
    except Exception as e:
        _log(purpose, prov.name, model, reply, int((time.monotonic() - t0) * 1000), 0.0, repr(e))
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
        if "embed" in name or name.startswith(("bge", "all-minilm", "granite-embedding")) or family in _EMBEDDING_FAMILIES:
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
    has_key = bool(keychain.get(ANTHROPIC_KEY))
    models = list_ollama_models(cfg.get("ollama_url") or "http://localhost:11434")
    if has_key and not cfg.get("local_only"):
        return {"writing": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"},
                "analysis": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "medium"}}
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
    models = list_ollama_models(cfg.get("ollama_url") or "http://localhost:11434")
    return {
        "anthropic_key": keychain.source(ANTHROPIC_KEY),
        "openai_key": keychain.source(OPENAI_KEY),
        "ollama_running": bool(models) or _ollama_up(cfg.get("ollama_url") or ""),
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
        return f"Claude Code ({model.removeprefix('claude-code/')})"
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
        groups.append(("Claude through Claude Code (your subscription)",
                       [(f"claude_code|{m}", f"{label}") for m, label in CLAUDE_CODE_MODELS]))
    if not cfg.get("local_only") and keychain.get(ANTHROPIC_KEY):
        groups.append(("Claude API (billed per call)", [(f"anthropic|{m}", label) for m, label in ANTHROPIC_MODELS]))
    local = [m.get("name") for m in _chat_models(list_ollama_models(cfg.get("ollama_url") or "http://localhost:11434"))]
    if local:
        groups.append(("On this Mac (Ollama)", [(f"ollama|{m}", m) for m in local]))
    other = [cfg.get(t) or {} for t in ("writing", "analysis")]
    custom = sorted({o["model"] for o in other if o.get("provider") == "openai" and o.get("model")})
    if custom:
        groups.append(("OpenAI-compatible server", [(f"openai|{m}", m) for m in custom]))
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
