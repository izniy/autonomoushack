"""The decision space: a proposal and the quantities derived from it.

A proposal is small and enumerable on purpose, so the solver is an exact loop and the
checkers can be exhaustive.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .scenario import DECISION_LEAD_MIN, Scenario

PilotAction = Literal["SPEED_UP", "CONTINUE", "REDUCE_SPEED", "HOLD", "DIVERT"]
CrewPlan = Literal["SAME_CREW", "RESERVE_CREW"]

REMOTE_STAND = "REMOTE"
MAX_SPEED_ABSORB_MIN = 10        # delay absorbable by slowing down over the remaining cruise
RESERVE_CREW_READY_MIN = 45      # reserve crew callout to ready, minutes after decision time
RESERVE_CREW_COST = 2500.0       # synthetic callout cost


class Proposal(BaseModel):
    slot_id: str | None            # None only for DIVERT
    pilot_action: PilotAction
    gate: str | None               # gate id, REMOTE, or None for DIVERT
    crew_plan: CrewPlan = "SAME_CREW"

    def key(self) -> str:
        return f"{self.slot_id or '-'}|{self.pilot_action}|{self.gate or '-'}|{self.crew_plan}"


class Derived(BaseModel):
    """Everything a checker or cost function needs, computed once per proposal."""
    arrival_min: int | None
    delay_vs_eta_min: int          # positive = later than ETA
    arrival_delay_min: int | None  # vs schedule
    hold_min: int
    fuel_used_kg: float            # above the planned burn to destination
    fuel_remaining_kg: float
    on_block_min: int | None
    next_leg_dep_min: int | None
    next_leg_delay_min: int
    missed_pax: int
    missed_connections: list[int]
    gate_changed: bool
    bin_start_min: int | None


def actions_for_delay(delay: int) -> list[PilotAction]:
    """REDUCE_SPEED absorbs up to MAX_SPEED_ABSORB_MIN en route and holds for the rest;
    HOLD takes the whole delay in the stack."""
    if delay < 0:
        return ["SPEED_UP"]
    if delay == 0:
        return ["CONTINUE"]
    return ["REDUCE_SPEED", "HOLD"]


def enumerate_proposals(s: Scenario) -> list[Proposal]:
    out: list[Proposal] = []
    gates = [g.id for g in s.ground.gates] + [REMOTE_STAND]
    crews: list[CrewPlan] = ["SAME_CREW", "RESERVE_CREW"] if s.ground.next_leg else ["SAME_CREW"]
    for slot in s.atc.slots:
        delay = slot.start_min - s.pilot.eta_min
        for act in actions_for_delay(delay):
            for g in gates:
                for c in crews:
                    out.append(Proposal(slot_id=slot.id, pilot_action=act, gate=g, crew_plan=c))
    out.append(Proposal(slot_id=None, pilot_action="DIVERT", gate=None))
    return out


def derive(s: Scenario, p: Proposal) -> Derived:
    f = s.pilot.fuel
    w = s.weights  # noqa: F841  (weights are applied in costs.py)
    if p.pilot_action == "DIVERT":
        used = f.alternate_fuel_kg
        remaining = f.fuel_on_board_kg - DECISION_LEAD_MIN * f.cruise_burn_kg_min - used
        return Derived(
            arrival_min=None, delay_vs_eta_min=0, arrival_delay_min=None, hold_min=0,
            fuel_used_kg=used, fuel_remaining_kg=remaining, on_block_min=None,
            next_leg_dep_min=None, next_leg_delay_min=0,
            missed_pax=sum(c.pax for c in s.pilot.connections),
            missed_connections=[c.number for c in s.pilot.connections],
            gate_changed=True, bin_start_min=None,
        )

    slot = next(x for x in s.atc.slots if x.id == p.slot_id)
    delay = slot.start_min - s.pilot.eta_min
    hold = delay
    if p.pilot_action == "REDUCE_SPEED":
        hold = max(0, delay - MAX_SPEED_ABSORB_MIN)
    elif p.pilot_action != "HOLD":
        hold = 0
    if p.pilot_action == "HOLD":
        used = delay * f.hold_burn_kg_min
    elif p.pilot_action == "REDUCE_SPEED":
        # slowing down saves a little over the absorbed minutes; the remainder is held
        used = -(delay - hold) * f.cruise_burn_kg_min * 0.15 + hold * f.hold_burn_kg_min
    elif p.pilot_action == "SPEED_UP":
        used = -delay * f.speed_up_extra_kg_min
    else:
        used = 0.0
    remaining = f.fuel_on_board_kg - DECISION_LEAD_MIN * f.cruise_burn_kg_min - used

    gnd = s.ground
    on_block = slot.start_min + gnd.on_block_taxi_min
    if p.gate == REMOTE_STAND:
        walk = gnd.remote_stand_bus_min + 8
    else:
        walk = next(g for g in gnd.gates if g.id == p.gate).walk_to_connections_min
    missed = [c for c in s.pilot.connections
              if on_block + walk + c.min_connect_min > c.sched_dep_min]

    next_dep = None
    next_delay = 0
    if gnd.next_leg:
        ready = on_block + gnd.min_turnaround_min
        if p.crew_plan == "RESERVE_CREW":
            ready = max(ready, s.now_min + RESERVE_CREW_READY_MIN)
        next_dep = max(gnd.next_leg.sched_dep_min, ready)
        next_delay = next_dep - gnd.next_leg.sched_dep_min

    return Derived(
        arrival_min=slot.start_min, delay_vs_eta_min=delay,
        arrival_delay_min=slot.start_min - s.flight.sched_arr_min, hold_min=hold,
        fuel_used_kg=round(used, 1), fuel_remaining_kg=round(remaining, 1),
        on_block_min=on_block, next_leg_dep_min=next_dep, next_leg_delay_min=next_delay,
        missed_pax=sum(c.pax for c in missed), missed_connections=[c.number for c in missed],
        gate_changed=p.gate != gnd.original_gate, bin_start_min=slot.bin_start_min,
    )
