from tests.conftest import requires_data
from triad import data


def test_hhmm_roundtrip():
    assert data.hhmm_to_min("2156") == 21 * 60 + 56
    assert data.min_to_hhmm(21 * 60 + 56) == "21:56"
    assert data.min_to_hhmm(25 * 60) == "01:00"


@requires_data
def test_pick_flight_is_seeded_and_qualifying():
    a = data.pick_flight(seed=7, dest="ORD")
    b = data.pick_flight(seed=7, dest="ORD")
    assert a == b
    assert a["DESTINATION_AIRPORT"] == "ORD"
    assert a["WEATHER_DELAY"] > 15 or a["LATE_AIRCRAFT_DELAY"] > 20


@requires_data
def test_context_queries_return_sane_shapes():
    fl = data.pick_flight(seed=7, dest="ORD")
    bank = data.arrival_bank(fl)
    assert bank and all(abs(r["sched_arr_min"] - data.hhmm_to_min(fl["SCHEDULED_ARRIVAL"])) <= 30 for r in bank)
    aar = data.nominal_arrival_rate("ORD", 17)
    assert aar["p95_per_15min"] >= aar["p50_per_15min"] > 0
    assert data.peak_ground_occupancy("ORD", 8, 7) > 0
    alt = data.nearest_alternate("ORD")
    assert alt and 40 <= alt[1] <= 250
