from __future__ import annotations

import argparse

from ..db import SessionLocal
from .processor import run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Health Agent persistent event worker")
    parser.add_argument("--once", action="store_true", help="process one queued event")
    args = parser.parse_args()
    if not args.once:
        parser.error("当前版本只支持 --once，避免无边界轮询开发数据库")
    db = SessionLocal()
    try:
        print(run_once(db) or {"status": "idle"})
    finally:
        db.close()


if __name__ == "__main__":
    main()
