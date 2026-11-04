import pytest

from axfpga.quality import Context, QualityError, Stimulus, load_plugin


def write(tmp_path, body, name="p.py"):
    (tmp_path / name).write_text(body)
    return name


GOOD = '''
def stimulus(stream, ctx):
    base = 0 if stream == "tuning" else 100
    return [{"a": base + i} for i in range(3)]

def metric(stream, vectors, exact, approx, ctx):
    return {"name": "M", "value": 3, "unit": "dB", "extra": {"n": len(vectors)}}

def power_stimulus(vectors, ctx):
    return vectors[:1]
'''


def test_load_and_call(tmp_path):
    p = load_plugin(write(tmp_path, GOOD), tmp_path, [])
    ctx = Context(seed=5)
    assert p.has_stimulus() and p.has_metric() and p.has_power_stimulus()
    assert p.stimulus("tuning", ctx).vectors == [{"a": 0}, {"a": 1}, {"a": 2}]
    assert p.stimulus("heldout", ctx).vectors[0] == {"a": 100}
    m = p.metric("tuning", [{"a": 1}], [], [], ctx)
    assert m == {"name": "M", "value": 3.0, "unit": "dB", "extra": {"n": 1}} and isinstance(m["value"], float)
    assert p.power_stimulus([{"a": 1}, {"a": 2}], ctx) == [{"a": 1}]


def test_metric_only_and_extra_default(tmp_path):
    body = 'def metric(s, v, e, a, c):\n    return {"name": "x", "value": 1.5, "unit": ""}\n'
    p = load_plugin(write(tmp_path, body), tmp_path)
    assert p.has_metric() and not p.has_stimulus()
    assert p.metric("tuning", [], [], [], Context())["extra"] == {}
    with pytest.raises(QualityError, match="provides no stimulus"):
        p.stimulus("tuning", Context())


def test_stimulus_note_and_search_dirs(tmp_path):
    sub = tmp_path / "lib"
    sub.mkdir()
    body = 'from axfpga.quality import Stimulus\ndef stimulus(s, c):\n    return Stimulus([{"a": 1}], "hello")\n'
    write(sub, body, "q.py")
    p = load_plugin("q.py", tmp_path / "nowhere", [sub])
    assert p.stimulus("tuning", Context()).note == "hello"


def test_two_plugins_same_stem_isolated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    write(a, 'def stimulus(s, c):\n    return [{"a": 1}]\n')
    write(b, 'def stimulus(s, c):\n    return [{"a": 2}]\n')
    assert load_plugin("p.py", a).stimulus("tuning", Context()).vectors == [{"a": 1}]
    assert load_plugin("p.py", b).stimulus("tuning", Context()).vectors == [{"a": 2}]


def test_sys_path_unchanged(tmp_path):
    import sys
    before = list(sys.path)
    load_plugin(write(tmp_path, GOOD), tmp_path)
    assert sys.path == before


def test_errors(tmp_path):
    with pytest.raises(QualityError, match=r"axc_plugin 'missing.py': file not found \(looked in:"):
        load_plugin("missing.py", tmp_path, [])
    with pytest.raises(QualityError, match=r"axc_plugin 'bad.py': SyntaxError"):
        load_plugin(write(tmp_path, "def (:\n", "bad.py"), tmp_path)
    with pytest.raises(QualityError, match=r"axc_plugin 'boom.py': RuntimeError: x"):
        load_plugin(write(tmp_path, "raise RuntimeError('x')\n", "boom.py"), tmp_path)
    with pytest.raises(QualityError, match="defines none of"):
        load_plugin(write(tmp_path, "x = 1\nstimulus = 3\n", "none.py"), tmp_path)


@pytest.mark.parametrize("ret,msg", [
    ("None", "must return a non-empty list"),
    ("[]", "must return a non-empty list"),
    ("[1]", "vector 0 is not a"),
    ("[{'a': 1.5}]", "vector 0 is not a"),
    ("[{'a': 'x'}]", "vector 0 is not a"),
    ("[{'a': 1}, {1: 2}]", "vector 1 is not a"),
])
def test_bad_stimulus_return(tmp_path, ret, msg):
    p = load_plugin(write(tmp_path, f"def stimulus(s, c):\n    return {ret}\n"), tmp_path)
    with pytest.raises(QualityError, match=msg):
        p.stimulus("tuning", Context())


def test_stimulus_exception_wrapped_and_bad_stream(tmp_path):
    p = load_plugin(write(tmp_path, "def stimulus(s, c):\n    raise KeyError('k')\n"), tmp_path)
    with pytest.raises(QualityError, match=r"axc_plugin 'p.py': stimulus\(\) failed: KeyError"):
        p.stimulus("tuning", Context())
    with pytest.raises(QualityError, match="unknown stream 'val'"):
        p.stimulus("val", Context())


@pytest.mark.parametrize("ret,msg", [
    ("[1]", "must return a dict"),
    ("{'name': 'a', 'value': 1}", "lacks unit"),
    ("{'unit': 'a'}", "lacks name, value"),
    ("{'name': 'a', 'value': 'x', 'unit': ''}", "'value' must be a number"),
    ("{'name': 'a', 'value': None, 'unit': ''}", "'value' must be a number"),
    ("{'name': 'a', 'value': True, 'unit': ''}", "'value' must be a number"),
])
def test_bad_metric_return(tmp_path, ret, msg):
    p = load_plugin(write(tmp_path, f"def metric(s, v, e, a, c):\n    return {ret}\n"), tmp_path)
    with pytest.raises(QualityError, match=msg):
        p.metric("tuning", [], [], [], Context())


def test_metric_exception_and_power_errors(tmp_path):
    body = "def metric(s, v, e, a, c):\n    1/0\ndef power_stimulus(v, c):\n    return []\n"
    p = load_plugin(write(tmp_path, body), tmp_path)
    with pytest.raises(QualityError, match=r"metric\(\) failed: ZeroDivisionError"):
        p.metric("heldout", [], [], [], Context())
    with pytest.raises(QualityError, match=r"power_stimulus\(\) must return"):
        p.power_stimulus([], Context())


def test_context_picklable():
    import pickle
    from axfpga.quality import NumberFormat
    c = Context(design_id="d", fmt=NumberFormat("sm", 16, 9), inputs=[("a", 8)], attrs={"axc_seed": "3"})
    assert pickle.loads(pickle.dumps(c)) == c
