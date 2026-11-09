"""Model providers: Anthropic (Claude, cloud), Ollama (local), and any OpenAI-compatible server
(LM Studio, mlx_lm.server, llama.cpp...). Each exposes complete(system, user, schema, max_tokens)."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx2

from . import jsonutil
from .errors import LLMConfigError, LLMError, LLMRefusal


@dataclass
class ProviderReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    truncated: bool = False
    model: str = ""


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


# ----------------------------------------------------------------------------------------
# Anthropic
# ----------------------------------------------------------------------------------------

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_EFFORT_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable|mythos)-(5|[6-9])|^claude-opus-4-[5-9]|^claude-sonnet-4-6")


class AnthropicProvider:
    name = "anthropic"
    is_cloud = True

    def __init__(self, model: str, api_key: str, effort: str = "medium"):
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3)

    def complete(self, system: str, user: str, schema: dict | None, max_tokens: int) -> ProviderReply:
        a = self._anthropic
        output_config: dict = {}
        if _EFFORT_RE.match(self.model) and self.effort:
            effort = self.effort
            if self.model.startswith("claude-opus-4-5") and effort in ("xhigh", "max"):
                effort = "high"
            output_config["effort"] = effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            # The system prompt carries the stable context (profile, style guide, examples),
            # so cache it: repeated drafts reuse it at a fraction of the price.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        if output_config:
            kwargs["output_config"] = output_config
        try:
            if self.model in _FALLBACK_MODELS:
                ctx = self.client.beta.messages.stream(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                ctx = self.client.messages.stream(**kwargs)
            with ctx as stream:
                msg = stream.get_final_message()
        except a.AuthenticationError as e:
            raise LLMConfigError("Anthropic rejected the API key. Check it in Settings → AI models.") from e
        except a.PermissionDeniedError as e:
            raise LLMConfigError(f"The API key can't use {self.model}: {e.message}") from e
        except a.NotFoundError as e:
            raise LLMConfigError(f"Model '{self.model}' was not found. Pick another model in Settings.") from e
        except a.RateLimitError as e:
            raise LLMError("Anthropic rate limit reached. The task can be retried in a minute.") from e
        except a.BadRequestError as e:
            raise LLMError(f"Anthropic rejected the request: {e.message}") from e
        except a.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError("Couldn't reach the Anthropic API. Check your internet connection.") from e

        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            why = getattr(details, "explanation", None) or "no explanation given"
            raise LLMRefusal(f"The model declined this request ({why}).")
        text = "".join(getattr(blk, "text", "") for blk in msg.content if blk.type == "text")
        u = msg.usage
        return ProviderReply(
            text=text,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            truncated=msg.stop_reason == "max_tokens",
            model=str(getattr(msg, "model", self.model)),
        )


# ----------------------------------------------------------------------------------------
# Ollama (native API: lets us set the context window and constrain output to a JSON schema)
# ----------------------------------------------------------------------------------------

_caps_lock = threading.Lock()
_caps_cache: dict[tuple[str, str], list[str]] = {}


class OllamaProvider:
    name = "ollama"
    is_cloud = False

    def __init__(self, model: str, base_url: str, num_ctx: int = 32768, think: bool = False,
                 max_ctx: int = 65536):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.max_ctx = max(max_ctx, num_ctx)
        self.think = think

    def capabilities(self) -> list[str]:
        key = (self.base_url, self.model)
        with _caps_lock:
            if key in _caps_cache:
                return _caps_cache[key]
        try:
            r = httpx2.post(f"{self.base_url}/api/show", json={"model": self.model}, timeout=15)
            caps = (r.json().get("capabilities") or []) if r.status_code == 200 else []
        except httpx2.HTTPError:
            caps = []
        with _caps_lock:
            _caps_cache[key] = caps
        return caps

    def complete(self, system: str, user: str, schema: dict | None, max_tokens: int) -> ProviderReply:
        if schema is not None:
            # Grammar-constrained decoding keeps the shape valid, but the model only knows
            # what the fields mean if the schema is in the prompt.
            system = f"{system}\n\nRespond with a single JSON object matching this JSON schema:\n{jsonutil.describe(schema)}"
        est_tokens = (len(system) + len(user)) // 3 + max_tokens + 512
        num_ctx = self.num_ctx
        while num_ctx < est_tokens and num_ctx < self.max_ctx:
            num_ctx *= 2
        if est_tokens > num_ctx:
            raise LLMError(
                f"This request (~{est_tokens:,} tokens) is larger than the local model's context window "
                f"({num_ctx:,}). Raise the context size in Settings or shorten the input."
            )
        body: dict = {
            "model": self.model,
            "stream": False,
            "keep_alive": "30m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {
                "num_ctx": num_ctx,
                "num_predict": max_tokens,
                "temperature": 0.2 if schema is not None and not self.think else 0.7,
            },
        }
        if schema is not None:
            body["format"] = schema
        if "thinking" in self.capabilities():
            body["think"] = bool(self.think)
        try:
            r = httpx2.post(f"{self.base_url}/api/chat", json=body,
                            timeout=httpx2.Timeout(1800.0, connect=5.0))
        except httpx2.ConnectError as e:
            raise LLMConfigError("Ollama isn't running. Open the Ollama app (or run `ollama serve`).") from e
        except httpx2.TimeoutException as e:
            raise LLMError("The local model took longer than 30 minutes and was stopped.") from e
        if r.status_code == 404:
            raise LLMConfigError(f"The local model '{self.model}' isn't installed. In Terminal run: ollama pull {self.model}")
        if r.status_code >= 400:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        d = r.json()
        msg = d.get("message") or {}
        return ProviderReply(
            text=msg.get("content") or "",
            input_tokens=d.get("prompt_eval_count") or 0,
            output_tokens=d.get("eval_count") or 0,
            truncated=d.get("done_reason") == "length",
            model=self.model,
        )


def list_ollama_models(base_url: str) -> list[dict]:
    try:
        r = httpx2.get(f"{base_url.rstrip('/')}/api/tags", timeout=3)
        r.raise_for_status()
        return r.json().get("models") or []
    except (httpx2.HTTPError, ValueError):
        return []


# ----------------------------------------------------------------------------------------
# OpenAI-compatible servers (LM Studio, mlx_lm.server, llama.cpp, vLLM, ...)
# ----------------------------------------------------------------------------------------

class OpenAICompatProvider:
    name = "openai"

    def __init__(self, model: str, base_url: str, api_key: str = ""):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.is_cloud = not is_local_url(self.base_url)

    def complete(self, system: str, user: str, schema: dict | None, max_tokens: int) -> ProviderReply:
        if schema is not None:
            system = f"{system}\n\nRespond with a single JSON object matching this JSON schema:\n{jsonutil.describe(schema)}"
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.2 if schema is not None else 0.7,
        }
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.base_url}/chat/completions"
        try:
            r = httpx2.post(url, json=body, headers=headers, timeout=httpx2.Timeout(1800.0, connect=5.0))
            if r.status_code == 400 and schema is not None:
                body.pop("response_format")  # server without structured-output support
                r = httpx2.post(url, json=body, headers=headers, timeout=httpx2.Timeout(1800.0, connect=5.0))
        except httpx2.ConnectError as e:
            raise LLMConfigError(f"Couldn't connect to {self.base_url}. Is the model server running?") from e
        except httpx2.TimeoutException as e:
            raise LLMError("The model server timed out.") from e
        if r.status_code in (401, 403):
            raise LLMConfigError("The model server rejected the API key.")
        if r.status_code >= 400:
            raise LLMError(f"Model server error {r.status_code}: {r.text[:300]}")
        d = r.json()
        choice = (d.get("choices") or [{}])[0]
        usage = d.get("usage") or {}
        return ProviderReply(
            text=(choice.get("message") or {}).get("content") or "",
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            truncated=choice.get("finish_reason") == "length",
            model=str(d.get("model") or self.model),
        )
