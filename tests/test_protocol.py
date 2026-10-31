"""Protocol, messages and ledger. Uses stub agents; no model, no network."""

import json

import pytest

from triad import config
from triad.ledger import Ledger, verify_entries, verify_file
from triad.messages import parse_message
from triad.proposal import Proposal
from triad.protocol import run
from triad.scenario import random_scenario
from triad.solver import evaluate_all, fifo_baseline, optimum
from triad.stubs import StubAgent, stub_team

from .conftest import requires_data


# ---------------------------------------------------------------- messages

def test_counter_requires_proposal():
    with pytest.raises(ValueError):
        parse_message({"decision": "COUNTER", "rationale": "x"}, "ATC")


def test_reject_requires_rule_ids():
    with pytest.raises(ValueError):
        parse_message({"decision": "REJECT", "rationale": "x"}, "ATC")


def test_accept_must_not_carry_proposal():
    with pytest.raises(ValueError):
        parse_message({"decision": "ACCEPT", "rationale": "x",
                       "proposal": {"slot_id": "S00", "pilot_action": "HOLD", "gate": "A10"}}, "ATC")


def test_unknown_fields_and_wrong_signer_are_rejected():
    with pytest.raises(ValueError):
        parse_message({"decision": "ACCEPT", "rationale": "x", "extra": 1}, "ATC")
    with pytest.raises(ValueError):
        parse_message({"agent": "PILOT", "decision": "ACCEPT", "rationale": "x"}, "ATC")


def test_prose_is_not_a_message():
    with pytest.raises(ValueError):
        parse_message("let's just land", "ATC")


def test_valid_message_round_trips():
    m = parse_message(json.dumps({"decision": "COUNTER", "rationale": "ok",
                                  "proposal": {"slot_id": "S01", "pilot_action": "HOLD", "gate": "REMOTE"}}), "PILOT")
    assert m.agent == "PILOT" and m.proposal.key() == "S01|HOLD|REMOTE|SAME_CREW"


# ---------------------------------------------------------------- ledger

def test_ledger_chain_verifies_and_detects_tampering(tmp_path):
    path = tmp_path / "l.jsonl"
    led = Ledger("X", path)
    for i in range(5):
        led.append("T", "ORCHESTRATOR", i, {"i": i, "nested": {"a": [1, 2]}})
    assert verify_file(path).ok
    assert verify_entries(led.dump()).ok

    lines = path.read_text().splitlines()
    rec = json.loads(lines[2])
    rec["payload"]["i"] = 99
    lines[2] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    r = verify_file(path)
    assert not r.ok and r.first_bad_seq == 2 and r.reason == "hash mismatch"

    # dropping an entry breaks the chain at the gap
    path.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    r = verify_file(path)
    assert not r.ok and r.first_bad_seq == 2


# ---------------------------------------------------------------- protocol

@pytest.fixture(scope="module")
def scenario():
    return random_scenario(seed=29)


@requires_data
def test_round_cap_and_fallback(scenario, tmp_path):
    out, led = run(scenario, stub_team("stubborn"), ledger_path=tmp_path / "l.jsonl")
    assert out.rounds_used == config.MAX_ROUNDS
    assert out.decision_source == "FALLBACK_SOLVER"
    assert out.final.proposal.key() == optimum(scenario).proposal.key()
    assert out.infeasible_counters_caught == out.counters_attempted  # stubborn counters are self-serving
    assert verify_file(tmp_path / "l.jsonl").ok
    assert led.of_type("DECISION")[0].payload["requires_human_approval"] is True


@requires_data
def test_agreeable_agents_reach_consensus_on_seed(scenario):
    out, _ = run(scenario, stub_team("agreeable"))
    assert out.decision_source == "CONSENSUS" and out.rounds_used == 1
    assert out.final.proposal.key() == fifo_baseline(scenario).proposal.key()


@requires_data
def test_broken_agents_abstain_then_fallback(scenario):
    out, led = run(scenario, stub_team("broken"))
    assert out.decision_source == "FALLBACK_SOLVER"
    assert "abstained" in out.fallback_reason
    assert out.rounds_used == config.MAX_ABSTAINS_PER_AGENT
    assert len(led.of_type("ABSTAIN")) == 3 * config.MAX_ABSTAINS_PER_AGENT
    # the retry happened before every abstain
    assert all("attempt_1_error" in e.payload["raw"] for e in led.of_type("ABSTAIN"))


@requires_data
def test_flaky_agent_is_rescued_by_the_retry(scenario):
    out, led = run(scenario, stub_team("flaky"))
    assert out.abstains == 0
    assert all("attempt_0_error" in e.payload["raw"] for e in led.of_type("MESSAGE"))


@requires_data
def test_infeasible_counters_never_reach_the_table(scenario):
    out, led = run(scenario, stub_team("greedy"))
    feasible_keys = {e.proposal.key() for e in evaluate_all(scenario) if e.feasible}
    for e in led.of_type("PROPOSAL"):
        assert Proposal.model_validate(e.payload["on_table"]["proposal"]).key() in feasible_keys
    for e in led.of_type("INFEASIBLE_COUNTER"):
        assert e.payload["failing_rules"], "every discarded counter names the rule it failed"


class _Scripted(StubAgent):
    """Replays a fixed list of raw messages, one per call."""

    def __init__(self, party, script):
        super().__init__(party, "scripted")
        self.script = list(script)

    def respond(self, view, error=None):
        self.calls += 1
        return self.script.pop(0) if self.script else {"agent": self.party, "decision": "ACCEPT", "rationale": "ok"}


def _infeasible_for(scenario, party):
    for e in evaluate_all(scenario):
        if any(not v.ok for v in e.verdicts[party]) and e.proposal.pilot_action != "DIVERT":
            return e
    raise RuntimeError("no proposal infeasible for " + party)


@requires_data
def test_self_violation_is_refused(scenario):
    # Nothing infeasible can reach the table through a counter, so forge the seed: put a
    # proposal that fails PILOT's own rules on the table and have everyone ACCEPT it.
    from triad import protocol

    bad = _infeasible_for(scenario, "PILOT")
    orig = protocol.fifo_baseline
    protocol.fifo_baseline = lambda s, evals=None: bad   # forged seed for this test only
    try:
        agents = {
            "ATC": _Scripted("ATC", [{"agent": "ATC", "decision": "ACCEPT", "rationale": "ok"}]),
            "PILOT": _Scripted("PILOT", [{"agent": "PILOT", "decision": "ACCEPT", "rationale": "ok"}]),
            "GROUND": _Scripted("GROUND", [{"agent": "GROUND", "decision": "ACCEPT", "rationale": "ok"}]),
        }
        out, led = run(scenario, agents, max_rounds=1)
    finally:
        protocol.fifo_baseline = orig
    assert out.self_violations >= 1
    assert out.decision_source == "FALLBACK_SOLVER", "a refused ACCEPT must not count towards consensus"
    assert "PILOT" in {e.payload["agent"] for e in led.of_type("SELF_VIOLATION")}


@requires_data
def test_non_concession_is_discarded(scenario):
    evals = evaluate_all(scenario)
    feas = sorted((e for e in evals if e.feasible and e.proposal.pilot_action != "DIVERT"),
                  key=lambda e: e.costs["PILOT"].total)
    assert len(feas) >= 2
    dearer, cheaper = feas[-1], feas[0]
    pilot = _Scripted("PILOT", [
        {"agent": "PILOT", "decision": "COUNTER", "proposal": dearer.proposal.model_dump(), "rationale": "r1"},
        {"agent": "PILOT", "decision": "COUNTER", "proposal": cheaper.proposal.model_dump(), "rationale": "r2 (cheaper for me)"},
    ])
    agents = {"ATC": StubAgent("ATC", "stubborn"), "PILOT": pilot, "GROUND": StubAgent("GROUND", "stubborn")}
    out, led = run(scenario, agents)
    kinds = [e.payload["agent"] for e in led.of_type("NON_CONCESSION")]
    assert "PILOT" in kinds
    assert out.non_concessions >= 1


@requires_data
def test_unfounded_reject_is_logged(scenario):
    agents = {
        "ATC": _Scripted("ATC", [{"agent": "ATC", "decision": "REJECT", "rejected_rule_ids": ["ATC-HOLD-MAX"], "rationale": "no"}]),
        "PILOT": StubAgent("PILOT", "agreeable"),
        "GROUND": StubAgent("GROUND", "agreeable"),
    }
    out, led = run(scenario, agents, max_rounds=1)
    assert out.unfounded_rejects == 1
    assert out.decision_source == "FALLBACK_SOLVER"


@requires_data
def test_unknown_slot_in_counter_is_caught_as_schema_violation(scenario):
    agents = {
        "ATC": _Scripted("ATC", [{"agent": "ATC", "decision": "COUNTER", "rationale": "x",
                                  "proposal": {"slot_id": "S99", "pilot_action": "HOLD", "gate": "A10"}}]),
        "PILOT": StubAgent("PILOT", "agreeable"),
        "GROUND": StubAgent("GROUND", "agreeable"),
    }
    out, led = run(scenario, agents, max_rounds=1)
    e = led.of_type("INFEASIBLE_COUNTER")[0]
    assert e.payload["failing_rules"][0]["rule_id"] == "PUB-SCHEMA"
    assert out.infeasible_counters_caught == 1


@requires_data
def test_outcome_metrics_are_consistent(scenario):
    out, _ = run(scenario, stub_team("yielding"))
    assert out.final.feasible
    assert out.final.system_cost >= out.optimum.system_cost
    assert out.gap_vs_optimum >= 0
    assert out.counters_attempted >= out.infeasible_counters_caught + out.non_concessions
