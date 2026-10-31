from tests.conftest import requires_data
from triad.scenario import random_scenario


@requires_data
def test_scenario_is_reproducible_and_consistent():
    a = random_scenario(seed=7, dest="ORD")
    b = random_scenario(seed=7, dest="ORD")
    assert a.model_dump() == b.model_dump()

    # slots are 5 minutes apart starting no later than the earliest achievable arrival
    starts = [s.start_min for s in a.atc.slots]
    assert all(y - x == 5 for x, y in zip(starts, starts[1:]))
    assert starts[0] <= a.pilot.earliest_arr_min < starts[0] + 5

    # gate windows never overlap on the same gate
    for g in a.ground.gates:
        for (s1, e1), (s2, e2) in zip(g.busy, g.busy[1:]):
            assert e1 <= s2

    # the original gate is blocked across the ETA, which is the ground conflict
    og = next(g for g in a.ground.gates if g.id == a.ground.original_gate)
    assert any(s <= a.pilot.eta_min < e for s, e in og.busy)

    # fuel above the floor is expressed in minutes of holding and is positive
    f = a.pilot.fuel
    floor = f.final_reserve_min * f.cruise_burn_kg_min + f.alternate_fuel_kg + f.contingency_kg
    assert f.fuel_on_board_kg > floor

    # every synthetic field family is declared
    for key in ("aircraft", "pilot.fuel", "ground.gates", "disruption.capacity_factor"):
        assert a.provenance[key] == "synthetic"
