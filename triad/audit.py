"""Human-readable audit report rendered from a ledger alone: what was proposed, what each
party said, how the judge ruled on it and why, what was finally decided, and what the model
calls behind each message cost. Everything here is derived from ledger entries, so the report
can be regenerated from the JSONL file by anyone holding it.
"""

from __future__ import annotations

import time
from typing import Any

from .ledger import Entry, VerifyResult, verify_entries
from .messages import PARTIES

RULING_TYPES = ("ACCEPT_OK", "SELF_VIOLATION", "REJECT_PREFERENCE", "UNFOUNDED_REJECT",
                "LATE_COUNTER", "INFEASIBLE_COUNTER", "NON_CONCESSION", "REGRESSIVE_COUNTER",
                "COUNTER_OK")


def _t(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _rules(failing: list[dict]) -> str:
    return ", ".join(f"{v['rule_id']} ({v.get('detail', '')})" for v in failing)


def _key(prop: dict | None) -> str:
    return "|".join(str(prop[k]) for k in ("slot_id", "pilot_action", "gate", "crew_plan")) if prop else ""


def _ruling_detail(ruling: Entry | None, outcome: str) -> str:
    if ruling is None:
        return "rejects on a rule the proposal really fails" if outcome == "REJECT_OK" else ""
    pl = ruling.payload
    match ruling.type:
        case "INFEASIBLE_COUNTER":
            return "discarded, fails " + _rules(pl.get("failing_rules", [])) if pl.get("failing_rules") else \
                "discarded: " + str(pl.get("note") or pl.get("reason") or "")
        case "SELF_VIOLATION":
            return "ACCEPT refused: fails own rules " + _rules(pl.get("failing_rules", []))
        case "UNFOUNDED_REJECT":
            return f"cited {pl.get('claimed')} but the proposal passes them"
        case "NON_CONCESSION":
            return str(pl.get("note", ""))
        case "REGRESSIVE_COUNTER":
            return (f"discarded: system cost {pl.get('system_cost', 0):,.0f} vs {pl.get('on_table_system_cost', 0):,.0f} "
                    f"on the table, worst-off utility not raised")
        case "COUNTER_OK":
            return f"system cost {pl.get('system_cost', 0):,.0f}; mutually acceptable {pl.get('mutually_acceptable')}"
        case "ACCEPT_OK" | "REJECT_PREFERENCE":
            return f"own utility {pl.get('my_utility', '')}"
        case "LATE_COUNTER":
            return "counter in the final vote round is ignored"
    return ""


def model_calls(entries: list[Entry]) -> dict[str, Any]:
    """Aggregate the per-call telemetry stored next to each message."""
    calls = [c for e in entries if e.type in ("MESSAGE", "ABSTAIN")
             for k, c in e.payload.get("raw", {}).items() if k.endswith("_call") and isinstance(c, dict)]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    for c in calls:
        for k in usage:
            usage[k] += int((c.get("usage") or {}).get(k, 0) or 0)
    return {"calls": len(calls), "models": sorted({c.get("model", "?") for c in calls}),
            "prompt_versions": sorted({str(c.get("prompt_version")) for c in calls}),
            "output_modes": sorted({str(c.get("output_mode")) for c in calls}),
            "latency_s": round(sum(float(c.get("latency_s", 0) or 0) for c in calls), 2), **usage}


def audit_markdown(entries: list[Entry], outcome: dict[str, Any] | None = None,
                   human: dict[str, Any] | None = None, verification: VerifyResult | None = None) -> str:
    """`verification` lets a caller that read the entries from a file pass the file-level
    result, which also checks the head anchor; otherwise only the chain is recomputed."""
    ver = verification or verify_entries([e.model_dump() for e in entries])
    sc = next((e for e in entries if e.type == "SCENARIO"), None)
    dec = next((e for e in entries if e.type == "DECISION"), None)
    s = sc.payload["scenario"] if sc else {}
    f = s.get("flight", {})
    out = [f"# Negotiation audit — {entries[0].scenario_id if entries else '?'}", ""]
    status = "VERIFIED" if ver.ok else f"BROKEN at seq {ver.first_bad_seq} ({ver.reason})"
    out.append(f"Ledger: {len(entries)} entries, chain {status}, "
               f"head `{entries[-1].hash[:16]}…`" if entries else "Ledger: empty")
    out.append("")
    if sc:
        d = s.get("disruption", {})
        out += ["## Scenario", "",
                (f"- {f.get('airline_name')} {f.get('airline')}{f.get('number')} {f.get('origin')}→{f.get('dest')} on {f.get('date')}, "
                f"tail {f.get('tail')}; trigger {d.get('trigger')} (weather {f.get('weather_delay_min')} min, "
                f"late aircraft {f.get('late_aircraft_delay_min')} min)"),
                f"- {d.get('note', '')}",
                (f"- Decision space: {sc.payload['n_proposals']} proposals, {sc.payload['n_feasible_landing']} legal landing options, "
                f"{sc.payload['n_mutually_acceptable']} clear every party's threshold ({sc.payload['acceptance_threshold']})"),
                f"- Utility rule: `{sc.payload['utility_rule']}`", ""]
    for e in entries:
        if e.type == "NO_FEASIBLE_LANDING":
            out += ["## No legal landing option", "", f"```\n{e.payload}\n```", ""]
        if e.type == "NO_ACCEPTABLE_OPTION" and e.round == 0:
            out += [f"> {e.payload['note']}", ""]

    rounds = sorted({e.round for e in entries if e.type == "ROUND_START"})
    for r in rounds:
        rs = next(e for e in entries if e.type == "ROUND_START" and e.round == r)
        end = next((e for e in entries if e.type == "ROUND_END" and e.round == r), None)
        outcomes = end.payload.get("outcomes", {}) if end else {}
        out += [f"## Round {r}{' (final vote)' if rs.payload.get('final_round') else ''} — on the table: `{rs.payload['on_table']}`", ""]
        out.append("| party | decision | proposal | ruling | detail |")
        out.append("|---|---|---|---|---|")
        for p in PARTIES:
            msg = next((e for e in entries if e.type == "MESSAGE" and e.round == r and e.actor == p), None)
            ruling = next((e for e in entries if e.round == r and e.type in RULING_TYPES and e.payload.get("agent") == p), None)
            abst = next((e for e in entries if e.type == "ABSTAIN" and e.round == r and e.actor == p), None)
            if abst:
                out.append(f"| {p} | ABSTAIN | | ABSTAIN | {abst.payload['reason'][:160]} |")
                continue
            if not msg:
                continue
            m = msg.payload["message"]
            rat = (m.get("rationale") or "").replace("|", "/").replace("\n", " ")
            detail = _ruling_detail(ruling, outcomes.get(p, ""))
            out.append(f"| {p} | {m['decision']} | `{_key(m.get('proposal'))}` | {outcomes.get(p, ruling.type if ruling else '')} "
                       f"| {detail} — _{rat[:200]}_ |")
        if end:
            out += ["", (f"Round result: consensus **{end.payload['consensus']}**; next on table `{end.payload['next_on_table']}` "
                        f"(rule: {end.payload['rule']})"), ""]

    if dec:
        d = dec.payload
        fin = d["final"]
        prop = fin["proposal"]
        out += ["## Decision", "",
                f"- Source: **{d['decision_source']}**" + (f" — {d['fallback_code']}: {d['fallback_reason']}" if d.get("fallback_code") else ""),
                (f"- Final: slot {prop['slot_id']}, {prop['pilot_action']}, gate {prop['gate']}, {prop['crew_plan']}; "
                f"system cost {fin['system_cost']:,.0f} (FIFO {d['fifo_system_cost']:,.0f}, optimum {d['optimum_system_cost']:,.0f})"),
                f"- Utilities: {d['utilities']['final']}",
                "- Requires human approval: yes", ""]
        if d.get("diversion_assessment"):
            out += [f"- Diversion assessment: `{d['diversion_assessment']}`", ""]
    if human:
        out += ["## Human decision", "",
                f"- {human['action']} by {human.get('actor', 'USER')} at {_t(human['ts'])}"
                + (f"; override to `{human['proposal_key']}`" if human.get("proposal_key") else ""),
                f"- Note: {human.get('note') or '—'}", ""]
    mc = model_calls(entries)
    if mc["calls"]:
        out += ["## Model calls", "",
                (f"- {mc['calls']} calls to {', '.join(mc['models'])}; prompt version {', '.join(mc['prompt_versions'])}; "
                f"output mode {', '.join(mc['output_modes'])}"),
                (f"- Tokens: {mc['input_tokens']:,} in ({mc['cache_read_input_tokens']:,} from cache), {mc['output_tokens']:,} out; "
                f"summed latency {mc['latency_s']} s"), ""]
    out += ["## Ledger", "", "| seq | time | round | actor | type | hash |", "|---|---|---|---|---|---|"]
    for e in entries:
        out.append(f"| {e.seq} | {_t(e.ts)} | {e.round} | {e.actor} | {e.type} | `{e.hash[:12]}` |")
    return "\n".join(out) + "\n"
