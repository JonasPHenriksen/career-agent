"""User settings stored as JSON values in the `settings` table, merged over defaults."""
from __future__ import annotations

import copy
import json
from typing import Any

from . import db

DEFAULTS: dict[str, Any] = {
    "profile": {
        "name": "",
        "email": "",
        "phone": "",
        "city": "",
        "linkedin": "",
        "website": "",
        "headline": "",
        "target_roles": "",
        "target_industries": "",
        "locations": "",
        "work_modes": ["onsite", "hybrid", "remote"],
        "min_salary": "",
        "availability": "",
        "deal_breakers": "",
        "always_mention": "",
        "never_mention": "",
        "about": "",
    },
    "llm": {
        # provider: "anthropic" | "ollama" | "openai" ; "" = not configured yet
        "writing": {"provider": "", "model": "", "effort": "high"},
        "analysis": {"provider": "", "model": "", "effort": "medium"},
        "ollama_url": "http://localhost:11434",
        "ollama_num_ctx": 32768,
        "openai_base_url": "http://localhost:1234/v1",
        "local_only": False,
        "redact_for_cloud": True,
        "concurrency": 0,  # 0 = automatic (1 for local models, 3 for cloud)
        "embed_model": "bge-m3",  # local Ollama embedding model for semantic search
        "check_with": "auto",  # which model fact-checks drafts: "auto", "writing" or "analysis"
    },
    "automation": {
        "auto_score": True,
        "auto_draft": True,
        "auto_draft_threshold": 80,
        "auto_draft_daily_cap": 5,
        "followup_days": 10,
        "ghost_days": 30,
        "notifications": True,
    },
    "writing": {
        "letter_words": 0,  # 0 = as long as your own past letters
        "language": "auto",  # auto = same language as the job ad
        "pdf_font": "sans",  # sans | serif
        "accent_color": "#1f4e79",
        "extra_banned_phrases": "",
    },
    "style_guide": "",
    "style_stats": {},
    "state": {"llm_bootstrapped": False, "last_maintenance": "", "last_digest": ""},
}


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _merge(base.get(k), v) if k in base else v
        return out
    return copy.deepcopy(override)


def get(key: str) -> Any:
    row = db.q1("SELECT value FROM settings WHERE key = ?", (key,))
    default = copy.deepcopy(DEFAULTS.get(key))
    if not row:
        return default
    try:
        stored = json.loads(row["value"])
    except ValueError:
        return default
    if isinstance(default, dict) and isinstance(stored, dict):
        return _merge(default, stored)
    return stored


def set(key: str, value: Any) -> None:  # noqa: A001 - module-level API
    db.ex(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def update(key: str, partial: dict) -> dict:
    merged = _merge(get(key) or {}, partial)
    set(key, merged)
    return merged
