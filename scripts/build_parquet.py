"""One-off: convert the DOT flights CSV to parquet for fast DuckDB queries."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

from triad.config import FLIGHTS_CSV, FLIGHTS_PARQUET


def main() -> None:
    if not FLIGHTS_CSV.exists():
        sys.exit(f"missing {FLIGHTS_CSV}")
    t0 = time.time()
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
            SELECT * FROM read_csv_auto('{FLIGHTS_CSV}', all_varchar=false)
        ) TO '{FLIGHTS_PARQUET}' (FORMAT PARQUET)
        """
    )
    n = con.execute(f"SELECT count(*) FROM '{FLIGHTS_PARQUET}'").fetchone()[0]
    print(f"wrote {FLIGHTS_PARQUET} ({n:,} rows) in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
