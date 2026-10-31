"""Rule registry: every hard constraint has an id, an owner, and a cited source.

Checkers in `checkers.py` return a `Verdict` per rule so that the ledger and the UI can
show, for every proposal, which rule was applied, what was measured, the limit, and the
margin. Sources marked (synthetic SOP) are operator procedures we had to invent; the
regulatory ones are real citations and should be verified against current text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

Party = Literal["ATC", "PILOT", "GROUND", "PUBLIC"]


@dataclass(frozen=True)
class Rule:
    id: str
    owner: Party
    title: str
    source: str


RULES: dict[str, Rule] = {r.id: r for r in [
    # --- ATC
    Rule("ATC-SLOT-CAP", "ATC", "Arrivals in the 15-min bin must not exceed the effective AAR",
         "FAA Order JO 7210.3 ch.10 (arrival acceptance rate / traffic management initiatives)"),
    Rule("ATC-SEP-WAKE", "ATC", "Time-based wake turbulence separation must fit inside the slot",
         "ICAO Doc 4444 PANS-ATM 8.7.4 (time-based wake separation minima); FAA JO 7110.65 5-5-4"),
    Rule("ATC-HOLD-MAX", "ATC", "Airborne holding must not exceed the facility holding limit",
         "Facility directive (synthetic SOP)"),
    # --- Pilot / AOC
    Rule("PLT-FUEL-RES", "PILOT", "Fuel after the action must cover alternate + 45-min final reserve + contingency",
         "14 CFR 91.167(a) IFR fuel requirements; ICAO Annex 6 Part I 4.3.6"),
    Rule("PLT-DIVERT-FUEL", "PILOT", "Fuel to reach the alternate with final reserve intact",
         "14 CFR 91.167(a)(2)-(3)"),
    Rule("PLT-TURB-HOLD", "PILOT", "With moderate turbulence reported in the hold, holding is capped at 10 min",
         "Operator flight operations manual (synthetic SOP)"),
    # --- Ground
    Rule("GND-GATE-SIZE", "GROUND", "Gate size category must accommodate the aircraft",
         "ICAO Annex 14 aerodrome reference code; airport stand allocation rules"),
    Rule("GND-GATE-FREE", "GROUND", "Gate must be free for the whole stay including the buffer",
         "Airport stand allocation procedure (synthetic SOP, 10-min buffer)"),
    Rule("GND-CREW-DUTY", "GROUND", "Next leg must depart before the operating crew's duty limit",
         "14 CFR Part 117 flight duty period limits (Table B)"),
    Rule("GND-TURN-MIN", "GROUND", "Turnaround must respect the type minimum",
         "Operator ground operations manual (synthetic SOP)"),
    # --- Public / structural
    Rule("PUB-SCHEMA", "PUBLIC", "Message must match the schema and reference existing slots and gates",
         "Protocol definition"),
]}


class Verdict(BaseModel):
    rule_id: str
    ok: bool
    measured: float | str | None = None
    limit: float | str | None = None
    margin: float | None = None
    detail: str = ""

    @property
    def rule(self) -> Rule:
        return RULES[self.rule_id]

    def describe(self) -> str:
        r = RULES[self.rule_id]
        flag = "ok " if self.ok else "FAIL"
        m = f" margin {self.margin:+.0f}" if self.margin is not None else ""
        return f"[{flag}] {self.rule_id}: {self.detail}{m}  ({r.source})"


def passed(verdicts: list[Verdict]) -> bool:
    return all(v.ok for v in verdicts)


def failures(verdicts: list[Verdict]) -> list[Verdict]:
    return [v for v in verdicts if not v.ok]
