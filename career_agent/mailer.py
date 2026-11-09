"""Create a ready-to-review email draft in Apple Mail, with the PDFs attached. You press Send."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = """
on run argv
  set theSubject to item 1 of argv
  set theBody to item 2 of argv
  set theTo to item 3 of argv
  tell application "Mail"
    set newMessage to make new outgoing message with properties {subject:theSubject, content:theBody & return & return, visible:true}
    tell newMessage
      if theTo is not "" then
        make new to recipient at end of to recipients with properties {address:theTo}
      end if
      repeat with i from 4 to count of argv
        make new attachment with properties {file name:(POSIX file (item i of argv))} at after the last paragraph
      end repeat
    end tell
    activate
  end tell
end run
"""


class MailError(Exception):
    pass


def create_draft(subject: str, body: str, to: str | None, attachments: list[Path]) -> None:
    if sys.platform != "darwin":
        raise MailError("Mail drafts need macOS Mail.")
    args = ["osascript", "-e", SCRIPT, subject, body, to or ""] + [str(p) for p in attachments]
    res = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        msg = res.stderr.strip()
        if "Not authorized" in msg or "-1743" in msg:
            raise MailError("macOS blocked access to Mail. Allow it in System Settings → Privacy & Security → Automation.")
        raise MailError(f"Mail couldn't create the draft: {msg[:300]}")
