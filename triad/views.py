"""What each party is allowed to see: its own private state, its own checker's verdicts and
costs, the public situation and the public log. Nothing from the other parties' private data.
"""

from __future__ import annotations

from typing import Any

from . import config
from .checkers import fuel_floor_kg
from .messages import Candidate, Party3
from .scenario import Scenario
from .solver import Evaluation


def party_candidate(s: Scenario, party: Party3, e: Evaluation, utility: float, threshold: float) -> Candidate:
    vs = e.verdicts[party]
    c = e.costs[party]
    p, d = e.proposal, e.derived
    feasible = all(v.ok for v in vs)
    if p.pilot_action == "DIVERT":
        summary = f"DIVERT to {s.pilot.fuel.alternate}"
    else:
        summary = (f"slot {p.slot_id} ({s.t(d.arrival_min)}, {d.delay_vs_eta_min:+d} min vs ETA), "
                   f"{p.pilot_action}, gate {p.gate}, {p.crew_plan}")
    return Candidate(proposal=p, feasible_for_me=feasible, my_cost=c.total, my_utility=round(utility, 3),
                     acceptable_for_me=feasible and utility >= threshold,
                     my_breakdown=c.breakdown, verdicts=vs, summary=summary)


def private_state(s: Scenario, party: Party3) -> dict[str, Any]:
    if party == "ATC":
        a = s.atc
        return {
            "arrival_rate_nominal_per_15min": a.nominal_aar_per_15,
            "arrival_rate_effective_per_15min": a.effective_aar_per_15,
            "arrival_runways": a.arrival_runways,
            "planned_arrivals_per_bin": {s.t(b): n for b, n in a.demand_per_bin.items()},
            "max_airborne_holding_min": a.max_hold_min,
            "slots": [f"{x.id}={x.label}" for x in a.slots],
            "this_aircraft_wake": s.aircraft.wake,
        }
    if party == "PILOT":
        f = s.pilot.fuel
        return {
            "eta": s.t(s.pilot.eta_min),
            "earliest_possible_arrival": s.t(s.pilot.earliest_arr_min),
            "fuel_on_board_kg": f.fuel_on_board_kg,
            "legal_fuel_floor_kg_at_landing": round(fuel_floor_kg(s)),
            "hold_burn_kg_per_min": f.hold_burn_kg_min,
            "alternate": f"{f.alternate} ({f.alternate_nm} nm)",
            "turbulence_in_hold": f.turbulence_hold_penalty,
            "passengers": s.pilot.pax_total,
            "connecting_passengers": [
                {"flight": c.number, "to": c.dest, "departs": s.t(c.sched_dep_min), "pax": c.pax,
                 "min_connect_min": c.min_connect_min} for c in s.pilot.connections],
            "note": "diversion is not negotiable here; if no landing option is legal the duty manager decides",
        }
    g = s.ground
    return {
        "original_gate": g.original_gate,
        "gates": [{"id": x.id, "size": x.size, "busy": [f"{s.t(a)}-{s.t(b)}" for a, b in x.busy],
                   "walk_to_connections_min": x.walk_to_connections_min} for x in g.gates],
        "remote_stand_bus_min": g.remote_stand_bus_min,
        "taxi_in_min": g.on_block_taxi_min,
        "min_turnaround_min": g.min_turnaround_min,
        "next_leg": (f"{s.flight.airline}{g.next_leg.number} to {g.next_leg.dest} scheduled "
                     f"{s.t(g.next_leg.sched_dep_min)}") if g.next_leg else None,
        "operating_crew_duty_ends": s.t(g.crew_duty_end_min) if g.crew_duty_end_min else None,
    }


def situation_text(s: Scenario, max_rounds: int | None = None) -> str:
    f = s.flight
    max_rounds = max_rounds or config.MAX_ROUNDS
    return (f"{s.disruption.note} Decision time {s.t(s.now_min)}. {f.airline}{f.number} {f.origin}->{f.dest}, "
            f"{s.aircraft.name}, scheduled arrival {f.sched_arr}. Arrival slots are 5 minutes apart; "
            f"REDUCE_SPEED absorbs up to 10 minutes en route and holds the remainder; HOLD takes it all "
            f"in the stack; SPEED_UP only for slots before the ETA. The negotiation is capped at "
            f"{max_rounds} rounds; round {max_rounds} is a vote (ACCEPT or REJECT only); without "
            f"unanimous ACCEPT a deterministic solver decides and the outcome is flagged for review.")
