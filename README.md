# TRIAD

Cross-organisational disruption negotiation for Airbus Challenge #2 (Autonomous SG 2026).

Three agents — **ATC**, **Pilot/AOC** and **Ground** — negotiate a revised arrival slot, pilot
action, gate and crew plan for a real delayed flight from the 2015 US DOT on-time dataset.
The negotiation runs for at most three rounds around a proposal the orchestrator owns; the
OCC duty manager approves or overrides the result; the system never executes anything.

**Design principle: agents reason, concede and explain; code decides what is legal, what it
costs, who wins a round, when consensus exists and when to fall back.**

```
  DOT row  ──►  Scenario (real / derived / synthetic, labelled)
                   │
                   ▼
            enumerate every proposal  ──►  3 deterministic checkers (rule ids + sources)
                   │                        3 cost functions, public weights
                   ▼
            FIFO seed on the table
                   │
   ┌── round 1..3 ─┤  ATC ▸ PILOT ▸ GROUND answer in parallel: ACCEPT | COUNTER | REJECT(rule ids)
   │               │  judge: self-violation? unfounded reject? infeasible counter? concession?
   │               │  all ACCEPT → CONSENSUS ; else best feasible counter goes on the table
   └───────────────┤
                   ▼
   CONSENSUS  or  FALLBACK_SOLVER (coded reason)  or  UNRESOLVED (no legal landing)
                   │
                   ▼
   hash-chained ledger ──► dashboard ──► human APPROVE / OVERRIDE (legal options only) ──► ledger
```

## What is deterministic and what is a model

| Concern | Decided by | Where |
|---|---|---|
| Is a proposal legal for ATC / Pilot / Ground? | code — rule registry with regulatory sources | `rules.py`, `checkers.py` |
| What does it cost each party, and the system? | code — public weights | `costs.py` |
| What is the FIFO status quo, what is the optimum? | code — exhaustive over ≤ 200 proposals | `solver.py` |
| Which counter wins a round, is there consensus, when to stop | code | `judge.py` |
| Why should my organisation accept, counter or reject; what am I conceding; a rationale | model (or a scripted stub) | `agents.py`, `stubs.py` |
| Fallback when the model errs, stalls or the round cap is hit | code — same solver, flagged with a code | `judge.py` |
| Final approval | human | `api.py`, dashboard |

A model reply is a structured message (`ACCEPT | COUNTER(proposal) | REJECT(rule_ids)` plus
concessions, rationale, confidence). The judge then checks it:

| Ruling | Meaning |
|---|---|
| `ACCEPT_OK` | accepted a proposal that passes the accepter's own rules |
| `SELF_VIOLATION` | tried to accept a proposal that fails its own hard rules — refused |
| `REJECT_OK` | rejected citing a rule the proposal really fails |
| `UNFOUNDED_REJECT` | cited rules the proposal passes — counted as a plain non-accept |
| `REJECT_PREFERENCE` | rejected without a rule — legitimate, counts against consensus |
| `COUNTER_OK` | feasible counter that is a concession; candidate to go on the table |
| `INFEASIBLE_COUNTER` | fails any party's checker (or references nothing) — discarded, rules logged |
| `NON_CONCESSION` | cheaper for the proposer than its last counter — discarded |
| `LATE_COUNTER` | counter in the final round, which only votes — ignored |

Consensus is three explicit `ACCEPT`s in one round. Otherwise the feasible conceding counter
with the lowest system cost (mutually acceptable first, tie → earliest slot) goes on the
table. After the round cap, two abstains by one agent, or any agent error, the solver's
optimum is used and the outcome carries a `fallback_code`:
`NO_CONSENSUS_ROUND_CAP | NO_ACCEPTABLE_OPTION | AGENT_ABSTAINED | AGENT_ERROR | NO_FEASIBLE_LANDING`.
`DIVERT` is never negotiated: when no landing option is legal the outcome is `UNRESOLVED` with
a diversion assessment for the duty manager.

Every state change is one entry in a SHA-256 hash-chained JSONL ledger: scenario, options
each party saw, each message with the model id / prompt hash / tokens / latency behind it,
each ruling with the measured-vs-limit verdicts, each round result with the rule that chose
the next proposal, the decision, and the human step. `triad verify` and the dashboard
recompute the chain; `triad audit` rebuilds a Markdown report from the file alone.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env            # add ANTHROPIC_API_KEY for live agents; stubs need none
```

Place the datasets in `data/` (not committed):

```
data/flights.csv     # 2015 US DOT flight delays and cancellations (~590 MB)
data/airlines.csv
data/airports.csv
```

Then build the parquet cache once: `.venv/bin/python scripts/build_parquet.py`.

## Usage

```bash
triad scenario --seed 29                 # one disruption, with provenance labels
triad solve --seed 29                    # decision space, FIFO and optimum
triad negotiate --seed 29 --stub yielding   # full protocol with scripted agents, no API
triad negotiate --seed 29                # live model agents (TRIAD_MODEL, default Sonnet)
triad verify runs/EV4636-2015-01-07.ledger.jsonl
triad audit  runs/EV4636-2015-01-07.ledger.jsonl > audit.md
triad eval --arms greedy yielding agreeable stubborn          # 30 seeds, 4 stub arms
triad eval --arms yielding llm:claude-haiku-4-5-20251001 -n 20
triad serve                              # dashboard at http://127.0.0.1:8000
pytest
```

Demo seeds are `29, 7, 101`; evaluation seeds start at 1000 and never overlap them.

Stub policies: `greedy` (counters with its cheapest option until the table is within
tolerance), `yielding` (concedes progressively), `agreeable` (accepts anything legal),
`stubborn` (never accepts), `reckless` (counters with infeasible proposals), `flaky`
(fails the schema once), `broken` (always errors). They exist to exercise every path of the
judge without a model, and to give the model arms something to be compared against.

## Dashboard and API

`triad serve` starts FastAPI with the single-page console. `POST /api/runs` starts a
negotiation in a worker thread; `GET /api/runs/{id}/events` streams every ledger entry over
SSE as it is written, so the three agent columns fill in live with the judge's ruling under
each message. When the run ends the duty manager sees the decision, its source and fallback
code, system cost against FIFO and optimum, each party's utility, and can **Approve** or
**Override** to any *legal* option (an override that fails a hard rule is refused with the
failing rules) or hand off with the diversion assessment. The human step is appended to the
same chain as `HUMAN_DECISION`, actor `USER`. `/api/runs/{id}/audit.md|.json|verify`
download the trail; `/api/eval` shows the latest batch summary on the Evaluation tab.

## Evaluation

`triad eval` runs arms over disjoint seeds with a thread pool and writes
`runs/eval/<tag>/{rows.jsonl, summary.json, summary.md}` plus every ledger. Metrics:
consensus rate (overall and given an option exists that clears every party's threshold),
fallback codes, counters attempted vs infeasible counters caught, whether the final decision
passes all checkers (must be 1.00), gap to the optimum, saving vs FIFO, per-party minimum
utility, rounds, wall time, model calls, tokens and an estimated USD per run.

Stub arms over 30 seeds (`runs/eval/stubs-30`, reproducible with `triad eval --tag stubs-30`):

| | agreeable | greedy | yielding | stubborn |
|---|---|---|---|---|
| consensus rate | 0.97 | 0.87 | 0.90 | 0.00 |
| counters attempted / infeasible caught | 0 / 0 | 93 / 84 | 93 / 78 | 174 / 144 |
| final decision passes all checkers | 1.00 | 1.00 | 1.00 | 1.00 |
| gap to optimum, mean | 257 | 445 | 361 | 0 |
| saving vs FIFO, mean | 0 | −188 | −104 | 257 |
| share of runs at optimum | 0.38 | 0.45 | 0.55 | 1.00 |

Read honestly: `agreeable` reaches consensus on the FIFO seed every time, which is vacuous;
`greedy` and `yielding` reach consensus on options that are *worse for the system than
FIFO* on average, because the judge lets a legal, conceding counter win when it is cheapest
among the counters offered, not among all options; `stubborn` scores a perfect gap only
because the solver decides every time. The point of the stub arms is to show what the
protocol does with each behaviour and that no illegal proposal ever got through, not to
claim a saving. The live-model arm is what has to beat FIFO, and its numbers are only
meaningful when produced with a key — see Limits.

## Limits and caveats

* **Synthetic fields.** Fuel on board, burn rates, gate map and occupancy, pax connection
  counts, crew duty end and aircraft type are seeded from the row and labelled `synthetic`
  in the scenario, the UI and the ledger. Flight, delay breakdown, arrival bank, next leg of
  the same tail and nominal arrival rate are `real` or `derived` from the 2015 data.
* **Cost weights are public assumptions**, not airline figures; the system cost is in
  USD-like units. Utilities are normalised per party over the feasible set, so a utility of
  1 means "my cheapest legal option", not "free".
* **Consensus can be vacuous** (see above); the gap to the optimum and the saving vs FIFO
  are the honest measures.
* **No live model results are shipped unless a key was available when the evaluation ran.**
  Without `ANTHROPIC_API_KEY` the live agents abstain, the judge records `AGENT_ERROR` and the
  solver decides — the system degrades to a flagged deterministic answer rather than
  pretending.
* **Dollar figures per run are estimates** from list prices in `config.PRICES_PER_MTOK`.
* 2015 data, single disrupted flight at a time, no A-CDM integration, model latency of a few
  seconds per round.

## Layout

```
triad/
  data.py       DuckDB queries over the DOT dataset (one cursor per thread)
  scenario.py   scenario assembly with real / derived / synthetic provenance
  proposal.py   proposal fields, enumeration and derived timings
  rules.py      rule registry (id, owner, title, source)
  checkers.py   per-party deterministic checkers → verdicts (measured, limit, margin)
  costs.py      per-party cost functions, public weights
  solver.py     feasible set, FIFO baseline, optimum
  config.py     model ids, timeouts, weights, thresholds (env-overridable)
  messages.py   strict message schema and the view each agent receives
  views.py      per-party private state and option rendering
  judge.py      round rulings, consensus, fallback, utilities, ledger writes
  protocol.py   parallel ask / retry / abstain loop around the judge
  agents.py     live model agents (structured output, forced-tool fallback, telemetry)
  stubs.py      scripted agents for tests and evaluation baselines
  ledger.py     hash-chained JSONL ledger and verification
  audit.py      Markdown audit report from a ledger
  eval.py       batch evaluation over arms and seeds
  api.py        FastAPI service: runs, SSE stream, options, human decision, audit
  static/       dashboard
  cli.py        triad scenario | solve | negotiate | verify | audit | eval | serve
tests/          118 tests: checkers, solver, ledger tamper, judge paths, agents with a fake
                client, eval harness, audit and API end to end
```

See [PLAN.md](PLAN.md) for the original design and rubric mapping.
