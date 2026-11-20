"""Model providers: Claude through Claude Code (your subscription), Ollama (local), and any OpenAI-compatible
server (LM Studio, mlx_lm.server, llama.cpp...). Each exposes complete(system, user, schema, max_tokens)."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx2

from . import jsonutil
from .errors import LLMConfigError, LLMError, LLMRefusal, LLMTransient


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
    """The models a running Ollama serves ([] when it isn't running)."""
    try:
        r = httpx2.get(f"{base_url.rstrip('/')}/api/tags", timeout=3)
        r.raise_for_status()
        return r.json().get("models") or []
    except (httpx2.HTTPError, ValueError):
        return []


def installed_ollama_models(models_dir: str | None = None) -> list[dict]:
    """The models Ollama has on this Mac, read from its model folder (OLLAMA_MODELS, else ~/.ollama/models) in the
    shape /api/tags gives, so the list is right while Ollama isn't running too. A model you pull or remove shows up
    or disappears at once."""
    import json
    import os

    root = os.path.expanduser(models_dir or os.environ.get("OLLAMA_MODELS") or "~/.ollama/models")
    manifests = os.path.join(root, "manifests")
    out = []
    for dirpath, _, files in os.walk(manifests):
        for tag in files:
            parts = os.path.relpath(os.path.join(dirpath, tag), manifests).split(os.sep)
            if len(parts) != 4 or tag.startswith("."):
                continue
            host, namespace, model, tag = parts
            name = (f"{model}:{tag}" if namespace == "library" else f"{namespace}/{model}:{tag}") \
                if host == "registry.ollama.ai" else f"{host}/{namespace}/{model}:{tag}"
            try:
                with open(os.path.join(dirpath, tag), encoding="utf-8") as fh:
                    manifest = json.load(fh)
                config = {}
                digest = ((manifest.get("config") or {}).get("digest") or "").replace(":", "-")
                if digest:
                    with open(os.path.join(root, "blobs", digest), encoding="utf-8") as fh:
                        config = json.load(fh)
            except (OSError, ValueError):
                continue
            out.append({"name": name, "size": sum(int(layer.get("size") or 0) for layer in manifest.get("layers") or []),
                        "details": {"family": config.get("model_family") or ""},
                        "capabilities": config.get("capabilities") or []})
    return sorted(out, key=lambda m: m["name"])


def ollama_models(base_url: str) -> list[dict]:
    """The models you can pick: what a running Ollama serves, else what's installed (for Ollama on this Mac)."""
    return list_ollama_models(base_url) or (installed_ollama_models() if is_local_url(base_url) else [])


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


# ----------------------------------------------------------------------------------------
# Claude through Claude Code (your Claude subscription, no API key)
# ----------------------------------------------------------------------------------------

# Exact versions, so you always know which model did the work: the ones a Claude subscription runs through Claude
# Code. Checked 2026-11-20 by asking each one: Mythos isn't on the plan, and Opus 4.1 and Fable 5 are left out
# because another model answers in their place (Opus 5.5 and Opus 4.8).
CLAUDE_CODE_MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5 (best writing)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 (fast, for analysis)"),
    ("claude-haiku-5-5", "Claude Haiku 5.5 (fastest, lightest on your plan)"),
    ("claude-fable-5-2", "Claude Fable 5.2 (most capable)"),
]
CLAUDE_CODE_EARLIER = [
    ("claude-fable-5-1", "Claude Fable 5.1"),
    ("claude-opus-5", "Claude Opus 5"),
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("claude-opus-4-6", "Claude Opus 4.6"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-opus-4-5", "Claude Opus 4.5"),
    ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
    ("claude-haiku-4-5", "Claude Haiku 4.5"),
]


def claude_name(model: str) -> str:
    """ "claude-opus-4-8" → "Claude Opus 4.8"; anything else as it is."""
    m = re.match(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?$", model)
    if not m:
        return model
    return f"Claude {m.group(1).capitalize()} {m.group(2)}{'.' + m.group(3) if m.group(3) else ''}"
# The short names Claude Code also accepts follow whatever is newest. Older settings and queued tasks may still
# hold one; they run on the version each stood for when the app switched to exact versions.
CLAUDE_CODE_ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
                       "fable": "claude-fable-5-2"}


def find_claude_binary(configured: str | None = None) -> str | None:
    """The `claude` command line: the configured path, then PATH and the usual install places, then the copy
    the Claude desktop app keeps (newest version first)."""
    import glob
    import os
    import shutil

    found = [configured] if configured else []
    found += [shutil.which("claude"), os.path.expanduser("~/.local/bin/claude"),
              os.path.expanduser("~/.claude/local/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]

    def version(path: str) -> tuple:
        part = path.split("/claude-code/", 1)[-1].split("/", 1)[0]
        return tuple(int(n) for n in re.findall(r"\d+", part))

    bundled = glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"))
    found += sorted(bundled, key=version, reverse=True)
    return next((p for p in found if p and os.path.isfile(p) and os.access(p, os.X_OK)), None)


class ClaudeCodeProvider:
    """Claude through the Claude Code command line in print mode. It uses the Claude subscription you're logged in
    with (an API key in the environment is removed so it can't be billed per call), and runs with every tool
    switched off, no plugins, MCP servers, settings or saved sessions, in an empty temporary folder: it can only
    answer, never act."""

    name = "claude_code"
    is_cloud = True

    def __init__(self, model: str, binary: str, token: str = "", effort: str | None = None, timeout: int = 1200):
        model = CLAUDE_CODE_ALIASES.get(model, model)
        self.model, self.binary, self.token, self.effort, self.timeout = model, binary, token, effort, timeout

    def args(self, system_file: str, schema: dict | None) -> list[str]:
        import json

        args = [self.binary, "-p", "--output-format", "json", "--model", self.model, "--system-prompt-file", system_file,
                "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                "--disable-slash-commands"]
        if self.effort in ("low", "medium", "high", "xhigh", "max"):
            args += ["--effort", self.effort]
        if schema is not None:
            args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        return args

    def complete(self, system: str, user: str, schema: dict | None, max_tokens: int) -> ProviderReply:
        try:
            return self._ask(system, user, schema)
        except LLMError as e:
            # Now and then Claude Code gives up on fitting its answer to the schema. A second try nearly always
            # works, and is cheaper than a failed task you have to start again by hand.
            if "structured_output_retries" not in str(e):
                raise
            return self._ask(system, user, schema)

    def _ask(self, system: str, user: str, schema: dict | None) -> ProviderReply:
        import json
        import os
        import subprocess
        import tempfile

        # A clean environment: no API key (so it can't be billed per call), and none of the CLAUDE_* variables a
        # terminal inside the Claude app passes on. With those, Claude Code adds a small side-call to another
        # model on every request, which costs usage and muddles which model did the work.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE"))}
        if self.token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.token
        with tempfile.TemporaryDirectory(prefix="career-agent-claude-") as cwd:
            system_file = os.path.join(cwd, "system.txt")
            with open(system_file, "w", encoding="utf-8") as fh:
                fh.write(system)
            try:
                proc = subprocess.run(self.args(system_file, schema), input=user, capture_output=True, text=True,
                                      cwd=cwd, env=env, timeout=self.timeout)
            except FileNotFoundError as e:
                raise LLMConfigError("Claude Code wasn't found. Set its path in Settings → AI models.") from e
            except subprocess.TimeoutExpired as e:
                raise LLMTransient(f"Claude Code didn't answer within {self.timeout // 60} minutes.") from e
        out = (proc.stdout or "").strip()
        try:
            d = json.loads(out[out.index("{"):] if "{" in out else out)
        except ValueError:
            detail = (proc.stderr or out or f"exit code {proc.returncode}").strip()[:300]
            raise LLMError(f"Claude Code gave no usable answer: {detail}") from None
        if d.get("is_error") or d.get("subtype", "success") != "success":
            msg = str(d.get("result") or d.get("subtype") or "unknown error")
            low = msg.lower()
            if any(w in low for w in ("not logged in", "/login", "log in", "authenticat", "invalid api key", "oauth")):
                raise LLMConfigError("Claude Code isn't logged in. Log in once in Terminal: Settings → AI models shows how.")
            if any(w in low for w in ("usage limit", "rate limit", "overloaded", "limit reached", "try again", "529")):
                raise LLMTransient(f"Claude Code: {msg[:200]}", retry_after=3600 if "usage limit" in low else None)
            raise LLMError(f"Claude Code: {msg[:300]}")
        text = d.get("result") or ""
        if schema is not None and d.get("structured_output") is not None:
            text = json.dumps(d["structured_output"], ensure_ascii=False)
        usage = d.get("usage") or {}
        # The model that wrote the answer: when several are listed, the one with the most output.
        by_model = d.get("modelUsage") or {}
        used = max(by_model, key=lambda m: (by_model[m] or {}).get("outputTokens") or 0, default="") or self.model
        # Fresh and cached input are kept apart (cached input counts for far less of a plan's usage).
        # "claude-code/…" never matches a price: a subscription isn't billed per call.
        return ProviderReply(text=text, input_tokens=int(usage.get("input_tokens") or 0),
                             output_tokens=int(usage.get("output_tokens") or 0),
                             cache_read=int(usage.get("cache_read_input_tokens") or 0),
                             cache_write=int(usage.get("cache_creation_input_tokens") or 0),
                             model=f"claude-code/{used}")
