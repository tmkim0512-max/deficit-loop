import json
import os
import subprocess
import sys

import pytest

from deficit_loop import spec as sp
from deficit_loop.judge import Guards, judge
from deficit_loop.oracle import Measurement
from deficit_loop.spec import Deficit, GoalSpec, SpecError, parse_measurement
from deficit_loop.state import Attempt, BusyError, State, Store
from deficit_loop.supplier import ExternalCliSupplier, ScriptSupplier, SupplierUnavailable, SupplyResult, build_supplier

# ----------------------------------------------------------------- spec


def test_parse_measurement_dedups_by_kind_target():
    deficits, scope = parse_measurement(json.dumps({"deficits": [
        {"kind": "a", "target": "x", "detail": "1"}, {"kind": "a", "target": "x", "detail": "2"}, {"kind": "b", "target": "x"}],
        "scope": 3}))
    assert [d.key for d in deficits] == [("a", "x"), ("b", "x")]
    assert deficits[0].detail == "1"
    assert scope == 3


def test_parse_measurement_accepts_bare_list():
    deficits, scope = parse_measurement('[{"kind": "a", "target": "x"}]')
    assert [d.key for d in deficits] == [("a", "x")] and scope is None
    assert parse_measurement("[]") == ([], None)


@pytest.mark.parametrize("text", ["not json", '{"deficits": 1}', '{"deficits": [{"kind": "a"}]}', '{"deficits": [], "scope": "x"}'])
def test_parse_measurement_rejects_bad_shapes(text):
    with pytest.raises(SpecError):
        parse_measurement(text)


def test_spec_validation():
    base = {"name": "g", "oracle": ["true"], "supplier": {"kind": "script", "command": ["true"]}}
    s = GoalSpec.from_dict(base)
    assert s.integration_branch == "deficit-loop/g" and s.budget.stall_k == 3
    for bad in [{**base, "name": "a/b"}, {**base, "supplier": {"kind": "magic"}}, {**base, "supplier": {"kind": "script"}}, {"name": "g"}]:
        with pytest.raises(SpecError):
            GoalSpec.from_dict(bad)
    s2 = GoalSpec.from_dict({**base, "budget": {"stall_k": 7, "unknown": 1}})
    assert s2.budget.stall_k == 7


def test_external_supplier_disabled_without_env(monkeypatch):
    monkeypatch.delenv(sp.EXTERNAL_CMD_ENV, raising=False)
    s = GoalSpec.from_dict({"name": "g", "oracle": ["true"], "supplier": {"kind": "external"}})
    with pytest.raises(SupplierUnavailable):
        build_supplier(s)
    monkeypatch.setenv(sp.EXTERNAL_CMD_ENV, "echo hi")
    assert isinstance(build_supplier(s), ExternalCliSupplier)
    assert isinstance(build_supplier(_spec()), ScriptSupplier)

# ------------------------------------------------------------- supplier


def _spec():
    return GoalSpec.from_dict({"name": "g", "oracle": ["true"], "supplier": {"kind": "script", "command": ["true"]}})


def test_script_supplier_passes_deficit_and_classifies(tmp_path):
    d = Deficit("k", "t", "dd")
    ok = ScriptSupplier([sys.executable, "-c", "import os,sys,json; assert json.load(sys.stdin)['target']=='t'; print(os.environ['DL_KIND'])"]).supply(_spec(), d, tmp_path, 10)
    assert ok.ok and not ok.external_error and ok.report.strip() == "k"
    temp = ScriptSupplier([sys.executable, "-c", "import sys; sys.exit(75)"]).supply(_spec(), d, tmp_path, 10)
    assert temp.external_error and not temp.ok
    rate = ScriptSupplier([sys.executable, "-c", "import sys; print('HTTP 429 Too Many Requests', file=sys.stderr); sys.exit(1)"]).supply(_spec(), d, tmp_path, 10)
    assert rate.external_error
    defect = ScriptSupplier([sys.executable, "-c", "import sys; print('AssertionError: nope', file=sys.stderr); sys.exit(1)"]).supply(_spec(), d, tmp_path, 10)
    assert not defect.external_error and not defect.ok
    missing = ScriptSupplier(["/nonexistent/agent"]).supply(_spec(), d, tmp_path, 10)
    assert not missing.ok and not missing.external_error
    slow = ScriptSupplier([sys.executable, "-c", "import time; time.sleep(5)"]).supply(_spec(), d, tmp_path, 0.2)
    assert slow.external_error


def test_external_cli_supplier_runs_shell_string(tmp_path):
    r = ExternalCliSupplier('echo "fix $DL_TARGET"').supply(_spec(), Deficit("k", "t"), tmp_path, 10)
    assert r.ok and r.report.strip() == "fix t"

# ---------------------------------------------------------------- judge


def M(keys, scope=None):
    return Measurement([Deficit(k, t) for k, t in keys], scope)


def test_judge_ignores_self_report_and_checks_each_axis():
    g, lie = Guards(), SupplyResult(True, False, "I fixed everything", "", 0)
    before = M([("a", "x"), ("a", "y")], 5)
    assert judge(g, ("a", "x"), before, M([("a", "y")], 5), 5, ["f.py"], (), lie).accept
    v = judge(g, ("a", "x"), before, before, 5, ["f.py"], (), lie)
    assert not v.accept and "still present" in v.reason
    v = judge(g, ("a", "x"), before, M([("a", "y"), ("b", "z")], 5), 5, ["f.py"], (), lie)
    assert not v.accept and "new deficits" in v.reason and "b:z" in v.reason
    v = judge(g, ("a", "x"), before, M([("a", "y")], 4), 5, ["f.py"], (), lie)
    assert not v.accept and "scope shrank" in v.reason
    v = judge(g, ("a", "x"), before, M([("a", "y")], 5), 5, ["tools/oracle.py"], ("tools/",), lie)
    assert not v.accept and "protected" in v.reason
    # scope guard is inert when the oracle does not report scope
    assert judge(g, ("a", "x"), before, M([("a", "y")], None), 5, [], (), lie).accept
    # and a supplier that says it failed is still accepted if the measurement says the deficit is gone
    assert judge(g, ("a", "x"), before, M([("a", "y")], 5), 5, [], (), SupplyResult(False, False, "", "", 1)).accept

# ---------------------------------------------------------------- state


def test_state_roundtrip_and_events(tmp_path):
    store = Store(tmp_path, "g")
    s = State(goal="g", attempts=[Attempt(1, ["a", "x"], "ACCEPT", "done", "r", "b", "s1", "s2")], rejects={"a\tx": 1})
    store.save(s)
    assert not store.state_path.with_suffix(".json.tmp").exists()
    s2 = store.load()
    assert s2 == s and isinstance(s2.attempts[0], Attempt)
    store.event("one", n=1)
    store.event("two", n=2)
    with open(store.events_path, "a") as f:
        f.write('{"event": "torn')          # simulate a crash mid-write
    store.event("three")                    # the next record must not glue itself onto the torn one
    ev = store.events()
    assert [e["event"] for e in ev] == ["one", "two", "corrupt_line", "three"]


def test_torn_utf8_event_does_not_block_logging_or_reading(tmp_path):
    store = Store(tmp_path, "g")
    store.event("one", note="한글")
    with open(store.events_path, "ab") as f:
        f.write(b'{"event": "torn", "note": "\xed\x95')     # cut inside a multibyte character
    store.event("two")
    assert [e["event"] for e in store.events()] == ["one", "corrupt_line", "two"]


def test_supplier_timeout_kills_the_whole_process_tree(tmp_path):
    marker = tmp_path / "marker"
    code = f"import subprocess, time; subprocess.Popen(['sh', '-c', 'sleep 1.5; touch {marker}']); time.sleep(30)"
    r = ScriptSupplier([sys.executable, "-c", code]).supply(_spec(), Deficit("k", "t"), tmp_path, 0.3)
    assert r.external_error and "killed process group" in r.stderr
    __import__("time").sleep(2.5)
    assert not marker.exists(), "grandchild survived the supplier timeout"


def test_lock_is_exclusive(tmp_path):
    a, b = Store(tmp_path, "g"), Store(tmp_path, "g")
    a.acquire()
    with pytest.raises(BusyError):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


def test_lock_blocks_other_process(tmp_path):
    store = Store(tmp_path, "g")
    store.acquire()
    code = ("import sys; sys.path.insert(0, %r); from pathlib import Path; from deficit_loop.state import Store, BusyError\n"
            "try:\n    Store(Path(%r), 'g').acquire(); print('acquired')\nexcept BusyError:\n    print('busy')") % (
        str(__import__("pathlib").Path(__file__).resolve().parents[1]), str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    store.release()
    assert out.strip() == "busy"
