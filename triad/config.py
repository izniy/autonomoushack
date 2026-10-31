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

MODEL = os.getenv("AGENT_MODEL") or os.getenv("TRIAD_MODEL", "")
EVAL_MODEL = os.getenv("TRIAD_EVAL_MODEL", "claude-haiku-4-5-20251001")
AGENT_TIMEOUT_S = 30.0
AGENT_MAX_TOKENS = 800

# Protocol limits are enforced in code, never by prompting.
MAX_ROUNDS = 3
SCHEMA_RETRIES = 1
MAX_ABSTAINS_PER_AGENT = 2

# Disruption filter from the challenge brief.
WEATHER_DELAY_MIN = 15
LATE_AIRCRAFT_DELAY_MIN = 20
