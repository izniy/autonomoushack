"""Audit rendering from the ledger alone, and the HTTP API end to end with stub agents:
start, stream, options, human approve/override (legal only), audit, verify."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from triad import api, config
from triad.audit import audit_markdown, model_calls
from triad.ledger import Entry, verify_entries
from triad.protocol import run
from triad.scenario import random_scenario
from triad.stubs import stub_team


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    api.RUNS.clear()
    return TestClient(api.app)


def _wait(client, run_id):
    """Drain the SSE stream; it closes when the run is done."""
    kinds = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as r:
        for line in r.iter_lines():
            if line.startswith("event: "):
                kinds.append(line[7:])
    return kinds


def test_audit_markdown_from_ledger_file(tmp_path):
    s = random_scenario(seed=29)
    o, ledger = run(s, stub_team("yielding"), ledger_path=tmp_path / "l.jsonl")
    entries = [Entry.model_validate(json.loads(ln)) for ln in (tmp_path / "l.jsonl").read_text().splitlines()]
    md = audit_markdown(entries)
    assert "chain VERIFIED" in md and "## Round 1" in md and "## Decision" in md
    assert o.decision_source in md
    for p in ("ATC", "PILOT", "GROUND"):
        assert f"| {p} |" in md
    assert "## Model calls" not in md          # stubs make no model calls
    assert model_calls(entries)["calls"] == 0


def test_audit_reports_broken_chain(tmp_path):
    s = random_scenario(seed=7)
    _, ledger = run(s, stub_team("greedy"))
    raw = ledger.dump()
    raw[3]["payload"]["tampered"] = True
    assert not verify_entries(raw).ok
    md = audit_markdown([Entry.model_validate(r) for r in raw])
    assert "chain BROKEN at seq 3" in md


def test_api_run_stream_and_approve(client):
    r = client.post("/api/runs", json={"seed": 29, "mode": "yielding"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    kinds = _wait(client, run_id)
    assert kinds[0] == "scenario" and kinds[-1] == "done" and kinds.count("ledger") >= 5
    info = client.get(f"/api/runs/{run_id}").json()
    assert info["done"] and info["outcome"]["decision_source"] in ("CONSENSUS", "FALLBACK_SOLVER")
    assert info["outcome"]["ledger_entries"] == kinds.count("ledger")
    assert client.get(f"/api/runs/{run_id}/verify").json()["ok"]

    opts = client.get(f"/api/runs/{run_id}/options").json()
    assert opts and opts == sorted(opts, key=lambda o: o["system_cost"])
    assert all(set(o["utilities"]) == {"ATC", "PILOT", "GROUND"} for o in opts)

    d = client.post(f"/api/runs/{run_id}/decision", json={"action": "approve", "note": "ok"}).json()
    assert d["action"] == "approve" and d["proposal_key"] == info["outcome"]["final"]["proposal"]["slot_id"] + "|" + \
        info["outcome"]["final"]["proposal"]["pilot_action"] + "|" + info["outcome"]["final"]["proposal"]["gate"] + "|" + \
        info["outcome"]["final"]["proposal"]["crew_plan"]
    # second decision refused; the ledger now ends with the human step and still verifies
    assert client.post(f"/api/runs/{run_id}/decision", json={"action": "approve"}).status_code == 409
    audit = client.get(f"/api/runs/{run_id}/audit.json").json()
    assert audit["ledger"][-1]["type"] == "HUMAN_DECISION" and audit["ledger"][-1]["actor"] == "USER"
    assert audit["verify"]["ok"]
    md = client.get(f"/api/runs/{run_id}/audit.md").text
    assert "## Human decision" in md and "approve by USER" in md
    assert client.get("/api/runs").json()[0]["run_id"] == run_id


def test_api_override_must_be_legal(client):
    run_id = client.post("/api/runs", json={"seed": 7, "mode": "stubborn"}).json()["run_id"]
    _wait(client, run_id)
    assert client.post(f"/api/runs/{run_id}/decision",
                       json={"action": "override", "proposal_key": "nope|HOLD|G1|KEEP"}).status_code == 400
    # an infeasible proposal from the decision space is refused with the failing rules
    j = api.RUNS[run_id].judge
    bad = next(e for e in j.evals if not e.feasible and e.proposal.pilot_action != "DIVERT")
    r = client.post(f"/api/runs/{run_id}/decision", json={"action": "override", "proposal_key": bad.proposal.key()})
    assert r.status_code == 400 and r.json()["detail"]["failing_rules"]
    # a legal one is recorded with its cost delta
    opts = client.get(f"/api/runs/{run_id}/options").json()
    r = client.post(f"/api/runs/{run_id}/decision", json={"action": "override", "proposal_key": opts[-1]["key"],
                                                         "note": "prefer the later slot", "actor": "DUTY_MGR"})
    assert r.status_code == 200 and r.json()["delta_vs_negotiated"] >= 0 and r.json()["actor"] == "DUTY_MGR"
    assert client.get(f"/api/runs/{run_id}/verify").json()["ok"]


def test_api_decision_before_done_and_unknown_run(client):
    assert client.get("/api/runs/nope").status_code == 404
    run_id = client.post("/api/runs", json={"seed": 101, "mode": "agreeable"}).json()["run_id"]
    # the run may have finished already (stubs are fast); either 409 or 200 is a valid state here
    assert client.post(f"/api/runs/{run_id}/decision", json={"action": "bogus"}).status_code in (400, 409)
    _wait(client, run_id)
    assert client.get("/api/health").json()["ok"]
    assert client.get("/api/scenario?seed=29").json()["n_feasible"] > 0


def test_api_eval_endpoint(client, tmp_path):
    assert client.get("/api/eval").status_code == 404
    d = tmp_path / "eval" / "t1"
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({"arms": {"greedy": {"n": 1}}}))
    assert client.get("/api/eval").json()["tag"] == "t1"
