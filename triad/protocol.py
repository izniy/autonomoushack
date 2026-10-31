"""Orchestrator: owns the proposal on the table, runs at most MAX_ROUNDS rounds, and decides
by rule, never by persuasion.

Agents only ever return one message per round. The orchestrator checks every message
against code (schema, the sender's own hard rules, all parties' hard rules for a counter,
the concession rule) and logs one reason for every state change. If the agents do not
reach explicit unanimous ACCEPT within the round cap, or anything goes wrong, the
deterministic solver decides and the outcome is flagged FALLBACK_SOLVER.
"""

from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from . import config
from .checkers import fuel_floor_kg
from .ledger import Ledger
from .messages import PARTIES, AgentMessage, AgentView, Candidate, Party3, PriorMessage, parse_message
from .proposal import REMOTE_STAND, Proposal
from .scenario import Scenario
from .solver import Evaluation, evaluate, evaluate_all, fifo_baseline, optimum

SHORTLIST = 10


class Agent(Protocol):
    party: Party3

    def respond(self, view: AgentView, error: str | None = None) -> str | dict: ...

    def meta(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------- views

def _party_candidate(s: Scenario, party: Party3, e: Evaluation) -> Candidate:
    vs = e.verdicts[party]
    c = e.costs[party]
    p, d = e.proposal, e.derived
    if p.pilot_action == "DIVERT":
        summary = f"DIVERT to {s.pilot.fuel.alternate}"
    else:
        summary = (f"slot {p.slot_id} ({s.t(d.arrival_min)}, {d.delay_vs_eta_min:+d} min vs ETA), "
                   f"{p.pilot_action}, gate {p.gate}, {p.crew_plan}")
    return Candidate(proposal=p, feasible_for_me=all(v.ok for v in vs), my_cost=c.total,
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


def situation_text(s: Scenario) -> str:
    f = s.flight
    return (f"{s.disruption.note} Decision time {s.t(s.now_min)}. {f.airline}{f.number} {f.origin}->{f.dest}, "
            f"{s.aircraft.name}, scheduled arrival {f.sched_arr}. Arrival slots are 5 minutes apart; "
            f"REDUCE_SPEED absorbs up to 10 minutes en route and holds the remainder; HOLD takes it all "
            f"in the stack; SPEED_UP only for slots before the ETA. The negotiation is capped at "
            f"{config.MAX_ROUNDS} rounds; without unanimous ACCEPT a deterministic solver decides.")


def build_view(s: Scenario, party: Party3, rnd: int, current: Evaluation, evals: list[Evaluation],
               log: list[PriorMessage], last_counter_cost: float | None) -> AgentView:
    mine = [e for e in evals if all(v.ok for v in e.verdicts[party])]
    mine.sort(key=lambda e: (e.costs[party].total, e.system_cost))
    options = [_party_candidate(s, party, e) for e in mine[:SHORTLIST]]
    return AgentView(
        scenario_id=s.id, party=party, round=rnd, max_rounds=config.MAX_ROUNDS,
        situation=situation_text(s), private_state=private_state(s, party),
        current=_party_candidate(s, party, current), options=options, public_log=log,
        my_last_counter_cost=last_counter_cost, weights=s.weights.model_dump(),
    )


# ---------------------------------------------------------------- outcome

class RoundRecord(BaseModel):
    round: int
    on_table: str
    messages: dict[str, dict]
    outcomes: dict[str, str]
    next_on_table: str | None
    consensus: bool


class Outcome(BaseModel):
    scenario_id: str
    decision_source: str                # CONSENSUS | FALLBACK_SOLVER
    fallback_reason: str | None
    final: Evaluation | None
    fifo: Evaluation | None
    optimum: Evaluation | None
    rounds: list[RoundRecord]
    rounds_used: int
    counters_attempted: int
    infeasible_counters_caught: int
    non_concessions: int
    self_violations: int
    unfounded_rejects: int
    abstains: int
    wall_s: float
    agent_meta: dict[str, Any]

    @property
    def gap_vs_optimum(self) -> float | None:
        return round(self.final.system_cost - self.optimum.system_cost, 1) if self.final and self.optimum else None

    @property
    def saving_vs_fifo(self) -> float | None:
        return round(self.fifo.system_cost - self.final.system_cost, 1) if self.fifo and self.final else None


# ---------------------------------------------------------------- orchestrator

def _valid_references(s: Scenario, p: Proposal) -> str | None:
    if p.pilot_action == "DIVERT":
        return None if p.slot_id is None and p.gate is None else "DIVERT carries no slot or gate"
    if p.slot_id not in {x.id for x in s.atc.slots}:
        return f"unknown slot {p.slot_id!r}"
    if p.gate != REMOTE_STAND and p.gate not in {g.id for g in s.ground.gates}:
        return f"unknown gate {p.gate!r}"
    slot = next(x for x in s.atc.slots if x.id == p.slot_id)
    delay = slot.start_min - s.pilot.eta_min
    if (delay < 0) != (p.pilot_action == "SPEED_UP") or (delay == 0) != (p.pilot_action == "CONTINUE"):
        return f"{p.pilot_action} is not consistent with slot {p.slot_id} ({delay:+d} min vs ETA)"
    if p.crew_plan == "RESERVE_CREW" and not s.ground.next_leg:
        return "no next leg: crew plan must be SAME_CREW"
    return None


def run(s: Scenario, agents: dict[Party3, Agent], ledger_path: Path | None = None,
        max_rounds: int | None = None, parallel: bool = True) -> tuple[Outcome, Ledger]:
    t0 = time.time()
    if max_rounds is None:
        max_rounds = config.MAX_ROUNDS
    if not 1 <= max_rounds <= config.MAX_ROUNDS:
        raise ValueError(f"max_rounds must be between 1 and {config.MAX_ROUNDS}")
    if set(agents) != set(PARTIES) or any(agents[p].party != p for p in PARTIES):
        raise ValueError("exactly one correctly bound agent per party is required")
    ledger = Ledger(s.id, ledger_path)
    log: list[PriorMessage] = []

    evals = evaluate_all(s)
    by_key = {e.proposal.key(): e for e in evals}
    fifo = fifo_baseline(s, evals)
    opt = optimum(s, evals)
    current = fifo
    ledger.append("SCENARIO", "ORCHESTRATOR", 0, {
        "scenario": s.model_dump(), "n_proposals": len(evals),
        "n_feasible": sum(e.feasible for e in evals), "seed_rule": "FIFO: earliest legal slot, hold, original gate else first free else remote",
    })
    if opt is None or current is None:
        reason = "NO_FEASIBLE_GENERATED_PROPOSAL"
        ledger.append("DECISION", "ORCHESTRATOR", 0, {
            "decision_source": "UNRESOLVED", "fallback_reason": reason,
            "final": None, "requires_human_approval": True,
        })
        return Outcome(
            scenario_id=s.id, decision_source="UNRESOLVED", fallback_reason=reason,
            final=None, fifo=fifo, optimum=opt, rounds=[], rounds_used=0,
            counters_attempted=0, infeasible_counters_caught=0, non_concessions=0,
            self_violations=0, unfounded_rejects=0, abstains=0,
            wall_s=round(time.time() - t0, 2),
            agent_meta={p: agents[p].meta() for p in PARTIES},
        ), ledger
    ledger.append("PROPOSAL", "ORCHESTRATOR", 0, {"on_table": current.model_dump(), "source": "FIFO seed"})

    counters = infeasible = nonconc = selfviol = unfounded = abstains_total = 0
    abstain_count: dict[str, int] = {p: 0 for p in PARTIES}
    last_counter_cost: dict[str, float | None] = {p: None for p in PARTIES}
    rounds: list[RoundRecord] = []
    decision_source, reason = "FALLBACK_SOLVER", f"no consensus within {max_rounds} rounds"

    def ask(party: Party3, view: AgentView) -> tuple[AgentMessage | None, str, dict]:
        """Returns (message or None, abstain reason, raw record)."""
        raw_rec: dict = {}
        err = None
        for attempt in range(1 + config.SCHEMA_RETRIES):
            try:
                raw = agents[party].respond(view, error=err)
                raw_rec[f"attempt_{attempt}"] = raw if isinstance(raw, (dict, str)) else str(raw)
                return parse_message(raw, party), "", raw_rec
            except ValueError as e:
                err = f"schema error: {e}"
                raw_rec[f"attempt_{attempt}_error"] = err
            except Exception as e:  # noqa: BLE001  (API errors, timeouts)
                err = f"agent error: {type(e).__name__}: {e}"
                raw_rec[f"attempt_{attempt}_error"] = err
                raw_rec["traceback"] = traceback.format_exc()[-800:]
                break  # provider failures abstain; they are not formatting repairs
        return None, err or "no response", raw_rec

    for rnd in range(1, max_rounds + 1):
        ledger.append("ROUND_START", "ORCHESTRATOR", rnd, {"on_table": current.proposal.key()})
        # Snapshot before launching any branch. Only judge-validated summaries cross rounds.
        views = {p: build_view(s, p, rnd, current, evals, list(log), last_counter_cost[p]) for p in PARTIES}
        for p in PARTIES:
            ledger.append("VIEW", p, rnd, {"options": [c.summary for c in views[p].options],
                                           "current_feasible_for_me": views[p].current.feasible_for_me,
                                           "current_my_cost": views[p].current.my_cost})
        if parallel:
            with ThreadPoolExecutor(max_workers=3) as ex:
                results = dict(zip(PARTIES, ex.map(lambda p: ask(p, views[p]), PARTIES)))
        else:
            results = {p: ask(p, views[p]) for p in PARTIES}

        accepted: dict[str, bool] = {}
        ok_counters: list[tuple[str, Evaluation]] = []
        msgs_rec: dict[str, dict] = {}
        outcomes: dict[str, str] = {}
        for p in PARTIES:
            msg, why, raw_rec = results[p]
            if msg is None:
                abstain_count[p] += 1
                abstains_total += 1
                accepted[p] = False
                outcomes[p] = "ABSTAIN"
                msgs_rec[p] = {"decision": "ABSTAIN", "reason": why}
                ledger.append("ABSTAIN", p, rnd, {"reason": why, "raw": raw_rec})
                log.append(PriorMessage(round=rnd, agent=p, decision="REJECT", proposal_key=None,
                                        rationale=f"(abstained: {why[:120]})", outcome="ABSTAIN"))
                continue
            msgs_rec[p] = msg.model_dump()
            ledger.append("MESSAGE", p, rnd, {"message": msg.model_dump(), "raw": raw_rec})
            my_fail = [v for v in current.verdicts[p] if not v.ok]
            detail = ""
            if msg.decision == "ACCEPT":
                if my_fail:
                    selfviol += 1
                    accepted[p] = False
                    outcomes[p] = "SELF_VIOLATION"
                    detail = "ACCEPT refused: proposal fails your own rules " + ", ".join(v.rule_id for v in my_fail)
                    ledger.append("SELF_VIOLATION", "ORCHESTRATOR", rnd, {
                        "agent": p, "refused_accept_of": current.proposal.key(),
                        "failing_rules": [v.model_dump() for v in my_fail]})
                else:
                    accepted[p] = True
                    outcomes[p] = "ACCEPT_OK"
            elif msg.decision == "REJECT":
                accepted[p] = False
                real = {v.rule_id for v in my_fail}
                claimed = set(msg.rejected_rule_ids)
                if not (claimed & real):
                    unfounded += 1
                    outcomes[p] = "UNFOUNDED_REJECT"
                    detail = f"cited {sorted(claimed)} but the proposal passes all your rules"
                    ledger.append("UNFOUNDED_REJECT", "ORCHESTRATOR", rnd, {
                        "agent": p, "claimed": sorted(claimed), "actually_failing": sorted(real),
                        "note": "treated as a plain non-accept; a REJECT must cite a rule the proposal really fails"})
                else:
                    outcomes[p] = "REJECT_OK"
            else:  # COUNTER
                accepted[p] = False
                counters += 1
                prop = msg.proposal
                bad_ref = _valid_references(s, prop)
                ev = by_key.get(prop.key()) if not bad_ref else None
                if ev is None:
                    infeasible += 1
                    outcomes[p] = "INFEASIBLE_COUNTER"
                    detail = f"PUB-SCHEMA: {bad_ref or 'not in decision space'}"
                    ledger.append("INFEASIBLE_COUNTER", "ORCHESTRATOR", rnd, {
                        "agent": p, "proposal": prop.model_dump(),
                        "failing_rules": [{"rule_id": "PUB-SCHEMA", "ok": False, "detail": bad_ref or "not in decision space"}]})
                elif not ev.feasible:
                    infeasible += 1
                    outcomes[p] = "INFEASIBLE_COUNTER"
                    detail = "; ".join(f"{v.rule_id} ({v.detail})" for v in ev.failures())
                    ledger.append("INFEASIBLE_COUNTER", "ORCHESTRATOR", rnd, {
                        "agent": p, "proposal": prop.model_dump(),
                        "failing_rules": [v.model_dump() for v in ev.failures()],
                        "note": "verdicts come from the owning party's checker; the counter is discarded"})
                else:
                    own = ev.costs[p].total
                    floor = last_counter_cost[p]
                    if floor is not None and own < floor - 1e-6:
                        nonconc += 1
                        outcomes[p] = "NON_CONCESSION"
                        detail = f"your cost {own:.0f} is below your previous counter's {floor:.0f}; counters must concede"
                        ledger.append("NON_CONCESSION", "ORCHESTRATOR", rnd, {
                            "agent": p, "proposal": prop.model_dump(), "my_cost": own,
                            "previous_counter_cost": floor,
                            "note": "a new counter may not be cheaper for its author than the previous one"})
                    else:
                        last_counter_cost[p] = own
                        ok_counters.append((p, ev))
                        outcomes[p] = "COUNTER_OK"
                        ledger.append("COUNTER_OK", "ORCHESTRATOR", rnd, {
                            "agent": p, "proposal": prop.model_dump(), "system_cost": ev.system_cost,
                            "verdicts": {k: [v.model_dump() for v in vs] for k, vs in ev.verdicts.items()}})
            log.append(PriorMessage(round=rnd, agent=p, decision=msg.decision,
                                    proposal_key=msg.proposal.key() if msg.proposal else None,
                                    rationale="", outcome=outcomes[p], detail=detail))

        consensus = all(accepted.get(p, False) for p in PARTIES)
        next_eval = None
        if consensus:
            decision_source, reason = "CONSENSUS", None
        elif ok_counters:
            slot_idx = {x.id: i for i, x in enumerate(s.atc.slots)}
            _, next_eval = min(ok_counters, key=lambda pe: (pe[1].system_cost,
                                                            slot_idx.get(pe[1].proposal.slot_id, 10_000)))
        rounds.append(RoundRecord(round=rnd, on_table=current.proposal.key(), messages=msgs_rec,
                                  outcomes=outcomes,
                                  next_on_table=next_eval.proposal.key() if next_eval else None,
                                  consensus=consensus))
        ledger.append("ROUND_END", "ORCHESTRATOR", rnd, {
            "accepted": accepted, "outcomes": outcomes, "consensus": consensus,
            "next_on_table": next_eval.proposal.key() if next_eval else current.proposal.key(),
            "rule": "lowest system cost among feasible counters, tie earliest slot"})
        if consensus:
            break
        if any(n >= config.MAX_ABSTAINS_PER_AGENT for n in abstain_count.values()):
            reason = "agent abstained twice: " + ", ".join(p for p, n in abstain_count.items() if n >= config.MAX_ABSTAINS_PER_AGENT)
            break
        if next_eval is not None:
            current = next_eval
            ledger.append("PROPOSAL", "ORCHESTRATOR", rnd, {"on_table": current.model_dump(), "source": "best feasible counter"})

    if decision_source == "CONSENSUS":
        final = current
    else:
        final = opt
    # Never trust a cached feasibility flag at the resolution boundary.
    final = evaluate(s, final.proposal)
    if not final.feasible:
        decision_source, reason = "FALLBACK_SOLVER", "final revalidation failed"
        fresh = evaluate_all(s)
        fallback = optimum(s, fresh)
        final = evaluate(s, fallback.proposal) if fallback else None
        if final is None or not final.feasible:
            decision_source, reason, final = "UNRESOLVED", "engine revalidation failure", None
    ledger.append("DECISION", "ORCHESTRATOR", len(rounds), {
        "decision_source": decision_source, "fallback_reason": reason, "final": final.model_dump() if final else None,
        "fifo_system_cost": fifo.system_cost, "optimum_system_cost": opt.system_cost,
        "requires_human_approval": True,
        "agent_meta": {p: agents[p].meta() for p in PARTIES},
    })
    out = Outcome(
        scenario_id=s.id, decision_source=decision_source, fallback_reason=reason, final=final,
        fifo=fifo, optimum=opt, rounds=rounds, rounds_used=len(rounds),
        counters_attempted=counters, infeasible_counters_caught=infeasible, non_concessions=nonconc,
        self_violations=selfviol, unfounded_rejects=unfounded, abstains=abstains_total,
        wall_s=round(time.time() - t0, 2),
        agent_meta={p: agents[p].meta() for p in PARTIES},
    )
    return out, ledger


def describe_outcome(s: Scenario, o: Outcome) -> str:
    from .solver import describe_eval

    lines = [f"Negotiation {o.scenario_id}: {o.decision_source}"
             + (f" ({o.fallback_reason})" if o.fallback_reason else "")
             + f" after {o.rounds_used} round(s), {o.wall_s}s"]
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
    saving = f"{o.saving_vs_fifo:+,.0f}" if o.saving_vs_fifo is not None else "unavailable"
    gap = f"{o.gap_vs_optimum:,.0f}" if o.gap_vs_optimum is not None else "unavailable"
    lines.append(f"  saving vs FIFO {saving}; gap to optimum {gap}; "
                 f"counters {o.counters_attempted} (infeasible caught {o.infeasible_counters_caught}, "
                 f"non-concession {o.non_concessions}); self-violations {o.self_violations}; "
                 f"unfounded rejects {o.unfounded_rejects}; abstains {o.abstains}")
    return "\n".join(lines)
