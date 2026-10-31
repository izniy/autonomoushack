"""DuckDB queries over the 2015 US DOT on-time dataset.

Everything here is *real* or *derived* from the dataset. Synthetic fields are
generated in `scenario.py` and labelled there.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from functools import lru_cache

import duckdb

from .config import (
    AIRLINES_CSV,
    AIRPORTS_CSV,
    FLIGHTS_PARQUET,
    LATE_AIRCRAFT_DELAY_MIN,
    WEATHER_DELAY_MIN,
)

# Dataset times are HHMM strings/ints; we work in minutes after local midnight.
_HHMM_TO_MIN = "(CAST({c} AS INTEGER) // 100) * 60 + (CAST({c} AS INTEGER) % 100)"


def hhmm_to_min(v: str | int | None) -> int | None:
    if v is None:
        return None
    v = int(v)
    return (v // 100) * 60 + v % 100


def min_to_hhmm(m: int) -> str:
    m = int(m) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


@lru_cache(maxsize=1)
def _root() -> duckdb.DuckDBPyConnection:
    if not FLIGHTS_PARQUET.exists():
        raise FileNotFoundError(f"{FLIGHTS_PARQUET} missing; run scripts/build_parquet.py")
    con = duckdb.connect()
    con.execute(f"CREATE VIEW f AS SELECT * FROM '{FLIGHTS_PARQUET}'")
    con.execute(f"CREATE VIEW airports AS SELECT * FROM read_csv_auto('{AIRPORTS_CSV}')")
    con.execute(f"CREATE VIEW airlines AS SELECT * FROM read_csv_auto('{AIRLINES_CSV}')")
    return con


_local = threading.local()


def connect() -> duckdb.DuckDBPyConnection:
    """One cursor per thread over the shared in-memory database: a DuckDB connection refuses
    concurrent calls, and the evaluation and the API build scenarios from several threads."""
    cur = getattr(_local, "cur", None)
    if cur is None:
        cur = _local.cur = _root().cursor()
    return cur


FLIGHT_COLS = [
    "YEAR", "MONTH", "DAY", "DAY_OF_WEEK", "AIRLINE", "FLIGHT_NUMBER", "TAIL_NUMBER",
    "ORIGIN_AIRPORT", "DESTINATION_AIRPORT", "SCHEDULED_DEPARTURE", "DEPARTURE_DELAY",
    "SCHEDULED_TIME", "AIR_TIME", "DISTANCE", "SCHEDULED_ARRIVAL", "ARRIVAL_DELAY",
    "DIVERTED", "CANCELLED", "AIR_SYSTEM_DELAY", "SECURITY_DELAY", "AIRLINE_DELAY",
    "LATE_AIRCRAFT_DELAY", "WEATHER_DELAY",
]


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def qualifying_filter() -> str:
    return (
        f"(WEATHER_DELAY > {WEATHER_DELAY_MIN} OR LATE_AIRCRAFT_DELAY > {LATE_AIRCRAFT_DELAY_MIN})"
        " AND CANCELLED = 0 AND DIVERTED = 0 AND TAIL_NUMBER IS NOT NULL"
        " AND length(ORIGIN_AIRPORT) = 3 AND length(DESTINATION_AIRPORT) = 3"
    )


def pick_flight(
    seed: int | None = None,
    dest: str | None = None,
    month: int | None = None,
    flight_id: tuple[int, int, str, int] | None = None,
) -> dict:
    """Pick one disrupted flight.

    `flight_id` = (month, day, airline, flight_number) selects a specific row;
    otherwise a seeded pseudo-random qualifying row is returned.
    """
    con = connect()
    cols = ", ".join(FLIGHT_COLS)
    where = [qualifying_filter()]
    params: list = []
    if flight_id:
        m, d, al, fn = flight_id
        where += ["MONTH = ?", "DAY = ?", "AIRLINE = ?", "FLIGHT_NUMBER = ?"]
        params += [m, d, al, fn]
    if dest:
        where.append("DESTINATION_AIRPORT = ?")
        params.append(dest)
    if month:
        where.append("MONTH = ?")
        params.append(month)
    # Deterministic "random" pick: order by a hash of the row key and the seed. DuckDB's
    # setseed()+random() is not stable across threads, so it is unsuitable for reproducibility.
    order = "hash(MONTH, DAY, AIRLINE, FLIGHT_NUMBER, ?)" if seed is not None else "random()"
    if seed is not None:
        params.append(seed)
    cur = con.execute(
        f"SELECT {cols} FROM f WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 1", params
    )
    rows = _rows(cur)
    if not rows:
        raise LookupError("no qualifying flight for the given filters")
    return rows[0]


def arrival_bank(flight: dict, window_min: int = 30, center_min: int | None = None) -> list[dict]:
    """Flights scheduled to arrive at the same destination within +/- window of `center_min`
    (default: this flight's scheduled arrival)."""
    con = connect()
    sa = center_min if center_min is not None else hhmm_to_min(flight["SCHEDULED_ARRIVAL"])
    cur = con.execute(
        f"""
        SELECT AIRLINE, FLIGHT_NUMBER, TAIL_NUMBER, ORIGIN_AIRPORT, DISTANCE,
               SCHEDULED_ARRIVAL, ARRIVAL_DELAY,
               {_HHMM_TO_MIN.format(c='SCHEDULED_ARRIVAL')} AS sched_arr_min
        FROM f
        WHERE DESTINATION_AIRPORT = ? AND MONTH = ? AND DAY = ? AND CANCELLED = 0
          AND abs({_HHMM_TO_MIN.format(c='SCHEDULED_ARRIVAL')} - ?) <= ?
          AND NOT (AIRLINE = ? AND FLIGHT_NUMBER = ?)
        ORDER BY sched_arr_min
        """,
        [
            flight["DESTINATION_AIRPORT"], flight["MONTH"], flight["DAY"], sa, window_min,
            flight["AIRLINE"], flight["FLIGHT_NUMBER"],
        ],
    )
    return _rows(cur)


def next_leg(flight: dict) -> dict | None:
    """Next flight of the same tail departing the destination later that day, if any."""
    con = connect()
    sa = hhmm_to_min(flight["SCHEDULED_ARRIVAL"])
    cur = con.execute(
        f"""
        SELECT AIRLINE, FLIGHT_NUMBER, TAIL_NUMBER, ORIGIN_AIRPORT, DESTINATION_AIRPORT,
               SCHEDULED_DEPARTURE, DEPARTURE_DELAY, LATE_AIRCRAFT_DELAY, CANCELLED,
               {_HHMM_TO_MIN.format(c='SCHEDULED_DEPARTURE')} AS sched_dep_min
        FROM f
        WHERE TAIL_NUMBER = ? AND MONTH = ? AND DAY = ? AND ORIGIN_AIRPORT = ?
          AND {_HHMM_TO_MIN.format(c='SCHEDULED_DEPARTURE')} > ?
        ORDER BY sched_dep_min LIMIT 1
        """,
        [flight["TAIL_NUMBER"], flight["MONTH"], flight["DAY"], flight["DESTINATION_AIRPORT"], sa],
    )
    rows = _rows(cur)
    return rows[0] if rows else None


def connection_candidates(flight: dict, lo: int = 45, hi: int = 180) -> list[dict]:
    """Same-airline departures from the destination lo..hi minutes after scheduled arrival.

    A proxy for connecting banks (flights, not passengers).
    """
    con = connect()
    sa = hhmm_to_min(flight["SCHEDULED_ARRIVAL"])
    cur = con.execute(
        f"""
        SELECT FLIGHT_NUMBER, DESTINATION_AIRPORT, SCHEDULED_DEPARTURE,
               {_HHMM_TO_MIN.format(c='SCHEDULED_DEPARTURE')} AS sched_dep_min
        FROM f
        WHERE ORIGIN_AIRPORT = ? AND AIRLINE = ? AND MONTH = ? AND DAY = ? AND CANCELLED = 0
          AND {_HHMM_TO_MIN.format(c='SCHEDULED_DEPARTURE')} BETWEEN ? AND ?
        ORDER BY sched_dep_min
        """,
        [flight["DESTINATION_AIRPORT"], flight["AIRLINE"], flight["MONTH"], flight["DAY"],
         sa + lo, sa + hi],
    )
    return _rows(cur)


def nominal_arrival_rate(airport: str, hour: int) -> dict:
    """Historical arrivals per 15-min bin at `airport` for this hour-of-day across 2015.

    Returns p50 and p95 of the per-bin counts; p95 is used as the nominal AAR per bin.
    """
    con = connect()
    cur = con.execute(
        f"""
        WITH bins AS (
            SELECT MONTH, DAY,
                   ({_HHMM_TO_MIN.format(c='SCHEDULED_ARRIVAL')}) // 15 AS bin,
                   count(*) AS n
            FROM f
            WHERE DESTINATION_AIRPORT = ? AND CANCELLED = 0
              AND CAST(SCHEDULED_ARRIVAL AS INTEGER) // 100 = ?
            GROUP BY 1, 2, 3
        )
        SELECT quantile_cont(n, 0.5) AS p50, quantile_cont(n, 0.95) AS p95, max(n) AS mx
        FROM bins
        """,
        [airport, hour],
    )
    p50, p95, mx = cur.fetchone()
    return {"p50_per_15min": p50 or 0, "p95_per_15min": p95 or 0, "max_per_15min": mx or 0}


def peak_ground_occupancy(airport: str, month: int, day: int) -> int:
    """Peak number of aircraft simultaneously on the ground at `airport` that day.

    Derived from tail numbers: an aircraft occupies the ground from scheduled arrival until
    its next scheduled departure from the same airport. Used as a gate-count estimate.
    """
    con = connect()
    cur = con.execute(
        f"""
        WITH arr AS (
            SELECT TAIL_NUMBER, {_HHMM_TO_MIN.format(c='SCHEDULED_ARRIVAL')} AS t
            FROM f WHERE DESTINATION_AIRPORT = ? AND MONTH = ? AND DAY = ? AND CANCELLED = 0
        ), dep AS (
            SELECT TAIL_NUMBER, {_HHMM_TO_MIN.format(c='SCHEDULED_DEPARTURE')} AS t
            FROM f WHERE ORIGIN_AIRPORT = ? AND MONTH = ? AND DAY = ? AND CANCELLED = 0
        ), ev AS (
            SELECT t, 1 AS d FROM arr UNION ALL SELECT t, -1 AS d FROM dep
        )
        SELECT max(run) FROM (
            SELECT sum(d) OVER (ORDER BY t, d ROWS UNBOUNDED PRECEDING) AS run FROM ev
        )
        """,
        [airport, month, day, airport, month, day],
    )
    v = cur.fetchone()[0]
    return int(v or 0)


@dataclass(frozen=True)
class Airport:
    code: str
    name: str
    city: str
    state: str
    lat: float | None
    lon: float | None


@lru_cache(maxsize=1)
def airports() -> dict[str, Airport]:
    con = connect()
    out = {}
    for r in _rows(con.execute("SELECT * FROM airports")):
        out[r["IATA_CODE"]] = Airport(
            r["IATA_CODE"], r["AIRPORT"], r["CITY"], r["STATE"], r["LATITUDE"], r["LONGITUDE"]
        )
    return out


@lru_cache(maxsize=1)
def airline_names() -> dict[str, str]:
    con = connect()
    return {r["IATA_CODE"]: r["AIRLINE"] for r in _rows(con.execute("SELECT * FROM airlines"))}


def haversine_nm(a: Airport, b: Airport) -> float:
    r = 3440.065  # earth radius, nautical miles
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def nearest_alternate(dest: str, min_nm: float = 40, max_nm: float = 250) -> tuple[Airport, float] | None:
    """Nearest airport in the dataset between min_nm and max_nm of `dest` with traffic.

    Only airports that appear as destinations in the dataset are considered, so the
    alternate is a real commercial airport rather than a tiny field.
    """
    apts = airports()
    d = apts.get(dest)
    if not d or d.lat is None:
        return None
    con = connect()
    busy = {r[0] for r in con.execute(
        "SELECT DESTINATION_AIRPORT FROM f GROUP BY 1 HAVING count(*) > 2000"
    ).fetchall()}
    best = None
    for code, a in apts.items():
        if code == dest or a.lat is None or code not in busy:
            continue
        dist = haversine_nm(d, a)
        if min_nm <= dist <= max_nm and (best is None or dist < best[1]):
            best = (a, dist)
    return best
