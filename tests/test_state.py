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
