"""Host-hazard detection shared by `preflight` (report only) and `base`
(which acts on it before touching sshd): pure functions over canned command
output (`ss -Htlnp`, `systemctl`, `sshd -T`, daemon.json, dockerd's command
line), so every decision here is testable without a live host.

Both hazards were hit for real, on Proxmox community-scripts Debian 13 LXC
templates (see CHANGELOG 0.9.0):

- **ssh.socket owning the ssh port alongside an enabled ssh.service.**
  systemd (pid 1) holds the listener, so the moment sshd re-execs on a
  `systemctl reload ssh` (SIGHUP) it can't bind, logs `fatal: Cannot bind any
  address.` and exits, while the reload job itself can still report success.
  Open sessions survive; every new login fails. Ubuntu 24.04's default
  socket activation is NOT this: there ssh.service is `disabled` and started
  by the socket with the listener handed to it, so sshd shares the socket
  with systemd instead of fighting it for the port.
- **dockerd listening on TCP** (2375 plain, 2376 TLS, or any `tcp://` host):
  unauthenticated on 2375, and root on the host either way; on a privileged
  container that reaches the hypervisor too.
"""

from __future__ import annotations

import json
import re

DOCKER_TCP_PORTS = (2375, 2376)
# dockerd's own Swarm listeners (cluster management, node gossip): not the
# Engine API, so not a finding.
DOCKER_SWARM_PORTS = (2377, 7946)

# ss -p's process column: users:(("sshd",pid=812,fd=3),("systemd",pid=1,fd=52))
_SS_USER_RE = re.compile(r'\("([^"]+)",pid=\d+')
# -H tcp://..., -Htcp://..., --host=tcp://..., --host tcp://...
_DOCKERD_TCP_ARG_RE = re.compile(r"(?:^|\s)(?:-H|--host)(?:=|\s*)(tcp://\S+)")


def tcp_listeners(ss_out: str) -> list[tuple[str, int, set[str]]]:
    """(local address, port, owning process names) per line of `ss -Htlnp`."""
    listeners = []
    for line in ss_out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        addr = fields[3]
        try:
            port = int(addr.rsplit(":", 1)[-1])
        except ValueError:
            continue
        listeners.append((addr, port, set(_SS_USER_RE.findall(line))))
    return listeners


def listener_owners(ss_out: str, port: int) -> set[str]:
    """Every process name holding a TCP listener on `port`, any address."""
    owners: set[str] = set()
    for _addr, p, names in tcp_listeners(ss_out):
        if p == port:
            owners |= names
    return owners


def ssh_socket_conflict(ss_out: str, port: int, socket_active: str, service_enabled: str) -> bool:
    """True when ssh.socket is active and systemd holds `port` while
    ssh.service is also `enabled`: the state in which an sshd reload kills
    the listener. `socket_active`/`service_enabled` are the raw stdout of
    `systemctl is-active ssh.socket` / `systemctl is-enabled ssh.service`.
    Ubuntu 24.04's socket activation (ssh.service `disabled`) is not a
    conflict, and neither is plain ssh.service with the socket inactive."""
    return (socket_active.strip() == "active"
            and service_enabled.strip() == "enabled"
            and "systemd" in listener_owners(ss_out, port))


SSH_SOCKET_CONFLICT_FIX = ("systemctl disable --now ssh.socket && systemctl enable ssh.service && "
                           "systemctl restart ssh.service")


# ------------------------------------------------------------------ sshd
SSHD_DROPIN_PATH = "/etc/ssh/sshd_config.d/01-charmer-hardening.conf"
# Pre-0.9.0 name: sorted after e.g. Ubuntu's 50-cloud-init.conf and lost to it.
SSHD_LEGACY_DROPIN_PATH = "/etc/ssh/sshd_config.d/60-charmer-key-only.conf"

# PermitRootLogin values already at least as strict as prohibit-password;
# writing prohibit-password over them would loosen the operator's setting.
_STRICTER_ROOT_LOGIN = {"no", "forced-commands-only"}


def sshd_hardening_dropin(current_permit_root_login: str) -> str:
    """The key-only drop-in. sshd keeps the FIRST value it reads for an
    option and reads sshd_config.d/*.conf in lexical order, hence the 01-
    prefix. PermitRootLogin goes to prohibit-password (never `no`: the
    operator may SSH in as root with a key) unless the current effective
    value is already stricter, which a 01- file would otherwise override."""
    lines = [
        "# Managed by charmer (ssh.disable_password_auth). The 01- prefix is deliberate:",
        "# sshd keeps the first value it reads, so this must sort before other drop-ins.",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
    ]
    if current_permit_root_login.strip().lower() not in _STRICTER_ROOT_LOGIN:
        lines.append("PermitRootLogin prohibit-password")
    lines.append("PubkeyAuthentication yes")
    return "\n".join(lines) + "\n"


def sshd_effective_problems(sshd_T_out: str) -> list[str]:
    """What's still wrong in `sshd -T`'s effective config for key-only auth;
    empty means hardened."""
    eff: dict[str, str] = {}
    for line in sshd_T_out.splitlines():
        key, _, value = line.strip().partition(" ")
        if key:
            eff.setdefault(key.lower(), value.strip().lower())
    problems = []
    for key, want in (("passwordauthentication", "no"),
                      ("kbdinteractiveauthentication", "no"),
                      ("pubkeyauthentication", "yes")):
        got = eff.get(key)
        if got != want:
            problems.append(f"{key} {got or '(not reported)'}, want {want}")
    if eff.get("permitrootlogin", "yes") == "yes":
        problems.append(f"permitrootlogin {eff.get('permitrootlogin', '(not reported)')}, "
                        "want prohibit-password or stricter")
    return problems


# ---------------------------------------------------------------- docker
DOCKERD_CMDLINE_PROBE = "pid=$(pidof -s dockerd) && tr '\\0' ' ' < /proc/$pid/cmdline || true"
DOCKER_DAEMON_JSON_PROBE = "cat /etc/docker/daemon.json 2>/dev/null || true"


def docker_tcp_findings(ss_out: str, daemon_json: str, dockerd_cmdline: str) -> list[str]:
    """Every sign of a Docker API reachable over TCP: a listener on
    2375/2376 (whoever owns it), dockerd itself listening on any other TCP
    port except its Swarm ones, and any tcp:// host in daemon.json or on
    dockerd's command line. Empty means unix-socket only."""
    findings: list[str] = []
    for addr, port, names in tcp_listeners(ss_out):
        if port in DOCKER_TCP_PORTS or ("dockerd" in names and port not in DOCKER_SWARM_PORTS):
            findings.append(f"{addr} listening ({', '.join(sorted(names)) or 'unknown process'})")

    hosts: list[str] = []
    if daemon_json.strip():
        try:
            doc = json.loads(daemon_json)
            raw = doc.get("hosts", []) if isinstance(doc, dict) else []
            hosts = [h for h in raw if isinstance(h, str)] if isinstance(raw, list) else []
        except ValueError:
            # Unparseable daemon.json (dockerd would refuse it too): still
            # surface anything that looks like a TCP host rather than
            # reporting clean.
            hosts = re.findall(r"tcp://[^\"\s,\]]+", daemon_json)
    findings += [f"daemon.json hosts: {h}" for h in hosts if h.startswith("tcp://")]
    findings += [f"dockerd args: {h}" for h in _DOCKERD_TCP_ARG_RE.findall(dockerd_cmdline)]
    return findings
