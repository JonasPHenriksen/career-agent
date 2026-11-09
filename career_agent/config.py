"""Paths and runtime configuration.

All personal data lives in DATA_DIR (default: ./data next to the code). Override with
the CAREER_AGENT_DATA environment variable or `python -m career_agent --data <dir>`.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Career Agent"
ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

HOST = os.environ.get("CAREER_AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CAREER_AGENT_PORT", "8765"))

DATA_DIR: Path
DB_PATH: Path
FILES_DIR: Path
EXPORTS_DIR: Path


def configure(data_dir: str | os.PathLike | None = None) -> None:
    """(Re)point every data path at `data_dir`."""
    global DATA_DIR, DB_PATH, FILES_DIR, EXPORTS_DIR
    base = data_dir or os.environ.get("CAREER_AGENT_DATA") or (ROOT / "data")
    DATA_DIR = Path(base).expanduser().resolve()
    DB_PATH = DATA_DIR / "career.db"
    FILES_DIR = DATA_DIR / "files"
    EXPORTS_DIR = DATA_DIR / "exports"


def ensure_dirs() -> None:
    for d in (DATA_DIR, FILES_DIR, EXPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


configure()
