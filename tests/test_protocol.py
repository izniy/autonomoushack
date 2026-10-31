"""Protocol, messages and ledger. Uses stub agents; no model, no network."""

import json

import pytest

from triad import config
from triad.judge import Judge, Reply
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


def test_only_reject_names_rules():
    # a REJECT without rules is a vote against on preference; rules on an ACCEPT make no sense
    assert parse_message({"decision": "REJECT", "rationale": "x"}, "ATC").rejected_rule_ids == []
    with pytest.raises(ValueError):
        parse_message({"decision": "ACCEPT", "rationale": "x", "rejected_rule_ids": ["ATC-HOLD-MAX"]}, "ATC")


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
    # Nothing infeasible can reach the table through a counter, so seed the judge with a
    # proposal that fails PILOT's own rules and have everyone ACCEPT it.
    bad = _infeasible_for(scenario, "PILOT")
    judge = Judge(scenario, max_rounds=1, seed=bad)
    views = judge.start_round(1)
    replies = {p: Reply(parse_message({"decision": "ACCEPT", "rationale": "ok"}, p)) for p in views}
    j = judge.judge_round(1, replies)
    out = judge.finish({}, 0.0)
    assert j.outcomes["PILOT"] == "SELF_VIOLATION" and not j.consensus
    assert out.self_violations >= 1
    assert out.decision_source == "FALLBACK_SOLVER", "a refused ACCEPT must not count towards consensus"
    assert "PILOT" in {e.payload["agent"] for e in judge.ledger.of_type("SELF_VIOLATION")}


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
    out, led = run(scenario, agents, max_rounds=2)   # round 1 is not the vote round
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
    assert 0.0 <= out.min_utility_final <= 1.0


# ---------------------------------------------------------------- judge

def _accept(p):
    return Reply(parse_message({"decision": "ACCEPT", "rationale": "ok"}, p))


@requires_data
def test_utilities_are_normalised_over_the_feasible_landing_set(scenario):
    j = Judge(scenario)
    assert j.feasible and all(e.proposal.pilot_action != "DIVERT" for e in j.feasible)
    for p in ("ATC", "PILOT", "GROUND"):
        us = [j.utility(p, e) for e in j.feasible]
        assert max(us) == 1.0 and min(us) == 0.0 or len(set(us)) == 1
    # the optimum clears the threshold for everyone whenever anything does
    if j.acceptable:
        assert all(j.utility(p, j.opt) >= 0 for p in ("ATC", "PILOT", "GROUND"))
    assert j.ledger.of_type("SCENARIO")[0].payload["n_mutually_acceptable"] == len(j.acceptable)


@requires_data
def test_final_round_counter_is_a_late_counter(scenario):
    j = Judge(scenario, max_rounds=1)
    views = j.start_round(1)
    assert all(v.final_round for v in views.values())
    best = views["ATC"].options[0].proposal.model_dump()
    replies = {"ATC": Reply(parse_message({"decision": "COUNTER", "proposal": best, "rationale": "x"}, "ATC")),
               "PILOT": _accept("PILOT"), "GROUND": _accept("GROUND")}
    res = j.judge_round(1, replies)
    assert res.outcomes["ATC"] == "LATE_COUNTER" and res.stop and not res.consensus
    out = j.finish({}, 0.0)
    assert out.late_counters == 1 and out.counters_attempted == 0
    assert out.fallback_code in ("NO_CONSENSUS_ROUND_CAP", "NO_ACCEPTABLE_OPTION")


@requires_data
def test_preference_reject_is_legitimate_but_blocks_consensus(scenario):
    j = Judge(scenario, max_rounds=1)
    j.start_round(1)
    replies = {"ATC": Reply(parse_message({"decision": "REJECT", "rationale": "too costly for us"}, "ATC")),
               "PILOT": _accept("PILOT"), "GROUND": _accept("GROUND")}
    res = j.judge_round(1, replies)
    assert res.outcomes["ATC"] == "REJECT_PREFERENCE" and not res.consensus
    assert j.unfounded == 0
    assert "my_utility" in j.ledger.of_type("REJECT_PREFERENCE")[0].payload


@requires_data
def test_divert_counter_is_refused_as_public_rule(scenario):
    j = Judge(scenario, max_rounds=2)
    j.start_round(1)
    divert = {"slot_id": None, "pilot_action": "DIVERT", "gate": None}
    replies = {"PILOT": Reply(parse_message({"decision": "COUNTER", "proposal": divert, "rationale": "x"}, "PILOT")),
               "ATC": _accept("ATC"), "GROUND": _accept("GROUND")}
    res = j.judge_round(1, replies)
    assert res.outcomes["PILOT"] == "INFEASIBLE_COUNTER"
    assert "PUB-DIVERT" in j.ledger.of_type("INFEASIBLE_COUNTER")[0].payload["failing_rules"][0]["detail"]
    assert all(c.proposal.pilot_action != "DIVERT" for c in j.view("PILOT", 1).options)


@requires_data
def test_round_winner_prefers_mutually_acceptable_counters(scenario):
    j = Judge(scenario, max_rounds=3)
    if not j.acceptable:
        pytest.skip("scenario has no mutually acceptable option")
    acc = max(j.acceptable, key=lambda e: e.system_cost)                   # dearest acceptable
    cheaper = [e for e in j.feasible if not j.is_acceptable(e) and e.system_cost < acc.system_cost]
    if not cheaper:
        pytest.skip("no cheaper unacceptable counter to compare against")
    assert j.pick_winner([("ATC", cheaper[0]), ("PILOT", acc)]).proposal.key() == acc.proposal.key()


@requires_data
def test_agent_errors_get_their_own_reason_code(scenario):
    class Crashing(StubAgent):
        def respond(self, view, error=None):
            raise RuntimeError("connection reset")

    agents = {"ATC": Crashing("ATC"), "PILOT": StubAgent("PILOT", "agreeable"), "GROUND": StubAgent("GROUND", "agreeable")}
    out, led = run(scenario, agents)
    assert out.decision_source == "FALLBACK_SOLVER" and out.fallback_code == "AGENT_ERROR"
    assert out.rounds_used == config.MAX_ABSTAINS_PER_AGENT
    assert "traceback" in led.of_type("ABSTAIN")[0].payload["raw"]


def test_no_feasible_landing_is_unresolved_not_a_diversion_order():
    from triad.scenario import random_scenario

    s = random_scenario(seed=29)
    # Starve the aircraft of fuel so that no landing option is legal.
    s.pilot.fuel.fuel_on_board_kg = 0.0
    j = Judge(s)
    assert j.unresolved and j.decision_source == "UNRESOLVED" and j.code == "NO_FEASIBLE_LANDING"
    out = j.finish({}, 0.0)
    assert out.final.proposal.pilot_action == "DIVERT"
    assert out.diversion_assessment and out.diversion_assessment["landing_options_failed_by_rule"]
    assert j.ledger.of_type("DECISION")[0].payload["requires_human_approval"] is True
    out2, _ = run(s, stub_team("agreeable"))
    assert out2.decision_source == "UNRESOLVED" and out2.rounds_used == 0
