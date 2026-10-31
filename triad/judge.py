"""The judge: everything the orchestrator decides, as plain Python with no model and no
runtime framework in it.

Owns the proposal on the table, the feasible and mutually-acceptable sets, per-party
utilities, the round outcomes (ACCEPT_OK, SELF_VIOLATION, INFEASIBLE_COUNTER,
NON_CONCESSION, UNFOUNDED_REJECT, LATE_COUNTER, ABSTAIN ...), the round winner, consensus,
the fallback and the ledger entries for all of it. Any runtime (the ADK pipeline, the
direct loop, a test) feeds it one `Reply` per party per round and reads back the decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from . import config
from .ledger import Entry, Ledger
from .messages import PARTIES, AgentMessage, AgentView, Party3, PriorMessage
from .proposal import REMOTE_STAND, Proposal
from .scenario import Scenario
from .solver import Evaluation, evaluate_all, fifo_baseline, optimum
from .views import party_candidate, private_state, situation_text

SHORTLIST = 10

DecisionSource = Literal["CONSENSUS", "FALLBACK_SOLVER", "UNRESOLVED"]
FallbackCode = Literal["NO_CONSENSUS_ROUND_CAP", "NO_ACCEPTABLE_OPTION", "AGENT_ABSTAINED",
                       "AGENT_ERROR", "NO_FEASIBLE_LANDING"]


@dataclass
class Reply:
    """What a runtime hands the judge for one party in one round."""
    message: AgentMessage | None        # None = the party abstains this round
    error: str = ""                     # why it abstained (schema / agent error)
    raw: dict[str, Any] = field(default_factory=dict)   # attempts, errors, usage, latency


class RoundRecord(BaseModel):
    round: int
    on_table: str
    messages: dict[str, dict]
    outcomes: dict[str, str]
    next_on_table: str | None
    consensus: bool


class RoundJudgement(BaseModel):
    round: int
    consensus: bool
    outcomes: dict[str, str]
    next_on_table: str
    stop: bool                          # consensus, abstain cap, or round cap
    stop_code: FallbackCode | None = None
    stop_reason: str | None = None


class Outcome(BaseModel):
    scenario_id: str
    decision_source: DecisionSource
    fallback_code: FallbackCode | None
    fallback_reason: str | None
    final: Evaluation
    fifo: Evaluation
    optimum: Evaluation
    rounds: list[RoundRecord]
    rounds_used: int
    n_feasible_landing: int
    n_mutually_acceptable: int
    acceptance_threshold: float
    utilities: dict[str, dict[str, float]]      # final / fifo / optimum -> party -> utility
    counters_attempted: int
    infeasible_counters_caught: int
    non_concessions: int
    self_violations: int
    unfounded_rejects: int
    late_counters: int
    abstains: int
    wall_s: float
    agent_meta: dict[str, Any]
    diversion_assessment: dict[str, Any] | None = None

    @property
    def gap_vs_optimum(self) -> float:
        return round(self.final.system_cost - self.optimum.system_cost, 1)

    @property
    def saving_vs_fifo(self) -> float:
        return round(self.fifo.system_cost - self.final.system_cost, 1)

    @property
    def min_utility_final(self) -> float:
        return min(self.utilities["final"].values())


def valid_references(s: Scenario, p: Proposal) -> str | None:
    """Public schema rules on a counter: things every party can verify without private data."""
    if p.pilot_action == "DIVERT":
        return "PUB-DIVERT: diversion is a human decision and is not negotiated"
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


class Judge:
    def __init__(self, s: Scenario, ledger_path: Path | None = None, max_rounds: int | None = None,
                 evals: list[Evaluation] | None = None, seed: Evaluation | None = None,
                 on_entry: Callable[[Entry], None] | None = None):
        self.s = s
        self.max_rounds = max_rounds or config.MAX_ROUNDS
        self.threshold = config.ACCEPTANCE_THRESHOLD
        self.ledger = Ledger(s.id, ledger_path, on_append=on_entry)
        self.evals = evals if evals is not None else evaluate_all(s)
        self.by_key = {e.proposal.key(): e for e in self.evals}

        landing = [e for e in self.evals if e.proposal.pilot_action != "DIVERT"]
        self.divert = next((e for e in self.evals if e.proposal.pilot_action == "DIVERT"), None)
        self.feasible = [e for e in landing if e.feasible]
        self._lo = {p: min((e.costs[p].total for e in self.feasible), default=0.0) for p in PARTIES}
        self._hi = {p: max((e.costs[p].total for e in self.feasible), default=0.0) for p in PARTIES}
        self.acceptable = [e for e in self.feasible if self.is_acceptable(e)]
        self.unresolved = not self.feasible

        if self.unresolved:
            # Nothing legal short of diverting: the system does not divert, a human does.
            self.fifo = self.opt = self.current = self.divert
        else:
            self.fifo = seed or fifo_baseline(s, landing)
            self.opt = optimum(s, landing)
            self.current = self.fifo

        self.log: list[PriorMessage] = []
        self.rounds: list[RoundRecord] = []
        self.counters = self.infeasible = self.nonconc = self.selfviol = self.unfounded = 0
        self.late = self.abstains_total = 0
        self.abstain_count: dict[str, int] = {p: 0 for p in PARTIES}
        self.error_abstains: dict[str, int] = {p: 0 for p in PARTIES}
        self.last_counter_cost: dict[str, float | None] = {p: None for p in PARTIES}
        self.decision_source: DecisionSource = "FALLBACK_SOLVER"
        self.code: FallbackCode | None = "NO_CONSENSUS_ROUND_CAP"
        self.reason: str | None = f"no consensus within {self.max_rounds} rounds"

        self.ledger.append("SCENARIO", "ORCHESTRATOR", 0, {
            "scenario": s.model_dump(), "n_proposals": len(self.evals),
            "n_feasible_landing": len(self.feasible), "n_mutually_acceptable": len(self.acceptable),
            "acceptance_threshold": self.threshold,
            "utility_rule": "u_p = 1 - (cost_p - min_F cost_p) / (max_F cost_p - min_F cost_p) over the feasible landing set",
            "seed_rule": "FIFO: earliest legal slot, hold, original gate else first free else remote",
        })
        if self.unresolved:
            self.decision_source, self.code = "UNRESOLVED", "NO_FEASIBLE_LANDING"
            self.reason = "no landing option passes every party's hard rules"
            self.ledger.append("NO_FEASIBLE_LANDING", "ORCHESTRATOR", 0, self.diversion_assessment())
        else:
            if not self.acceptable:
                self.ledger.append("NO_ACCEPTABLE_OPTION", "ORCHESTRATOR", 0, {
                    "note": "no feasible option clears every party's acceptance threshold; "
                            "consensus would require someone to accept below it"})
            self.ledger.append("PROPOSAL", "ORCHESTRATOR", 0, {
                "on_table": self.current.model_dump(), "source": "FIFO seed",
                "utilities": self.utilities(self.current)})

    # ------------------------------------------------------------ utilities

    def utility(self, party: Party3, e: Evaluation) -> float:
        lo, hi = self._lo[party], self._hi[party]
        if hi - lo < 1e-9:
            return 1.0
        return max(0.0, min(1.0, 1.0 - (e.costs[party].total - lo) / (hi - lo)))

    def utilities(self, e: Evaluation) -> dict[str, float]:
        return {p: round(self.utility(p, e), 3) for p in PARTIES}

    def is_acceptable(self, e: Evaluation) -> bool:
        return e.feasible and all(self.utility(p, e) >= self.threshold for p in PARTIES)

    def diversion_assessment(self) -> dict[str, Any]:
        by_rule: dict[str, int] = {}
        for e in self.evals:
            if e.proposal.pilot_action != "DIVERT":
                for rid in set(e.failed_rule_ids()):
                    by_rule[rid] = by_rule.get(rid, 0) + 1
        d = self.divert
        return {
            "alternate": self.s.pilot.fuel.alternate,
            "alternate_nm": self.s.pilot.fuel.alternate_nm,
            "divert_feasible": bool(d and d.feasible),
            "divert_fuel_remaining_kg": round(d.derived.fuel_remaining_kg) if d else None,
            "divert_failing_rules": [v.model_dump() for v in d.failures()] if d else [],
            "landing_options_failed_by_rule": dict(sorted(by_rule.items())),
            "note": "no legal landing option; diversion assessment is information for the duty manager, not a decision",
        }

    # ------------------------------------------------------------ views

    def is_final_round(self, rnd: int) -> bool:
        return rnd >= self.max_rounds

    def view(self, party: Party3, rnd: int) -> AgentView:
        s = self.s
        mine = [e for e in self.evals if e.proposal.pilot_action != "DIVERT"
                and all(v.ok for v in e.verdicts[party])]
        mine.sort(key=lambda e: (e.costs[party].total, e.system_cost))

        def cand(e: Evaluation):
            return party_candidate(s, party, e, self.utility(party, e), self.threshold)

        return AgentView(
            scenario_id=s.id, party=party, round=rnd, max_rounds=self.max_rounds,
            final_round=self.is_final_round(rnd), situation=situation_text(s, self.max_rounds),
            private_state=private_state(s, party), current=cand(self.current),
            options=[cand(e) for e in mine[:SHORTLIST]], public_log=self.public_log_for(party),
            my_last_counter_cost=self.last_counter_cost[party], acceptance_threshold=self.threshold,
            mutually_acceptable_exists=bool(self.acceptable), weights=s.weights.model_dump(),
        )

    def public_log_for(self, party: Party3) -> list[PriorMessage]:
        """Other parties' messages reach a prompt only as structured fields (decision, proposal,
        cited rules, the judge's ruling). Their free text is for the human on the dashboard and
        stays in the ledger, so one model's output can never be instructions to another."""
        return [m if m.agent == party else m.model_copy(update={"rationale": ""}) for m in self.log]

    def start_round(self, rnd: int) -> dict[Party3, AgentView]:
        self.ledger.append("ROUND_START", "ORCHESTRATOR", rnd, {
            "on_table": self.current.proposal.key(), "final_round": self.is_final_round(rnd)})
        views = {p: self.view(p, rnd) for p in PARTIES}
        for p in PARTIES:
            v = views[p]
            self.ledger.append("VIEW", p, rnd, {
                "options": [c.summary for c in v.options],
                "current_feasible_for_me": v.current.feasible_for_me,
                "current_my_cost": v.current.my_cost, "current_my_utility": v.current.my_utility})
        return views

    # ------------------------------------------------------------ judging

    def _judge_one(self, p: Party3, rnd: int, reply: Reply, accepted: dict, ok_counters: list,
                   msgs_rec: dict, outcomes: dict) -> None:
        led, cur = self.ledger, self.current
        if reply.message is None:
            self.abstain_count[p] += 1
            self.abstains_total += 1
            if reply.error.startswith("agent error"):
                self.error_abstains[p] += 1
            accepted[p] = False
            outcomes[p] = "ABSTAIN"
            msgs_rec[p] = {"decision": "ABSTAIN", "reason": reply.error}
            led.append("ABSTAIN", p, rnd, {"reason": reply.error, "raw": reply.raw})
            self.log.append(PriorMessage(round=rnd, agent=p, decision="REJECT", proposal_key=None,
                                         rationale="", outcome="ABSTAIN", detail=f"abstained: {reply.error[:120]}"))
            return

        msg = reply.message
        msgs_rec[p] = msg.model_dump()
        led.append("MESSAGE", p, rnd, {"message": msg.model_dump(), "raw": reply.raw})
        my_fail = [v for v in cur.verdicts[p] if not v.ok]
        my_u = self.utility(p, cur)
        detail = ""
        accepted[p] = False

        if msg.decision == "ACCEPT":
            if my_fail:
                self.selfviol += 1
                outcomes[p] = "SELF_VIOLATION"
                detail = "ACCEPT refused: proposal fails your own rules " + ", ".join(v.rule_id for v in my_fail)
                led.append("SELF_VIOLATION", "ORCHESTRATOR", rnd, {
                    "agent": p, "refused_accept_of": cur.proposal.key(),
                    "failing_rules": [v.model_dump() for v in my_fail]})
            else:
                accepted[p] = True
                outcomes[p] = "ACCEPT_OK"
                led.append("ACCEPT_OK", "ORCHESTRATOR", rnd, {
                    "agent": p, "my_utility": round(my_u, 3), "below_threshold": my_u < self.threshold})

        elif msg.decision == "REJECT":
            real = {v.rule_id for v in my_fail}
            claimed = set(msg.rejected_rule_ids)
            if not claimed:
                outcomes[p] = "REJECT_PREFERENCE"
                detail = f"vote against on preference (your utility {my_u:.2f}, threshold {self.threshold})"
                led.append("REJECT_PREFERENCE", "ORCHESTRATOR", rnd, {
                    "agent": p, "my_utility": round(my_u, 3), "above_threshold": my_u >= self.threshold,
                    "note": "legitimate non-accept without a rule; counted against consensus"})
            elif not (claimed & real):
                self.unfounded += 1
                outcomes[p] = "UNFOUNDED_REJECT"
                detail = f"cited {sorted(claimed)} but the proposal passes all your rules"
                led.append("UNFOUNDED_REJECT", "ORCHESTRATOR", rnd, {
                    "agent": p, "claimed": sorted(claimed), "actually_failing": sorted(real),
                    "note": "treated as a plain non-accept; a REJECT must cite a rule the proposal really fails"})
            else:
                outcomes[p] = "REJECT_OK"

        else:  # COUNTER
            prop = msg.proposal
            if self.is_final_round(rnd):
                self.late += 1
                outcomes[p] = "LATE_COUNTER"
                detail = "the final round is a vote: ACCEPT or REJECT only; counter ignored"
                led.append("LATE_COUNTER", "ORCHESTRATOR", rnd, {"agent": p, "proposal": prop.model_dump()})
            else:
                self.counters += 1
                bad_ref = valid_references(self.s, prop)
                ev = self.by_key.get(prop.key()) if not bad_ref else None
                if ev is None:
                    self.infeasible += 1
                    outcomes[p] = "INFEASIBLE_COUNTER"
                    detail = f"PUB-SCHEMA: {bad_ref or 'not in decision space'}"
                    led.append("INFEASIBLE_COUNTER", "ORCHESTRATOR", rnd, {
                        "agent": p, "proposal": prop.model_dump(),
                        "failing_rules": [{"rule_id": "PUB-SCHEMA", "ok": False,
                                           "detail": bad_ref or "not in decision space"}]})
                elif not ev.feasible:
                    self.infeasible += 1
                    outcomes[p] = "INFEASIBLE_COUNTER"
                    detail = "; ".join(f"{v.rule_id} ({v.detail})" for v in ev.failures())
                    led.append("INFEASIBLE_COUNTER", "ORCHESTRATOR", rnd, {
                        "agent": p, "proposal": prop.model_dump(),
                        "failing_rules": [v.model_dump() for v in ev.failures()],
                        "note": "verdicts come from the owning party's checker; the counter is discarded"})
                else:
                    own = ev.costs[p].total
                    floor = self.last_counter_cost[p]
                    if floor is not None and own < floor - 1e-6:
                        self.nonconc += 1
                        outcomes[p] = "NON_CONCESSION"
                        detail = f"your cost {own:.0f} is below your previous counter's {floor:.0f}; counters must concede"
                        led.append("NON_CONCESSION", "ORCHESTRATOR", rnd, {
                            "agent": p, "proposal": prop.model_dump(), "my_cost": own,
                            "previous_counter_cost": floor,
                            "note": "a new counter may not be cheaper for its author than the previous one"})
                    else:
                        self.last_counter_cost[p] = own
                        ok_counters.append((p, ev))
                        outcomes[p] = "COUNTER_OK"
                        led.append("COUNTER_OK", "ORCHESTRATOR", rnd, {
                            "agent": p, "proposal": prop.model_dump(), "system_cost": ev.system_cost,
                            "utilities": self.utilities(ev), "mutually_acceptable": self.is_acceptable(ev),
                            "verdicts": {k: [v.model_dump() for v in vs] for k, vs in ev.verdicts.items()}})

        self.log.append(PriorMessage(round=rnd, agent=p, decision=msg.decision,
                                     proposal_key=msg.proposal.key() if msg.proposal else None,
                                     cited_rules=sorted(msg.rejected_rule_ids), rationale=msg.rationale,
                                     outcome=outcomes[p], detail=detail))

    def pick_winner(self, ok_counters: list[tuple[str, Evaluation]]) -> Evaluation | None:
        """Lowest system cost among feasible, conceding counters; those clearing every party's
        threshold first; tie -> earliest slot."""
        if not ok_counters:
            return None
        slot_idx = {x.id: i for i, x in enumerate(self.s.atc.slots)}
        _, ev = min(ok_counters, key=lambda pe: (not self.is_acceptable(pe[1]), pe[1].system_cost,
                                                 slot_idx.get(pe[1].proposal.slot_id, 10_000)))
        return ev

    def judge_round(self, rnd: int, replies: dict[Party3, Reply]) -> RoundJudgement:
        accepted: dict[str, bool] = {}
        ok_counters: list[tuple[str, Evaluation]] = []
        msgs_rec: dict[str, dict] = {}
        outcomes: dict[str, str] = {}
        for p in PARTIES:   # fixed order: the ledger does not depend on who answered first
            self._judge_one(p, rnd, replies[p], accepted, ok_counters, msgs_rec, outcomes)

        consensus = all(accepted.get(p, False) for p in PARTIES)
        next_eval = None if consensus else self.pick_winner(ok_counters)
        self.rounds.append(RoundRecord(round=rnd, on_table=self.current.proposal.key(), messages=msgs_rec,
                                       outcomes=outcomes,
                                       next_on_table=next_eval.proposal.key() if next_eval else None,
                                       consensus=consensus))
        self.ledger.append("ROUND_END", "ORCHESTRATOR", rnd, {
            "accepted": accepted, "outcomes": outcomes, "consensus": consensus,
            "next_on_table": (next_eval or self.current).proposal.key(),
            "rule": "lowest system cost among feasible conceding counters, mutually acceptable first, tie earliest slot"})

        stop, code, reason = False, None, None
        if consensus:
            self.decision_source, self.code, self.reason = "CONSENSUS", None, None
            stop = True
        else:
            capped = [p for p, n in self.abstain_count.items() if n >= config.MAX_ABSTAINS_PER_AGENT]
            if capped:
                errs = all(self.error_abstains[p] == self.abstain_count[p] for p in capped)
                code = "AGENT_ERROR" if errs else "AGENT_ABSTAINED"
                reason = ("agent abstained twice: " if not errs else "agent errored twice: ") + ", ".join(capped)
                self.code, self.reason, stop = code, reason, True
            elif self.is_final_round(rnd):
                if not self.acceptable:
                    self.code = "NO_ACCEPTABLE_OPTION"
                    self.reason = f"no consensus within {self.max_rounds} rounds; no option cleared every threshold"
                code, reason, stop = self.code, self.reason, True
            elif next_eval is not None:
                self.current = next_eval
                self.ledger.append("PROPOSAL", "ORCHESTRATOR", rnd, {
                    "on_table": self.current.model_dump(), "source": "best feasible counter",
                    "utilities": self.utilities(self.current)})
        return RoundJudgement(round=rnd, consensus=consensus, outcomes=outcomes,
                              next_on_table=self.current.proposal.key(), stop=stop,
                              stop_code=code, stop_reason=reason)

    # ------------------------------------------------------------ decision

    def finish(self, agent_meta: dict[str, Any], wall_s: float) -> Outcome:
        final = self.current if self.decision_source == "CONSENSUS" else self.opt
        assess = self.diversion_assessment() if self.unresolved else None
        utilities = {"final": self.utilities(final), "fifo": self.utilities(self.fifo),
                     "optimum": self.utilities(self.opt)}
        self.ledger.append("DECISION", "ORCHESTRATOR", len(self.rounds), {
            "decision_source": self.decision_source, "fallback_code": self.code, "fallback_reason": self.reason,
            "final": final.model_dump(), "utilities": utilities,
            "fifo_system_cost": self.fifo.system_cost, "optimum_system_cost": self.opt.system_cost,
            "requires_human_approval": True, "diversion_assessment": assess,
        })
        return Outcome(
            scenario_id=self.s.id, decision_source=self.decision_source, fallback_code=self.code,
            fallback_reason=self.reason, final=final, fifo=self.fifo, optimum=self.opt,
            rounds=self.rounds, rounds_used=len(self.rounds),
            n_feasible_landing=len(self.feasible), n_mutually_acceptable=len(self.acceptable),
            acceptance_threshold=self.threshold, utilities=utilities,
            counters_attempted=self.counters, infeasible_counters_caught=self.infeasible,
            non_concessions=self.nonconc, self_violations=self.selfviol, unfounded_rejects=self.unfounded,
            late_counters=self.late, abstains=self.abstains_total, wall_s=round(wall_s, 2),
            agent_meta=agent_meta, diversion_assessment=assess,
        )
