"""API keys: environment variable first, then the macOS Keychain (or a 0600 file elsewhere)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from . import config

SERVICE = "career-agent"


def _use_keychain() -> bool:
    return sys.platform == "darwin" and os.environ.get("CAREER_AGENT_SECRETS") != "file"


def _file():
    return config.DATA_DIR / "secrets.json"


def get(name: str) -> str:
    env = os.environ.get(name)
    if env:
        return env.strip()
    if _use_keychain():
        res = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
            capture_output=True, text=True,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    try:
        return json.loads(_file().read_text()).get(name, "")
    except (OSError, ValueError):
        return ""


def set(name: str, value: str) -> None:  # noqa: A001
    value = value.strip()
    if _use_keychain():
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value],
            capture_output=True, text=True, check=True,
        )
        return
    config.ensure_dirs()
    path = _file()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data[name] = value
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def delete(name: str) -> None:
    if _use_keychain():
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", name],
                       capture_output=True, text=True)
        return
    path = _file()
    try:
        data = json.loads(path.read_text())
        data.pop(name, None)
        path.write_text(json.dumps(data))
    except (OSError, ValueError):
        pass


def source(name: str) -> str:
    """Where a key comes from, for display: 'environment', 'keychain', 'file' or ''."""
    if os.environ.get(name):
        return "environment"
    if get(name):
        return "keychain" if _use_keychain() else "file"
    return ""
