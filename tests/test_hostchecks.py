import subprocess

import pytest

from charmer.config import SSHTarget
from charmer.hostchecks import (SSH_SOCKET_CONFLICT_FIX, SSHD_DROPIN_PATH, docker_tcp_findings,
                                listener_owners, ssh_socket_conflict, sshd_effective_problems,
                                sshd_hardening_dropin)
from charmer.phases import base_setup
from charmer.phases.base_setup import BASE_PACKAGES, DOCKER_INSTALL
from charmer.sshexec import Result

# `ss -Htlnp` as seen on each kind of host.
SS_UBUNTU_2404_SOCKET = (
    'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1234,fd=3),("systemd",pid=1,fd=112))\n'
    'LISTEN 0 4096 [::]:22 [::]:* users:(("sshd",pid=1234,fd=4),("systemd",pid=1,fd=113))\n'
)
# The community-scripts Debian 13 LXC state that killed sshd on reload.
SS_DEBIAN_CONFLICT = (
    'LISTEN 0 4096 *:22 *:* users:(("systemd",pid=1,fd=45))\n'
    'LISTEN 0 4096 0.0.0.0:2375 0.0.0.0:* users:(("dockerd",pid=301,fd=7))\n'
)
SS_PLAIN_SERVICE = (
    'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=6))\n'
    'LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=900,fd=7))\n'
    'LISTEN 0 4096 127.0.0.1:3001 0.0.0.0:* users:(("docker-proxy",pid=4410,fd=7))\n'
)


def test_listener_owners():
    assert listener_owners(SS_UBUNTU_2404_SOCKET, 22) == {"sshd", "systemd"}
    assert listener_owners(SS_DEBIAN_CONFLICT, 22) == {"systemd"}
    assert listener_owners(SS_PLAIN_SERVICE, 22) == {"sshd"}
    assert listener_owners(SS_PLAIN_SERVICE, 2222) == set()


def test_socket_conflict_detected_on_debian_lxc_state():
    assert ssh_socket_conflict(SS_DEBIAN_CONFLICT, 22, "active\n", "enabled\n")


def test_ubuntu_2404_socket_activation_is_not_a_conflict():
    # ssh.service is `disabled` there; the socket hands it the listener.
    assert not ssh_socket_conflict(SS_UBUNTU_2404_SOCKET, 22, "active", "disabled")


def test_plain_service_is_not_a_conflict():
    assert not ssh_socket_conflict(SS_PLAIN_SERVICE, 22, "inactive", "enabled")


def test_socket_conflict_uses_the_configured_port():
    assert not ssh_socket_conflict(SS_DEBIAN_CONFLICT, 2222, "active", "enabled")


def test_dropin_content():
    dropin = sshd_hardening_dropin("yes")
    lines = [ln for ln in dropin.splitlines() if not ln.startswith("#")]
    assert lines == ["PasswordAuthentication no", "KbdInteractiveAuthentication no",
                     "PermitRootLogin prohibit-password", "PubkeyAuthentication yes"]
    assert "PermitRootLogin no" not in dropin


@pytest.mark.parametrize("current", ["no", "forced-commands-only"])
def test_dropin_never_loosens_stricter_root_login(current):
    assert "PermitRootLogin" not in sshd_hardening_dropin(current)


def test_dropin_is_stable_on_rerun():
    # After the first apply, sshd -T reports our own prohibit-password: the
    # re-rendered drop-in must still carry it, or the main config's `yes`
    # would win again.
    assert sshd_hardening_dropin("prohibit-password") == sshd_hardening_dropin("yes")


def test_dropin_path_sorts_before_cloud_init():
    assert SSHD_DROPIN_PATH.rsplit("/", 1)[1] < "50-cloud-init.conf"


def test_sshd_effective_problems():
    hardened = ("port 22\npasswordauthentication no\nkbdinteractiveauthentication no\n"
                "pubkeyauthentication yes\npermitrootlogin prohibit-password\n")
    assert sshd_effective_problems(hardened) == []
    # A late-sorting drop-in losing to 50-cloud-init.conf + the template's root login.
    lost = hardened.replace("passwordauthentication no", "passwordauthentication yes") \
                   .replace("permitrootlogin prohibit-password", "permitrootlogin yes")
    problems = sshd_effective_problems(lost)
    assert len(problems) == 2
    assert any(p.startswith("passwordauthentication yes") for p in problems)
    assert any(p.startswith("permitrootlogin yes") for p in problems)
    assert sshd_effective_problems(hardened.replace("prohibit-password", "no")) == []


def test_docker_tcp_findings_on_community_scripts_host():
    daemon_json = '{"hosts": ["unix:///var/run/docker.sock", "tcp://0.0.0.0:2375"]}'
    findings = docker_tcp_findings(SS_DEBIAN_CONFLICT, daemon_json, "dockerd ")
    assert findings == ["0.0.0.0:2375 listening (dockerd)", "daemon.json hosts: tcp://0.0.0.0:2375"]


def test_docker_tcp_findings_clean_host():
    assert docker_tcp_findings(SS_PLAIN_SERVICE, '{\n  "dns": ["1.1.1.1", "8.8.8.8"]\n}\n',
                               "/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock ") == []
    assert docker_tcp_findings("", "", "") == []


@pytest.mark.parametrize("cmdline", [
    "/usr/bin/dockerd -H tcp://0.0.0.0:2376 --tlsverify",
    "/usr/bin/dockerd -Htcp://10.0.0.5:4243",
    "/usr/bin/dockerd --host=tcp://0.0.0.0:2375",
    "/usr/bin/dockerd -H fd:// --host tcp://127.0.0.1:2375",
])
def test_docker_tcp_findings_from_dockerd_args(cmdline):
    findings = docker_tcp_findings("", "", cmdline)
    assert len(findings) == 1 and findings[0].startswith("dockerd args: tcp://")


def test_docker_tcp_findings_any_port_and_swarm_exemption():
    ss = ('LISTEN 0 4096 0.0.0.0:4243 0.0.0.0:* users:(("dockerd",pid=5,fd=9))\n'
          'LISTEN 0 4096 *:2377 *:* users:(("dockerd",pid=5,fd=10))\n'
          'LISTEN 0 4096 *:7946 *:* users:(("dockerd",pid=5,fd=11))\n')
    assert docker_tcp_findings(ss, "", "") == ["0.0.0.0:4243 listening (dockerd)"]


def test_docker_tcp_findings_unparseable_daemon_json_still_reports():
    assert docker_tcp_findings("", '{"hosts": ["tcp://0.0.0.0:2375",]', "") == \
        ["daemon.json hosts: tcp://0.0.0.0:2375"]


# ---------------------------------------------------------- Docker CE repo
def _repo_prelude(os_release) -> str:
    """DOCKER_INSTALL up to (not including) its first side effect, reading a
    fake os-release instead of the real one."""
    head = DOCKER_INSTALL.split("install -m 0755", 1)[0]
    return head.replace("/etc/os-release", str(os_release)) + 'echo "$ID $VERSION_CODENAME"\n'


@pytest.mark.parametrize("distro,codename,ok", [
    ("ubuntu", "noble", True), ("debian", "trixie", True), ("fedora", "", False),
])
def test_docker_install_branches_on_os_release(tmp_path, distro, codename, ok):
    os_release = tmp_path / "os-release"
    os_release.write_text(f"ID={distro}\nVERSION_CODENAME={codename}\n")
    r = subprocess.run(["sh", "-c", _repo_prelude(os_release)], capture_output=True, text=True)
    assert (r.returncode == 0) is ok
    if ok:
        assert r.stdout.strip() == f"{distro} {codename}"
    else:
        assert "ubuntu/debian only" in r.stderr


def test_docker_install_has_no_hardcoded_distro():
    assert "linux/ubuntu" not in DOCKER_INSTALL
    assert "https://download.docker.com/linux/$ID/gpg" in DOCKER_INSTALL
    assert "https://download.docker.com/linux/$ID $VERSION_CODENAME stable" in DOCKER_INSTALL


def test_base_packages_avoid_transitional_names():
    pkgs = BASE_PACKAGES.split()
    for gone in ("gnupg2", "lsb-release", "apt-transport-https"):
        assert gone not in pkgs
    assert "gnupg" in pkgs and "ufw" in pkgs


# ------------------------------------------------- base's sshd hardening flow
SSHD_T_TEMPLATE = ("passwordauthentication yes\nkbdinteractiveauthentication no\n"
                   "pubkeyauthentication yes\npermitrootlogin yes\n")
SSHD_T_HARDENED = ("passwordauthentication no\nkbdinteractiveauthentication no\n"
                   "pubkeyauthentication yes\npermitrootlogin prohibit-password\n")


class FakeConn:
    """Answers the handful of commands _harden_sshd runs; `ss_after` is
    what `ss -Htlnp` shows once the reload/restart has happened."""

    def __init__(self, ss_before, socket_active, service_enabled, ss_after, sshd_T_after=SSHD_T_HARDENED):
        self.name = "agent1"
        self.cfg = SSHTarget(disable_password_auth=True)
        self.ss_before, self.ss_after = ss_before, ss_after
        self.socket_active, self.service_enabled = socket_active, service_enabled
        self.sshd_T_after = sshd_T_after
        self.ran: list[str] = []
        self.dropin_written = False
        self.restarted = False

    def run(self, cmd, sudo=None, timeout=30.0):
        self.ran.append(cmd)
        if cmd == "sshd -T":
            return Result(0, self.sshd_T_after if self.dropin_written else SSHD_T_TEMPLATE, "")
        if "base64 -d >" in cmd and SSHD_DROPIN_PATH in cmd:
            self.dropin_written = True
        if cmd == "ss -Htlnp":
            return Result(0, self.ss_after if self.restarted else self.ss_before, "")
        if cmd == "systemctl is-active ssh.socket":
            return Result(0 if self.socket_active == "active" else 3, self.socket_active, "")
        if cmd == "systemctl is-enabled ssh.service":
            return Result(0, self.service_enabled, "")
        if cmd == "systemctl is-active ssh.service":
            return Result(0, "active" if "sshd" in listener_owners(self.ss_after, 22) else "failed", "")
        if cmd in (SSH_SOCKET_CONFLICT_FIX, "systemctl reload ssh || systemctl reload sshd"):
            self.restarted = True
        return Result(0, "", "")


class FakeCtx:
    def __init__(self):
        self.checks = []

    def begin(self, *a, **k):
        pass

    def record(self, host, name, ok, detail="", warn=False):
        self.checks.append((name, ok, detail))


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(base_setup.time, "sleep", lambda s: None)


def test_harden_switches_to_plain_service_on_socket_conflict(no_sleep):
    conn = FakeConn(SS_DEBIAN_CONFLICT, "active", "enabled", SS_PLAIN_SERVICE)
    ctx = FakeCtx()
    base_setup.BasePhase()._harden_sshd(ctx, conn)
    assert SSH_SOCKET_CONFLICT_FIX in conn.ran
    assert "systemctl reload ssh || systemctl reload sshd" not in conn.ran
    assert ctx.checks[-1][1] is True and "switched" in ctx.checks[-1][2]


def test_harden_reloads_on_ubuntu_2404_socket_activation(no_sleep):
    conn = FakeConn(SS_UBUNTU_2404_SOCKET, "active", "disabled", SS_UBUNTU_2404_SOCKET)
    ctx = FakeCtx()
    base_setup.BasePhase()._harden_sshd(ctx, conn)
    assert SSH_SOCKET_CONFLICT_FIX not in conn.ran
    assert "systemctl reload ssh || systemctl reload sshd" in conn.ran
    assert ctx.checks[-1][1] is True


def test_harden_aborts_before_reload_when_effective_values_lose(no_sleep):
    conn = FakeConn(SS_PLAIN_SERVICE, "inactive", "enabled", SS_PLAIN_SERVICE,
                    sshd_T_after=SSHD_T_TEMPLATE)
    ctx = FakeCtx()
    with pytest.raises(RuntimeError, match="drop-in removed"):
        base_setup.BasePhase()._harden_sshd(ctx, conn)
    assert f"rm -f {SSHD_DROPIN_PATH}" in conn.ran
    assert not conn.restarted


def test_harden_fails_loudly_when_sshd_dies_after_reload(no_sleep):
    # Undetected variant: reload "succeeds", then only systemd holds :22.
    conn = FakeConn(SS_PLAIN_SERVICE, "inactive", "enabled", 'LISTEN 0 4096 *:22 *:* users:(("systemd",pid=1,fd=45))\n')
    ctx = FakeCtx()
    with pytest.raises(RuntimeError, match="not listening"):
        base_setup.BasePhase()._harden_sshd(ctx, conn)
    assert "NEW SSH LOGINS WILL FAIL" in ctx.checks[-1][2]


def test_harden_restores_socket_when_switch_fails(no_sleep):
    conn = FakeConn(SS_DEBIAN_CONFLICT, "active", "enabled", "")
    ctx = FakeCtx()
    with pytest.raises(RuntimeError):
        base_setup.BasePhase()._harden_sshd(ctx, conn)
    assert "systemctl enable --now ssh.socket" in conn.ran
