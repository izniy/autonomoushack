"""Live agent adapter, exercised with a fake client: request shape, extraction, telemetry,
the structured-output -> forced-tool fallback, and the full protocol with model replies."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from triad import config
from triad.agents import DRAFT_SCHEMA, LlmAgent, llm_team, render_view, system_prompt
from triad.judge import Judge
from triad.protocol import run
from triad.scenario import random_scenario


def _resp(payload, kind="text"):
    block = (SimpleNamespace(type="text", text=json.dumps(payload)) if kind == "text"
             else SimpleNamespace(type="tool_use", input=payload, name="submit_message"))
    usage = SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=900,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=[block], usage=usage, stop_reason="end_turn", id="msg_test")


class FakeClient:
    """Answers ACCEPT when the proposal on the table passes the party's rules, else counters
    with the party's cheapest option. Optionally rejects output_config once."""

    def __init__(self, reject_structured=False):
        self.requests = []
        self.reject_structured = reject_structured
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **req):
        self.requests.append(req)
        if self.reject_structured and "output_config" in req:
            self.reject_structured = False
            raise anthropic.BadRequestError(
                "output_config.format is not supported for this model",
                response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                body={"error": {"message": "output_config.format is not supported"}})
        text = req["messages"][0]["content"]
        on_table = text.split("PROPOSAL ")[1].split("\n")[0]
        passes = "passes my rules" in text.split("## On the table now")[1].split("## Your options")[0]
        final = "FINAL" in text.split("\n")[0]
        if passes or final:
            payload = {"decision": "ACCEPT" if passes else "REJECT", "proposal": None,
                       "rejected_rule_ids": [], "concessions": [], "rationale": f"ok with {on_table}",
                       "confidence": 0.8}
        else:
            opt = json.loads(text.split("OPTION 1 ")[1].split("\n")[0])
            payload = {"decision": "COUNTER", "proposal": opt, "rejected_rule_ids": [], "concessions": [],
                       "rationale": "my cheapest legal option", "confidence": 0.7}
        return _resp(payload, "tool" if "tools" in req else "text")


@pytest.fixture
def scenario():
    return random_scenario(seed=29)


def test_schema_is_structured_output_compatible():
    assert DRAFT_SCHEMA["additionalProperties"] is False
    assert set(DRAFT_SCHEMA["required"]) == set(DRAFT_SCHEMA["properties"])


def test_render_view_and_prompt_mention_no_vendor(scenario):
    j = Judge(scenario)
    v = j.view("PILOT", 1)
    text = render_view(v)
    assert "## Your private data" in text and "OPTION 1" in text and "fuel_on_board_kg" in text
    for party in ("ATC", "PILOT", "GROUND"):
        sp = system_prompt(party).lower()
        assert "anthropic" not in sp and "claude" not in sp
        assert party.lower() in sp or party == "PILOT"


def test_request_shape_and_telemetry(scenario):
    client = FakeClient()
    a = LlmAgent("ATC", model="test-model", client=client)
    j = Judge(scenario)
    out = a.respond(j.view("ATC", 1))
    req = client.requests[0]
    assert req["model"] == "test-model" and req["temperature"] == 0.0
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"]["format"]["schema"] is DRAFT_SCHEMA
    assert out["decision"] in ("ACCEPT", "COUNTER")
    assert a.last_call["usage"]["input_tokens"] == 1200 and len(a.last_call["prompt_sha256"]) == 64
    assert a.meta()["calls"] == 1 and a.meta()["usd"] > 0


def test_falls_back_to_forced_tool_when_structured_output_rejected(scenario):
    client = FakeClient(reject_structured=True)
    a = LlmAgent("GROUND", model="test-model", client=client)
    out = a.respond(Judge(scenario).view("GROUND", 1))
    assert a.output_mode == "tool" and out["decision"]
    assert "tools" in client.requests[-1] and client.requests[-1]["tool_choice"]["name"] == "submit_message"
    assert a.last_call["output_mode"] == "tool"
    # subsequent calls go straight to the tool path
    a.respond(Judge(scenario).view("GROUND", 1))
    assert "output_config" not in client.requests[-1]


def test_retry_prompt_carries_the_error(scenario):
    client = FakeClient()
    a = LlmAgent("PILOT", model="test-model", client=client)
    a.respond(Judge(scenario).view("PILOT", 1), error="schema error: COUNTER requires a proposal")
    assert "rejected before reaching the judge" in client.requests[0]["messages"][0]["content"]


def test_full_protocol_with_model_replies(scenario, tmp_path):
    client = FakeClient()
    team = llm_team(model="test-model", client=client)
    o, ledger = run(scenario, team, ledger_path=tmp_path / "l.jsonl")
    assert o.decision_source in ("CONSENSUS", "FALLBACK_SOLVER")
    assert o.final.feasible
    msgs = ledger.of_type("MESSAGE")
    assert msgs and all("attempt_0_call" in m.payload["raw"] for m in msgs)
    call = msgs[0].payload["raw"]["attempt_0_call"]
    assert call["model"] == "test-model" and call["usage"]["output_tokens"] == 80
    assert o.agent_meta["ATC"]["kind"] == "llm"


def test_cost_table():
    assert config.cost_usd("claude-haiku-4-5-20251001", {"input_tokens": 1_000_000, "output_tokens": 0}) == 1.0
    assert config.price_for("unknown-model") == config.PRICES_PER_MTOK["claude-sonnet-5"]
