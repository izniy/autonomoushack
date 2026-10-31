"""HTTP API and dashboard server.

POST /api/runs starts a negotiation. The protocol runs in a worker thread while every
hash-chained ledger entry is streamed to the browser over SSE the moment it is written. The
duty manager then approves or overrides the decision; that is appended to the same ledger
(actor USER), so the audit trail covers the human step too. An override must itself pass
every party's hard rules; the API refuses one that does not and says which rules fail.

    uvicorn triad.api:app --reload
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .audit import audit_markdown, model_calls
from .judge import Judge, Outcome
from .ledger import Entry, VerifyResult, verify_entries, verify_file
from .messages import PARTIES
from .protocol import drive
from .scenario import describe, random_scenario
from .solver import summarize
from .stubs import stub_team

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="triad", version="0.2")


# ---------------------------------------------------------------- run registry

@dataclass
class Run:
    id: str
    seed: int | None
    mode: str                      # "llm" | stub policy
    model: str | None
    scenario_id: str
    judge: Judge
    outcome: Outcome | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    error: str | None = None
    human: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Serialises the human step: the check for an existing decision and the write must be one
    # step, or simultaneous clicks each pass the check and each append a HUMAN_DECISION.
    decision_lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, kind: str, data: Any) -> None:
        """Thread-safe: called from the worker thread (ledger entries) and the event loop."""
        with self.lock:
            self.events.append({"kind": kind, "data": data})

    def outcome_dict(self) -> dict[str, Any] | None:
        if self.outcome is None:
            return None
        o = self.outcome
        d = o.model_dump()
        d["gap_vs_optimum"], d["saving_vs_fifo"] = o.gap_vs_optimum, o.saving_vs_fifo
        d["ledger_head"], d["ledger_entries"] = self.judge.ledger.head, len(self.judge.ledger.entries)
        d["model_calls"] = model_calls(self.judge.ledger.entries)
        return d


RUNS: dict[str, Run] = {}
MAX_RUNS = 200


class StartRequest(BaseModel):
    seed: int | None = None
    dest: str | None = None
    any_bank: bool = False
    mode: str = "llm"              # "llm" or a stub policy name
    model: str | None = None


class DecisionRequest(BaseModel):
    action: str                    # "approve" | "override"
    proposal_key: str | None = None
    note: str = ""
    actor: str = "USER"


def _team_for(req: StartRequest):
    if req.mode == "llm":
        from .agents import llm_team
        model = req.model or config.MODEL
        return llm_team(model), model
    return stub_team(req.mode), None


def _negotiate(run: Run, team) -> None:
    """Worker thread body. The SSE generator polls `run.events`, so nothing here touches the
    event loop and the run survives the request that started it."""
    try:
        run.outcome = drive(run.judge, team)
        run.push("done", run.outcome_dict())
    except Exception as e:  # noqa: BLE001  surfaced to the UI, never swallowed
        run.error = f"{type(e).__name__}: {e}"
        run.push("error", run.error)
    finally:
        run.done = True


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    page = STATIC / "index.html"
    if not page.exists():
        return PlainTextResponse("dashboard not built; API is live at /docs")
    return FileResponse(page)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "model": config.MODEL, "max_rounds": config.MAX_ROUNDS,
            "acceptance_threshold": config.ACCEPTANCE_THRESHOLD, "runs": len(RUNS)}


@app.get("/api/scenario")
def get_scenario(seed: int | None = None, dest: str | None = None, any_bank: bool = False) -> dict[str, Any]:
    s = random_scenario(seed=seed, dest=dest, busy=not any_bank)
    sm = summarize(s)
    return {"scenario": s.model_dump(), "text": describe(s),
            "n_total": sm.n_total, "n_feasible": sm.n_feasible, "infeasible_by_rule": sm.infeasible_by_rule,
            "fifo": sm.fifo.model_dump() if sm.fifo else None,
            "optimum": sm.optimum.model_dump() if sm.optimum else None}


@app.post("/api/runs")
def start_run(req: StartRequest) -> dict[str, Any]:
    if len(RUNS) >= MAX_RUNS:
        oldest = next(iter(RUNS))
        del RUNS[oldest]
    try:
        s = random_scenario(seed=req.seed, dest=req.dest, busy=not req.any_bank)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    try:
        team, model = _team_for(req)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    run_id = uuid.uuid4().hex[:10]
    path = config.RUNS_DIR / "api" / f"{run_id}-{s.id}.ledger.jsonl"
    early: list[Entry] = []        # written while the judge is being built, before `run` exists
    live: list[Run] = []

    def on_entry(e: Entry) -> None:
        (live[0].push("ledger", e.model_dump()) if live else early.append(e))

    judge = Judge(s, ledger_path=path, on_entry=on_entry)
    run = Run(id=run_id, seed=s.seed, mode=req.mode, model=model, scenario_id=s.id, judge=judge)
    RUNS[run_id] = run
    run.push("scenario", {"text": describe(s), "scenario": s.model_dump(), "mode": req.mode, "model": model,
                          "n_feasible_landing": len(judge.feasible), "n_mutually_acceptable": len(judge.acceptable)})
    for e in early:
        run.push("ledger", e.model_dump())
    live.append(run)
    threading.Thread(target=_negotiate, args=(run, team), name=f"negotiate-{run_id}", daemon=True).start()
    return {"run_id": run_id, "scenario_id": s.id, "seed": s.seed, "mode": req.mode, "model": model}


def _run(run_id: str) -> Run:
    try:
        return RUNS[run_id]
    except KeyError:
        raise HTTPException(404, "unknown run") from None


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    return [{"run_id": r.id, "scenario_id": r.scenario_id, "seed": r.seed, "mode": r.mode, "model": r.model,
             "done": r.done, "error": r.error, "human": bool(r.human),
             "decision_source": r.outcome.decision_source if r.outcome else None}
            for r in reversed(list(RUNS.values()))]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = _run(run_id)
    return {"run_id": run.id, "scenario_id": run.scenario_id, "seed": run.seed, "mode": run.mode,
            "model": run.model, "done": run.done, "error": run.error, "human": run.human,
            "outcome": run.outcome_dict(), "n_events": len(run.events)}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, after: int = 0) -> StreamingResponse:
    run = _run(run_id)

    async def gen():
        i, idle = after, 0.0
        while True:
            while i < len(run.events):
                ev = run.events[i]
                yield f"id: {i}\nevent: {ev['kind']}\ndata: {json.dumps(ev['data'], default=str)}\n\n"
                i, idle = i + 1, 0.0
            if run.done and i >= len(run.events):
                return
            await asyncio.sleep(0.1)
            idle += 0.1
            if idle >= 15:
                idle = 0.0
                yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def option_rows(j: Judge) -> list[dict[str, Any]]:
    """Every legal landing option, cheapest first: what a human may override to."""
    return [{"key": e.proposal.key(), "proposal": e.proposal.model_dump(), "system_cost": e.system_cost,
             "costs": {p: e.costs[p].total for p in PARTIES}, "utilities": j.utilities(e),
             "mutually_acceptable": j.is_acceptable(e),
             "delay_vs_eta_min": e.derived.delay_vs_eta_min, "missed_pax": e.derived.missed_pax,
             "fuel_used_kg": round(e.derived.fuel_used_kg, 1), "next_leg_delay_min": e.derived.next_leg_delay_min}
            for e in sorted(j.feasible, key=lambda e: e.system_cost)]


@app.get("/api/runs/{run_id}/options")
def options(run_id: str) -> list[dict[str, Any]]:
    return option_rows(_run(run_id).judge)


def record_human_decision(j: Judge, o: Outcome, req: DecisionRequest) -> dict[str, Any]:
    """Validate and append a HUMAN_DECISION entry. Pure function of judge state, so the CLI
    and tests can use it without the HTTP layer."""
    # `actor` is whatever the client sent: the API has no authentication, and the ledger says so.
    payload: dict[str, Any] = {"action": req.action, "note": req.note, "actor": req.actor,
                               "actor_authenticated": False, "ts": time.time(),
                               "decision_source": o.decision_source, "negotiated": o.final.proposal.key()}
    if req.action == "approve":
        if o.decision_source == "UNRESOLVED":
            raise HTTPException(400, "nothing to approve: no legal landing option; record an override or divert")
        payload["proposal_key"] = o.final.proposal.key()
    elif req.action == "override":
        if req.proposal_key == "DIVERT":
            payload["proposal_key"] = "DIVERT"
            payload["diversion_assessment"] = j.diversion_assessment()
        else:
            e = j.by_key.get(req.proposal_key or "")
            if e is None or e.proposal.pilot_action == "DIVERT":
                raise HTTPException(400, "unknown proposal key")
            if not e.feasible:
                raise HTTPException(400, {"error": "override fails hard rules; refused",
                                          "failing_rules": [v.model_dump() for v in e.failures()]})
            payload["proposal_key"] = e.proposal.key()
            payload["system_cost"] = e.system_cost
            payload["utilities"] = j.utilities(e)
            payload["delta_vs_negotiated"] = round(e.system_cost - o.final.system_cost, 1)
    else:
        raise HTTPException(400, "action must be approve or override")
    entry = j.ledger.append("HUMAN_DECISION", req.actor, o.rounds_used, payload)
    return {**payload, "hash": entry.hash, "seq": entry.seq}


@app.post("/api/runs/{run_id}/decision")
def human_decision(run_id: str, req: DecisionRequest) -> dict[str, Any]:
    run = _run(run_id)
    if run.outcome is None:
        raise HTTPException(409, run.error or "negotiation has not finished")
    with run.decision_lock:
        if run.human:
            raise HTTPException(409, "a human decision was already recorded")
        run.human = record_human_decision(run.judge, run.outcome, req)
    run.push("human", run.human)
    return run.human


def _verify_run(run: Run) -> VerifyResult:
    """Verify what was persisted (the file and its head anchor) when there is a file, so the
    dashboard checks the same trail `triad verify` would; otherwise the in-memory chain."""
    path = run.judge.ledger.path
    if path is not None and path.exists():
        return verify_file(path)
    return verify_entries(run.judge.ledger.dump())


@app.get("/api/runs/{run_id}/audit.json")
def audit_json(run_id: str) -> dict[str, Any]:
    run = _run(run_id)
    return {"run_id": run.id, "scenario_id": run.scenario_id, "mode": run.mode, "model": run.model,
            "outcome": run.outcome_dict(), "human": run.human, "ledger": run.judge.ledger.dump(),
            "ledger_head": run.judge.ledger.head, "verify": _verify_run(run).model_dump()}


@app.get("/api/runs/{run_id}/audit.md")
def audit_md(run_id: str) -> PlainTextResponse:
    run = _run(run_id)
    md = audit_markdown(run.judge.ledger.entries, run.outcome_dict(), run.human, verification=_verify_run(run))
    return PlainTextResponse(md, media_type="text/markdown")


@app.get("/api/runs/{run_id}/verify")
def verify(run_id: str) -> dict[str, Any]:
    return _verify_run(_run(run_id)).model_dump()


@app.get("/api/eval")
def latest_eval(tag: str | None = None) -> dict[str, Any]:
    root = config.RUNS_DIR / "eval"
    if not root.exists():
        raise HTTPException(404, "no evaluation runs yet; run `triad eval`")
    dirs = [root / tag] if tag else sorted((d for d in root.iterdir() if (d / "summary.json").exists()),
                                           key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs:
        f = d / "summary.json"
        if f.exists():
            return {"tag": d.name, **json.loads(f.read_text())}
    raise HTTPException(404, "no evaluation summary found")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
