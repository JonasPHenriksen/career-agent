"""macOS notifications (Notification Center) via osascript."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from . import settings


def send(title: str, message: str) -> None:
    if os.environ.get("CAREER_AGENT_NO_NOTIFY") or sys.platform != "darwin":
        return
    try:
        if not settings.get("automation").get("notifications", True):
            return
        script = f"display notification {json.dumps(message[:240])} with title {json.dumps('Career Agent')} subtitle {json.dumps(title[:120])}"
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - notifications are best effort
        pass
