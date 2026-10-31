"""Deterministic evaluation of the proposal space: feasible set, FIFO baseline, optimum.

No model is involved anywhere in this module. The optimum doubles as the fallback decision
when the agents do not converge, and as the upper bound in the evaluation.
"""

from __future__ import annotations

from pydantic import BaseModel

from .checkers import check_all
from .costs import Cost, cost_all, system_cost
from .proposal import Derived, Proposal, derive, enumerate_proposals
from .rules import Verdict
from .scenario import Scenario


class Evaluation(BaseModel):
    proposal: Proposal
    derived: Derived
    verdicts: dict[str, list[Verdict]]
    costs: dict[str, Cost]
    system_cost: float

    @property
    def feasible(self) -> bool:
        return all(v.ok for vs in self.verdicts.values() for v in vs)

    def failures(self) -> list[Verdict]:
        return [v for vs in self.verdicts.values() for v in vs if not v.ok]

    def failed_rule_ids(self) -> list[str]:
        return [v.rule_id for v in self.failures()]


def evaluate(s: Scenario, p: Proposal) -> Evaluation:
    d = derive(s, p)
    verdicts = check_all(s, p, d)
    costs = cost_all(s, p, d)
    return Evaluation(proposal=p, derived=d, verdicts=verdicts, costs=costs,
                      system_cost=system_cost(costs))


def evaluate_all(s: Scenario) -> list[Evaluation]:
    return [evaluate(s, p) for p in enumerate_proposals(s)]


def feasible_set(s: Scenario, evals: list[Evaluation] | None = None) -> list[Evaluation]:
    return [e for e in (evals or evaluate_all(s)) if e.feasible]


def _slot_index(s: Scenario, p: Proposal) -> int:
    if p.slot_id is None:
        return 10_000
    return next(i for i, x in enumerate(s.atc.slots) if x.id == p.slot_id)


def optimum(s: Scenario, evals: list[Evaluation] | None = None) -> Evaluation | None:
    """Lowest system cost over the feasible set; ties broken by earliest slot, then by the
    smallest fuel margin consumed (prefer the option that keeps more reserve)."""
    feas = feasible_set(s, evals)
    if not feas:
        return None
    return min(feas, key=lambda e: (e.system_cost, _slot_index(s, e.proposal),
                                    -e.derived.fuel_remaining_kg))


def fifo_baseline(s: Scenario, evals: list[Evaluation] | None = None) -> Evaluation | None:
    """Status quo: take the first legal thing in the order people reach for it.

    Earliest slot, absorb the delay by holding, original gate if free else the first free
    gate else a remote stand, keep the operating crew unless that is illegal. No one looks
    at the downstream cost. If nothing legal exists short of diverting, divert.
    """
    feas = feasible_set(s, evals)
    if not feas:
        return None
    gate_rank = {g.id: i + 1 for i, g in enumerate(s.ground.gates)}
    gate_rank[s.ground.original_gate] = 0

    def key(e: Evaluation):
        p = e.proposal
        return (
            p.pilot_action == "DIVERT",
            _slot_index(s, p),
            0 if p.pilot_action in ("HOLD", "CONTINUE", "SPEED_UP") else 1,
            gate_rank.get(p.gate, 99),
            0 if p.crew_plan == "SAME_CREW" else 1,
        )

    return min(feas, key=key)


class Summary(BaseModel):
    n_total: int
    n_feasible: int
    fifo: Evaluation | None
    optimum: Evaluation | None
    infeasible_by_rule: dict[str, int]


def summarize(s: Scenario) -> Summary:
    evals = evaluate_all(s)
    by_rule: dict[str, int] = {}
    for e in evals:
        for rid in set(e.failed_rule_ids()):
            by_rule[rid] = by_rule.get(rid, 0) + 1
    return Summary(n_total=len(evals), n_feasible=sum(e.feasible for e in evals),
                   fifo=fifo_baseline(s, evals), optimum=optimum(s, evals),
                   infeasible_by_rule=dict(sorted(by_rule.items())))


def describe_eval(s: Scenario, e: Evaluation | None, title: str) -> str:
    if e is None:
        return f"{title}: no feasible proposal"
    p, d = e.proposal, e.derived
    where = "DIVERT to " + s.pilot.fuel.alternate if p.pilot_action == "DIVERT" else (
        f"slot {p.slot_id} {s.t(d.arrival_min)} ({p.pilot_action}, {d.delay_vs_eta_min:+d} min vs ETA)"
        f" gate {p.gate} crew {p.crew_plan}")
    parts = [f"{title}: {where}  system cost {e.system_cost:,.0f}"]
    parts.append("   " + "  ".join(f"{k} {c.total:,.0f}" for k, c in e.costs.items())
                 + f"  | missed pax {d.missed_pax}, fuel {d.fuel_used_kg:+.0f} kg, next leg {d.next_leg_delay_min:+d} min")
    return "\n".join(parts)
