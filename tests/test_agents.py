"""SDK and orchestration integration without network or historical data."""

import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from triad.agents import AnthropicAgent
from triad.ledger import verify_entries
from triad.messages import PARTIES
from triad.protocol import build_view, describe_outcome, run
from triad.scenario import _TYPES, Scenario
from triad.solver import evaluate_all, fifo_baseline
from triad.stubs import stub_team


@pytest.fixture
def fixture_scenario():
    # Explicit synthetic fixture; never presented as a historical DOT record.
    return Scenario.model_validate(
        {
            "id": "synthetic-test",
            "seed": 0,
            "now_min": 560,
            "flight": {
                "airline": "AA",
                "airline_name": "Test",
                "number": 1,
                "tail": "TEST",
                "origin": "LAX",
                "dest": "ORD",
                "date": "2015-01-01",
                "sched_dep": "0800",
                "sched_arr": "1000",
                "sched_arr_min": 600,
                "distance_nm": 1000,
                "air_time_min": None,
                "departure_delay_min": 0,
                "weather_delay_min": 20,
                "late_aircraft_delay_min": 0,
                "arrival_delay_min_actual": 0,
            },
            "disruption": {"trigger": "WEATHER", "capacity_factor": 1, "note": "Synthetic test"},
            "aircraft": _TYPES["A320"],
            "bank": [],
            "atc": {
                "arrival_runways": 2,
                "nominal_aar_per_15": 10,
                "effective_aar_per_15": 10,
                "demand_per_bin": {},
                "wakes_per_bin": {},
                "max_hold_min": 40,
                "slots": [
                    {"id": "S00", "start_min": 600, "label": "10:00", "bin_start_min": 600},
                    {"id": "S01", "start_min": 605, "label": "10:05", "bin_start_min": 600},
                ],
            },
            "pilot": {
                "eta_min": 600,
                "earliest_arr_min": 600,
                "pax_total": 100,
                "connections": [],
                "fuel": {
                    "fuel_on_board_kg": 10000,
                    "final_reserve_min": 45,
                    "alternate": "DFW",
                    "alternate_nm": 100,
                    "alternate_fuel_kg": 1000,
                    "contingency_kg": 100,
                    "hold_burn_kg_min": 36,
                    "cruise_burn_kg_min": 40,
                    "speed_up_extra_kg_min": 10,
                    "turbulence_hold_penalty": False,
                },
            },
            "ground": {
                "gates": [{"id": "G1", "size": "C", "busy": [], "walk_to_connections_min": 5}],
                "original_gate": "G1",
                "remote_stand_bus_min": 10,
                "gate_buffer_min": 5,
                "min_turnaround_min": 35,
                "crew_duty_end_min": None,
                "next_leg": None,
                "on_block_taxi_min": 10,
            },
            "provenance": {},
            "historical": {},
        }
    )


def sdk_response(party="ATC", **overrides):
    data = {"agent": party, "decision": "ACCEPT", "rationale": "passes supplied rules"}
    data.update(overrides)
    return NS(
        id="msg-test",
        model="test-model",
        stop_reason="tool_use",
        usage=NS(input_tokens=100, output_tokens=20),
        content=[NS(type="tool_use", name="submit_response", input=data)],
    )


def make_agent(party="ATC", response=None):
    client = Mock()
    client.messages.create.return_value = response or sdk_response(party)
    return AnthropicAgent(party, model="test-model", client=client)


def test_sdk_request_is_isolated_and_usage_recorded(fixture_scenario):
    s = fixture_scenario
    agent = make_agent()
    view = build_view(s, "ATC", 1, fifo_baseline(s), evaluate_all(s), [], None)
    assert agent.respond(view)["agent"] == "ATC"
    request = agent.client.messages.create.call_args.kwargs
    assert request["tool_choice"] == {"type": "tool", "name": "submit_response"}
    assert request["temperature"] == 0
    assert len(request["messages"]) == 1
    payload = json.loads(request["messages"][0]["content"])
    assert "fuel_on_board_kg" not in payload["private_state"]
    assert "gates" not in payload["private_state"]
    assert agent.meta()["input_tokens"] == 100
    assert agent.meta()["estimated_cost_usd"] is None
    assert agent.meta()["calls"][0]["prompt_hash"]
    with pytest.raises(ValueError, match="does not belong"):
        make_agent("PILOT").respond(view)


def test_three_sdk_agents_reach_early_consensus(fixture_scenario):
    agents = {p: make_agent(p) for p in PARTIES}
    out, ledger = run(fixture_scenario, agents)
    assert out.decision_source == "CONSENSUS"
    assert out.rounds_used == 1
    assert out.final.feasible
    assert all(a.client.messages.create.call_count == 1 for a in agents.values())
    assert verify_entries(ledger.dump()).ok


def test_schema_repair_and_truncation(fixture_scenario):
    agents = {p: make_agent(p) for p in PARTIES}
    for p, agent in agents.items():
        bad = sdk_response(p)
        bad.stop_reason = "max_tokens"
        agent.client.messages.create.side_effect = [bad, sdk_response(p)]
    out, _ = run(fixture_scenario, agents)
    assert out.abstains == 0
    assert all(a.calls[1]["repair"] for a in agents.values())


def test_provider_failure_abstains_without_format_retries(fixture_scenario):
    agents = {p: make_agent(p) for p in PARTIES}
    for agent in agents.values():
        agent.client.messages.create.side_effect = RuntimeError("provider unavailable")
    out, ledger = run(fixture_scenario, agents)
    assert out.decision_source == "FALLBACK_SOLVER"
    assert out.final.feasible
    assert all(a.client.messages.create.call_count == out.rounds_used for a in agents.values())
    assert ledger.of_type("ABSTAIN")


def test_no_feasible_options_skips_all_calls(fixture_scenario):
    fixture_scenario.pilot.fuel.fuel_on_board_kg = 0
    agents = {p: make_agent(p) for p in PARTIES}
    out, ledger = run(fixture_scenario, agents)
    assert out.decision_source == "UNRESOLVED"
    assert out.final is None and out.rounds_used == 0
    assert out.gap_vs_optimum is None and out.saving_vs_fifo is None
    assert all(not a.client.messages.create.called for a in agents.values())
    assert "unavailable" in describe_outcome(fixture_scenario, out)
    assert verify_entries(ledger.dump()).ok


def test_round_cap_and_binding(fixture_scenario):
    for cap in (0, 4, -1):
        with pytest.raises(ValueError, match="max_rounds"):
            run(fixture_scenario, stub_team(), max_rounds=cap)
    with pytest.raises(ValueError, match="correctly bound"):
        run(fixture_scenario, {"ATC": make_agent("PILOT")})
    out, _ = run(fixture_scenario, stub_team("stubborn"))
    assert out.rounds_used == 3
    assert out.decision_source == "FALLBACK_SOLVER"
    assert out.final.proposal == out.optimum.proposal
