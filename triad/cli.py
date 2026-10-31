"""Command-line entry points."""

from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="triad")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scenario", help="build and print a disruption scenario")
    sp.add_argument("--seed", type=int)
    sp.add_argument("--dest")
    sp.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "scenario":
        from .scenario import describe, random_scenario

        s = random_scenario(seed=args.seed, dest=args.dest)
        print(json.dumps(s.model_dump(), indent=1) if args.json else describe(s))


if __name__ == "__main__":
    main()
