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

For the same reason, `base.unattended_upgrades: false` (optional, default
true i.e. left alone) masks Ubuntu's own `unattended-upgrades.service` +
`apt-daily-upgrade.timer` on every host: a package changing under a running
stack on the OS's own schedule is the same package-drift-outside-the-
provisioner's-control risk, just silent and on the OS's own timetable
instead of the operator's. Masking (not just disabling) also survives a
package's postinst re-enabling the unit on upgrade.

`ssh.disable_password_auth` (per-host: the top-level `ssh:` block and each
`newt_agents[].ssh:` block) is an opt-in, per-host switch to key-only sshd
once the operator is confident key/agent auth works. config.py refuses it
alongside `auth: password`, so by the time this phase runs the connection
in hand is already proven to work without a password. The drop-in sorts
early (01-) because sshd keeps the first value it reads, sets PermitRootLogin
prohibit-password (never `no`, and never loosening an already-stricter
value), and is checked against `sshd -T`'s effective values before anything
is reloaded. Before reloading, this phase also checks for ssh.socket owning
the ssh port alongside an enabled ssh.service (see hostchecks.py): a reload
there kills sshd's listener, so the host is switched to plain ssh.service
instead. Either way, apply and verify both confirm sshd itself (not only
systemd) is listening afterwards and ssh.service is active.

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

**OS hostname, Pangolin host only, optional `pangolin.host.hostname`:**
same resolution shape as `monitor_ips`: config value first, else an
interactive prompt (Enter to leave the host's hostname untouched), whose
answer (including a blank "skip") is pinned in state so `--replay base`
never re-asks or drifts from what the file says. Nothing charmer renders
(Pangolin's `config.yml`, the Traefik/Gerbil/Postgres Compose stack, or
charmer's own SSH targeting, which is always by `pangolin.host.ip`) is
keyed off the OS-level hostname, so setting/changing it is safe against an
already-running stack; see README "base" for the detail on why and the one
cosmetic gotcha (`/etc/hosts`) this phase also fixes so `sudo` never warns
about it.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
import time

from ..hostchecks import (SSH_SOCKET_CONFLICT_FIX, SSHD_DROPIN_PATH, SSHD_LEGACY_DROPIN_PATH,
                          listener_owners, sshd_effective_problems, sshd_hardening_dropin,
                          ssh_socket_conflict)
from ..remote import push_file
from ..sshexec import NodeConn
from .base import Phase, PhaseContext

_HOSTNAME_LABEL_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")

# Named for what Ubuntu 24.04 and Debian 13 both ship as real packages
# (gnupg, not the gnupg2 transitional name); the Docker repo setup reads
# /etc/os-release itself, so no lsb-release/apt-transport-https.
BASE_PACKAGES = "curl wget gnupg ca-certificates ufw jq unzip"
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

# download.docker.com has a separate repo per distro (linux/ubuntu,
# linux/debian), picked from /etc/os-release's ID: Newt agents are
# arbitrary SSH hosts, and Debian ones (e.g. Proxmox LXC templates) are
# common. Anything else fails loudly instead of pointing apt at the wrong
# repo.
DOCKER_INSTALL = r"""
set -e
. /etc/os-release
case "$ID" in
  ubuntu|debian) ;;
  *) echo "no Docker CE repo setup for distro '$ID' (ubuntu/debian only): install Docker yourself, then re-run base" >&2; exit 1 ;;
esac
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
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


def _valid_hostname(v: str) -> bool:
    return bool(v) and len(v) <= 253 and all(_HOSTNAME_LABEL_RE.match(label) for label in v.split("."))


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


def _sshd_listening(conn: NodeConn) -> tuple[bool, str]:
    """sshd itself (not only systemd) holds a listener on the port charmer
    connects on, and ssh.service is active. `sshd -t` passing proves
    neither: a reload can validate cleanly and still leave sshd dead."""
    port = conn.cfg.port
    owners = listener_owners(conn.run("ss -Htlnp").out, port)
    active = conn.run("systemctl is-active ssh.service").out.strip()
    ok = "sshd" in owners and active == "active"
    return ok, f":{port} held by {', '.join(sorted(owners)) or 'nothing'}; ssh.service {active or 'unknown'}"


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

    # ------------------------------------------------------------- hostname
    def _hostname_pinned(self, ctx: PhaseContext) -> str:
        """Resolved value without prompting, for plan(), which must never
        block on input. Same shape as _monitor_ips_pinned()."""
        if ctx.cfg.host_hostname:
            return ctx.cfg.host_hostname
        return ctx.state.data["generated"].get("host_hostname", "")

    def _hostname(self, ctx: PhaseContext) -> str:
        if ctx.cfg.host_hostname:
            return ctx.cfg.host_hostname

        def ask() -> str:
            v = input(f"Set the OS hostname for the Pangolin host ({ctx.host.name}, "
                      "e.g. pangolin-prod01; Enter to leave it unchanged): ").strip()
            while v:
                if _valid_hostname(v):
                    return v
                v = input("  invalid hostname (letters/digits/hyphens per label, no leading/"
                          "trailing hyphen), try again (Enter to leave unchanged): ").strip()
            return ""

        return ctx.state.get_or_generate("host_hostname", ask)

    def plan(self, ctx: PhaseContext) -> list[str]:
        names = ", ".join(c.name for c in ctx.fleet)
        disable_uu = not ctx.cfg.unattended_upgrades
        mon_ips = self._monitor_ips_pinned(ctx)
        already_asked = bool(ctx.cfg.monitor_ips) or "monitor_ips" in ctx.state.data["generated"]
        hostname = self._hostname_pinned(ctx)
        hostname_asked = bool(ctx.cfg.host_hostname) or "host_hostname" in ctx.state.data["generated"]
        if mon_ips:
            ssh_desc = (f"allow ssh only from {', '.join(mon_ips)}, plus whatever IP each host's own "
                        "connection is coming from right now (auto-detected, so charmer never locks "
                        "itself out)")
        elif already_asked:
            ssh_desc = "allow ssh"
        else:
            ssh_desc = ("allow ssh (you will be asked whether to scope it to specific "
                       "IPs; Enter to skip, the answer is pinned in state)")
        if hostname:
            hostname_line = f"set the Pangolin host's OS hostname to {hostname!r} (hostnamectl + /etc/hosts), if not already set"
        elif hostname_asked:
            hostname_line = "Pangolin host's OS hostname left unchanged (previously skipped)"
        else:
            hostname_line = ("you will be asked whether to set the Pangolin host's OS hostname; "
                             "Enter to skip, the answer is pinned in state")
        lines = [
            f"apt update + install baseline packages on {names}",
            "Pangolin host also gets chrony",
            hostname_line,
            "mask unattended-upgrades + apt-daily-upgrade.timer on every host (OS auto-updates "
            "can replace packages on their own schedule; set base.unattended_upgrades: true to "
            "leave them alone)" if disable_uu else
            "leave OS unattended-upgrades as configured (base.unattended_upgrades: true)",
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
                         f"{', '.join(hardened)}: write {SSHD_DROPIN_PATH} (PasswordAuthentication no, "
                         "KbdInteractiveAuthentication no, PubkeyAuthentication yes, PermitRootLogin "
                         "prohibit-password unless already stricter), validated with `sshd -t` and "
                         "checked against `sshd -T`'s effective values before reload")
            lines.append("  on any of those hosts where ssh.socket owns the ssh port alongside an enabled "
                         "ssh.service (a reload there kills sshd's listener): switch to plain ssh.service "
                         f"instead ({SSH_SOCKET_CONFLICT_FIX}), falling back to restarting ssh.socket if "
                         "sshd doesn't come up; then confirm sshd itself is listening")
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        disable_uu = not ctx.cfg.unattended_upgrades
        mon_ips = self._monitor_ips(ctx)  # may prompt, before any node is touched
        hostname = self._hostname(ctx)  # may prompt, before any node is touched

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

            # Masking (not just disabling) stops `systemctl start` --
            # including a package upgrade's postinst re-enabling the timer --
            # from ever bringing it back without an explicit unmask. Both the
            # service and its timer trigger are covered; -daily.timer (list
            # refresh only, no upgrade) is left alone.
            if disable_uu:
                r = conn.run("systemctl disable --now unattended-upgrades.service "
                             "apt-daily-upgrade.timer 2>/dev/null; "
                             "systemctl mask unattended-upgrades.service "
                             "apt-daily-upgrade.timer")
                ctx.record(node, "unattended-upgrades masked", r.ok, r.err if not r.ok else "")

            if is_host and hostname:
                current = conn.run("hostname").out.strip()
                if current == hostname:
                    ctx.record(node, "OS hostname", True, f"already {hostname}")
                else:
                    ctx.begin(node, "setting OS hostname", f"{current} -> {hostname}")
                    r = conn.run(f"hostnamectl set-hostname {shlex.quote(hostname)}", sudo=True)
                    if r.ok:
                        # hostnamectl only rewrites /etc/hostname; a stale 127.0.1.1
                        # line in /etc/hosts left pointing at the old name is what
                        # makes sudo print "unable to resolve host <old-name>" on
                        # every future invocation (cosmetic, not a functional
                        # break, but cheap to avoid outright).
                        conn.run(
                            f"sed -i 's/^127\\.0\\.1\\.1.*/127.0.1.1\\t{hostname}/' /etc/hosts",
                            sudo=True)
                    ctx.record(node, "OS hostname set", r.ok, f"{current} -> {hostname}" if r.ok else r.err)

            if conn.run("command -v docker && docker compose version").ok:
                ctx.record(node, "docker", True, "already installed")
            elif conn.run("command -v docker").ok:
                # Docker from somewhere else (e.g. the distro's docker.io):
                # installing docker-ce over it makes apt fail on conflicting
                # packages, so leave it to the operator.
                ctx.record(node, "docker", False,
                           "docker is installed but `docker compose` isn't; not installing Docker CE over "
                           "an existing Docker: add a compose v2 plugin yourself, then re-run base")
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
            # the whole pipeline on that).
            if conn.cfg.disable_password_auth:
                self._harden_sshd(ctx, conn)

    def _harden_sshd(self, ctx: PhaseContext, conn: NodeConn) -> None:
        """Drop-in, `sshd -t`, `sshd -T` effective-value check, then reload
        (or, on a socket/service conflict, switch to plain ssh.service), then
        prove sshd itself is listening. Every failure before the reload
        removes the drop-in again, so nothing half-applied is left for the
        next unrelated sshd restart to pick up."""
        node = conn.name
        port = conn.cfg.port
        ctx.begin(node, "disabling SSH password authentication", "key-only from here on")

        r = conn.run("sshd -T")
        if not r.ok:
            ctx.record(node, "ssh password auth disabled", False, f"`sshd -T` failed: {r.err}")
            raise RuntimeError(f"{node}: can't read sshd's effective config: password auth left as-is")
        current_root = next((line.split(None, 1)[1] for line in r.out.splitlines()
                             if line.lower().startswith("permitrootlogin ")), "")
        push_file(conn, sshd_hardening_dropin(current_root), SSHD_DROPIN_PATH, mode="0644")

        def abort(detail: str) -> None:
            conn.run(f"rm -f {SSHD_DROPIN_PATH}")
            ctx.record(node, "ssh password auth disabled", False, detail)
            raise RuntimeError(f"{node}: {detail}: drop-in removed, sshd not reloaded, "
                               "password auth left as-is")

        r = conn.run("sshd -t")
        if not r.ok:
            abort(f"`sshd -t` failed: {r.err}")
        problems = sshd_effective_problems(conn.run("sshd -T").out)
        if problems:
            abort(f"effective sshd config still not key-only ({'; '.join(problems)}): a value set in "
                  "sshd_config before its Include line, or in an even earlier-sorting sshd_config.d "
                  "file, wins over the drop-in")
        conn.run(f"rm -f {SSHD_LEGACY_DROPIN_PATH}")

        conflict = ssh_socket_conflict(conn.run("ss -Htlnp").out, port,
                                       conn.run("systemctl is-active ssh.socket").out,
                                       conn.run("systemctl is-enabled ssh.service").out)
        if conflict:
            ctx.begin(node, "switching sshd to plain ssh.service",
                      f"ssh.socket owns :{port} alongside an enabled ssh.service")
            r = conn.run(SSH_SOCKET_CONFLICT_FIX, timeout=60)
            action = "switched from ssh.socket to plain ssh.service"
        else:
            r = conn.run("systemctl reload ssh || systemctl reload sshd", timeout=30)
            action = "reloaded"

        # A reload job can report success while sshd dies right after
        # re-exec, so the reload's own exit status proves nothing: poll for
        # the listener instead, after a moment for the SIGHUP to land (the
        # reload is asynchronous; polling at once can still see the old
        # sshd holding the port).
        time.sleep(2)
        listening, state = False, ""
        for _ in range(10):
            listening, state = _sshd_listening(conn)
            if listening:
                break
            time.sleep(1)
        if listening:
            ctx.record(node, "ssh password auth disabled", True, f"{SSHD_DROPIN_PATH}; sshd {action}; {state}")
            return

        journal = conn.run("journalctl -u ssh.service -n 5 --no-pager -o cat").out.replace("\n", " | ")
        if conflict:
            # Put the socket back (enabled too, so it survives a reboot) so
            # new logins keep working the way they did before.
            conn.run("systemctl enable --now ssh.socket")
            hint = ("ssh.socket re-enabled so new logins keep working as before; fix ssh.service by "
                    f"hand, then `{SSH_SOCKET_CONFLICT_FIX}`")
        else:
            hint = ("NEW SSH LOGINS WILL FAIL until sshd is back: from a session that's still open, "
                    "check `journalctl -u ssh.service`; if systemd owns the port, "
                    f"`{SSH_SOCKET_CONFLICT_FIX}`")
        ctx.record(node, "ssh password auth disabled", False,
                   f"sshd not listening after {action if r.ok else action + ' (command failed: ' + r.err + ')'}: "
                   f"{state}; journal: {journal}; {hint}")
        raise RuntimeError(f"{node}: sshd is not listening on :{port} after hardening")

    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        disable_uu = not ctx.cfg.unattended_upgrades
        mon_ips = self._monitor_ips_pinned(ctx)
        hostname = self._hostname_pinned(ctx)
        for conn in ctx.fleet:
            node = conn.name
            checks = [
                ("docker compose available", "docker compose version >/dev/null"),
                ("ufw active", "ufw status | grep -q 'Status: active'"),
            ]
            if disable_uu:
                checks.append(("unattended-upgrades masked",
                               "systemctl is-enabled unattended-upgrades.service 2>&1 | "
                               "grep -q masked"))
            for ip in mon_ips:
                checks.append((f"ssh allow-list includes {ip}", f"ufw status | grep -qF '{ip}'"))
            if conn is ctx.host and hostname:
                checks.append(("OS hostname matches", f"[ \"$(hostname)\" = {shlex.quote(hostname)} ]"))
            for label, cmd in checks:
                r = conn.run(cmd)
                ctx.record(node, f"verify: {label}", r.ok, r.err if not r.ok else "")
                ok = ok and r.ok

            if conn.cfg.disable_password_auth:
                problems = sshd_effective_problems(conn.run("sshd -T").out)
                ctx.record(node, "verify: sshd effective config key-only", not problems, "; ".join(problems))
                listening, state = _sshd_listening(conn)
                ctx.record(node, "verify: sshd listening + ssh.service active", listening, state)
                ok = ok and not problems and listening

            # Real probe, not just checking daemon.json is present: proves a
            # freshly-created container actually gets working DNS end to
            # end, the same way preflight's TUN check proves capability
            # instead of just checking the device file exists.
            r = conn.run("docker run --rm --pull=missing busybox:1.36 nslookup docker.com", timeout=60)
            ctx.record(node, "verify: container DNS resolution", r.ok, r.err if not r.ok else "")
            ok = ok and r.ok
        return ok
