"""
Entry point: python3 -m viz <topology.json> [--port PORT] [--no-browser]
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m viz",
        description="MeshCore topology visualiser",
    )
    parser.add_argument("topology", help="Path to topology JSON file")
    parser.add_argument(
        "--port", type=int, default=8050, help="HTTP port (default: 8050)"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open a browser tab automatically",
    )
    parser.add_argument(
        "--trace",
        default=None,
        metavar="FILE",
        help="Packet trace JSON produced by --trace-out (enables Phase 2 overlay)",
    )
    args = parser.parse_args()

    path = Path(args.topology)
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    trace_path = Path(args.trace) if args.trace else None
    if trace_path is not None and not trace_path.exists():
        print(f"error: trace file not found: {trace_path}", file=sys.stderr)
        sys.exit(1)

    app = create_app(path, trace_path=trace_path)
    url = f"http://127.0.0.1:{args.port}"

    if not args.no_browser:
        def _open_browser() -> None:
            time.sleep(1.2)
            webbrowser.open(url)
        threading.Thread(target=_open_browser, daemon=True).start()

    print(f"Visualiser: {url}  (Ctrl-C to quit)", file=sys.stderr)
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
