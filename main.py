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
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# A windowed PyInstaller build (build_app.ps1 passes -NoConsole) has no stdio
# when it is launched by anything other than Electron, and every print() below
# would then raise AttributeError on None.
if sys.stdout is None:  # pragma: no cover - only true in the frozen GUI build
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:  # pragma: no cover
    sys.stderr = sys.stdout

from ide.server import serve  # noqa: E402


def prewarm() -> int:
    """Install the notion2api dependencies and exit. No server, no workspace.

    The desktop app runs this the moment its window opens, so pip is already
    working while the user is still picking a project folder. Progress is
    printed as one JSON object per line for electron/main.js to parse.
    """
    from ide.deps import DependencyError, DepsInstaller  # noqa: PLC0415
    from ide.supervisor import find_vendored  # noqa: PLC0415

    def say(payload: dict) -> None:
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    runtime = find_vendored()
    if runtime is None:
        say({"t": "deps", "state": "skipped", "phase": "vendor/notion2api is not in this build"})
        return 0

    installer = DepsInstaller(
        runtime, emit=lambda kind, text: say({"t": "deps", kind: text})
    )
    try:
        installer.ensure()
    except DependencyError as exc:
        # Not fatal: the backend retries and produces the full diagnostic report.
        say({"t": "deps", "state": "failed", "error": str(exc)})
        return 1
    say({"t": "deps", "state": "ready", "phase": "dependencies ready"})
    return 0


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
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help="only install the notion2api dependencies, then exit",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="run the startup checks, print the full diagnostic report, then exit",
    )
    args = parser.parse_args()

    if args.prewarm:
        raise SystemExit(prewarm())

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

    if args.diagnose:
        from ide.server import Workspace  # noqa: PLC0415

        workspace = Workspace(root)
        workspace.boot_sequence()
        print(workspace.boot.report())
        if workspace.boot.report_path:
            print("[ide] saved to %s" % workspace.boot.report_path)
        workspace.shutdown()
        raise SystemExit(2 if workspace.boot.failures else 0)

    if args.open:
        url = "http://%s:%d" % (args.host, args.port)
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # Non-zero when a required component failed: the IDE refuses to serve a
    # broken setup, and prints where the diagnostic report was written.
    raise SystemExit(serve(str(root), args.host, args.port))


if __name__ == "__main__":
    main()
