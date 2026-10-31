"""Deterministic core: proposals, checkers, costs, solver."""

import pytest

from triad import checkers, costs, rules
from triad.proposal import REMOTE_STAND, Proposal, actions_for_delay, derive, enumerate_proposals
from triad.scenario import random_scenario
from triad.solver import evaluate, evaluate_all, feasible_set, fifo_baseline, optimum, summarize

from .conftest import requires_data

SEEDS = [0, 1, 5, 6, 12, 25]


@pytest.fixture(scope="module", params=SEEDS)
def scenario(request):
    return random_scenario(seed=request.param)


def test_actions_for_delay():
    assert actions_for_delay(-3) == ["SPEED_UP"]
    assert actions_for_delay(0) == ["CONTINUE"]
    assert actions_for_delay(4) == ["REDUCE_SPEED", "HOLD"]
    assert actions_for_delay(30) == ["REDUCE_SPEED", "HOLD"]


def test_every_rule_in_registry_has_owner_and_source():
    for r in rules.RULES.values():
        assert r.owner in ("ATC", "PILOT", "GROUND", "PUBLIC")
        assert r.source


@requires_data
def test_enumeration_is_unique_and_ends_with_divert(scenario):
    props = enumerate_proposals(scenario)
    keys = [p.key() for p in props]
    assert len(keys) == len(set(keys))
    assert props[-1].pilot_action == "DIVERT"
    assert sum(p.pilot_action == "DIVERT" for p in props) == 1


@requires_data
def test_verdicts_reference_registered_rules_and_owner(scenario):
    for p in enumerate_proposals(scenario):
        for party, vs in checkers.check_all(scenario, p).items():
            for v in vs:
                assert v.rule_id in rules.RULES
                assert rules.RULES[v.rule_id].owner == party


@requires_data
def test_hold_burns_more_fuel_than_reduce_speed(scenario):
    slot = scenario.atc.slots[-1]
    g = scenario.ground.gates[0].id
    hold = derive(scenario, Proposal(slot_id=slot.id, pilot_action="HOLD", gate=g))
    slow = derive(scenario, Proposal(slot_id=slot.id, pilot_action="REDUCE_SPEED", gate=g))
    assert hold.fuel_used_kg > slow.fuel_used_kg
    assert hold.hold_min > slow.hold_min
    assert hold.arrival_min == slow.arrival_min


@requires_data
def test_fuel_rule_fails_when_reserve_is_breached(scenario):
    # Holding for the last slot at full burn breaches the floor in every scenario with a
    # tight fuel draw; check the verdict is internally consistent either way.
    slot = scenario.atc.slots[-1]
    p = Proposal(slot_id=slot.id, pilot_action="HOLD", gate=REMOTE_STAND)
    d = derive(scenario, p)
    v = next(v for v in checkers.check_pilot(scenario, p, d) if v.rule_id == "PLT-FUEL-RES")
    assert v.ok == (d.fuel_remaining_kg >= checkers.fuel_floor_kg(scenario))
    assert v.margin == pytest.approx(d.fuel_remaining_kg - checkers.fuel_floor_kg(scenario))


@requires_data
def test_slot_cap_rule_blocks_full_bins(scenario):
    full = [b for b, n in scenario.atc.demand_per_bin.items() if n + 1 > scenario.atc.effective_aar_per_15]
    for slot in scenario.atc.slots:
        p = Proposal(slot_id=slot.id, pilot_action="HOLD", gate=REMOTE_STAND)
        v = next(v for v in checkers.check_atc(scenario, p) if v.rule_id == "ATC-SLOT-CAP")
        assert v.ok == (slot.bin_start_min not in full)


@requires_data
def test_gate_size_rule(scenario):
    small = [g for g in scenario.ground.gates
             if checkers._SIZE_ORDER[g.size] < checkers._SIZE_ORDER[scenario.aircraft.size]]
    for g in small:
        p = Proposal(slot_id=scenario.atc.slots[0].id, pilot_action="SPEED_UP", gate=g.id)
        v = next(v for v in checkers.check_ground(scenario, p) if v.rule_id == "GND-GATE-SIZE")
        assert not v.ok


@requires_data
def test_costs_are_non_negative_and_sum(scenario):
    for e in evaluate_all(scenario):
        for c in e.costs.values():
            assert c.total >= 0
            assert c.total == pytest.approx(sum(c.breakdown.values()), abs=0.2)
        assert e.system_cost == pytest.approx(sum(c.total for c in e.costs.values()), abs=0.2)


@requires_data
def test_optimum_is_feasible_and_minimal(scenario):
    evals = evaluate_all(scenario)
    feas = feasible_set(scenario, evals)
    assert feas, "divert is always feasible, so the feasible set is never empty"
    opt = optimum(scenario, evals)
    assert opt.feasible
    assert all(opt.system_cost <= e.system_cost for e in feas)


@requires_data
def test_fifo_is_feasible_and_not_better_than_optimum(scenario):
    evals = evaluate_all(scenario)
    fifo = fifo_baseline(scenario, evals)
    assert fifo.feasible
    assert fifo.system_cost >= optimum(scenario, evals).system_cost


@requires_data
def test_fifo_prefers_earliest_slot_and_holding(scenario):
    evals = evaluate_all(scenario)
    fifo = fifo_baseline(scenario, evals)
    if fifo.proposal.pilot_action == "DIVERT":
        return
    idx = {s.id: i for i, s in enumerate(scenario.atc.slots)}
    earliest = min(idx[e.proposal.slot_id] for e in feasible_set(scenario, evals)
                   if e.proposal.slot_id is not None)
    assert idx[fifo.proposal.slot_id] == earliest
    assert fifo.proposal.pilot_action != "REDUCE_SPEED" or not any(
        e.proposal.slot_id == fifo.proposal.slot_id and e.proposal.pilot_action == "HOLD"
        for e in feasible_set(scenario, evals))


@requires_data
def test_summary_counts(scenario):
    sm = summarize(scenario)
    assert sm.n_feasible <= sm.n_total
    assert sm.n_feasible >= 1
    assert all(n <= sm.n_total for n in sm.infeasible_by_rule.values())


@requires_data
def test_divert_evaluation_has_no_slot_rules(scenario):
    e = evaluate(scenario, Proposal(slot_id=None, pilot_action="DIVERT", gate=None))
    assert e.derived.arrival_min is None
    assert "ATC-HOLD-MAX" not in [v.rule_id for vs in e.verdicts.values() for v in vs]
    assert costs.system_cost(e.costs) >= scenario.weights.divert
