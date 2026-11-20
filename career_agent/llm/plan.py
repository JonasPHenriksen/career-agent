"""Which Claude models your subscription runs through Claude Code, kept up to date as the plan changes.

Claude Code updates itself and carries the ID of every model it can use; your plan decides which of those you may.
So the app reads the IDs from the installed Claude Code program, asks each one for a single word through your
subscription, and keeps those that answer as themselves (a retired ID can be answered by another model, and then
you wouldn't get the version you picked). It checks again when Claude Code has been updated, once a week, and when
you press Check again in Settings. Until the first check, the list checked by hand on 2026-11-20 is used."""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from .. import db, keychain, settings
from .errors import LLMConfigError, LLMError, LLMTransient
from .providers import CLAUDE_CODE_EARLIER, CLAUDE_CODE_MODELS, ClaudeCodeProvider, claude_name, find_claude_binary

log = logging.getLogger("career_agent.llm.plan")

CHECK_EVERY_DAYS = 7
RETRY_HOURS = 6  # after a check that couldn't finish (usage limit, not logged in), wait this long before trying again
OLDEST_MAJOR = 4  # versions before Claude 4 are retired
NOT_MODELS = {"code", "eval", "cli", "sdk", "agent", "app", "desktop", "test", "internal"}  # "claude-code-2" etc.
HINTS = {"opus": "best writing", "sonnet": "fast, for analysis", "haiku": "fastest, lightest on your plan",
         "fable": "most capable"}
FAMILY_ORDER = ("opus", "sonnet", "haiku", "fable")

_ID = re.compile(rb"(?<![\w.-])claude-([a-z]{3,12})-(\d{1,2})(?:-(\d{1,2}))?(?![\w.-])")
_NO_MODEL = ("issue with the selected model", "may not exist", "not have access", "model not found", "invalid model")


def _version(model: str) -> tuple[str, int, int] | None:
    m = re.match(r"^claude-([a-z]+)-(\d+)(?:-(\d+))?$", model)
    return (m.group(1), int(m.group(2)), int(m.group(3) or 0)) if m else None


def _binary() -> str | None:
    cfg = settings.get("llm")
    return find_claude_binary(cfg.get("claude_code_path") or None)


def _stamp(binary: str) -> str:
    """Changes whenever Claude Code is updated."""
    st = os.stat(binary)
    return f"{binary}|{int(st.st_mtime)}|{st.st_size}"


def candidates(binary: str) -> list[str]:
    """Every model ID the Claude Code program knows, from Claude 4 on ("claude-opus-4-0" counts as "claude-opus-4")."""
    with open(binary, "rb") as fh:
        data = fh.read()
    found = set()
    for m in _ID.finditer(data):
        family, major, minor = m.group(1).decode(), int(m.group(2)), int(m.group(3) or 0)
        if family in NOT_MODELS or major < OLDEST_MAJOR:
            continue
        found.add(f"claude-{family}-{major}" + (f"-{minor}" if minor else ""))
    # Today's known versions too, so a model is never dropped only because the program spells its ID differently.
    found |= {m for m, _ in CLAUDE_CODE_MODELS + CLAUDE_CODE_EARLIER}
    return sorted(found)


def probe(model: str, binary: str, token: str = "") -> bool:
    """True if the plan runs `model` and that model itself answers. A usage limit or a missing login is raised:
    then nothing can be said about the plan."""
    prov = ClaudeCodeProvider(model, binary, token=token, effort="low", timeout=240)
    try:
        reply = prov.complete("Reply with the single word OK.", "OK?", None, 16)
    except (LLMTransient, LLMConfigError):
        raise
    except LLMError as e:
        if any(w in str(e).lower() for w in _NO_MODEL):
            return False
        raise LLMTransient(f"Couldn't check {claude_name(model)}: {str(e)[:200]}") from e
    return reply.model == f"claude-code/{model}"


def check() -> dict:
    """Ask every model Claude Code knows, and save which ones your plan runs. Tells you about models that came or went."""
    from .. import notify

    binary = _binary()
    if not binary:
        raise LLMConfigError("Claude Code wasn't found on this Mac.")
    if settings.get("llm").get("local_only"):
        raise LLMConfigError("Local-only mode is on, so Claude Code isn't asked.")
    settings.update("state", {"claude_models_tried_at": db.now()})
    token = keychain.get("CLAUDE_CODE_OAUTH_TOKEN") or ""  # llm.CLAUDE_CODE_TOKEN
    ids = candidates(binary)
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = dict(zip(ids, pool.map(lambda m: probe(m, binary, token), ids)))
    available = [m for m in ids if answers[m]]
    if not available:
        raise LLMTransient("No Claude model answered; your plan may be at its usage limit. Trying again later.")
    before = saved()
    old = set(before["available"]) if before else {m for m, _ in CLAUDE_CODE_MODELS + CLAUDE_CODE_EARLIER}
    came, went = sorted(set(available) - old), sorted(old - set(available))
    settings.update("state", {"claude_models": {"checked_at": db.now(), "claude_code": _stamp(binary),
                                                "available": available,
                                                "unavailable": [m for m in ids if not answers[m]]}})
    from .. import llm
    llm._choices_cache["at"] = 0.0  # the model pickers show the new list at once
    if came and before:  # the first check only sets the list
        notify.send("New Claude model on your plan", ", ".join(claude_name(m) for m in came) + " can be picked in Settings.")
    gone_in_use = [m for m in went if m in in_use()]
    if gone_in_use:
        notify.send("A Claude model you use is gone",
                    ", ".join(claude_name(m) for m in gone_in_use) + " is no longer on your plan. Pick another in Settings.")
    return {"available": available, "new": came, "gone": went}


def saved() -> dict | None:
    got = settings.get("state").get("claude_models")
    return got if isinstance(got, dict) and got.get("available") else None


def in_use() -> set[str]:
    """The Claude Code models your settings use (the two tiers and any step)."""
    cfg = settings.get("llm")
    picks = [cfg.get("writing") or {}, cfg.get("analysis") or {}, *(cfg.get("steps") or {}).values()]
    return {p["model"] for p in picks if p.get("provider") == "claude_code" and p.get("model")}


def due() -> bool:
    """Time for an automatic check: Claude Code is used, and it was updated or the last check is a week old."""
    if not in_use() or settings.get("llm").get("local_only"):
        return False
    binary = _binary()
    if not binary:
        return False
    state = settings.get("state")
    now = db.parse_ts(db.now())
    tried = db.parse_ts(state.get("claude_models_tried_at"))
    if tried and now - tried < timedelta(hours=RETRY_HOURS):
        return False
    last = saved()
    if not last:
        return True
    try:
        if last.get("claude_code") != _stamp(binary):
            return True
    except OSError:
        return False
    checked = db.parse_ts(last.get("checked_at"))
    return not checked or now - checked >= timedelta(days=CHECK_EVERY_DAYS)


def choices() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(newest of each kind, earlier versions) as [(model id, label)], from the last check (or the built-in list)."""
    last = saved()
    if not last:
        return CLAUDE_CODE_MODELS, CLAUDE_CODE_EARLIER
    models = [(m, v) for m in last["available"] if (v := _version(m))]

    def family_rank(family: str) -> tuple[int, str]:
        return (FAMILY_ORDER.index(family) if family in FAMILY_ORDER else len(FAMILY_ORDER), family)

    newest: dict[str, tuple[str, tuple]] = {}
    for m, v in models:
        if v[0] not in newest or v[1:] > newest[v[0]][1][1:]:
            newest[v[0]] = (m, v)
    top = [(m, f"{claude_name(m)} ({HINTS[v[0]]})" if v[0] in HINTS else claude_name(m))
           for m, v in sorted(newest.values(), key=lambda mv: family_rank(mv[1][0]))]
    rest = sorted(((m, v) for m, v in models if m not in {t for t, _ in top}),
                  key=lambda mv: (-mv[1][1], -mv[1][2], family_rank(mv[1][0])))
    return top, [(m, claude_name(m)) for m, _ in rest]


def status() -> dict:
    """What Settings shows: when the list was last checked and how many models the plan runs."""
    last = saved() or {}
    return {"checked_at": last.get("checked_at") or "", "count": len(last.get("available") or []),
            "tried_at": settings.get("state").get("claude_models_tried_at") or ""}
