"""The batch evaluation harness: seeds, rows, summary."""

from __future__ import annotations

import json

import pytest

from triad import config
from triad.eval import eval_seeds, parse_arm, run_eval, summary_markdown

from .conftest import requires_data


def test_eval_seeds_are_disjoint_from_demo_seeds():
    seeds = eval_seeds(config.EVAL_SEEDS_DEFAULT)
    assert len(seeds) == config.EVAL_SEEDS_DEFAULT
    assert not set(seeds) & set(config.DEMO_SEEDS)
    with pytest.raises(ValueError):
        eval_seeds(3, base=config.DEMO_SEEDS[0])


def test_parse_arm():
    assert not parse_arm("greedy").live
    assert parse_arm("llm").model_id == config.MODEL
    assert parse_arm("llm:claude-haiku-4-5-20251001").model_id == "claude-haiku-4-5-20251001"
    with pytest.raises(ValueError):
        parse_arm("nonsense")


@requires_data
def test_stub_eval_writes_rows_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    rows, summary, out_dir = run_eval(["agreeable", "stubborn"], seeds=[1000, 1001], tag="t")
    assert out_dir == tmp_path / "eval" / "t"
    assert len(rows) == 4
    assert {r["arm"] for r in rows} == {"agreeable", "stubborn"}
    assert len(list((out_dir / "ledgers").glob("*.jsonl"))) == 4
    lines = (out_dir / "rows.jsonl").read_text().splitlines()
    assert len(lines) == 4 and all(json.loads(x)["final_feasible"] for x in lines)

    arms = json.loads((out_dir / "summary.json").read_text())["arms"]
    assert arms["stubborn"]["consensus_rate"] == 0.0
    assert arms["stubborn"]["violations"]["late_counters"] > 0
    assert arms["agreeable"]["final_feasible_rate"] == 1.0
    assert arms["stubborn"]["gap_vs_optimum_mean"] == 0.0   # solver fallback is optimal by definition
    md = summary_markdown(summary)
    assert "| consensus rate |" in md and "stubborn" in md
