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
        "level": "",  # student | junior | mid | senior | lead | executive ; "" = not said
        "target_roles": "",
        "target_industries": "",
        "locations": "",
        "max_km": "",  # longest distance from `city` to on-site or hybrid work, measured in code (places.py); "" = no limit
        "preferred_km": "",  # the distance you'd rather stay within; jobs between this and max_km rank lower (rank.py)
        "big_employers": "moderate",  # off | moderate | strong: how much lower jobs at big, well-known employers rank
        "work_modes": ["onsite", "hybrid", "remote"],
        # kinds of job to look at (False = skip those ads without spending model calls); see jobkinds.py
        "job_kinds": {"student": True, "internship": True, "part_time": True, "temporary": True,
                      "freelance": True, "research": True, "volunteer": True},
        "min_salary": "",
        "availability": "",
        "deal_breakers": "",
        "always_mention": "",
        "never_mention": "",
        "about": "",
    },
    "llm": {
        # provider: "claude_code" | "ollama" ; "" = not configured yet
        "writing": {"provider": "", "model": "", "effort": "high"},
        "analysis": {"provider": "", "model": "", "effort": "medium"},
        "ollama_url": "http://localhost:11434",
        "ollama_num_ctx": 32768,
        "local_only": False,
        "redact_for_cloud": True,
        "concurrency": 0,  # 0 = automatic (1 for local models, 3 for cloud)
        "embed_model": "bge-m3",  # local Ollama embedding model for semantic search
        "steps": {},  # per step (see llm.STEPS): {"provider", "model", "effort"}; missing = the tier's model, usual effort
        "check_with": "auto",  # which model fact-checks drafts: "auto", "writing" or "analysis"
        "claude_code_path": "",  # the `claude` program; "" = find it (PATH, ~/.local/bin, the Claude desktop app)
    },
    "automation": {
        "auto_score": True,
        "auto_draft": True,
        "auto_draft_threshold": 80,
        "auto_draft_daily_cap": 5,
        "followup_days": 10,
        "ghost_days": 30,
        "notifications": True,
        "on_hold": False,  # True = nothing starts on its own (job sources, watched folders, unfinished work)
    },
    "writing": {
        "letter_words": 0,  # 0 = as long as your own past letters
        "language": "auto",  # auto = same language as the job ad
        "pdf_font": "sans",  # sans | serif
        "accent_color": "#1f4e79",
        "extra_banned_phrases": "",
    },
    "cv": {
        "base_id": None,  # which base CV (table cv_bases) tailored CVs start from
        "photo": "",  # your photo on the CV: a file name in the files folder; "" = no photo
        "primary": "#2254bf",  # the sidebar's colour; the light background is derived from it
        "font": "IBM Plex Serif",  # falls back to Charter or Georgia when it isn't installed
        "size": 9.6,  # body text size in points
        "sidebar": 34,  # sidebar width in percent
        "sidebar_order": ["skills", "certificates", "languages", "interests"],
        "main_order": ["work", "education", "projects"],
    },
    "knowledge": {
        "folder": "",  # the agent folder, e.g. ~/Nextcloud/Agents; "" = use <Nextcloud>/Agents if it exists
        "use": True,  # put its profile, summaries and style guides into the writing prompts
    },
    "backup": {  # see backup.py
        "enabled": True,  # back up automatically (once a folder is chosen)
        "folder": "",  # where the zips go, e.g. ~/Nextcloud/Backups/Career Agent; "" = no automatic backup yet
        "every_days": 7,
        "mode": "accumulate",  # accumulate = a new dated zip each time | overwrite = replace the one zip
        "keep": 0,  # with accumulate: how many of the newest to keep; 0 = all
    },
    "dismissed_facts": [],  # titles of facts you removed for good: reading a document again doesn't bring them back
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
