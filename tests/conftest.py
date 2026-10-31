import pytest

from triad.config import FLIGHTS_PARQUET

requires_data = pytest.mark.skipif(
    not FLIGHTS_PARQUET.exists(), reason="flights.parquet not built"
)
