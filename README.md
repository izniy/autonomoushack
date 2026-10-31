# TRIAD

Cross-organisational disruption negotiation for Airbus Challenge #2 (Autonomous SG 2026).

Three agents — **ATC**, **Pilot/AOC** and **Ground** — negotiate a revised arrival slot, pilot
action, gate and turnaround for a real delayed flight from the 2015 US DOT on-time dataset.
Negotiation runs for at most three rounds around an orchestrator-owned proposal.

Design principle: **agents reason, concede and explain; code decides what is legal, what it
costs, and who wins a round.**

* Every proposal is validated by each party's deterministic rules engine (fuel reserve, wake
  separation, slot capacity, gate compatibility, crew duty, turnaround minima), each rule
  carrying an ID and a regulatory/SOP source.
* Every message, verdict and orchestrator decision is appended to a hash-chained consensus
  ledger.
* If the agents do not converge, a deterministic solver picks the lowest-cost feasible option
  and flags it for human review. The OCC duty manager approves; the system never executes.

See [PLAN.md](PLAN.md) for the full design, evaluation plan and build order.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env            # add API key for live agents; stub/fallback modes need none
```

Place the datasets in `data/`:

```
data/flights.csv     # 2015 US DOT flight delays and cancellations (not committed, ~590 MB)
data/airlines.csv
data/airports.csv
```

Then build the parquet cache once:

```bash
.venv/bin/python scripts/build_parquet.py
```

## Usage

```bash
.venv/bin/triad scenario --random          # build and print a disruption scenario
.venv/bin/triad negotiate --random --stub  # run the protocol with stub agents (no API)
.venv/bin/pytest
```

## Layout

```
triad/
  data.py       DuckDB queries over the DOT dataset
  scenario.py   scenario assembly with real / derived / synthetic provenance
  rules.py      rule registry (id, source, check)
  checkers.py   per-party deterministic checkers
  costs.py      per-party cost functions
  solver.py     feasible-set enumeration, FIFO baseline, optimal fallback
  messages.py   strict message schemas
  ledger.py     hash-chained JSONL ledger
  protocol.py   round orchestration
  agents.py     LLM-backed and stub agents
  eval.py       batch evaluation
  api.py        FastAPI service + dashboard
```
