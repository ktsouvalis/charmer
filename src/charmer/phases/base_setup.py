"""base: hostname, baseline packages, Docker CE, UFW.

Runs across the whole fleet (Pangolin host + every Newt agent) since all of
them need Docker; the Pangolin host additionally gets the public 80/443 UFW
rule (Gerbil/Traefik's own listeners, see pangolin_phase.py), and
51820/udp + 21820/udp for Gerbil (host-published wildcard, exactly as the
official compose does it: its WireGuard handshake and holepunch/relay
ports need an explicit UFW allow or every Newt/client P2P connection fails
with HOLEPUNCH_MISSING). Newt agents only ever make an OUTBOUND connection
to Pangolin, so they get no inbound rule beyond ssh, nothing to open for
them.

Idempotent by construction: apt installs are no-ops when satisfied, UFW
rules can be re-added freely. `apt upgrade` is deliberately not run here:
package drift belongs to the operator's patching policy, not the
provisioner.

`ssh.disable_password_auth` (per-host: the top-level `ssh:` block and each
`newt_agents[].ssh:` block) is an opt-in, per-host switch to key-only sshd
once the operator is confident key/agent auth works. config.py refuses it
alongside `auth: password`, so by the time this phase runs the connection
in hand is already proven to work without a password.

`monitor.ips` (site-wide, optional) scopes ssh on every host, Pangolin and
every Newt agent alike, to a fixed allow-list of admin/monitoring source
IPs/CIDRs instead of `ufw allow ssh` (open to anywhere), which is what an
empty/omitted list keeps doing. Resolution mirrors disable_password_auth's
sibling secrets: config value first, else an interactive prompt (Enter to
skip and leave ssh open) whose answer is pinned in state so `--replay`
never re-asks.

`monitor.ips` answers "what should stay allowed long-term", typically a
separate admin/monitoring box, which is a different question from "what IP
is charmer itself connecting from right now." The two silently diverging
(operator provisions from their workstation, but only types a separate
monitoring host's IP into `monitor.ips`) used to be a real lockout: the
already-open connection survives the apply that scopes ssh down (established
connections aren't torn down by a UFW reload), so provisioning finishes
looking clean, but the very next connection attempt, even charmer's own,
on a later `--only`/`--replay` run, times out. `_client_ip()` closes this:
every node's own currently-connected source IP (read off sshd's own
`$SSH_CLIENT` for that connection) is folded into its allow-list in addition
to `monitor.ips`, whether or not the operator thought to include it.

Switching the list later (adding/removing an IP) only adds the new rule(s):
UFW here is additive, not reconciling, matching the rest of this phase's
idempotency model. A stale IP from a previous run has to be cleared by hand
(`ufw status numbered` + `ufw delete <n>` on the affected host). The one
case handled automatically is the open-vs-scoped transition: switching from
no monitor.ips to a populated one first deletes the old wide-open ssh rule,
otherwise it would keep allowing ssh from anywhere alongside the new
per-IP rules.
"""

from __future__ import annotations

import ipaddress

from ..remote import push_file
from ..sshexec import NodeConn
from .base import Phase, PhaseContext

BASE_PACKAGES = ("curl wget gnupg2 ca-certificates lsb-release "
                 "apt-transport-https ufw jq unzip")
HOST_PACKAGES = "chrony openssl"
APT = "DEBIAN_FRONTEND=noninteractive apt-get -y -qq"

# Written only when the host has no daemon.json at all: never overwrites an
# operator's existing Docker config. Fixes a common Docker + systemd-resolved
# interaction: containers on a user-defined bridge network can end up with
# the host's 127.0.0.53 stub resolver copied verbatim into their own
# /etc/resolv.conf instead of a real, reachable-from-inside-the-netns
# nameserver, since 127.0.0.53 only listens in the host's own network
# namespace. Every inter-container DNS lookup then fails with "connection
# refused" (e.g. gerbil/traefik resolving "pangolin"), nothing to do with
# Pangolin/Traefik config, but it surfaces there since that's where the
# resulting dead end is first visible.
DOCKER_DAEMON_JSON = '{\n  "dns": ["1.1.1.1", "8.8.8.8"]\n}\n'

DOCKER_INSTALL = r"""
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
> /etc/apt/sources.list.d/docker.list
apt-get -qq update
DEBIAN_FRONTEND=noninteractive apt-get -y -qq install docker-ce docker-ce-cli containerd.io \
docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
"""


def _valid_ip_or_cidr(v: str) -> bool:
    try:
        ipaddress.ip_network(v, strict=False)
        return True
    except ValueError:
        return False


def _client_ip(conn: NodeConn) -> str | None:
    """The source IP sshd sees for the connection `conn` already has open,
    read off $SSH_CLIENT, which sshd sets per-connection, so every exec over
    the same transport reports it consistently. Used to always fold the
    caller's own address into the ssh allow-list (see `_monitor_ips`):
    `monitor.ips` is operator-typed and answers a different question ("what
    should stay allowed long-term"), not "what is charmer's own connection
    right now": the two silently diverging is exactly how an operator ends
    up locked out of a host mid-pipeline."""
    r = conn.run("echo $SSH_CLIENT")
    if not r.ok or not r.out:
        return None
    return r.out.split()[0] or None


class BasePhase(Phase):
    name = "base"

    # ---------------------------------------------------------- monitor ips
    def _monitor_ips_pinned(self, ctx: PhaseContext) -> list[str]:
        """Resolved value without prompting, for plan()/verify(), which
        must never block on input. Returns whatever apply() would return
        without a fresh prompt: the config value, or last run's pinned
        answer (including a pinned "skip")."""
        if ctx.cfg.monitor_ips:
            return ctx.cfg.monitor_ips
        pinned = ctx.state.data["generated"].get("monitor_ips", "")
        return [ip for ip in pinned.split(",") if ip]

    def _monitor_ips(self, ctx: PhaseContext) -> list[str]:
        if ctx.cfg.monitor_ips:
            return ctx.cfg.monitor_ips

        def ask() -> str:
            v = input("Admin/monitoring IP(s) or CIDR(s) to scope ssh to on every host "
                      "(comma-separated, e.g. 203.0.113.5,10.0.0.0/24; Enter to leave ssh "
                      "open to all): ").strip()
            while v:
                ips = [p.strip() for p in v.split(",") if p.strip()]
                bad = [ip for ip in ips if not _valid_ip_or_cidr(ip)]
                if bad:
                    v = input(f"  invalid: {', '.join(bad)}, try again "
                             "(Enter to leave ssh open): ").strip()
                    continue
                return ",".join(ips)
            return ""

        pinned = ctx.state.get_or_generate("monitor_ips", ask)
        return [ip for ip in pinned.split(",") if ip]

    def plan(self, ctx: PhaseContext) -> list[str]:
        names = ", ".join(c.name for c in ctx.fleet)
        mon_ips = self._monitor_ips_pinned(ctx)
        already_asked = bool(ctx.cfg.monitor_ips) or "monitor_ips" in ctx.state.data["generated"]
        if mon_ips:
            ssh_desc = (f"allow ssh only from {', '.join(mon_ips)}, plus whatever IP each host's own "
                        "connection is coming from right now (auto-detected, so charmer never locks "
                        "itself out)")
        elif already_asked:
            ssh_desc = "allow ssh"
        else:
            ssh_desc = ("allow ssh (you will be asked whether to scope it to specific "
                       "IPs; Enter to skip, the answer is pinned in state)")
        lines = [
            f"apt update + install baseline packages on {names}",
            "Pangolin host also gets chrony",
            "install Docker CE from download.docker.com on every host (no-op if present)",
            "write /etc/docker/daemon.json with real DNS resolvers if none exists yet, restarting docker: "
            "avoids a Docker + systemd-resolved interaction where containers get the host's unreachable "
            "127.0.0.53 stub instead of a working nameserver, breaking inter-container DNS (e.g. gerbil/"
            "traefik resolving \"pangolin\")",
            f"UFW on the Pangolin host: deny incoming / allow outgoing / {ssh_desc}, 80/tcp, 443/tcp, "
            "51820/udp, 21820/udp (Gerbil WireGuard + holepunch/relay, host-published wildcard), force enable",
            f"UFW on each Newt agent: deny incoming / allow outgoing / {ssh_desc} "
            "(Newt only ever connects OUT to Pangolin, nothing else to open)",
        ]
        hardened = [c.name for c in ctx.fleet if c.cfg.disable_password_auth]
        if hardened:
            lines.append(f"disable SSH password authentication (key-only from here on) on: "
                         f"{', '.join(hardened)}; sshd config is validated (`sshd -t`) before reload")
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        mon_ips = self._monitor_ips(ctx)  # may prompt, before any node is touched

        for conn in ctx.fleet:
            is_host = conn is ctx.host
            node = conn.name

            node_mon_ips = list(mon_ips)
            if mon_ips:
                # Never let scoping lock out the connection charmer itself is
                # using right now, even if the operator's monitor.ips answer
                # (e.g. a separate monitoring host) doesn't include it.
                own_ip = _client_ip(conn)
                if own_ip and own_ip not in node_mon_ips:
                    node_mon_ips.append(own_ip)
                    ctx.record(node, "ssh allow-list", True,
                               f"auto-added {own_ip} (charmer's own connection) alongside monitor.ips")
            if node_mon_ips:
                ssh_rule = " && ".join(
                    f"ufw allow from {ip} to any port 22 proto tcp comment 'charmer monitor'"
                    for ip in node_mon_ips)
            else:
                ssh_rule = "ufw allow ssh"

            ctx.begin(node, "apt update")
            r = conn.run(f"{APT} update", timeout=300)
            ctx.record(node, "apt update", r.ok, r.err.splitlines()[-1] if (not r.ok and r.err) else "")

            packages = BASE_PACKAGES + (f" {HOST_PACKAGES}" if is_host else "")
            ctx.begin(node, "installing baseline packages")
            r = conn.run(f"{APT} install {packages}", timeout=600)
            ctx.record(node, "baseline packages", r.ok, r.err.splitlines()[-1] if (not r.ok and r.err) else "")
            if is_host:
                conn.run("systemctl enable --now chrony")

            if conn.run("command -v docker && docker compose version").ok:
                ctx.record(node, "docker", True, "already installed")
            else:
                ctx.begin(node, "installing Docker CE", "keyring + repo + packages")
                r = conn.run(DOCKER_INSTALL, timeout=900)
                ctx.record(node, "docker install", r.ok, r.err.splitlines()[-1] if (not r.ok and r.err) else "")

            if conn.run("test -f /etc/docker/daemon.json").ok:
                ctx.record(node, "docker daemon DNS", True, "existing /etc/docker/daemon.json left untouched")
            else:
                push_file(conn, DOCKER_DAEMON_JSON, "/etc/docker/daemon.json", mode="0644")
                r = conn.run("systemctl restart docker", timeout=30)
                ctx.record(node, "docker daemon DNS", r.ok,
                           "wrote /etc/docker/daemon.json (real resolvers) and restarted docker"
                           if r.ok else r.err)
                if not r.ok:
                    raise RuntimeError(f"{node}: docker restart after daemon.json write failed")

            if mon_ips:
                # Best-effort: clear a leftover wide-open ssh rule from a
                # prior run before adding the per-IP ones: UFW allows a
                # connection if ANY rule permits it, so an old "Anywhere"
                # rule left in place would silently defeat the new scoping.
                conn.run("ufw delete allow ssh; ufw delete allow OpenSSH; true")
            if is_host:
                script = ("ufw default deny incoming && ufw default allow outgoing && "
                          f"{ssh_rule} && ufw allow 80/tcp && ufw allow 443/tcp && "
                          "ufw allow 51820/udp && ufw allow 21820/udp && ufw --force enable")
            else:
                script = ("ufw default deny incoming && ufw default allow outgoing && "
                          f"{ssh_rule} && ufw --force enable")
            r = conn.run(script, timeout=60)
            ctx.record(node, "ufw rules + enable"
                       + (f" (ssh restricted to {', '.join(node_mon_ips)})" if node_mon_ips else ""),
                       r.ok, r.err if not r.ok else "")

            # Opt-in (ssh.disable_password_auth / newt_agents[].ssh.disable_password_auth),
            # refused at config-load time whenever this host's own auth is "password"
            # (config.py's _validate_ssh), so by the time apply() reaches here, this
            # connection is already proven to work over key/agent auth (preflight gates
            # the whole pipeline on that), and a drop-in + `sshd -t` before reload means
            # a bad config is caught before anything is reloaded, not after.
            if conn.cfg.disable_password_auth:
                ctx.begin(node, "disabling SSH password authentication", "key-only from here on")
                script = (
                    "install -d -m 0755 /etc/ssh/sshd_config.d && "
                    "printf 'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\n' "
                    "> /etc/ssh/sshd_config.d/60-charmer-key-only.conf && "
                    "sshd -t && (systemctl reload ssh || systemctl reload sshd)"
                )
                r = conn.run(script, timeout=30)
                ctx.record(node, "ssh password auth disabled", r.ok, r.err if not r.ok else "")
                if not r.ok:
                    raise RuntimeError(f"{node}: sshd config validation/reload failed: "
                                       "password auth left as-is")

    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        mon_ips = self._monitor_ips_pinned(ctx)
        for conn in ctx.fleet:
            node = conn.name
            checks = [
                ("docker compose available", "docker compose version >/dev/null"),
                ("ufw active", "ufw status | grep -q 'Status: active'"),
            ]
            for ip in mon_ips:
                checks.append((f"ssh allow-list includes {ip}", f"ufw status | grep -qF '{ip}'"))
            if conn.cfg.disable_password_auth:
                checks.append(("ssh password auth disabled", "sshd -T | grep -qi '^passwordauthentication no'"))
            for label, cmd in checks:
                r = conn.run(cmd)
                ctx.record(node, f"verify: {label}", r.ok, r.err if not r.ok else "")
                ok = ok and r.ok

            # Real probe, not just checking daemon.json is present: proves a
            # freshly-created container actually gets working DNS end to
            # end, the same way preflight's TUN check proves capability
            # instead of just checking the device file exists.
            r = conn.run("docker run --rm --pull=missing busybox:1.36 nslookup docker.com", timeout=60)
            ctx.record(node, "verify: container DNS resolution", r.ok, r.err if not r.ok else "")
            ok = ok and r.ok
        return ok
