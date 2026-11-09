"""Exercise the Claude provider end to end against a local stand-in for the Messages API (no network)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from career_agent.llm import jsonutil
from career_agent.llm.errors import LLMConfigError, LLMRefusal
from career_agent.llm.providers import AnthropicProvider


def _sse(text: str, stop_reason: str = "end_turn") -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 42}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class _Server:
    def __init__(self):
        self.requests: list[dict] = []
        self.reply = (200, _sse('{"ok": true}'))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status, payload = outer.reply
                self.send_response(status)
                self.send_header("content-type", "text/event-stream" if status == 200 else "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture()
def api(monkeypatch):
    server = _Server()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
    yield server
    server.httpd.shutdown()


def test_request_shape_and_parsing(api):
    schema = jsonutil.obj(ok=jsonutil.b())
    p = AnthropicProvider(model="claude-opus-5-5", api_key="sk-ant-test", effort="high")
    reply = p.complete(system="SYSTEM", user="USER", schema=schema, max_tokens=16000)
    assert reply.text == '{"ok": true}' and reply.input_tokens == 1200 and reply.output_tokens == 42
    assert reply.cache_read == 800 and not reply.truncated
    req = api.requests[-1]
    body = req["body"]
    assert req["path"].startswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True and body["max_tokens"] == 16000
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": schema}}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", "")
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in body and "temperature" not in body


def test_older_model_without_fallbacks_or_effort(api):
    p = AnthropicProvider(model="claude-haiku-4-5", api_key="sk-ant-test", effort="low")
    p.complete(system="S", user="U", schema=None, max_tokens=1000)
    body = api.requests[-1]["body"]
    assert "fallbacks" not in body and "output_config" not in body


def test_refusal_and_auth_errors(api):
    p = AnthropicProvider(model="claude-opus-5-5", api_key="sk-ant-test")
    api.reply = (200, _sse("", stop_reason="refusal"))
    with pytest.raises(LLMRefusal):
        p.complete(system="S", user="U", schema=None, max_tokens=100)
    api.reply = (401, json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}).encode())
    with pytest.raises(LLMConfigError):
        p.complete(system="S", user="U", schema=None, max_tokens=100)
