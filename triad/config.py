"""Runtime configuration loaded from environment / .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def resolve_data_dir(raw: str | None) -> Path:
    """A relative TRIAD_DATA_DIR is read against the repo root, not the shell's working
    directory, so `triad` works from any folder."""
    d = Path(raw or "data").expanduser()
    return d if d.is_absolute() else ROOT / d


DATA_DIR = resolve_data_dir(os.getenv("TRIAD_DATA_DIR"))
FLIGHTS_CSV = DATA_DIR / "flights.csv"
FLIGHTS_PARQUET = DATA_DIR / "flights.parquet"
AIRPORTS_CSV = DATA_DIR / "airports.csv"
AIRLINES_CSV = DATA_DIR / "airlines.csv"
RUNS_DIR = ROOT / "runs"

MODEL = os.getenv("TRIAD_MODEL") or os.getenv("AGENT_MODEL") or "claude-sonnet-5-5"
EVAL_MODEL = os.getenv("TRIAD_EVAL_MODEL") or "claude-haiku-5-5"

# Model call limits. A call that times out abstains for that round and the judge carries on.
MODEL_TIMEOUT_S = float(os.getenv("TRIAD_MODEL_TIMEOUT_S", "30"))
MODEL_MAX_RETRIES = int(os.getenv("TRIAD_MODEL_MAX_RETRIES", "0"))  # SDK-level; a failure abstains

# List prices per million tokens (input, output) used to report $/scenario. Override with
# TRIAD_PRICE_<MODEL>=in,out ; unknown models fall back to the Sonnet 5.5 rate.
# Base list prices from platform.claude.com/docs/en/models/overview (checked 2026-10-31).
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),     # listed as "from"; the lowest tier
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
CACHE_READ_DISCOUNT = 0.1      # cache reads bill at 10 % of the input rate
CACHE_WRITE_PREMIUM = 1.25     # cache writes bill at 125 % of the input rate


def price_for(model: str) -> tuple[float, float]:
    env = os.getenv("TRIAD_PRICE_" + model.upper().replace("-", "_").replace(".", "_"))
    if env:
        a, b = env.split(",")
        return float(a), float(b)
    return PRICES_PER_MTOK.get(model, PRICES_PER_MTOK["claude-sonnet-5-5"])


def cost_usd(model: str, usage: dict[str, int]) -> float:
    pin, pout = price_for(model)
    return (usage.get("input_tokens", 0) * pin
            + usage.get("cache_read_input_tokens", 0) * pin * CACHE_READ_DISCOUNT
            + usage.get("cache_creation_input_tokens", 0) * pin * CACHE_WRITE_PREMIUM
            + usage.get("output_tokens", 0) * pout) / 1e6


# Protocol limits are enforced in code, never by prompting.
MAX_ROUNDS = 3
SCHEMA_RETRIES = 1
MAX_ABSTAINS_PER_AGENT = 2

# A proposal is "acceptable" for a party when its normalised utility over the feasible set
# (1 = my cheapest legal option, 0 = my dearest) is at least this. Shown to agents, used by
# the judge to pick round winners and to report consensus availability. Never a hard rule.
ACCEPTANCE_THRESHOLD = float(os.getenv("TRIAD_ACCEPTANCE_THRESHOLD", "0.35"))

# Disruption filter from the challenge brief.
WEATHER_DELAY_MIN = 15
LATE_AIRCRAFT_DELAY_MIN = 20

# Seeds: the demo scenarios are never part of the evaluation range.
DEMO_SEEDS = [29, 7, 101]
EVAL_SEED_BASE = 1000
EVAL_SEEDS_DEFAULT = 30
