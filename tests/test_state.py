import pytest

from charmer.state import State


def test_get_or_generate_pins_once(tmp_path):
    state = State(tmp_path / "state.json", "site")
    assert state.get_or_generate("secret", lambda: "fixed") == "fixed"
    assert state.get_or_generate("secret", lambda: "changed") == "fixed"


def test_state_file_is_mode_0600(tmp_path):
    state = State(tmp_path / "state.json", "site")
    state.get_or_generate("secret", lambda: "x")
    assert oct((tmp_path / "state.json").stat().st_mode & 0o777) == "0o600"


def test_phase_status_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = State(path, "site")
    assert state.phase_status("preflight") == "pending"
    state.mark_phase("preflight", "done")

    reloaded = State(path, "site")
    assert reloaded.phase_status("preflight") == "done"


def test_state_refuses_mismatched_site(tmp_path):
    path = tmp_path / "state.json"
    State(path, "site-a").mark_phase("preflight", "done")
    try:
        State(path, "site-b")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "site-a" in str(exc)


def _write(path, data):
    import json
    path.write_text(json.dumps(data))


def test_state_refuses_akropolis_state_file(tmp_path):
    # Same shape and default path as akropolis; only the phase names differ.
    path = tmp_path / "state.json"
    _write(path, {"site": "site", "generated": {},
                  "phases": {"preflight": {"status": "done"}, "etcd": {"status": "done"}}})
    with pytest.raises(RuntimeError, match="etcd"):
        State(path, "site")


def test_state_refuses_other_tool_stamp(tmp_path):
    path = tmp_path / "state.json"
    _write(path, {"tool": "akropolis", "site": "site", "phases": {}, "generated": {}})
    with pytest.raises(RuntimeError, match="akropolis"):
        State(path, "site")


def test_unstamped_charmer_state_is_accepted_and_stamped(tmp_path):
    import json
    path = tmp_path / "state.json"
    _write(path, {"site": "site", "generated": {}, "phases": {"pangolin": {"status": "done"}}})
    state = State(path, "site")
    state.mark_phase("handoff", "done")
    assert json.loads(path.read_text())["tool"] == "charmer"
