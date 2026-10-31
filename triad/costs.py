"""Per-party cost functions. Deterministic; agents call these, they never compute costs.

All costs are expressed in the same USD-like unit through the public `Weights`, so the
orchestrator can add them into a system cost. Each party sees its own breakdown in full.
"""

from __future__ import annotations

from pydantic import BaseModel

from .proposal import REMOTE_STAND, RESERVE_CREW_COST, Derived, Proposal, derive
from .scenario import Scenario


class Cost(BaseModel):
    party: str
    total: float
    breakdown: dict[str, float]


def cost_atc(s: Scenario, p: Proposal, d: Derived) -> Cost:
    w = s.weights
    if p.pilot_action == "DIVERT":
        b = {"divert_coordination": 0.1 * w.divert}
        return Cost(party="ATC", total=round(sum(b.values()), 1), breakdown=b)
    demand = s.atc.demand_per_bin.get(d.bin_start_min, 0) + 1
    cap = s.atc.effective_aar_per_15
    # Pressure on the bank: filling the last 10% of a bin forces vectoring of others.
    pressure = max(0.0, demand - 0.9 * cap)
    b = {
        "holding_workload": 20.0 * d.hold_min,
        "bin_pressure": w.empty_slot * pressure,
        "speed_change_coordination": 150.0 if p.pilot_action in ("SPEED_UP", "REDUCE_SPEED") else 0.0,
    }
    return Cost(party="ATC", total=round(sum(b.values()), 1), breakdown=b)


def cost_pilot(s: Scenario, p: Proposal, d: Derived) -> Cost:
    w = s.weights
    if p.pilot_action == "DIVERT":
        b = {"diversion": w.divert, "missed_pax": w.missed_pax * d.missed_pax,
             "fuel": w.fuel_kg * d.fuel_used_kg}
        return Cost(party="PILOT", total=round(sum(b.values()), 1), breakdown=b)
    # Only the controllable part of the delay is costed: whatever was lost before the
    # decision point is sunk and identical across proposals.
    b = {
        "arrival_delay": w.delay_min * max(0, d.delay_vs_eta_min),
        "fuel": w.fuel_kg * d.fuel_used_kg,
        "missed_pax": w.missed_pax * d.missed_pax,
        "turbulence_exposure": 30.0 * d.hold_min if s.pilot.fuel.turbulence_hold_penalty else 0.0,
    }
    return Cost(party="PILOT", total=round(sum(b.values()), 1), breakdown=b)


def _min_next_leg_delay(s: Scenario) -> int:
    """Next-leg delay that no proposal can avoid (already late inbound)."""
    g = s.ground
    if not g.next_leg:
        return 0
    earliest_ready = s.pilot.earliest_arr_min + g.on_block_taxi_min + g.min_turnaround_min
    return max(0, earliest_ready - g.next_leg.sched_dep_min)


def cost_ground(s: Scenario, p: Proposal, d: Derived) -> Cost:
    w = s.weights
    g = s.ground
    if p.pilot_action == "DIVERT":
        nl = g.next_leg
        b = {"next_leg_cancelled": 0.5 * w.divert if nl else 0.0, "gate_release": 0.0}
        return Cost(party="GROUND", total=round(sum(b.values()), 1), breakdown=b)
    overtime = 0
    if g.next_leg and p.crew_plan == "SAME_CREW":
        overtime = max(0, d.next_leg_dep_min - g.next_leg.sched_dep_min)
    b = {
        "gate_change": w.gate_change * (1.5 if p.gate == REMOTE_STAND else 1.0) if d.gate_changed else 0.0,
        "remote_bus": 6.0 * s.pilot.pax_total if p.gate == REMOTE_STAND else 0.0,
        "next_leg_delay": w.delay_min * (d.next_leg_delay_min - _min_next_leg_delay(s)),
        "crew_overtime": w.crew_overtime_min * overtime,
        "reserve_crew_callout": RESERVE_CREW_COST if p.crew_plan == "RESERVE_CREW" else 0.0,
    }
    return Cost(party="GROUND", total=round(sum(b.values()), 1), breakdown=b)


COSTS = {"ATC": cost_atc, "PILOT": cost_pilot, "GROUND": cost_ground}


def cost(party: str, s: Scenario, p: Proposal, d: Derived | None = None) -> Cost:
    return COSTS[party](s, p, d or derive(s, p))


def cost_all(s: Scenario, p: Proposal, d: Derived | None = None) -> dict[str, Cost]:
    d = d or derive(s, p)
    return {k: fn(s, p, d) for k, fn in COSTS.items()}


def system_cost(costs: dict[str, Cost]) -> float:
    return round(sum(c.total for c in costs.values()), 1)
