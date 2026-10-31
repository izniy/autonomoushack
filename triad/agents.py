"""Live negotiating agents: one language-model call per party per round.

The model only ever sees its own party's view (private state, own verdicts, own costs and
utilities, the public log) and returns one structured message. Everything that matters is
checked afterwards by the judge in code; the model is asked for reasoning, concession and a
rationale a duty manager can read, not for a decision the system will trust unverified.

Per call we record model id, prompt hash, token usage, latency and stop reason, so the ledger
can tie every message back to the exact request that produced it.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from . import config
from .messages import AgentView, Candidate, Party3
from .proposal import CrewPlan, PilotAction
from .rules import RULES

PROMPT_VERSION = "2026-10-31.1"

# Loose on purpose: the model is held to the schema the API can enforce; the strict message
# rules (COUNTER needs a proposal, REJECT with ids must cite real failures, ...) are checked by
# `parse_message` and the judge, with one retry and then an abstain.
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["ACCEPT", "COUNTER", "REJECT"]},
        "proposal": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "slot_id": {"type": "string"},
                        "pilot_action": {"type": "string", "enum": list(PilotAction.__args__)},
                        "gate": {"type": "string"},
                        "crew_plan": {"type": "string", "enum": list(CrewPlan.__args__)},
                    },
                    "required": ["slot_id", "pilot_action", "gate", "crew_plan"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ],
            "description": "Required for COUNTER, null otherwise. Copy a proposal from your option list verbatim.",
        },
        "rejected_rule_ids": {"type": "array", "items": {"type": "string"},
                              "description": "REJECT only: rule ids the proposal on the table fails for you (ok=false in your verdicts). Empty list = preference vote."},
        "concessions": {"type": "array", "items": {"type": "string"},
                        "description": "What you are giving up relative to your preferred option."},
        "rationale": {"type": "string", "description": "For the duty manager. At most 400 characters."},
        "confidence": {"type": "number", "description": "0 to 1"},
    },
    "required": ["decision", "proposal", "rejected_rule_ids", "concessions", "rationale", "confidence"],
    "additionalProperties": False,
}

ROLE_BRIEF: dict[Party3, str] = {
    "ATC": (
        "You are the tower/approach supervisor at the destination airport. You own the arrival "
        "slots. Your hard rules are the arrival acceptance rate per 15-minute bin, wake-turbulence "
        "separation inside the slot and the airborne holding limit. Your cost is empty slots in a "
        "constrained bank and knock-on delay to the other arrivals. You do not care who sits at "
        "which gate except through the slot it implies."
    ),
    "PILOT": (
        "You are the captain together with the airline operations centre. You own the aircraft's "
        "fuel and the passengers. Your hard rules are the IFR fuel reserve after any holding or "
        "diversion and the turbulence cap on holding. Your cost is fuel burnt above plan, "
        "passengers who miss their connections (weighted) and your own delay. You cannot propose "
        "a diversion: that is a human decision outside this negotiation."
    ),
    "GROUND": (
        "You are the apron/stand controller together with the turnaround coordinator. You own the "
        "gates and the aircraft's next leg. Your hard rules are gate size, gate availability with "
        "buffer, the operating crew's duty limit and the minimum turnaround. Your cost is gate "
        "conflicts and idle time, crew call-outs and delay to the next departure."
    ),
}


def _protocol_rules(party: Party3) -> str:
    mine = [r for r in RULES.values() if r.owner == party]
    rules = "\n".join(f"  - {r.id}: {r.title} ({r.source})" for r in mine)
    return f"""You represent your organisation in a three-party negotiation (ATC, PILOT, GROUND) about one
disrupted arrival. The counterparties are other agents with their own private data; a judge
written in code (not a model) checks every message and decides each round. The judge never
reads your rationale to decide anything.

Your hard rules (the judge evaluates them from your organisation's data; you cannot waive them):
{rules}

What the judge does with your message:
  - ACCEPT counts only if the proposal on the table passes your own rules. Accepting one that
    fails them is refused and logged against you (SELF_VIOLATION).
  - COUNTER must be a proposal that passes all three parties' rules. You only see your own
    verdicts, so pick from your option list (all of them pass your rules) and use the public log
    to avoid options other parties' rules already rejected (INFEASIBLE_COUNTER lists the rules).
    A counter must also concede: it may not cost you less than your previous counter
    (NON_CONCESSION is discarded). Among valid counters the judge puts the one with the lowest
    system cost on the table, preferring options that clear every party's acceptance threshold.
  - REJECT with rule ids is only valid if the proposal really fails those rules for you
    (your verdicts show ok=false); otherwise it is logged UNFOUNDED_REJECT. REJECT with an
    empty list is a legitimate preference vote.
  - In the final round the only useful answers are ACCEPT or REJECT; a COUNTER is ignored.
  - Unanimous ACCEPT in one round is consensus. Otherwise, after the last round, a deterministic
    solver picks the lowest system-cost legal option and your organisation loses its say.

How to negotiate well:
  - Utility 1.0 is your cheapest legal option, 0.0 your dearest. The public acceptance
    threshold marks what the judge treats as acceptable for you. Accept proposals at or above
    it unless you can name a concretely better option the others can plausibly live with.
  - Concede early rather than late: a round spent re-proposing your favourite is a round closer
    to the solver. Read the public log: what the others accepted, countered or rejected, the
    rules they cited and how the judge ruled. You see their decisions, not their prose.
  - If the proposal on the table fails your rules, REJECT citing them, or COUNTER with the
    closest legal alternative.
  - Rationale: plain language for the airline duty manager, under 400 characters, naming the
    numbers that drove the decision. Do not mention being an AI or a model.

Reply with a single JSON object matching the schema. No prose outside it."""


def system_prompt(party: Party3) -> str:
    return f"{ROLE_BRIEF[party]}\n\n{_protocol_rules(party)}"


# ---------------------------------------------------------------- view rendering

def _verdicts(c: Candidate) -> str:
    parts = []
    for v in c.verdicts:
        m = f" margin {v.margin:+.0f}" if v.margin is not None else ""
        parts.append(f"{v.rule_id} {'ok' if v.ok else 'FAIL'}{m}" + (f" ({v.detail})" if not v.ok and v.detail else ""))
    return "; ".join(parts)


def _candidate(c: Candidate, label: str) -> str:
    bd = ", ".join(f"{k} {v:,.0f}" for k, v in c.my_breakdown.items() if v)
    return (f"{label} {json.dumps(c.proposal.model_dump())}\n"
            f"    {c.summary}\n"
            f"    my cost {c.my_cost:,.0f} ({bd or 'none'}); utility {c.my_utility:.2f}; "
            f"{'ACCEPTABLE' if c.acceptable_for_me else 'below threshold'}; "
            f"{'passes my rules' if c.feasible_for_me else 'FAILS my rules'}\n"
            f"    verdicts: {_verdicts(c)}")


def render_view(v: AgentView) -> str:
    lines = [f"# Round {v.round} of {v.max_rounds}" + (" (FINAL: vote ACCEPT or REJECT)" if v.final_round else ""),
             f"Scenario {v.scenario_id}. You are {v.party}.", "", "## Situation (public)", v.situation, "",
             "## Your private data", json.dumps(v.private_state, indent=None), "",
             (f"## Acceptance threshold (public): {v.acceptance_threshold}; an option clearing every "
              f"party's threshold {'exists' if v.mutually_acceptable_exists else 'does NOT exist'}."),
             f"System cost weights (public): {json.dumps(v.weights)}", "",
             "## On the table now", _candidate(v.current, "PROPOSAL")]
    if v.my_last_counter_cost is not None:
        lines.append(f"\nConcession floor: your next counter must cost you at least {v.my_last_counter_cost:,.0f}.")
    lines += ["", f"## Your options (pass your rules; sorted by your cost; top {len(v.options)})"]
    for i, c in enumerate(v.options, 1):
        lines.append(_candidate(c, f"OPTION {i}"))
    lines += ["", "## Public log"]
    if not v.public_log:
        lines.append("(none yet)")
    for m in v.public_log:
        lines.append(f"- r{m.round} {m.agent} {m.decision}" + (f" {m.proposal_key}" if m.proposal_key else "")
                     + (f" citing {m.cited_rules}" if m.cited_rules else "")
                     + f" -> {m.outcome}" + (f" [{m.detail}]" if m.detail else "")
                     + (f": {m.rationale}" if m.rationale else ""))
    lines += ["", "Answer for this round as one JSON object."]
    return "\n".join(lines)


# ---------------------------------------------------------------- the agent

class LlmAgent:
    """One party, one model. Thread-safe across parties (each party has its own instance);
    calls for the same party are sequential by construction of the protocol."""

    def __init__(self, party: Party3, model: str | None = None, client: Any = None,
                 temperature: float = 0.0, max_tokens: int = 1024, timeout_s: float | None = None):
        self.party = party
        self.model = model or config.MODEL
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = client
        self.timeout_s = timeout_s or config.MODEL_TIMEOUT_S
        self.output_mode = "json_schema"          # falls back to "tool" if the model lacks structured outputs
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}
        self.latency_s = 0.0
        self.last_call: dict[str, Any] | None = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=config.MODEL_MAX_RETRIES, timeout=self.timeout_s)
        return self._client

    def meta(self) -> dict[str, Any]:
        return {"kind": "llm", "model": self.model, "temperature": self.temperature,
                "output_mode": self.output_mode, "prompt_version": PROMPT_VERSION,
                "calls": self.calls, "usage": dict(self.usage), "latency_s": round(self.latency_s, 3),
                "usd": round(config.cost_usd(self.model, self.usage), 4)}

    # ---- request building

    def _messages(self, view: AgentView, error: str | None) -> list[dict[str, Any]]:
        user = render_view(view)
        if error:
            user += (f"\n\nYour previous answer this round was rejected before reaching the judge: {error}\n"
                     f"Answer again with one valid JSON object.")
        return [{"role": "user", "content": user}]

    def _request(self, view: AgentView, error: str | None) -> dict[str, Any]:
        system = system_prompt(self.party)
        req: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens, "temperature": self.temperature,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self._messages(view, error),
        }
        if self.output_mode == "json_schema":
            req["output_config"] = {"format": {"type": "json_schema", "schema": DRAFT_SCHEMA}}
        else:
            req["tools"] = [{"name": "submit_message", "description": "Submit your one message for this round.",
                             "input_schema": DRAFT_SCHEMA}]
            req["tool_choice"] = {"type": "tool", "name": "submit_message"}
        return req

    @staticmethod
    def _prompt_sha(req: dict[str, Any]) -> str:
        body = json.dumps({"system": req["system"], "messages": req["messages"]}, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    @staticmethod
    def _extract(resp: Any) -> dict | str:
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text   # the strict parser will reject it with a readable error

    def _is_structured_output_rejection(self, e: Exception) -> bool:
        msg = str(e).lower()
        return self.output_mode == "json_schema" and ("output_config" in msg or "output_format" in msg
                                                       or "structured" in msg or "json_schema" in msg)

    # ---- the call

    def respond(self, view: AgentView, error: str | None = None) -> dict | str:
        import anthropic

        req = self._request(view, error)
        self.last_call = {"model": self.model, "prompt_version": PROMPT_VERSION,
                          "prompt_sha256": self._prompt_sha(req), "output_mode": self.output_mode}
        t0 = time.time()
        try:
            resp = self.client.messages.create(**req)
        except anthropic.BadRequestError as e:
            if not self._is_structured_output_rejection(e):
                raise
            # The model does not support constrained JSON output: force a tool call instead.
            self.output_mode = "tool"
            req = self._request(view, error)
            self.last_call["output_mode"] = "tool"
            self.last_call["prompt_sha256"] = self._prompt_sha(req)
            resp = self.client.messages.create(**req)
        finally:
            dt = time.time() - t0
            self.latency_s += dt
            self.last_call["latency_s"] = round(dt, 3)
        self.calls += 1
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
        for k, n in usage.items():
            self.usage[k] += n
        self.last_call.update({"usage": usage, "stop_reason": resp.stop_reason, "response_id": resp.id})
        return self._extract(resp)


def llm_team(model: str | None = None, client: Any = None, **kw) -> dict[Party3, LlmAgent]:
    """Three agents sharing one client. Each party could run a different model; the eval does."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic(max_retries=config.MODEL_MAX_RETRIES,
                                     timeout=kw.get("timeout_s") or config.MODEL_TIMEOUT_S)
    return {p: LlmAgent(p, model=model, client=client, **kw) for p in ("ATC", "PILOT", "GROUND")}
