from pathlib import Path

import pytest

from charmer.config import ConfigError, load

EXAMPLE = Path(__file__).parents[1] / "config.example.yml"


def test_example_config_loads():
    cfg = load(EXAMPLE)
    assert cfg.name == "pangolin-uop"
    assert cfg.host_ip == "192.0.2.10"
    assert len(cfg.newt_agents) == 1
    assert cfg.newt_agents[0].name == "patras-edge"


def test_missing_config_version_is_rejected(tmp_path):
    import re
    path = tmp_path / "config.yml"
    text = re.sub(r"^\s*config_version:\s*1\s*(#.*)?$", "", EXAMPLE.read_text(), flags=re.MULTILINE)
    path.write_text(text)
    with pytest.raises(ConfigError, match="config_version is missing"):
        load(path)


def test_production_rejects_sqlite(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text()
                    .replace("environment: lab", "environment: production")
                    .replace("database: postgres", "database: sqlite"))
    with pytest.raises(ConfigError, match="sqlite is lab-only"):
        load(path)


def test_base_domain_is_required(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text().replace("base_domain: uop.gr", "base_domain: null"))
    with pytest.raises(ConfigError, match="pangolin.base_domain is required"):
        load(path)


def test_production_rejects_tls_none(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text()
                    .replace("environment: lab", "environment: production")
                    .replace("provider: self_signed", "provider: none"))
    with pytest.raises(ConfigError, match="tls.provider 'none' is refused"):
        load(path)


def test_newt_agent_rejects_latest_tag(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text().replace('image_tag: "1.17.0"', 'image_tag: "latest"'))
    with pytest.raises(ConfigError, match="image_tag must be a pinned version"):
        load(path)


def test_disable_password_auth_rejected_with_password_auth(tmp_path):
    path = tmp_path / "config.yml"
    text = EXAMPLE.read_text()
    text = text.replace(
        "  auth: agent                     # agent | key | password (password is prompted, never stored)",
        "  auth: password                  # agent | key | password (password is prompted, never stored)")
    text = text.replace(
        "  disable_password_auth: false    # opt-in: once you're sure key/agent auth works, set true",
        "  disable_password_auth: true     # opt-in: once you're sure key/agent auth works, set true")
    path.write_text(text)
    with pytest.raises(ConfigError, match="ssh.disable_password_auth is true but ssh.auth is 'password'"):
        load(path)


def test_disable_password_auth_accepted_with_key_or_agent_auth(tmp_path):
    path = tmp_path / "config.yml"
    text = EXAMPLE.read_text().replace(
        "  disable_password_auth: false    # opt-in: once you're sure key/agent auth works, set true",
        "  disable_password_auth: true     # opt-in: once you're sure key/agent auth works, set true")
    path.write_text(text)
    cfg = load(path)
    assert cfg.ssh.disable_password_auth is True


def test_smtp_enabled_requires_host_user_no_reply(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text() + "\nsmtp:\n  enabled: true\n")
    with pytest.raises(ConfigError, match="smtp.host is required"):
        load(path)


def test_smtp_disabled_by_default():
    cfg = load(EXAMPLE)
    assert cfg.smtp.enabled is False


def test_key_auth_requires_key_file(tmp_path):
    import re
    path = tmp_path / "config.yml"
    # First `auth: agent` in the file is the top-level ssh: block.
    text = re.sub(r"auth: agent(\s*#[^\n]*)?", "auth: key", EXAMPLE.read_text(), count=1)
    path.write_text(text)
    with pytest.raises(ConfigError, match="key_file is not set"):
        load(path)


def test_dashboard_host_falls_back_to_ip(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text().replace("hostname: pangolin.example.org", "hostname: ''"))
    cfg = load(path)
    assert cfg.dashboard_host == cfg.host_ip
    assert cfg.base_url == f"https://{cfg.host_ip}"


def test_maintenance_defaults_to_no_logo():
    cfg = load(EXAMPLE)
    assert cfg.maintenance.logo is None
    assert cfg.maintenance.message == "We'll be back shortly."


def test_maintenance_logo_must_exist(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text() + "\nmaintenance:\n  logo: ./nope.png\n")
    with pytest.raises(ConfigError, match="maintenance.logo does not exist"):
        load(path)


def test_maintenance_logo_rejects_unsupported_suffix(tmp_path):
    path = tmp_path / "config.yml"
    logo = tmp_path / "logo.gif"
    logo.write_bytes(b"GIF89a")
    path.write_text(EXAMPLE.read_text() + f"\nmaintenance:\n  logo: {logo}\n")
    with pytest.raises(ConfigError, match=r"maintenance.logo must be one of"):
        load(path)


def test_maintenance_accepts_configured_logo_and_message(tmp_path):
    path = tmp_path / "config.yml"
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n")
    path.write_text(EXAMPLE.read_text() + f"\nmaintenance:\n  logo: {logo}\n  message: Back soon\n")
    cfg = load(path)
    assert cfg.maintenance.logo == str(logo)
    assert cfg.maintenance.message == "Back soon"


def test_monitor_ips_defaults_to_empty():
    cfg = load(EXAMPLE)
    assert cfg.monitor_ips == []


def test_monitor_ips_accepts_ips_and_cidrs(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text() + "\nmonitor:\n  ips:\n    - 203.0.113.5\n    - 10.0.0.0/24\n")
    cfg = load(path)
    assert cfg.monitor_ips == ["203.0.113.5", "10.0.0.0/24"]


def test_monitor_ips_rejects_invalid_entry(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE.read_text() + "\nmonitor:\n  ips:\n    - not-an-ip\n")
    with pytest.raises(ConfigError, match=r"monitor.ips\[0\]: invalid IP or CIDR"):
        load(path)
