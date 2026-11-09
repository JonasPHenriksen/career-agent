"""Start Career Agent: `uv run python -m career_agent` (or ./start.sh)."""
from __future__ import annotations

import argparse
import logging
import os
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(prog="career_agent", description="Your personal career agent, running on this Mac.")
    parser.add_argument("--host", default=None, help="Address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port (default 8765)")
    parser.add_argument("--data", default=None, help="Data folder (default ./data)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    args = parser.parse_args()

    from . import config

    if args.data:
        os.environ["CAREER_AGENT_DATA"] = args.data
    config.configure(args.data)
    host = args.host or config.HOST
    port = args.port or config.PORT

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"\n  Career Agent is running at {url}\n  Your data: {config.DATA_DIR}\n  Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("career_agent.web.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
