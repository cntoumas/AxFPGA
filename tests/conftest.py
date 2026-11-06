import os

import pytest

# The content-hash cache would let a test pass on a stored result instead of running the tool: off by default in
# tests; tests/test_cache.py turns it on with its own directory.
os.environ.setdefault("AXFPGA_NO_CACHE", "1")
# Pipeline tests written for the Tournament flow keep it; tests of the design-space exploration set AXFPGA_SEARCH=meta.
os.environ.setdefault("AXFPGA_SEARCH", "tournament")


@pytest.fixture
def cache_on(tmp_path, monkeypatch):
    from axfpga import cache

    monkeypatch.delenv("AXFPGA_NO_CACHE", raising=False)
    monkeypatch.setenv("AXFPGA_CACHE", str(tmp_path / "cache"))
    cache.reset_stats()
    return cache
