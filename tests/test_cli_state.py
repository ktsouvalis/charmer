"""Encrypted state from the CLI: status without a passphrase, `charm state`."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from charmer import state as state_mod
from charmer.cli import cmd_state, cmd_status
from charmer.config import load
from charmer.state import State

EXAMPLE = Path(__file__).parents[1] / "config.example.yml"
PW = "correct horse battery"


@pytest.fixture
def site(tmp_path, monkeypatch):
    """The example config copied to tmp_path, so .state/ lands there."""
    monkeypatch.setattr(state_mod, "SCRYPT_N", 2 ** 10)
    monkeypatch.delenv(state_mod.PASSPHRASE_ENV, raising=False)
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text())
    cfg = load(path)
    return path, cfg


@pytest.fixture
def pinned(site):
    path, cfg = site
    st = State(cfg.state_file, cfg.name, PW)
    st.unlock()
    st.get_or_generate("pangolin_server_secret", lambda: "server-secret-xyz")
    st.get_or_generate("tls_cert_expiry", lambda: "2027-01-01")
    st.mark_phase("preflight", "done")
    return path, cfg


def _status(path, unlock=False):
    return cmd_status(SimpleNamespace(config=str(path), unlock=unlock, passphrase_file=None))


def _state(path, action, key=None):
    return cmd_state(SimpleNamespace(config=str(path), action=action, key=key,
                                     passphrase_file=None))


def test_status_without_passphrase(pinned, capsys):
    path, _ = pinned
    assert _status(path) == 0
    out = capsys.readouterr().out
    assert "pangolin server secret" in out
    assert "--unlock" in out
    assert "2027-01-01" not in out and "server-secret-xyz" not in out


def test_status_unlock_shows_metadata(pinned, capsys, monkeypatch):
    path, _ = pinned
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, PW)
    assert _status(path, unlock=True) == 0
    out = capsys.readouterr().out
    assert "cert expiry 2027-01-01" in out
    assert "server-secret-xyz" not in out


def test_status_unlock_without_state_file(site, capsys):
    path, _ = site
    assert _status(path, unlock=True) == 0  # nothing to decrypt, no prompt
    assert "not created yet" in capsys.readouterr().out


def test_status_warns_on_plaintext(site, capsys):
    path, cfg = site
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text(json.dumps({"site": cfg.name, "phases": {},
                                          "generated": {"pangolin_server_secret": "plain"}}))
    assert _status(path) == 0
    assert "charm state encrypt" in capsys.readouterr().out


def test_state_encrypt(site, monkeypatch):
    path, cfg = site
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, PW)
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text(json.dumps({"site": cfg.name, "phases": {},
                                          "generated": {"pangolin_server_secret": "plain-secret"}}))
    assert _state(path, "encrypt") == 0
    assert "plain-secret" not in cfg.state_file.read_text()


def test_state_set_and_get(pinned, monkeypatch, capsys):
    path, cfg = pinned
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, PW)
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": "rotated-secret")
    assert _state(path, "set", "pangolin_server_secret") == 0
    capsys.readouterr()
    assert _state(path, "get", "pangolin_server_secret") == 0
    assert capsys.readouterr().out.strip() == "rotated-secret"
    assert "rotated-secret" not in cfg.state_file.read_text()


def test_state_wrong_passphrase(pinned, monkeypatch):
    path, _ = pinned
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, "not the passphrase")
    with pytest.raises(SystemExit) as exc:
        _state(path, "get", "pangolin_server_secret")
    assert exc.value.code == 2
