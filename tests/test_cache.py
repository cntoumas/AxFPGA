"""Content-hash cache: keys ignore machine-specific paths, hits restore files, load-dependent results are not stored."""
from pathlib import Path

from axfpga import cache


def test_disabled_by_default_in_tests():
    assert not cache.enabled()


def test_roundtrip_and_files(cache_on, tmp_path):
    key = cache.make_key("t", top="a")
    assert cache.load("t", key) is None
    out = tmp_path / "o.v"
    out.write_text("module a; endmodule\n")
    cache.save("t", key, {"x": 1}, {"o.v": out})
    value, fdir = cache.load("t", key)
    assert value == {"x": 1}
    dest = tmp_path / "sub" / "copy.v"
    assert cache.restore(fdir, "o.v", dest) and dest.read_text() == out.read_text()
    assert cache.stats()["t"] == {"hits": 1, "misses": 1}


def test_key_depends_on_content_and_options(cache_on, tmp_path):
    f = tmp_path / "a.v"
    f.write_text("module a; endmodule\n")
    k1 = cache.make_key("s", [f], top="a")
    assert k1 == cache.make_key("s", [f], top="a")
    assert k1 != cache.make_key("s", [f], top="b")
    assert k1 != cache.make_key("other", [f], top="a")
    f.write_text("module a(input x); endmodule\n")
    assert k1 != cache.make_key("s", [f], top="a")


def test_key_ignores_work_directory_paths(cache_on, tmp_path):
    def netlist(d: Path) -> Path:
        d.mkdir()
        p = d / "n.v"
        p.write_text(f'(* src = "{d}/annotated/x.v:41.12-41.13" *)\nmodule m; endmodule\n')
        return p

    def meta(d: Path) -> Path:
        p = d / "meta.json"
        p.write_text('{"cells": ["$add$%s/annotated/x.v:59$17"], "file": "%s/cone.v"}' % (d, d))
        return p

    a, b = tmp_path / "run_a", tmp_path / "longer_run_b"
    assert cache.make_key("s", [netlist(a), meta(a)]) == cache.make_key("s", [netlist(b), meta(b)])
    assert cache.make_key("s", iface={"p": a / "x.v", "w": 3}) == cache.make_key("s", iface={"p": b / "x.v", "w": 3})


def test_disabled_does_not_touch_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("AXFPGA_NO_CACHE", "1")
    monkeypatch.setenv("AXFPGA_CACHE", str(tmp_path / "cache"))
    cache.save("t", "k" * 64, {"x": 1})
    assert cache.load("t", "k" * 64) is None
    assert not (tmp_path / "cache").exists()


class _V:
    def __init__(self, status, tmp):
        self.status, self.calls, self.f = status, 0, tmp

    @cache.cached_verify("unit")
    def verify(self):
        self.calls += 1
        return {"status": self.status, "n": self.calls}


def test_cached_verify_skips_load_dependent_status(cache_on, tmp_path):
    for status, cached in (("PROVEN", True), ("FALSIFIED", True), ("TIMEOUT", False), ("ERROR", False),
                           ("COMPILATION_ERROR", False)):
        a, b = _V(status, tmp_path), _V(status, tmp_path)
        a.verify()
        b.verify()
        assert (b.calls == 0) is cached, status


def test_concurrent_save_is_atomic(cache_on):
    key = cache.make_key("c")
    cache.save("c", key, {"v": 1})
    cache.save("c", key, {"v": 2})          # second writer: first entry stays
    assert cache.load("c", key)[0] == {"v": 1}
