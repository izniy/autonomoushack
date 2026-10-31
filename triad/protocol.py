"""Orchestrator entry point: runs at most MAX_ROUNDS rounds of the three-party negotiation
and decides by rule, never by persuasion.

The rules themselves live in `judge.py`. This module drives the loop: ask the three agents
in parallel, hand each answer (or its absence) to the judge as a `Reply`, stop when the judge
says so. If the agents do not reach explicit unanimous ACCEPT within the round cap, or
anything goes wrong, the deterministic solver decides and the outcome is flagged.
"""

from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from . import config
from .judge import Judge, Outcome, Reply, RoundRecord  # noqa: F401  (re-exported)
from .ledger import Ledger
from .messages import PARTIES, AgentView, Party3, parse_message
from .proposal import Proposal
from .scenario import Scenario


class Agent(Protocol):
    party: Party3

    def respond(self, view: AgentView, error: str | None = None) -> str | dict: ...

    def meta(self) -> dict[str, Any]: ...


def ask(agent: Agent, view: AgentView) -> Reply:
    """One schema retry, then abstain. A provider error (timeout, API failure) abstains at once:
    no repair call is spent on it, and the traceback is kept."""
    raw: dict = {}
    err = None
    for attempt in range(1 + config.SCHEMA_RETRIES):
        t0 = time.time()
        try:
            out = agent.respond(view, error=err)
            raw[f"attempt_{attempt}"] = out if isinstance(out, (dict, str)) else str(out)
            raw[f"attempt_{attempt}_latency_s"] = round(time.time() - t0, 3)
            _telemetry(agent, raw, attempt)
            return Reply(parse_message(out, view.party), "", raw)
        except ValueError as e:
            err = f"schema error: {e}"
            raw[f"attempt_{attempt}_error"] = err
        except Exception as e:  # noqa: BLE001  (API errors, timeouts)
            err = f"agent error: {type(e).__name__}: {e}"
            raw[f"attempt_{attempt}_error"] = err
            raw["traceback"] = traceback.format_exc()[-800:]
            _telemetry(agent, raw, attempt)
            break
    return Reply(None, err or "no response", raw)


def _telemetry(agent: Agent, raw: dict, attempt: int) -> None:
    """Agents that make model calls expose `last_call` (model, prompt hash, usage, latency,
    stop reason); it goes into the ledger next to the message it produced."""
    call = getattr(agent, "last_call", None)
    if isinstance(call, dict):
        raw[f"attempt_{attempt}_call"] = dict(call)


def run(s: Scenario, agents: dict[Party3, Agent], ledger_path: Path | None = None,
        max_rounds: int | None = None, parallel: bool = True) -> tuple[Outcome, Ledger]:
    judge = Judge(s, ledger_path=ledger_path, max_rounds=max_rounds)
    return drive(judge, agents, parallel), judge.ledger


def drive(judge: Judge, agents: dict[Party3, Agent], parallel: bool = True) -> Outcome:
    """Run the rounds against an already constructed judge (the API builds the judge itself so
    it can observe the ledger and answer questions about the option space afterwards)."""
    t0 = time.time()
    if not judge.unresolved:
        for rnd in range(1, judge.max_rounds + 1):
            views = judge.start_round(rnd)
            if parallel:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    replies = dict(zip(PARTIES, ex.map(ask, [agents[p] for p in PARTIES], [views[p] for p in PARTIES])))
            else:
                replies = {p: ask(agents[p], views[p]) for p in PARTIES}
            if judge.judge_round(rnd, replies).stop:
                break
    return judge.finish({p: agents[p].meta() for p in PARTIES}, time.time() - t0)


def describe_outcome(s: Scenario, o: Outcome) -> str:
    from .solver import describe_eval

    lines = [f"Negotiation {o.scenario_id}: {o.decision_source}"
             + (f" ({o.fallback_code}: {o.fallback_reason})" if o.fallback_reason else "")
             + f" after {o.rounds_used} round(s), {o.wall_s}s; "
             f"{o.n_feasible_landing} feasible, {o.n_mutually_acceptable} clear every threshold ({o.acceptance_threshold})"]
    for r in o.rounds:
        lines.append(f"  round {r.round}: on table {r.on_table}")
        for p in PARTIES:
            m = r.messages.get(p, {})
            key = (m.get("proposal") or {})
            key = Proposal.model_validate(key).key() if key else ""
            lines.append(f"    {p:6} {m.get('decision', '?'):8} {key:36} -> {r.outcomes.get(p, '')}"
                         + (f"  | {m['rationale'][:90]}" if m.get("rationale") else ""))
    lines.append(describe_eval(s, o.final, "FINAL"))
    lines.append(describe_eval(s, o.fifo, "FIFO "))
    lines.append(describe_eval(s, o.optimum, "OPT  "))
    u = " ".join(f"{k}={v:.2f}" for k, v in o.utilities["final"].items())
    lines.append(f"  utilities(final) {u}; saving vs FIFO {o.saving_vs_fifo:+,.0f}; gap to optimum {o.gap_vs_optimum:,.0f}; "
                 f"counters {o.counters_attempted} (infeasible caught {o.infeasible_counters_caught}, "
                 f"non-concession {o.non_concessions}, late {o.late_counters}); self-violations {o.self_violations}; "
                 f"unfounded rejects {o.unfounded_rejects}; abstains {o.abstains}")
    if o.diversion_assessment:
        lines.append(f"  diversion assessment: {o.diversion_assessment}")
    return "\n".join(lines)
