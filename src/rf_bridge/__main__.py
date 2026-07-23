"""CLI entry: python -m rf_bridge  [--allow-lan] [--port 20030]

Default port 30001. Binds 127.0.0.1 by default (radio control surface — keep it
local unless you explicitly open it). --allow-lan opens 0.0.0.0 so the Web
frontend can live on another machine (e.g. develop on Windows, hardware on Kali).
"""
from __future__ import annotations
import argparse
import os

from waitress import serve

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser("rf_bridge")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--allow-lan", action="store_true",
                    help="bind 0.0.0.0 (frontend on another host)")
    ap.add_argument("--port", type=int, default=30001)
    ap.add_argument("--captures",
                    default=os.path.expanduser("~/HackRF_One_Toolkit/captures"))
    args = ap.parse_args()

    host = "0.0.0.0" if args.allow_lan else args.bind
    app = create_app(args.captures)
    print(f"[RF_Bridge {app.name}] http://{host}:{args.port}  captures={args.captures}")
    serve(app, host=host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
