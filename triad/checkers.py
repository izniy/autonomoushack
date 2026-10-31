"""Per-party deterministic checkers.

Each party's checker owns that party's private data and answers one question about a
proposal: which of *my* hard rules does it pass or fail, and by how much. The orchestrator
and the other parties only ever see the verdicts, never the underlying state.
"""

from __future__ import annotations

import math

from .proposal import REMOTE_STAND, Derived, Proposal, derive
from .rules import Party, Verdict
from .scenario import Scenario

_SIZE_ORDER = {"C": 0, "D": 1, "E": 2}

# ICAO time-based arrival wake minima (seconds) for follower behind leader; otherwise the
# radar minimum (~3 NM at approach speed, taken as 90 s).
_WAKE_SEC = {("HEAVY", "MEDIUM"): 120, ("HEAVY", "LIGHT"): 180, ("MEDIUM", "LIGHT"): 180}
_RADAR_MIN_SEC = 90
_SLOT_SEC = 300


def _sep_after(leader: str, follower: str) -> int:
    return _WAKE_SEC.get((leader, follower), _RADAR_MIN_SEC)


# ---------------------------------------------------------------- ATC

def check_atc(s: Scenario, p: Proposal, d: Derived | None = None) -> list[Verdict]:
    d = d or derive(s, p)
    if p.pilot_action == "DIVERT":
        return [Verdict(rule_id="ATC-SLOT-CAP", ok=True, detail="divert: no arrival slot used")]
    out = []
    b = d.bin_start_min
    demand = s.atc.demand_per_bin.get(b, 0)
    cap = s.atc.effective_aar_per_15
    out.append(Verdict(
        rule_id="ATC-SLOT-CAP", ok=demand + 1 <= cap, measured=demand + 1, limit=cap,
        margin=cap - (demand + 1),
        detail=f"bin {s.t(b)} has {demand} planned arrivals, AAR {cap}/15 min",
    ))
    # Spread the bin's arrivals evenly over its three slots and the arrival runways, and
    # check the wake-separation time budget of our slot on one runway. An approximation:
    # the dataset gives scheduled minutes, not the sequence ATC will actually fly.
    wakes = s.atc.wakes_per_bin.get(b, [])
    per_slot = math.ceil(len(wakes) / (3 * s.atc.arrival_runways)) if wakes else 0
    leaders = sorted(wakes, key=lambda w: {"HEAVY": 0, "MEDIUM": 1, "LIGHT": 2}[w])[:per_slot]
    used = sum(_sep_after(leaders[i - 1] if i else "MEDIUM", leaders[i]) for i in range(len(leaders)))
    ours = _sep_after(leaders[-1] if leaders else "MEDIUM", s.aircraft.wake)
    out.append(Verdict(
        rule_id="ATC-SEP-WAKE", ok=used + ours <= _SLOT_SEC, measured=used + ours, limit=_SLOT_SEC,
        margin=_SLOT_SEC - (used + ours),
        detail=f"{len(leaders)} arrivals ahead in slot on this runway need {used}s, ours {ours}s behind "
               f"{leaders[-1] if leaders else 'none'} ({s.atc.arrival_runways} arrival runways)",
    ))
    out.append(Verdict(
        rule_id="ATC-HOLD-MAX", ok=d.hold_min <= s.atc.max_hold_min, measured=d.hold_min,
        limit=s.atc.max_hold_min, margin=s.atc.max_hold_min - d.hold_min,
        detail=f"holding {d.hold_min} min, facility limit {s.atc.max_hold_min}",
    ))
    return out


# ---------------------------------------------------------------- Pilot

def fuel_floor_kg(s: Scenario) -> float:
    f = s.pilot.fuel
    return f.final_reserve_min * f.cruise_burn_kg_min + f.alternate_fuel_kg + f.contingency_kg


def check_pilot(s: Scenario, p: Proposal, d: Derived | None = None) -> list[Verdict]:
    d = d or derive(s, p)
    f = s.pilot.fuel
    out = []
    if p.pilot_action == "DIVERT":
        floor = f.final_reserve_min * f.cruise_burn_kg_min
        out.append(Verdict(
            rule_id="PLT-DIVERT-FUEL", ok=d.fuel_remaining_kg >= floor,
            measured=d.fuel_remaining_kg, limit=floor, margin=d.fuel_remaining_kg - floor,
            detail=f"overhead {f.alternate} with {d.fuel_remaining_kg:.0f} kg, final reserve {floor:.0f} kg",
        ))
        return out
    floor = fuel_floor_kg(s)
    out.append(Verdict(
        rule_id="PLT-FUEL-RES", ok=d.fuel_remaining_kg >= floor, measured=d.fuel_remaining_kg,
        limit=floor, margin=d.fuel_remaining_kg - floor,
        detail=f"{p.pilot_action} burns {d.fuel_used_kg:+.0f} kg; landing with {d.fuel_remaining_kg:.0f} kg, "
               f"floor {floor:.0f} kg (alt {f.alternate} + {f.final_reserve_min} min + contingency)",
    ))
    if f.turbulence_hold_penalty:
        out.append(Verdict(
            rule_id="PLT-TURB-HOLD", ok=d.hold_min <= 10, measured=d.hold_min, limit=10,
            margin=10 - d.hold_min, detail="moderate turbulence reported in the hold",
        ))
    return out


# ---------------------------------------------------------------- Ground

def check_ground(s: Scenario, p: Proposal, d: Derived | None = None) -> list[Verdict]:
    d = d or derive(s, p)
    g = s.ground
    out = []
    if p.pilot_action == "DIVERT":
        out.append(Verdict(rule_id="GND-GATE-FREE", ok=True, detail="divert: no stand required"))
        return out
    if p.gate != REMOTE_STAND:
        gate = next(x for x in g.gates if x.id == p.gate)
        out.append(Verdict(
            rule_id="GND-GATE-SIZE", ok=_SIZE_ORDER[gate.size] >= _SIZE_ORDER[s.aircraft.size],
            measured=gate.size, limit=s.aircraft.size,
            detail=f"gate {gate.id} is code {gate.size}, aircraft needs {s.aircraft.size}",
        ))
        start = d.on_block_min - g.gate_buffer_min
        end = (d.next_leg_dep_min or d.on_block_min + g.min_turnaround_min) + g.gate_buffer_min
        clash = [(a, b) for a, b in gate.busy if a < end and start < b]
        out.append(Verdict(
            rule_id="GND-GATE-FREE", ok=not clash,
            measured=f"{s.t(start)}-{s.t(end)}",
            limit=", ".join(f"{s.t(a)}-{s.t(b)}" for a, b in gate.busy) or "free",
            margin=min((a - end if a >= start else start - b) for a, b in gate.busy) if gate.busy else None,
            detail=f"gate {gate.id} needed {s.t(start)}-{s.t(end)} incl. buffer"
                   + (f", occupied {s.t(clash[0][0])}-{s.t(clash[0][1])}" if clash else ", free"),
        ))
    else:
        out.append(Verdict(rule_id="GND-GATE-FREE", ok=True, detail="remote stand, always available"))
    if g.next_leg:
        out.append(Verdict(
            rule_id="GND-TURN-MIN", ok=d.next_leg_dep_min - d.on_block_min >= g.min_turnaround_min,
            measured=d.next_leg_dep_min - d.on_block_min, limit=g.min_turnaround_min,
            margin=d.next_leg_dep_min - d.on_block_min - g.min_turnaround_min,
            detail=f"on blocks {s.t(d.on_block_min)}, next leg off {s.t(d.next_leg_dep_min)}",
        ))
        if p.crew_plan == "SAME_CREW":
            out.append(Verdict(
                rule_id="GND-CREW-DUTY", ok=d.next_leg_dep_min <= g.crew_duty_end_min,
                measured=d.next_leg_dep_min, limit=g.crew_duty_end_min,
                margin=g.crew_duty_end_min - d.next_leg_dep_min,
                detail=f"next leg off {s.t(d.next_leg_dep_min)}, crew duty ends {s.t(g.crew_duty_end_min)}",
            ))
        else:
            out.append(Verdict(rule_id="GND-CREW-DUTY", ok=True,
                               detail="reserve crew assigned; operating crew released"))
    return out


# ---------------------------------------------------------------- aggregate

CHECKERS = {"ATC": check_atc, "PILOT": check_pilot, "GROUND": check_ground}


def check(party: Party, s: Scenario, p: Proposal, d: Derived | None = None) -> list[Verdict]:
    return CHECKERS[party](s, p, d)


def check_all(s: Scenario, p: Proposal, d: Derived | None = None) -> dict[str, list[Verdict]]:
    d = d or derive(s, p)
    return {k: fn(s, p, d) for k, fn in CHECKERS.items()}


def feasible(s: Scenario, p: Proposal, d: Derived | None = None) -> bool:
    return all(v.ok for vs in check_all(s, p, d).values() for v in vs)
