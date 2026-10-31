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

    ng = sub.add_parser("negotiate", help="run the three-party negotiation on a scenario")
    _add_scenario_args(ng)
    ng.add_argument("--stub", nargs="?", const="greedy", metavar="POLICY",
                    help="use deterministic stub agents (greedy|yielding|agreeable|stubborn|reckless|broken|flaky)")
    ng.add_argument("--ledger", help="write the hash-chained ledger to this JSONL path")
    ng.add_argument("--json", action="store_true", help="print the outcome as JSON")

    vf = sub.add_parser("verify", help="verify a ledger file's hash chain")
    vf.add_argument("path")

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
    elif args.cmd == "negotiate":
        from pathlib import Path

        from . import config
        from .protocol import describe_outcome, run
        from .scenario import describe, random_scenario

        s = random_scenario(seed=args.seed, dest=args.dest, busy=not args.any_bank)
        if args.stub:
            from .stubs import stub_team

            agents = stub_team(args.stub)
        else:
            from .agents import llm_team

            agents = llm_team()
        path = Path(args.ledger) if args.ledger else config.RUNS_DIR / f"{s.id}.ledger.jsonl"
        outcome, ledger = run(s, agents, ledger_path=path)
        if args.json:
            print(outcome.model_dump_json(indent=1))
        else:
            print(describe(s))
            print()
            print(describe_outcome(s, outcome))
            print(f"  ledger: {path} ({len(ledger.entries)} entries, head {ledger.head[:12]})")
    elif args.cmd == "verify":
        from pathlib import Path

        from .ledger import verify_file

        r = verify_file(Path(args.path))
        print(("OK" if r.ok else "TAMPERED") + f": {r.entries} entries"
              + (f", first bad seq {r.first_bad_seq}: {r.reason}" if not r.ok else ""))
        raise SystemExit(0 if r.ok else 1)


if __name__ == "__main__":
    main()
