"""Hash-chained, append-only JSONL ledger of a negotiation.

Every message, every verdict, every orchestrator decision is an entry. Each entry's hash
covers the previous hash, so editing or dropping any line breaks verification from that
point on. `verify()` recomputes the chain from the file alone.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

GENESIS = "0" * 64

EntryType = str  # see protocol.py for the vocabulary


class Entry(BaseModel):
    seq: int
    ts: float
    prev_hash: str
    hash: str = ""
    scenario_id: str
    round: int
    actor: str          # ATC | PILOT | GROUND | ORCHESTRATOR | USER
    type: EntryType
    payload: dict[str, Any]

    def body(self) -> dict:
        d = self.model_dump()
        d.pop("hash")
        return d


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(body)).encode()).hexdigest()


class Ledger:
    def __init__(self, scenario_id: str, path: Path | None = None,
                 on_append: Callable[[Entry], None] | None = None):
        self.scenario_id = scenario_id
        self.path = path
        self.entries: list[Entry] = []
        self.on_append = on_append      # live observers (the API streams entries to the browser)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    @property
    def head(self) -> str:
        return self.entries[-1].hash if self.entries else GENESIS

    def append(self, type: EntryType, actor: str, round: int, payload: dict[str, Any]) -> Entry:
        e = Entry(seq=len(self.entries), ts=time.time(), prev_hash=self.head,
                  scenario_id=self.scenario_id, round=round, actor=actor, type=type,
                  payload=json.loads(_canonical(payload)))
        e.hash = _hash(e.prev_hash, e.body())
        self.entries.append(e)
        if self.path:
            with self.path.open("a") as f:
                f.write(_canonical(e.model_dump()) + "\n")
        if self.on_append:
            self.on_append(e)
        return e

    def of_type(self, *types: EntryType) -> list[Entry]:
        return [e for e in self.entries if e.type in types]

    def dump(self) -> list[dict]:
        return [e.model_dump() for e in self.entries]


class VerifyResult(BaseModel):
    ok: bool
    entries: int
    first_bad_seq: int | None = None
    reason: str = ""


def verify_entries(entries: list[dict]) -> VerifyResult:
    prev = GENESIS
    for i, raw in enumerate(entries):
        try:
            e = Entry.model_validate(raw)
        except Exception as ex:  # noqa: BLE001
            return VerifyResult(ok=False, entries=len(entries), first_bad_seq=i, reason=f"unparseable: {ex}")
        if e.seq != i:
            return VerifyResult(ok=False, entries=len(entries), first_bad_seq=i, reason="sequence gap")
        if e.prev_hash != prev:
            return VerifyResult(ok=False, entries=len(entries), first_bad_seq=i, reason="prev_hash mismatch")
        if _hash(e.prev_hash, e.body()) != e.hash:
            return VerifyResult(ok=False, entries=len(entries), first_bad_seq=i, reason="hash mismatch")
        prev = e.hash
    return VerifyResult(ok=True, entries=len(entries))


def verify_file(path: Path) -> VerifyResult:
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    try:
        entries = [json.loads(ln) for ln in lines]
    except json.JSONDecodeError as e:
        return VerifyResult(ok=False, entries=len(lines), first_bad_seq=None, reason=f"bad JSON: {e}")
    return verify_entries(entries)
