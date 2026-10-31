"""Deterministic stand-in agents. No model involved.

They exist so the protocol can be run and tested end to end without an API key, and so
the orchestrator's defences (schema, self-violation, infeasible counter, concession rule,
abstain) each have a stub that trips them on purpose.
"""

from __future__ import annotations

import json
import random
from typing import Any

from .messages import AgentView, Party3


class StubAgent:
    """Policies:

    greedy      ACCEPT if the proposal on the table is within `tolerance` of my cheapest
                own-feasible option, else COUNTER with my cheapest option (which may well be
                infeasible for the other two: that is the point of the checkers).
    yielding    like greedy, but each round concedes one step further down my shortlist.
    agreeable   ACCEPT anything that passes my own rules; REJECT otherwise, citing them.
    stubborn    always COUNTER with my own best option; never concedes (trips NON_CONCESSION
                from round 2, since own cost cannot rise).
    reckless    ACCEPT everything, even what fails my own rules (trips SELF_VIOLATION).
    broken      returns prose instead of JSON (trips ABSTAIN after the retry).
    flaky       invalid on the first attempt, valid on the retry.
    """

    def __init__(self, party: Party3, policy: str = "greedy", tolerance: float = 0.15, seed: int = 0):
        self.party = party
        self.policy = policy
        self.tolerance = tolerance
        self.rng = random.Random(seed)
        self.calls = 0

    def meta(self) -> dict[str, Any]:
        return {"kind": "stub", "policy": self.policy}

    def respond(self, view: AgentView, error: str | None = None) -> str | dict:
        self.calls += 1
        cur = view.current
        best = view.options[0] if view.options else None
        fails = [v.rule_id for v in cur.verdicts if not v.ok]
        base = {"agent": self.party, "confidence": 0.9}

        if self.policy == "broken":
            return "I think we should just land as soon as possible, thanks."
        if self.policy == "flaky" and error is None:
            return {"agent": self.party, "decision": "MAYBE", "rationale": "hmm"}
        if self.policy == "reckless":
            return {**base, "decision": "ACCEPT", "rationale": "fine by me"}

        if not cur.feasible_for_me:
            if self.policy in ("agreeable",) or best is None:
                return {**base, "decision": "REJECT", "rejected_rule_ids": fails,
                        "rationale": f"fails my rules {', '.join(fails)}"}
        if self.policy == "agreeable":
            return {**base, "decision": "ACCEPT", "rationale": "passes my rules"}

        if self.policy == "stubborn":
            # ignores the final-round vote rule on purpose (trips LATE_COUNTER)
            return {**base, "decision": "COUNTER", "proposal": best.proposal.model_dump(),
                    "rationale": f"my best option, cost {best.my_cost:.0f}"}

        # greedy / yielding / flaky-after-retry
        if cur.feasible_for_me and best is not None and cur.my_cost <= best.my_cost * (1 + self.tolerance) + 1:
            return {**base, "decision": "ACCEPT", "rationale": f"within tolerance of my best ({best.my_cost:.0f})",
                    "concessions": [f"accepting cost {cur.my_cost:.0f} vs best {best.my_cost:.0f}"]}
        if view.final_round:
            # The last round is a vote: accept anything that clears my own threshold, else vote no.
            if cur.acceptable_for_me:
                return {**base, "decision": "ACCEPT", "rationale": f"final vote: utility {cur.my_utility} clears threshold",
                        "concessions": [f"accepting cost {cur.my_cost:.0f} vs best {best.my_cost:.0f}" if best else "none"]}
            return {**base, "decision": "REJECT", "rejected_rule_ids": fails,
                    "rationale": f"final vote: utility {cur.my_utility} below threshold {view.acceptance_threshold}"}
        pick = best
        if self.policy == "yielding":
            # Concede one step per round, but never below the concession floor.
            idx = min(view.round - 1, len(view.options) - 1)
            pick = view.options[idx]
            if view.my_last_counter_cost is not None:
                ok = [c for c in view.options if c.my_cost >= view.my_last_counter_cost - 1e-6]
                pick = ok[min(view.round - 1, len(ok) - 1)] if ok else pick
        if pick is None:
            return {**base, "decision": "REJECT", "rejected_rule_ids": fails or ["PUB-SCHEMA"],
                    "rationale": "nothing feasible on my side"}
        return json.dumps({**base, "decision": "COUNTER", "proposal": pick.proposal.model_dump(),
                           "rationale": f"prefer {pick.summary} (my cost {pick.my_cost:.0f} vs {cur.my_cost:.0f} on the table)",
                           "concessions": []})


def stub_team(policy: str = "greedy", **kw) -> dict[Party3, StubAgent]:
    return {p: StubAgent(p, policy, **kw) for p in ("ATC", "PILOT", "GROUND")}
