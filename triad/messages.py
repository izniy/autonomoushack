"""Wire format between agents and the orchestrator.

Strict on purpose: an agent that cannot produce a valid message abstains, it does not get
interpreted charitably. The orchestrator never reads free text to decide anything; the
`rationale` is for the human on the other side of the dashboard.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .proposal import Proposal
from .rules import Verdict

Party3 = Literal["ATC", "PILOT", "GROUND"]
PARTIES: tuple[Party3, ...] = ("ATC", "PILOT", "GROUND")
Decision = Literal["ACCEPT", "COUNTER", "REJECT"]


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Party3
    decision: Decision
    proposal: Proposal | None = Field(default=None, description="required for COUNTER")
    rejected_rule_ids: list[str] = Field(
        default_factory=list,
        description="REJECT: rules the current proposal fails for me; empty = a vote against on preference")
    concessions: list[str] = Field(default_factory=list, description="what I am giving up relative to my preferred option")
    rationale: str = Field(max_length=600)
    confidence: float = Field(ge=0.0, le=1.0, default=0.7)

    @model_validator(mode="after")
    def _shape(self) -> AgentMessage:
        if self.decision == "COUNTER" and self.proposal is None:
            raise ValueError("COUNTER requires a proposal")
        if self.decision != "COUNTER" and self.proposal is not None:
            raise ValueError("only COUNTER carries a proposal")
        if self.decision != "REJECT" and self.rejected_rule_ids:
            raise ValueError("only REJECT names rejected rules")
        return self


class Candidate(BaseModel):
    """One option as seen by a single party: its own verdicts, cost and utility only."""
    proposal: Proposal
    feasible_for_me: bool
    my_cost: float
    my_utility: float                   # 1 = my cheapest legal option, 0 = my dearest
    acceptable_for_me: bool             # feasible and utility >= the public threshold
    my_breakdown: dict[str, float]
    verdicts: list[Verdict]
    summary: str


class PriorMessage(BaseModel):
    round: int
    agent: Party3
    decision: Decision
    proposal_key: str | None
    cited_rules: list[str] = []   # rule ids a REJECT named (structured, judge-checked)
    rationale: str                # free text; shown only to its own author's later rounds
    outcome: str   # how the orchestrator treated it: ACCEPT_OK, COUNTER_OK, INFEASIBLE_COUNTER, ...
    detail: str = ""   # orchestrator's reason, e.g. the rules an infeasible counter failed


class AgentView(BaseModel):
    """Everything an agent is allowed to see when it answers."""
    scenario_id: str
    party: Party3
    round: int
    max_rounds: int
    final_round: bool                   # last round is a vote: ACCEPT or REJECT only
    situation: str                      # public disruption note + public timeline
    private_state: dict[str, Any]       # this party's own data, in plain terms
    current: Candidate                  # the proposal on the table, through my own checker
    options: list[Candidate]            # my shortlist, through my own checker (not others')
    public_log: list[PriorMessage]
    my_last_counter_cost: float | None  # concession floor: a new counter may not cost me less
    acceptance_threshold: float         # public utility threshold the judge uses
    mutually_acceptable_exists: bool    # judge: at least one option clears every party's threshold
    weights: dict[str, float]


def parse_message(raw: str | dict, party: Party3) -> AgentMessage:
    """Parse and validate; raises ValueError (schema) on anything short of a valid message."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"not JSON: {e}") from e
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError("message must be an object")
    data = dict(data)
    data.setdefault("agent", party)
    try:
        msg = AgentMessage.model_validate(data)
    except ValidationError as e:
        raise ValueError(str(e)) from e
    if msg.agent != party:
        raise ValueError(f"message signed {msg.agent!r} but sent by {party!r}")
    return msg


def message_json_schema() -> dict:
    return AgentMessage.model_json_schema()
