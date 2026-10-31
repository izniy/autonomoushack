# TRIAD — Cross-organisational disruption negotiation (Airbus Challenge #2)

> ATC ⇄ Pilot/AOC ⇄ Ground agents negotiate a *revised arrival slot + pilot action + gate + crew plan*
> for a real 2015 US DOT flight, in ≤ 3 rounds around an orchestrator-owned proposal. Every proposal is
> checked by each party's deterministic rules engine, every step lands in a hash-chained consensus ledger,
> and a non-AI solver is the fallback. The OCC duty manager approves — the system never executes.

**Design principle: agents reason, concede and explain; code decides what is legal, what it costs, and who wins a round.**

> **Status (31 Oct 2026):** every phase below is built; see [README.md](README.md) for the architecture as
> implemented, the CLI, the dashboard and the evaluation numbers. Deviations from the plan are marked
> *as built* inline. Live-model arms require `ANTHROPIC_API_KEY`; without it the agents abstain and the
> solver decides with `fallback_code = AGENT_ERROR`, which is recorded, not hidden.

## 0. Rubric mapping

| Criterion (weight) | How TRIAD scores it |
|---|---|
| Problem realism 20% | User: **airline OCC duty manager at a hub**, counterparties tower/approach supervisor and apron/stand controller. Status quo: phone calls, ACARS, hand-typed target times in an A-CDM portal; each party optimises locally. Cost: holding burns ~2,000+ kg/h narrowbody; all-in delay cost ~US$75–100/aircraft-min (A4A — verify & cite); missed connections and late-aircraft propagation (`LATE_AIRCRAFT_DELAY` on the next leg is *in the data*). TRIAD = the negotiation layer A-CDM doesn't have. |
| Technical soundness 25% | LLMs only interpret, counter, concede, explain. Feasibility, cost, round winner, turn cap and fallback are code. Tested on 50–100 sampled scenarios with stub agents, Haiku and Sonnet. |
| Honest evaluation 20% | Arms: FIFO status quo · solver optimum · LLM negotiation. Report consensus rate, fallback rate by cause, *attempted* infeasible counters (and % caught — must be 100%), optimality gap vs solver, Δ cost vs FIFO, latency, $/scenario. Synthetic fields labelled everywhere. Failure cases shipped on purpose. |
| Trust & traceability 20% | Rule registry with IDs + sources (14 CFR 91.167 fuel reserve; FAA JO 7110.65 wake minima; 14 CFR 117 FDP limits; SOP turnaround). Hash-chained JSONL ledger: every message, every `{rule, ok, measured, limit, margin}`, every orchestrator decision, model id, prompt hash. `decision_source: CONSENSUS | FALLBACK_SOLVER`. Approve / Override button. |
| Impact 10% | Negotiated vs FIFO inside the same simulator (measured); historical outcome of the real tail shown as context (labelled, not a controlled baseline). |
| Time discipline 5% | Minimum submittable at ~3 h; 3-minute script in §9. |

## 1. Scenario from a real row

Pick any row with `WEATHER_DELAY > 15` or `LATE_AIRCRAFT_DELAY > 20` (376,125 qualify). **Real / derived from data:**

* Disrupted flight: airline, number, tail, origin→dest, scheduled/actual times, distance, air time, delay breakdown.
* Arrival bank: flights scheduled into `dest` ±30 min → real slot contention.
* Rotation: next flight of the same `TAIL_NUMBER` departing `dest` that day → real downstream leg, real turnaround/crew pressure, and its recorded `LATE_AIRCRAFT_DELAY` (historical outcome). If no clean next leg, downstream = none (stated).
* Connection proxy: same-airline departures from `dest` 45–180 min after scheduled arrival (label: *flights, not pax*).
* Nominal AAR: p95 arrivals per 15 min at `dest` for that hour-of-day over 2015 → weather cut applied (e.g. 60 %).
* Gate count estimate: peak concurrent aircraft on ground at `dest` by tail (label: *derived estimate*).
* Diversion alternate: nearest suitable airport from `airports.csv` lat/long.

**Synthetic, seeded from the row, labelled in UI + report:** aircraft type (by airline, e.g. WN→737-700), wake/size category, fuel on board & burn rates, gate map with size categories and occupancy windows, pax connection counts, crew duty end time, min turnaround by type.

Stretch: LLM parses a free-text ATIS/NOTAM-style disruption note into the structured `Disruption` object (LLM doing *reading*, per rubric).

## 2. Decision space (small, enumerable)

`Proposal = {slot_id, pilot_action, gate, crew_plan}` *(as built: `turnaround_min` was replaced by `crew_plan`,
since the turnaround minimum is a hard rule, not a choice, while keeping or swapping the crew is)*

| Field | Options | Owner |
|---|---|---|
| `slot_id` | next K five-minute slots ≥ earliest ETA; capacity enforced per rolling 15-min bin (AAR) | ATC |
| `pilot_action` | `SPEED_UP`, `CONTINUE`, `REDUCE_SPEED`, `HOLD`, `DIVERT` — minutes implied by slot−ETA | Pilot |
| `gate` | gate map entries (size category, occupancy windows) or `REMOTE_STAND` | Ground |
| `crew_plan` | `SAME_CREW` or `RESERVE_CREW` (reserve crew, costed) — drives the FDP check | Ground |

≤ 200 combinations → exhaustive loop in ms. `DIVERT` is enumerated and costed but never negotiated: when no
landing option is legal the outcome is `UNRESOLVED` with a diversion assessment for the duty manager.

## 3. Parties, private state, checkers, costs

Each party = (a) a **deterministic checker + cost function in code** that owns that party's private data, and (b) an **LLM agent** that sees only its own private state, the public ledger, and tool results from its own checker/cost function. The orchestrator never receives raw private data — only `{rule_id, ok, measured, limit, margin}` and a scalar cost. That is how A-CDM works (target times are offered, parties answer feasible / not).

| Party | Private data | Hard rules (IDs → source) | Cost (minimise) |
|---|---|---|---|
| ATC | per-bin capacity, wake categories in the bank, holding stack limit | `ATC-SLOT-CAP` (AAR), `ATC-SEP-WAKE` (JO 7110.65), `ATC-HOLD-MAX` | empty slots, knock-on delay to other arrivals |
| Pilot/AOC | fuel on board, burn rates, alternate, turbulence, pax connections | `PLT-FUEL-RES` (14 CFR 91.167: dest + alt + 45 min), `PLT-TURB`, `PLT-DIVERT-FUEL` | fuel above plan, missed pax (weighted), own delay |
| Ground | gate map & occupancy, crew duty end, turnaround minima, next-leg schedule | `GND-GATE-SIZE`, `GND-GATE-FREE` (with buffer), `GND-TURN-MIN`, `GND-CREW-DUTY` (14 CFR 117) | gate conflict/idle min, crew overtime, next-leg delay |

Public rules (orchestrator): schema, slot/gate exist, proposal references, round cap. Total system cost = weighted sum of the three scalar costs; weights are public and shown.

## 4. Protocol — orchestrator-owned proposal, ≤ 3 rounds, enforced in code

```
Round 0 (no model call)
  build Scenario → enumerate proposals → query the 3 checkers → FEASIBLE SET
  current = FIFO seed (next slot by ETA order, HOLD, original/next free gate, min turnaround)
  (FIFO is also the status-quo baseline; it is chosen so that it violates at least one party's preference)
Rounds 1–3 (3 model calls in parallel per round; round 3 is a vote — counters there are LATE_COUNTER, ignored)
  each agent gets: public ledger, current proposal, its own private state, its own checker/cost tools
  each agent returns ONE message: ACCEPT | COUNTER(proposal) | REJECT(rule_ids)
  orchestrator:
    schema invalid → 1 retry → ABSTAIN (logged; never counts as ACCEPT; 2 abstains by one agent → fallback)
    ACCEPT on a proposal that fails the accepter's OWN hard rules → refused, logged SELF_VIOLATION
    REJECT citing rules the proposal passes → logged UNFOUNDED_REJECT (counts as a plain non-accept)
    COUNTER → run all 3 checkers; infeasible → logged INFEASIBLE_COUNTER and discarded
    COUNTER must be a concession: agent's own cost(new) ≥ own cost(previous counter) else logged NON_CONCESSION, discarded
    all three ACCEPT (explicitly) → CONSENSUS
    else current = feasible counter with lowest total cost (tie → earliest slot); next round
After round 3 without consensus, or any API error/timeout → FALLBACK_SOLVER with a fallback_code:
  NO_CONSENSUS_ROUND_CAP | NO_ACCEPTABLE_OPTION | AGENT_ABSTAINED | AGENT_ERROR | NO_FEASIBLE_LANDING
```

Properties: deterministic given agent outputs, cannot loop, every state change has one logged reason, the round winner is chosen by code not persuasion, safety margins can never be negotiated away. Agents are told the cap exists. ≤ 9 model calls per scenario (plus at most one retry each).

*As built:* each party also gets a normalised utility over the feasible set (1 = its cheapest legal option, 0 = its most expensive); an option is "mutually acceptable" when every party's utility clears `ACCEPTANCE_THRESHOLD = 0.35`. The judge prefers mutually acceptable counters when choosing what goes on the table, and the eval reports consensus rate conditional on such an option existing, so vacuous consensus on the FIFO seed is visible.

## 5. Fallback solver & baselines (no AI)

`solver.py` minimises total weighted cost over the feasible set (queries the same three checkers/cost oracles), lexicographic tie-break on safety margin then earliest slot. Output tagged `decision_source: FALLBACK_SOLVER`, flagged for human review. Also the **optimum arm** in the eval → optimality gap of negotiation.

**FIFO baseline** = the Round-0 seed, scored by the same cost functions.

## 6. Message schema (Pydantic, strict; tool-use / JSON schema, temperature 0)

```json
{"scenario_id":"WN1234-2015-06-12","round":2,"agent":"PILOT","decision":"COUNTER",
 "proposal":{"slot_id":"S03","pilot_action":"REDUCE_SPEED","gate":"B12","crew_plan":"SAME_CREW"},
 "constraint_results":[{"rule":"PLT-FUEL-RES","ok":true,"measured":3120,"limit":2700,"margin":420}],
 "my_cost":38.5,"concessions":["accepted later slot than preferred"],
 "rationale":"Reducing speed instead of holding saves ~180 kg and keeps 14 connecting pax.","confidence":0.8}
```

## 7. Ledger

`<scenario>.ledger.jsonl`, append-only. Entry `{seq, ts, prev_hash, hash, scenario_id, round, actor, type, payload}`; `hash = sha256(prev_hash + canonical_json)`. Model id, prompt version, prompt hash, output mode, latency and token usage ride in each `MESSAGE` payload as `attempt_<n>_call`. `triad verify` and `/api/runs/{id}/verify` recompute the chain; `triad audit` and `/audit.md|.json` rebuild the report from the file alone. The human decision is appended to the same chain as `HUMAN_DECISION`.

## 8. Build order (minimum submittable at ~3 h)

Stack: Python 3.13, DuckDB over `flights.parquet`, Pydantic v2, `anthropic` SDK (structured outputs, Sonnet 5 live demo, Haiku 4.5 batch), FastAPI + SSE + single HTML page, pytest.

```
triad/ data.py scenario.py rules.py checkers.py costs.py solver.py messages.py views.py
       judge.py protocol.py agents.py stubs.py ledger.py audit.py eval.py api.py cli.py static/index.html
tests/ checkers · solver · ledger tamper · judge paths with stub agents · agents with a fake client · eval · audit + API
```

| Phase | Deliverable | Status |
|---|---|---|
| P0 | `data.py` + parquet + `scenario.py`; CLI prints a scenario with provenance labels | done |
| P1 | rules/checkers/costs/solver + FIFO; unit tests | done |
| P2 | messages/ledger/judge/protocol with **stub agents** (seven policies, including reckless/flaky/broken) → full run, no model; then live agents with structured output and a forced-tool fallback | done |
| P3 | API + dashboard: scenario card w/ badges, 3 agent columns per round, live rule panel, decision + source, Approve/Override (legal options only), audit download | done |
| P4 | `eval.py` seeded scenarios × arms → `summary.md` (metrics, failure codes, cost); stub arms run, model arms pending a key | done / pending key |
| P5 | README, limits, rehearsal | README done |

Cut: `runway`, NOTAM parsing, heat-map. Kept: `REMOTE_STAND`, checkers, concession rule, fallback, ledger, eval.

## 9. Evaluation & demo

Metrics: consensus rate & mean rounds · fallback rate by cause · infeasible counters attempted vs caught (must be 100 %) · SELF_VIOLATION / NON_CONCESSION counts by agent · optimality gap vs solver · Δ delay-min / fuel / missed-pax vs FIFO · wall-clock & $/scenario · Haiku vs Sonnet.

Failure cases shipped: (a) low fuel + full bank → no legal hold → fallback diverts, flagged; (b) agent counters with incompatible gate → checker rejects, logged with rule; (c) schema failure → abstain → fallback; (d) negotiated outcome worse than solver (gap > 0) — explain.

Limits slide: synthetic fuel/gates/crew/pax, 2015 data, no live A-CDM integration, LLM latency, single-flight scope.

3-min pitch: 0:00 user + $/min + A-CDM gap → 0:30 architecture ("code decides legality and cost; agents only negotiate"), schema, round rule → 1:30 live run on a real row → 2:30 batch metrics, gap, one failure, limits.

Judge Q "why not just run the solver?": the solver needs every party to answer feasibility for every candidate under one fixed weighting; negotiation lets each party keep its data and priority-setting local, the solver stays the safety net, and we measured the cost of that trade-off (the gap).

## 10. Remaining before the pitch
* Put `ANTHROPIC_API_KEY` in `.env`, run `triad negotiate --seed 29` live and `triad eval --arms yielding llm:claude-haiku-4-5-20251001 -n 20`, paste the model-arm row into the README table.
* Rehearse the 3-minute script against `triad serve` with seeds 29, 7, 101.
