"""JSON schema helpers, lenient parsing, and coercion so downstream code can trust shapes."""
from __future__ import annotations

import json
import re
from typing import Any


# ---------- schema builders (kept to the subset every provider supports) ----------

def obj(**props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items: dict, desc: str | None = None) -> dict:
    d = {"type": "array", "items": items}
    if desc:
        d["description"] = desc
    return d


def s(desc: str | None = None, enum: list[str] | None = None) -> dict:
    d: dict = {"type": "string"}
    if desc:
        d["description"] = desc
    if enum:
        d["enum"] = enum
    return d


def i(desc: str | None = None) -> dict:
    d: dict = {"type": "integer"}
    if desc:
        d["description"] = desc
    return d


def b(desc: str | None = None) -> dict:
    d: dict = {"type": "boolean"}
    if desc:
        d["description"] = desc
    return d


# ---------- parsing ----------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def parse(text: str) -> Any | None:
    if not text:
        return None
    s_ = _FENCE.sub("", text.strip()).strip()
    for candidate in _candidates(s_):
        for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
            try:
                return json.loads(attempt)
            except ValueError:
                continue
    return None


def _candidates(text: str):
    yield text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        yield text[start:end + 1]


# ---------- coercion ----------

def coerce(data: Any, schema: dict) -> Any:
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            data = {}
        return {k: coerce(data.get(k), sub) for k, sub in schema.get("properties", {}).items()}
    if t == "array":
        if data is None or data == "":
            data = []
        elif not isinstance(data, list):
            data = [data]
        return [coerce(x, schema.get("items", {})) for x in data]
    if t == "string":
        if data is None:
            out = ""
        elif isinstance(data, (dict, list)):
            out = json.dumps(data, ensure_ascii=False)
        else:
            out = str(data).strip()
        enum = schema.get("enum")
        if enum and out not in enum:
            lowered = {e.lower(): e for e in enum}
            out = lowered.get(out.lower(), enum[-1])
        return out
    if t == "integer":
        try:
            return int(round(float(data)))
        except (TypeError, ValueError):
            return 0
    if t == "number":
        try:
            return float(data)
        except (TypeError, ValueError):
            return 0.0
    if t == "boolean":
        if isinstance(data, str):
            return data.strip().lower() in ("true", "yes", "1")
        return bool(data)
    return data


def describe(schema: dict) -> str:
    """Compact schema text for providers whose models don't see the schema natively."""
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


_LANGS = {"eng": "en", "english": "en", "dan": "da", "danish": "da", "dansk": "da", "swe": "sv", "swedish": "sv",
          "svenska": "sv", "nor": "no", "nob": "no", "nb": "no", "nn": "no", "norwegian": "no", "norsk": "no",
          "deu": "de", "ger": "de", "german": "de", "deutsch": "de", "nld": "nl", "dut": "nl", "dutch": "nl",
          "fra": "fr", "fre": "fr", "french": "fr", "spa": "es", "spanish": "es", "fin": "fi", "finnish": "fi"}


def lang_code(value: str | None) -> str | None:
    v = (value or "").strip().lower().replace("_", "-")
    if not v:
        return None
    v = _LANGS.get(v, v)
    v = v.split("-")[0]
    return _LANGS.get(v, v)[:3] if len(v) > 3 else v
