"""Batch evaluation: the same seeded scenarios through several negotiation arms.

Every run already carries its own FIFO and solver-optimum baselines (the Judge evaluates the
whole decision space before the first round), so the arms are the *negotiators*:

    greedy / yielding / agreeable / stubborn   deterministic stub policies (controls)
    llm[:model]                                the live role agents

Per arm we report: consensus rate (overall, and conditional on a mutually acceptable option
existing), rounds-to-consensus, fallback reason codes, infeasible counters attempted vs
caught, protocol violations by type, optimality gap vs the solver, saving vs FIFO, the
minimum party utility of the final decision vs FIFO and optimum, operational deltas vs FIFO
(delay, fuel, missed passengers), wall-clock, tokens and an estimated $ per scenario.

Outputs land in runs/eval/<tag>/: rows.jsonl (one row per run), summary.json, summary.md,
and ledgers/ (one hash-chained ledger per run).
"""

from __future__ import annotations

import json
import random
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .judge import Outcome
from .ledger import Ledger
from .protocol import run
from .scenario import Scenario, random_scenario

STUB_ARMS = ("greedy", "yielding", "agreeable", "stubborn")


def eval_seeds(n: int = config.EVAL_SEEDS_DEFAULT, base: int = config.EVAL_SEED_BASE) -> list[int]:
    seeds = [base + i for i in range(n)]
    clash = set(seeds) & set(config.DEMO_SEEDS)
    if clash:
        raise ValueError(f"eval seeds overlap demo seeds: {sorted(clash)}")
    return seeds


# ---------------------------------------------------------------- one run -> one row

def usage_from_ledger(ledger: Ledger) -> dict[str, float]:
    """Tokens, model latency and estimated cost summed over every model call recorded in
    MESSAGE/ABSTAIN entries (`attempt_<n>_call`, written by protocol.ask)."""
    tin = tout = tcache = 0
    lat: list[float] = []
    usd = 0.0
    for e in ledger.entries:
        if e.type not in ("MESSAGE", "ABSTAIN"):
            continue
        raw = e.payload.get("raw") or {}
        for k, call in raw.items():
            if not (k.endswith("_call") and isinstance(call, dict)):
                continue
            u = call.get("usage") or {}
            tin += int(u.get("input_tokens") or 0)
            tout += int(u.get("output_tokens") or 0)
            tcache += int(u.get("cache_read_input_tokens") or 0)
            if call.get("latency_s") is not None:
                lat.append(float(call["latency_s"]))
            if call.get("model") and u:
                usd += config.cost_usd(call["model"], u)
    return {"input_tokens": tin, "output_tokens": tout, "cache_read_tokens": tcache,
            "model_calls": len(lat), "model_latency_s": round(sum(lat), 3),
            "model_latency_max_s": round(max(lat), 3) if lat else 0.0, "usd": round(usd, 4)}


def row_for(arm: str, seed: int, s: Scenario, o: Outcome, ledger: Ledger) -> dict[str, Any]:
    use = usage_from_ledger(ledger)
    model = o.agent_meta.get("ATC", {}).get("model")
    fd, od = o.fifo.derived, o.final.derived
    hist = Counter(out for r in o.rounds for out in r.outcomes.values())
    return {
        "arm": arm, "seed": seed, "scenario_id": s.id, "model": model,
        "decision_source": o.decision_source, "fallback_code": o.fallback_code,
        "fallback_reason": o.fallback_reason, "rounds_used": o.rounds_used,
        "consensus": o.decision_source == "CONSENSUS",
        "n_feasible_landing": o.n_feasible_landing, "n_mutually_acceptable": o.n_mutually_acceptable,
        "final": o.final.proposal.key(), "fifo": o.fifo.proposal.key(), "optimum": o.optimum.proposal.key(),
        "system_cost_final": o.final.system_cost, "system_cost_fifo": o.fifo.system_cost,
        "system_cost_optimum": o.optimum.system_cost,
        "gap_vs_optimum": o.gap_vs_optimum, "saving_vs_fifo": o.saving_vs_fifo,
        "min_utility_final": round(min(o.utilities["final"].values()), 3),
        "min_utility_fifo": round(min(o.utilities["fifo"].values()), 3),
        "min_utility_optimum": round(min(o.utilities["optimum"].values()), 3),
        "utilities_final": o.utilities["final"],
        "delta_delay_min": od.delay_vs_eta_min - fd.delay_vs_eta_min,
        "delta_fuel_kg": round(od.fuel_used_kg - fd.fuel_used_kg, 1),
        "delta_missed_pax": od.missed_pax - fd.missed_pax,
        "delta_next_leg_delay_min": od.next_leg_delay_min - fd.next_leg_delay_min,
        "final_feasible": o.final.feasible,
        "counters_attempted": o.counters_attempted, "infeasible_counters_caught": o.infeasible_counters_caught,
        "non_concessions": o.non_concessions, "self_violations": o.self_violations,
        "unfounded_rejects": o.unfounded_rejects, "late_counters": o.late_counters,
        "regressive_counters": o.regressive_counters, "abstains": o.abstains, "gap_cause": o.gap_cause,
        "wall_s": round(o.wall_s, 3), **use,
        "ledger_entries": len(ledger.entries), "ledger_head": ledger.head,
        "outcome_hist": dict(hist),
    }


# ---------------------------------------------------------------- arms

@dataclass
class Arm:
    name: str
    model_id: str | None = None     # live model arm when set, else stub policy == name
    client: Any = None              # one API client shared by every run of a live arm

    @property
    def live(self) -> bool:
        return self.model_id is not None

    def agents(self):
        if self.live:
            from .agents import llm_team
            if self.client is None:
                import anthropic
                self.client = anthropic.Anthropic(max_retries=config.MODEL_MAX_RETRIES,
                                                  timeout=config.MODEL_TIMEOUT_S)
            return llm_team(self.model_id, client=self.client)
        from .stubs import stub_team
        return stub_team(self.name)


def parse_arm(spec: str) -> Arm:
    if spec in STUB_ARMS:
        return Arm(spec)
    if spec == "llm" or spec.startswith("llm:"):
        return Arm(spec, model_id=spec.partition(":")[2] or config.MODEL)
    raise ValueError(f"unknown arm {spec!r}; use one of {STUB_ARMS} or llm[:model-id]")


def run_one(arm: Arm, seed: int, out_dir: Path) -> dict[str, Any]:
    s = random_scenario(seed=seed)
    path = out_dir / "ledgers" / f"{arm.name.replace(':', '_')}-{seed}-{s.id}.jsonl"
    o, ledger = run(s, arm.agents(), ledger_path=path)
    return row_for(arm.name, seed, s, o, ledger)


# ---------------------------------------------------------------- summary

def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 3) if xs else None


def _ci95(xs: list[float], resamples: int = 2000, seed: int = 0) -> tuple[float, float] | None:
    """Bootstrap 95 % interval of the mean. Arms share seeds, so per-seed values are paired
    observations and the interval of a mean difference from FIFO or the optimum is exact to use."""
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(n)) / n for _ in range(resamples))
    return round(means[int(0.025 * resamples)], 1), round(means[int(0.975 * resamples)], 1)


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 3) if xs else None


def summarize_arm(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    resolvable = [r for r in rows if r["n_feasible_landing"] > 0]
    with_a = [r for r in rows if r["n_mutually_acceptable"] > 0]
    cons = [r for r in rows if r["consensus"]]
    neg = [r for r in rows if r["decision_source"] != "UNRESOLVED"]
    attempted = sum(r["counters_attempted"] for r in rows)
    caught = sum(r["infeasible_counters_caught"] for r in rows)
    return {
        "n": n,
        "model": rows[0]["model"] if rows else None,
        "consensus_rate": round(len(cons) / n, 3) if n else None,
        "consensus_rate_given_acceptable_option": round(
            sum(r["consensus"] for r in with_a) / len(with_a), 3) if with_a else None,
        "n_with_acceptable_option": len(with_a),
        "n_unresolved_no_feasible_landing": n - len(resolvable),
        "rounds_to_consensus_hist": dict(sorted(Counter(r["rounds_used"] for r in cons).items())),
        "decision_source": dict(Counter(r["decision_source"] for r in rows)),
        "fallback_codes": dict(Counter(r["fallback_code"] for r in rows if r["fallback_code"])),
        "counters_attempted": attempted,
        "infeasible_counters_caught": caught,
        "final_feasible_rate": round(sum(r["final_feasible"] for r in neg) / len(neg), 3) if neg else None,
        "violations": {
            "non_concessions": sum(r["non_concessions"] for r in rows),
            "self_violations": sum(r["self_violations"] for r in rows),
            "unfounded_rejects": sum(r["unfounded_rejects"] for r in rows),
            "late_counters": sum(r["late_counters"] for r in rows),
            "regressive_counters": sum(r["regressive_counters"] for r in rows),
            "abstains": sum(r["abstains"] for r in rows),
        },
        "never_dearer_than_fifo_rate": round(sum(r["saving_vs_fifo"] >= 0 for r in neg) / len(neg), 3) if neg else None,
        "gap_cause": dict(sorted(Counter(r["gap_cause"] for r in cons).items())),
        "gap_vs_optimum_mean": _mean([r["gap_vs_optimum"] for r in neg]),
        "gap_vs_optimum_median": _median([r["gap_vs_optimum"] for r in neg]),
        "share_at_optimum": round(sum(r["gap_vs_optimum"] <= 0 for r in neg) / len(neg), 3) if neg else None,
        "gap_vs_optimum_mean_consensus_only": _mean([r["gap_vs_optimum"] for r in cons]),
        "share_at_optimum_consensus_only": round(sum(r["gap_vs_optimum"] <= 0 for r in cons) / len(cons), 3) if cons else None,
        "gap_vs_optimum_ci95": _ci95([r["gap_vs_optimum"] for r in neg]),
        "saving_vs_fifo_mean": _mean([r["saving_vs_fifo"] for r in neg]),
        "saving_vs_fifo_ci95": _ci95([r["saving_vs_fifo"] for r in neg]),
        "saving_vs_fifo_mean_consensus_only": _mean([r["saving_vs_fifo"] for r in cons]),
        "min_utility_mean": {
            "final": _mean([r["min_utility_final"] for r in neg]),
            "fifo": _mean([r["min_utility_fifo"] for r in neg]),
            "optimum": _mean([r["min_utility_optimum"] for r in neg]),
        },
        "delta_vs_fifo_mean": {
            "delay_min": _mean([r["delta_delay_min"] for r in neg]),
            "fuel_kg": _mean([r["delta_fuel_kg"] for r in neg]),
            "missed_pax": _mean([r["delta_missed_pax"] for r in neg]),
            "next_leg_delay_min": _mean([r["delta_next_leg_delay_min"] for r in neg]),
        },
        "wall_s_mean": _mean([r["wall_s"] for r in rows]),
        "model_latency_s_mean": _mean([r["model_latency_s"] for r in rows]),
        "model_calls_mean": _mean([r["model_calls"] for r in rows]),
        "tokens_mean": {"input": _mean([r["input_tokens"] for r in rows]),
                        "cache_read": _mean([r["cache_read_tokens"] for r in rows]),
                        "output": _mean([r["output_tokens"] for r in rows])},
        "usd_mean": _mean([r["usd"] for r in rows]),
        "usd_total": round(sum(r["usd"] for r in rows), 4),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    arms = sorted({r["arm"] for r in rows})
    return {"arms": {a: summarize_arm([r for r in rows if r["arm"] == a]) for a in arms},
            "seeds": sorted({r["seed"] for r in rows}), "n_rows": len(rows)}


def _f(x: Any, fmt: str = ".2f") -> str:
    return "–" if x is None else format(x, fmt)


def _ci(ci: tuple[float, float] | list[float] | None) -> str:
    return "–" if not ci else f"[{ci[0]:,.0f}, {ci[1]:,.0f}]"


def summary_markdown(summary: dict[str, Any]) -> str:
    arms = summary["arms"]
    names = list(arms)
    lines = [(f"# Evaluation — {summary['n_rows']} runs over {len(summary['seeds'])} seeds "
             f"({summary['seeds'][0]}…{summary['seeds'][-1]})"), ""]
    lines.append("| metric | " + " | ".join(names) + " |")
    lines.append("|---|" + "---|" * len(names))

    def row(label: str, get) -> None:
        lines.append(f"| {label} | " + " | ".join(get(arms[a]) for a in names) + " |")

    row("n", lambda a: str(a["n"]))
    row("model", lambda a: a["model"] or "stub")
    row("consensus rate", lambda a: _f(a["consensus_rate"]))
    row("consensus rate given |A|>0", lambda a: f"{_f(a['consensus_rate_given_acceptable_option'])} (n={a['n_with_acceptable_option']})")
    row("rounds→consensus hist", lambda a: json.dumps(a["rounds_to_consensus_hist"]))
    row("decision source", lambda a: json.dumps(a["decision_source"]))
    row("fallback codes", lambda a: json.dumps(a["fallback_codes"]) or "{}")
    row("counters attempted / infeasible caught", lambda a: f"{a['counters_attempted']} / {a['infeasible_counters_caught']}")
    row("final decision passes all checkers", lambda a: _f(a["final_feasible_rate"]))
    row("violations", lambda a: json.dumps({k: v for k, v in a["violations"].items() if v}) or "{}")
    row("final never dearer than FIFO", lambda a: _f(a["never_dearer_than_fifo_rate"]))
    row("gap vs optimum mean / median", lambda a: f"{_f(a['gap_vs_optimum_mean'], '.0f')} / {_f(a['gap_vs_optimum_median'], '.0f')}")
    row("gap vs optimum 95 % CI", lambda a: _ci(a["gap_vs_optimum_ci95"]))
    row("share of runs at optimum", lambda a: _f(a["share_at_optimum"]))
    row("consensus runs only: gap mean / at optimum",
        lambda a: f"{_f(a['gap_vs_optimum_mean_consensus_only'], '.0f')} / {_f(a['share_at_optimum_consensus_only'])}")
    row("why consensus missed the optimum", lambda a: json.dumps(a["gap_cause"]))
    row("saving vs FIFO mean", lambda a: _f(a["saving_vs_fifo_mean"], ".0f"))
    row("saving vs FIFO 95 % CI", lambda a: _ci(a["saving_vs_fifo_ci95"]))
    row("consensus runs only: saving vs FIFO mean", lambda a: _f(a["saving_vs_fifo_mean_consensus_only"], ".0f"))
    row("min utility final / FIFO / opt", lambda a: " / ".join(_f(a["min_utility_mean"][k]) for k in ("final", "fifo", "optimum")))
    row("Δ vs FIFO: delay min / fuel kg / missed pax",
        lambda a: " / ".join(_f(a["delta_vs_fifo_mean"][k], ".1f") for k in ("delay_min", "fuel_kg", "missed_pax")))
    row("wall s mean", lambda a: _f(a["wall_s_mean"]))
    row("model latency s mean / calls", lambda a: f"{_f(a['model_latency_s_mean'])} / {_f(a['model_calls_mean'], '.1f')}")
    row("tokens in / out mean", lambda a: f"{_f(a['tokens_mean']['input'], '.0f')} / {_f(a['tokens_mean']['output'], '.0f')}")
    row("USD mean / total", lambda a: f"{_f(a['usd_mean'], '.4f')} / {_f(a['usd_total'], '.3f')}")
    lines.append("")
    lines.append("Every counter is re-evaluated by all three checkers before it can win a round, so "
                 "`infeasible caught` counts the counters that were tried and discarded; `final decision "
                 "passes all checkers` is the end-to-end safety check. `gap` and `saving` are system-cost "
                 "units (USD-like, public weights); the 95 % intervals are bootstrap intervals of the mean "
                 "over seeds. `final never dearer than FIFO` checks the no-regression invariant end to "
                 "end. `why consensus missed the optimum` attributes each consensus run's gap: the "
                 "optimum was never on any party's shortlist, was visible but never proposed, was "
                 "proposed but lost its round to a cheaper counter, or was on the table and voted down. "
                 "USD is an estimate from list prices in `config.PRICES_PER_MTOK`.")
    return "\n".join(lines)


# ---------------------------------------------------------------- driver

def run_eval(arm_specs: list[str], n: int | None = None, seeds: list[int] | None = None,
             tag: str | None = None, concurrency: int = 4, on_row=None
             ) -> tuple[list[dict[str, Any]], dict[str, Any], Path]:
    """Every (arm, seed) pair is one negotiation; `concurrency` of them run at a time. Rows are
    appended to rows.jsonl as they finish so a long live run is never lost wholesale."""
    arms = [parse_arm(a) for a in arm_specs]
    seeds = seeds or eval_seeds(n or config.EVAL_SEEDS_DEFAULT)
    out_dir = config.RUNS_DIR / "eval" / (tag or time.strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ledgers").mkdir(exist_ok=True)
    rows: list[dict[str, Any]] = []
    with (out_dir / "rows.jsonl").open("w") as fh, ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [ex.submit(run_one, arm, seed, out_dir) for arm in arms for seed in seeds]
        for fut in as_completed(futures):
            r = fut.result()
            rows.append(r)
            fh.write(json.dumps(r, default=str) + "\n")
            fh.flush()
            if on_row:
                on_row(r)
    rows.sort(key=lambda r: (r["arm"], r["seed"]))
    summary = summarize(rows)
    summary["tag"] = out_dir.name
    summary["config"] = {"max_rounds": config.MAX_ROUNDS, "acceptance_threshold": config.ACCEPTANCE_THRESHOLD,
                         "schema_retries": config.SCHEMA_RETRIES, "prices_per_mtok": config.PRICES_PER_MTOK}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    (out_dir / "summary.md").write_text(summary_markdown(summary))
    return rows, summary, out_dir
