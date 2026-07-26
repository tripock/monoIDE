#!/usr/bin/env python3
"""Entry point for the python side of Mono IDE.

    python main.py                        # serve the current directory
    python main.py /path/to/project       # serve another directory
    python main.py . --port 4400 --open   # and open a browser tab

The desktop app (electron/) spawns this file with an explicit --port and then
renders the UI itself, so no browser is involved there.

The bundled notion2api in vendor/notion2api is started automatically. Use
--base-url (or --no-embedded-api) when you want to point at your own instance.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ide.server import serve  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="monochrome agentic editor")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4321)
    parser.add_argument("--base-url", help="external notion2api base url, e.g. http://127.0.0.1:8000/v1")
    parser.add_argument("--api-key", help="notion2api API_KEY")
    parser.add_argument("--model", help="default model id")
    parser.add_argument("--api-port", type=int, help="preferred port for the bundled notion2api")
    parser.add_argument(
        "--no-embedded-api",
        action="store_true",
        help="do not start the bundled notion2api (use an already running one)",
    )
    parser.add_argument("--open", action="store_true", help="open a browser tab")
    args = parser.parse_args()

    if args.base_url:
        os.environ["MONOIDE_BASE_URL"] = args.base_url
        os.environ["MONOIDE_EXTERNAL_UPSTREAM"] = "1"
    if args.no_embedded_api:
        os.environ["MONOIDE_EXTERNAL_UPSTREAM"] = "1"
    if args.api_key:
        os.environ["MONOIDE_API_KEY"] = args.api_key
    if args.model:
        os.environ["MONOIDE_MODEL"] = args.model
    if args.api_port:
        os.environ["MONOIDE_API_PORT"] = str(args.api_port)

    root = Path(args.workspace).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")

    if args.open:
        url = "http://%s:%d" % (args.host, args.port)
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    serve(str(root), args.host, args.port)


if __name__ == "__main__":
    main()
