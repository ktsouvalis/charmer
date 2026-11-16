import json
import stat

import pytest

from charmer import state as state_mod
from charmer.state import State, StateLocked

PW = "correct horse battery"


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr(state_mod, "SCRYPT_N", 2 ** 10)  # tests, not security


def test_get_or_generate_pins_once(tmp_path):
    state = State(tmp_path / "state.json", "site", PW)
    state.unlock()
    assert state.get_or_generate("secret", lambda: "fixed") == "fixed"
    assert state.get_or_generate("secret", lambda: "changed") == "fixed"


def test_state_file_is_mode_0600(tmp_path):
    state = State(tmp_path / "state.json", "site", PW)
    state.unlock()
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


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "sub" / "site.json"


# --- encryption at rest -------------------------------------------------------

def _pinned(path, site="site", **values):
    st = State(path, site, PW)
    st.unlock()
    for k, v in values.items():
        st.get_or_generate(k, lambda v=v: v)
    return st


def test_secrets_never_written_in_plain_text(state_path):
    _pinned(state_path, pangolin_server_secret="s3cr3t-value-xyz", pangolin_postgres_password="pg-pw-123")
    raw = state_path.read_text()
    assert "s3cr3t-value-xyz" not in raw and "pg-pw-123" not in raw
    on_disk = json.loads(raw)
    assert "generated" not in on_disk
    assert on_disk["format"] == 2
    assert on_disk["generated_keys"] == ["pangolin_postgres_password", "pangolin_server_secret"]
    assert on_disk["generated_enc"]["kdf"] == "scrypt"


def test_round_trip(state_path):
    _pinned(state_path, a="1", b="2")
    st = State(state_path, "site", PW)
    st.unlock()
    assert st.data["generated"] == {"a": "1", "b": "2"}


def test_locked_state_refuses_reads(state_path):
    _pinned(state_path, a="1")
    st = State(state_path, "site")
    assert st.locked
    with pytest.raises(StateLocked):
        st.get_or_generate("a", lambda: "regenerated")
    with pytest.raises(StateLocked):
        st.data["generated"].get("a")


def test_locked_state_keeps_blob_on_phase_save(state_path):
    _pinned(state_path, a="1")
    blob = json.loads(state_path.read_text())["generated_enc"]
    st = State(state_path, "site")  # e.g. `charm shutdown`: no passphrase
    st.mark_phase("shutdown", "done")
    assert json.loads(state_path.read_text())["generated_enc"] == blob
    assert st.generated_keys() == ["a"]
    again = State(state_path, "site", PW)
    again.unlock()
    assert again.data["generated"] == {"a": "1"}


def test_wrong_passphrase(state_path):
    _pinned(state_path, a="1")
    before = state_path.read_text()
    st = State(state_path, "site", "not the passphrase")
    with pytest.raises(StateLocked, match="wrong passphrase"):
        st.unlock()
    assert st.locked
    assert state_path.read_text() == before


def test_blob_bound_to_site(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _pinned(a, "site", x="from-a")
    _pinned(b, "site-b", x="from-b")
    doc_b = json.loads(b.read_text())
    doc_b["generated_enc"] = json.loads(a.read_text())["generated_enc"]
    b.write_text(json.dumps(doc_b))
    with pytest.raises(StateLocked):
        State(b, "site-b", PW).unlock()


def test_plaintext_file_migrates_on_save(state_path):
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"site": "site", "phases": {"preflight": {"status": "done"}},
                                      "generated": {"pangolin_server_secret": "old-plain-token"}}))
    st = State(state_path, "site")
    assert st.plaintext_on_disk and not st.locked
    assert st.data["generated"]["pangolin_server_secret"] == "old-plain-token"  # readable before unlock
    with pytest.raises(StateLocked, match="plain text"):
        st.save()
    st = State(state_path, "site", PW)
    st.unlock()
    st.save()
    raw = state_path.read_text()
    assert "old-plain-token" not in raw
    st = State(state_path, "site", PW)
    assert st.encrypted and not st.plaintext_on_disk
    st.unlock()
    assert st.data["generated"] == {"pangolin_server_secret": "old-plain-token"}
    assert st.phase_status("preflight") == "done"


def test_nothing_pinned_needs_no_passphrase(state_path):
    State(state_path, "site").mark_phase("preflight", "done")
    on_disk = json.loads(state_path.read_text())
    assert "generated_enc" not in on_disk and on_disk["generated_keys"] == []


def test_change_passphrase(state_path):
    _pinned(state_path, a="1")
    calls = iter([PW, "a brand new passphrase"])
    st = State(state_path, "site", lambda _new: next(calls))
    st.change_passphrase()
    with pytest.raises(StateLocked):
        State(state_path, "site", PW).unlock()
    st = State(state_path, "site", "a brand new passphrase")
    st.unlock()
    assert st.data["generated"] == {"a": "1"}


def test_env_var_passphrase(state_path, monkeypatch):
    _pinned(state_path, a="1")
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, PW)
    st = State(state_path, "site")  # default source: env var first
    st.unlock()
    assert st.data["generated"] == {"a": "1"}


def test_passphrase_file(state_path, tmp_path, monkeypatch):
    monkeypatch.delenv(state_mod.PASSPHRASE_ENV, raising=False)
    _pinned(state_path, a="1")
    pw_file = tmp_path / "pw"
    pw_file.write_text(PW + "\n")
    st = State(state_path, "site", state_mod.passphrase_source(pw_file))
    st.unlock()
    assert st.data["generated"] == {"a": "1"}


def test_tmp_file_is_owner_only_from_creation(state_path, monkeypatch):
    seen = {}
    real_replace = type(state_path).replace

    def spy(self, target):
        seen["mode"] = stat.S_IMODE(self.stat().st_mode)
        return real_replace(self, target)

    monkeypatch.setattr(type(state_path), "replace", spy)
    State(state_path, "site").mark_phase("preflight", "done")
    assert seen["mode"] == 0o600
