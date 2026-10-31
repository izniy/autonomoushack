"""Runtime configuration loaded from environment / .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("TRIAD_DATA_DIR", ROOT / "data"))
FLIGHTS_CSV = DATA_DIR / "flights.csv"
FLIGHTS_PARQUET = DATA_DIR / "flights.parquet"
AIRPORTS_CSV = DATA_DIR / "airports.csv"
AIRLINES_CSV = DATA_DIR / "airlines.csv"
RUNS_DIR = ROOT / "runs"

MODEL = os.getenv("TRIAD_MODEL", "claude-sonnet-5")
EVAL_MODEL = os.getenv("TRIAD_EVAL_MODEL", "claude-haiku-4-5-20251001")

# Model call limits. A call that times out abstains for that round and the judge carries on.
MODEL_TIMEOUT_S = float(os.getenv("TRIAD_MODEL_TIMEOUT_S", "60"))
MODEL_MAX_RETRIES = int(os.getenv("TRIAD_MODEL_MAX_RETRIES", "2"))

# List prices per million tokens (input, output) used to report $/scenario. Override with
# TRIAD_PRICE_<MODEL>=in,out ; unknown models fall back to the Sonnet rate and are flagged.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
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
    return PRICES_PER_MTOK.get(model, PRICES_PER_MTOK["claude-sonnet-5"])


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
