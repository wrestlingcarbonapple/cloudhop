"""Entry point: `uv run cloudhop` (or `python -m cloudhop`) starts everything and prints the address."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import webbrowser
from pathlib import Path

from cloudhop import __version__
from cloudhop.jobs import JobStore
from cloudhop.rclone import Rclone
from cloudhop.runner import RunManager
from cloudhop.server import App, serve
from cloudhop.store import DataDir, Settings, default_data_dir


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cloudhop", description="Web UI for rclone transfer jobs.")
    parser.add_argument("--port", type=int, default=8722, help="web UI port (default 8722)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on; use 0.0.0.0 to allow other devices (default: this computer only)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help=f"where settings, jobs and logs are stored (default {default_data_dir()})")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    parser.add_argument("--version", action="version", version=f"cloudhop {__version__}")
    args = parser.parse_args(argv)

    data = DataDir((args.data_dir or default_data_dir()).expanduser().resolve())
    settings = Settings(data)
    rclone = Rclone(data, settings)
    jobs = JobStore(data)
    runs = RunManager(data, settings, rclone, jobs)

    print(f"cloudhop {__version__}")
    print(f"  data folder: {data.root}")
    if not rclone.binary():
        print("  rclone not found - downloading the official release (one time only)…")
        try:
            print(f"  installed rclone {rclone.install()}")
        except Exception as e:
            print(f"  could not download rclone: {e}\n  You can retry from Settings in the web page.")
    else:
        print(f"  rclone {rclone.version()} at {rclone.binary()}")

    app = App(settings, rclone, jobs, runs, token=data.access_token())
    server = serve(app, args.host, args.port)
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown_host}:{server.server_address[1]}/?token={app.token}"

    threading.Thread(target=runs.loop, daemon=True).start()

    print()
    print("  Open cloudhop in your browser:")
    print(f"    {url}")
    if args.host in ("0.0.0.0", "::"):
        print("  (other devices: replace 127.0.0.1 with this computer's network address)")
    print()
    print("  Keep this window open while transfers run. Press Ctrl+C to stop.")
    sys.stdout.flush()

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def shutdown(*_):
        print("\nStopping cloudhop… (unfinished runs resume next time you start it)")
        runs.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
