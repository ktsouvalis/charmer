import re
from pathlib import Path

import pytest

from charmer.config import ConfigError, load
from charmer.phases.adopt_newt_phase import agent_name_for
from charmer.phases.newt_ops import (append_newt_agents, connector_kind, gerbil_bounced, image_tag,
                                     parse_connector_credentials, write_agents_to_config)
from charmer.transcript import redact

EXAMPLE = Path(__file__).parents[1] / "config.example.yml"
AGENT = {"name": "old-edge", "ip": "192.0.2.30",
         "ssh": {"user": "root", "auth": "agent", "key_file": None, "port": 22, "become": True},
         "image_tag": "1.17.0", "tun_device": "/dev/net/tun", "docker_socket": False}


def test_connector_kind_and_tag():
    assert connector_kind("docker.io/fosrl/newt:1.17.0") == "newt"
    assert connector_kind("fosrl/newt") == "newt"
    assert connector_kind("fosrl/pangolin-cli:latest") == "cli"
    assert connector_kind("ghcr.io/fosrl/newt@sha256:abc") == "newt"
    assert connector_kind("fosrl/gerbil:1.5.0") is None
    assert connector_kind("someone/newtish:1") is None
    assert image_tag("docker.io/fosrl/newt:1.17.0") == "1.17.0"
    assert image_tag("localhost:5000/fosrl/newt") is None


def test_credentials_from_newt_env():
    creds = parse_connector_credentials({"Env": ["PANGOLIN_ENDPOINT=https://p.example", "NEWT_ID=abc",
                                                 "NEWT_SECRET=s=cr=t", "DOCKER_SOCKET=/var/run/docker.sock"]})
    assert creds == {"newt_id": "abc", "secret": "s=cr=t", "endpoint": "https://p.example",
                     "docker_socket": True}


def test_credentials_from_pangolin_cli_env_and_flags():
    assert parse_connector_credentials({"Env": ["SITE_ID=x", "SITE_SECRET=y"]})["newt_id"] == "x"
    creds = parse_connector_credentials(
        {"Cmd": ["up", "site", "--id", "i", "--secret=s", "--endpoint", "https://e"]})
    assert (creds["newt_id"], creds["secret"], creds["endpoint"]) == ("i", "s", "https://e")
    assert parse_connector_credentials({"Cmd": ["-id", "i", "-secret", "s"]})["secret"] == "s"


def test_no_credentials():
    assert parse_connector_credentials({"Env": ["CONFIG_FILE=/etc/newt.json"], "Cmd": None}) is None
    assert parse_connector_credentials({"Env": ["NEWT_ID=abc"]}) is None


def test_append_to_empty_list_keeps_comments():
    text = "# keep me\nsite:\n  name: x\nnewt_agents: []  # none yet\nrestore:\n  destructive: false\n"
    out = append_newt_agents(text, [AGENT])
    assert "# keep me" in out and "newt_agents: []" not in out
    import yaml
    data = yaml.safe_load(out)
    assert data["newt_agents"] == [AGENT] and data["restore"] == {"destructive": False}


def test_append_to_existing_block_before_next_section_comment():
    text = ("newt_agents:\n  - name: a\n    ip: 192.0.2.20\n\n# Newt credentials ...\n"
            "restore:\n  destructive: false\n")
    out = append_newt_agents(text, [AGENT])
    import yaml
    assert [a["name"] for a in yaml.safe_load(out)["newt_agents"]] == ["a", "old-edge"]
    assert out.index("old-edge") < out.index("# Newt credentials")


def test_append_without_key():
    import yaml
    assert yaml.safe_load(append_newt_agents("site:\n  name: x", [AGENT]))["newt_agents"] == [AGENT]


def test_write_agents_to_example_config(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text())
    cfg = write_agents_to_config(path, [AGENT])
    assert [a.name for a in cfg.newt_agents] == ["patras-edge", "old-edge"]
    assert [a.name for a in load(path).newt_agents] == ["patras-edge", "old-edge"]


def test_write_agents_rolls_back_an_invalid_result(tmp_path):
    path = tmp_path / "config.yml"
    original = EXAMPLE.read_text()
    path.write_text(original)
    with pytest.raises(ConfigError):
        write_agents_to_config(path, [dict(AGENT, name="patras-edge")])  # duplicate name
    assert path.read_text() == original


def test_gerbil_bounced():
    assert not gerbil_bounced("abc running", "abc running")
    assert gerbil_bounced("abc running", "def running")  # recreated
    assert gerbil_bounced("abc exited", "abc running")  # started
    assert gerbil_bounced("", "abc running")  # created
    assert not gerbil_bounced("abc running", "abc exited")  # nothing to redial to


def test_agent_name_for():
    assert agent_name_for("Home Lab (NAS)") == "home-lab-nas"
    assert agent_name_for("!!!") == "newt"


def test_redacts_json_secret():
    out = redact('curl -d \'{"newtId": "abc", "secret": "hunter2"}\'')
    assert "hunter2" not in out and '"newtId": "abc"' in out
    assert "hunter2" not in redact('["NEWT_ID=abc","NEWT_SECRET=hunter2"]')
    assert re.search(r"<redacted>", out)
