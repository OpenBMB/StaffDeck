from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.documents.model_manager import RapidDocModelManager


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare or validate RapidDoc model artifacts")
    parser.add_argument("--model-dir", default=None, help="Explicit RapidDoc model directory")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate model readiness only; never prepare or download models",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manager = RapidDocModelManager(model_dir=args.model_dir)

    if args.check_only:
        readiness = manager.check_readiness()
        if readiness.ready:
            print(f"READY {readiness.model_dir}")
            if readiness.version:
                print(f"VERSION {readiness.version}")
            return 0
        print(f"MISSING {readiness.model_dir}")
        if readiness.missing:
            print("MISSING_ITEMS " + ", ".join(readiness.missing))
        return 1

    readiness = manager.prepare()
    print(f"READY {readiness.model_dir}")
    if readiness.version:
        print(f"VERSION {readiness.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
