"""CLI entry: python -m hackrf_web [--allow-lan] [--port 20031] [--bridge-url URL]

Frontend only. Point --bridge-url at the RF_Bridge (default localhost; when you
develop on Windows with the radio on Kali, pass the Kali RF_Bridge URL).
"""
from __future__ import annotations
import argparse
import os

from waitress import serve

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser("hackrf_web")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--allow-lan", action="store_true", help="bind 0.0.0.0")
    ap.add_argument("--port", type=int, default=30000)
    ap.add_argument("--bridge-url",
                    default=os.environ.get("HACKRF_BRIDGE_URL", "http://127.0.0.1:30001"))
    args = ap.parse_args()

    host = "0.0.0.0" if args.allow_lan else args.bind
    app = create_app(args.bridge_url)
    print(f"[hackrf_web] http://{host}:{args.port}  → RF_Bridge {args.bridge_url}")
    serve(app, host=host, port=args.port, threads=16)


if __name__ == "__main__":
    main()
