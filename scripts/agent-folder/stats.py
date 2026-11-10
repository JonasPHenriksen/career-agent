#!/usr/bin/env python3
"""Writing statistics per document, measured with Career Agent's own voice code.

Usage: uv run --project <repo> python -I scripts/agent-folder/stats.py FILE [FILE ...]
One JSON line per file: words, sentence length, paragraph length, commas, sentences starting with I/Jeg, em dashes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
from career_agent import ingest, voice  # noqa: E402
from extract import text_of  # noqa: E402


def main(paths: list[str]) -> None:
    for arg in paths:
        p = Path(arg).expanduser()
        try:
            st = voice.text_stats(ingest.clean_text(text_of(p)))
            print(json.dumps({"file": p.name, **st}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 - one bad file shouldn't stop the rest
            print(json.dumps({"file": p.name, "error": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
