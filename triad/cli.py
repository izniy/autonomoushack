"""Command-line entry points."""

from __future__ import annotations

import argparse
import json


def _add_scenario_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--seed", type=int)
    sp.add_argument("--dest")
    sp.add_argument("--any-bank", action="store_true",
                    help="do not skip quiet arrival banks when sampling")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="triad")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scenario", help="build and print a disruption scenario")
    _add_scenario_args(sp)
    sp.add_argument("--json", action="store_true")

    sv = sub.add_parser("solve", help="enumerate the decision space; show FIFO and optimum")
    _add_scenario_args(sv)
    sv.add_argument("--all", action="store_true", help="list every feasible proposal")

    args = p.parse_args(argv)
    if args.cmd == "scenario":
        from .scenario import describe, random_scenario

        s = random_scenario(seed=args.seed, dest=args.dest, busy=not args.any_bank)
        print(json.dumps(s.model_dump(), indent=1) if args.json else describe(s))
    elif args.cmd == "solve":
        from .scenario import describe, random_scenario
        from .solver import describe_eval, evaluate_all, feasible_set, summarize

        s = random_scenario(seed=args.seed, dest=args.dest, busy=not args.any_bank)
        print(describe(s))
        sm = summarize(s)
        print(f"\n{sm.n_feasible}/{sm.n_total} proposals feasible; infeasible by rule: {sm.infeasible_by_rule}")
        print(describe_eval(s, sm.fifo, "FIFO"))
        print(describe_eval(s, sm.optimum, "OPT "))
        if args.all:
            print()
            for e in sorted(feasible_set(s, evaluate_all(s)), key=lambda e: e.system_cost):
                print(f"  {e.system_cost:9,.0f}  {e.proposal.key()}")


if __name__ == "__main__":
    main()
