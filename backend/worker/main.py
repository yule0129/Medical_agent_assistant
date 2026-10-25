from __future__ import annotations

import argparse
import time

from ..db import SessionLocal
from .processor import run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Health Agent persistent event worker")
    parser.add_argument("--once", action="store_true", help="process one queued event")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()
    if args.poll_interval < 0.1:
        parser.error("poll interval must be at least 0.1 seconds")
    try:
        while True:
            try:
                with SessionLocal() as db:
                    result = run_once(db)
                    if result:
                        print(result, flush=True)
            except Exception as exc:
                if args.once:
                    raise
                print({"status": "WORKER_ERROR", "error_type": type(exc).__name__}, flush=True)
                time.sleep(max(args.poll_interval, 1))
                continue
            if args.once:
                break
            if result is None:
                time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
