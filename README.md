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
.venv/bin/triad scenario --seed 29          # build and print a disruption scenario
.venv/bin/triad negotiate --seed 29 --stub  # run the protocol with stub agents (no API)
.venv/bin/pytest
```

## Anthropic agents and orchestration

Set `ANTHROPIC_API_KEY` and `AGENT_MODEL` in `.env` to a model ID available in your
Anthropic account (`TRIAD_MODEL` is also supported). No model is assumed or verified
by an offline test. Then run:

```bash
.venv/bin/triad negotiate --seed 29 --ledger runs/demo.jsonl
.venv/bin/triad verify runs/demo.jsonl
```

Three separate `AnthropicAgent` instances call the Anthropic Python SDK directly;
Google ADK and LiteLLM are not used. A forced `submit_response` tool call carries the
Pydantic message schema, following the
[Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create).
It submits data only; it cannot execute an aircraft or airport action.

The Python orchestrator freezes the role views before launching three parallel calls.
Each agent receives its own private metrics, the current proposal, its shortlist and
previous judge results. No conversation history is shared between SDK clients, and
another branch's same-round response cannot enter its prompt. Free-text rationales
stay in the audit log and are not replayed as scenario information.

Local validation handles schema, identity, feasibility, references and concessions.
There is at most one formatting repair per branch per round. Provider failures become
abstentions without an extra repair call; SDK retries are disabled and requests have
a 30-second timeout. Unanimous valid acceptance exits early; otherwise the engine
chooses the full-set solver fallback within three rounds (earlier after repeated
abstentions). An empty feasible set skips all model calls and returns `UNRESOLVED`.
Every final proposal is freshly validated. Prompt hashes, model/response IDs, latency
and available token usage are recorded in outcome metadata and the decision ledger.
Unknown usage and estimated cost remain null.

**Scope:** this integrates the existing repository's proposal/concession protocol.
The newer attached PRD's ranked option-ID ballots, Borda compromise, normalised utility
thresholds, LAND-only engine, SSE service and A2UI are not implemented by this change.
The existing engine still models DIVERT; this is a simulation, not evidence of alternate
airport suitability. Historical end-to-end tests need the missing `flights.parquet`;
`tests/test_agents.py` uses an explicitly synthetic fixture and mocked SDK responses.
No live-model availability, API cost, or held-out evaluation is claimed.

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
  agents.py     Anthropic SDK agents and isolated role prompts
  stubs.py      deterministic agents for offline protocol checks
```
