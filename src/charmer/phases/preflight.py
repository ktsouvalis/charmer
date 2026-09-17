"""Preflight: read-only validation of the Pangolin host and every Newt
agent before anything is changed. Safe to run against any host at any time,
including production, where it should refuse on existing artifacts.
"""

from __future__ import annotations

from ..config import REQUIRED_FREE_TCP_PORTS, REQUIRED_FREE_UDP_PORTS
from .base import Phase, PhaseContext

# What each completed phase legitimately occupies, so a resumed run doesn't
# flag its own footprint as a problem. Coarse (port number only, not bind
# address); same simplification akropolis's preflight makes.
PHASE_TCP_PORTS: dict[str, set[int]] = {"pangolin": {80, 443}}
PHASE_UDP_PORTS: dict[str, set[int]] = {"pangolin": {51820, 21820}}
PHASE_CONTAINERS: dict[str, tuple[str, ...]] = {"pangolin": ("pangolin", "gerbil", "traefik", "postgres")}

MIN_FREE_GB = 10

TUN_LXC_HINT = (
    "if this host is an unprivileged LXC container, the fix is on the "
    "hypervisor side, not here: add `lxc.cgroup2.devices.allow: c 10:200 rwm` "
    "and `lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file` to "
    "its <id>.conf, or make the container privileged / use a VM instead"
)


class PreflightPhase(Phase):
    name = "preflight"
    read_only = True

    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        lines = [
            f"SSH to the Pangolin host ({cfg.host_ip}) as {cfg.ssh.user!r} (auth: {cfg.ssh.auth}) "
            "and run read-only checks: reachability + sudo, OS release, free disk, required ports",
            f"required TCP ports free: {', '.join(map(str, REQUIRED_FREE_TCP_PORTS))} "
            f"(Gerbil/Traefik) / UDP: {', '.join(map(str, REQUIRED_FREE_UDP_PORTS))} (Gerbil/WireGuard)",
        ]
        if cfg.refuse_existing:
            lines.append("REFUSE the host if it already carries pangolin/gerbil/traefik/postgres containers")
        lines.append("state-aware: footprint of already-completed phases is expected, not a failure")
        if cfg.tls.provider != "none" and cfg.tls.hostname:
            note = "hard requirement" if cfg.tls.provider == "acme" else "warning only"
            lines.append(f"DNS: {cfg.tls.hostname} resolves to *something* ({note}); NAT/firewalling "
                         "beyond that is yours to verify by hand")
        if cfg.newt_agents:
            lines.append(f"for each of {len(cfg.newt_agents)} Newt agent(s): SSH reachability + sudo, "
                         "Docker presence, and a REAL TUN capability test (create + delete a probe "
                         "WireGuard-capable tun interface as root, not just checking the device file exists)")
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        done = {"pangolin"} if ctx.state.phase_status("pangolin") == "done" else set()
        midlife = bool(done)
        expected_tcp: set[int] = set()
        expected_udp: set[int] = set()
        for p in done:
            expected_tcp |= PHASE_TCP_PORTS.get(p, set())
            expected_udp |= PHASE_UDP_PORTS.get(p, set())

        host = ctx.host
        self._check_host(ctx, host, cfg, done, midlife, expected_tcp, expected_udp)

        for agent in ctx.agents:
            self._check_agent(ctx, agent)

    def _check_host(self, ctx: PhaseContext, conn, cfg, done, midlife, expected_tcp, expected_udp) -> None:
        node = conn.name
        try:
            conn.connect()
            r = conn.run("id -u")
            if r.ok and r.out == "0":
                ctx.record(node, "ssh + root/sudo", True, f"uid 0 as {cfg.ssh.user}")
            elif cfg.ssh.become:
                r2 = conn.run("id -u", sudo=True)
                ctx.record(node, "ssh + root/sudo", r2.ok and r2.out == "0",
                           "sudo -n works" if r2.ok else f"sudo failed: {r2.err}")
            else:
                ctx.record(node, "ssh + root/sudo", False, f"connected as uid {r.out} without become=true")
                return
        except Exception as exc:  # noqa: BLE001
            ctx.record(node, "ssh + root/sudo", False, str(exc))
            return

        r = conn.run(". /etc/os-release && echo $ID $VERSION_ID")
        expected = "ubuntu 24.04"
        ctx.record(node, "os release", r.out == expected, r.out or r.err, warn=(r.out != expected))

        r = conn.run("df --output=avail -BG / | tail -1 | tr -dc 0-9")
        if r.ok and r.out:
            free_gb = int(r.out)
            low = free_gb < MIN_FREE_GB
            ctx.record(node, "free disk on /", not low,
                       f"{free_gb} GB free (need >= {MIN_FREE_GB}"
                       + (", warning only; already provisioned)" if low and midlife else ")"),
                       warn=(low and midlife))
        else:
            ctx.record(node, "free disk on /", False, r.err or "df failed")

        r = conn.run("ss -Htln | awk '{print $4}'")
        listening_tcp: set[int] = set()
        for addr in r.out.splitlines():
            try:
                listening_tcp.add(int(addr.rsplit(":", 1)[-1]))
            except ValueError:
                pass
        occupied = sorted((set(REQUIRED_FREE_TCP_PORTS) & listening_tcp) - expected_tcp)
        owned = sorted(set(REQUIRED_FREE_TCP_PORTS) & listening_tcp & expected_tcp)
        detail = f"occupied: {occupied}" if occupied else "all free"
        if owned:
            detail += f" (ignoring {owned}, owned by completed phases)"
        ctx.record(node, "required TCP ports free", not occupied, detail)

        r = conn.run("ss -Hulnp | awk '{print $5}'")
        listening_udp: set[int] = set()
        for addr in r.out.splitlines():
            try:
                listening_udp.add(int(addr.rsplit(":", 1)[-1]))
            except ValueError:
                pass
        occupied_udp = sorted((set(REQUIRED_FREE_UDP_PORTS) & listening_udp) - expected_udp)
        ctx.record(node, "required UDP ports free", not occupied_udp,
                   f"occupied: {occupied_udp}" if occupied_udp else "all free")

        if cfg.refuse_existing:
            artifacts: list[str] = []
            r = conn.run("command -v docker >/dev/null && "
                         "docker ps --format '{{.Names}}' | grep -Ei 'pangolin|gerbil|traefik|postgres' || true")
            owned_names = PHASE_CONTAINERS.get("pangolin", ()) if "pangolin" in done else ()
            foreign = [n for n in r.out.splitlines() if n and not any(pat in n for pat in owned_names)]
            if foreign:
                artifacts.append(f"containers: {', '.join(foreign)}")
            ctx.record(node, "no existing pangolin artifacts", not artifacts,
                       "; ".join(artifacts) if artifacts else "clean host",
                       warn=(bool(artifacts) and midlife))

        if cfg.tls.provider != "none" and cfg.tls.hostname:
            hard = cfg.tls.provider == "acme"
            try:
                r = conn.run(f"getent ahostsv4 {cfg.tls.hostname} | awk '{{print $1}}' | sort -u")
                resolved = r.out.splitlines() if r.ok else []
                ctx.record(node, f"DNS {cfg.tls.hostname} resolves", bool(resolved),
                           f"resolves to {resolved or 'nothing'}", warn=(not resolved and not hard))
            except Exception as exc:  # noqa: BLE001
                ctx.record(node, f"DNS {cfg.tls.hostname} resolves", False, str(exc), warn=not hard)

    def _check_agent(self, ctx: PhaseContext, conn) -> None:
        node = conn.name
        try:
            conn.connect()
            r = conn.run("id -u")
            ok = r.ok
            ctx.record(node, "ssh reachable", ok, "" if ok else r.err)
            if not ok:
                return
        except Exception as exc:  # noqa: BLE001
            ctx.record(node, "ssh reachable", False, str(exc))
            return

        r = conn.run("command -v docker && docker compose version", sudo=False)
        ctx.record(node, "docker present", r.ok, "will be installed by the newt phase" if not r.ok else r.out, warn=not r.ok)

        # Real capability test, not just `test -c /dev/net/tun`: a device
        # node can exist and still be blocked at the cgroup layer (this is
        # exactly the failure mode of an unprivileged LXC container without
        # the tun device passed through: the node is there, opening it
        # isn't permitted). Creating and tearing down an actual tun
        # interface proves the whole path end to end.
        probe = "chrmr0"
        r = conn.run(f"ip tuntap add dev {probe} mode tun && ip link delete {probe}", sudo=True)
        if r.ok:
            ctx.record(node, "TUN device capability", True, "created + tore down a probe tun interface")
        else:
            ctx.record(node, "TUN device capability", False, f"{r.err or r.out}; {TUN_LXC_HINT}")

    def verify(self, ctx: PhaseContext) -> bool:
        failures = [c for c in ctx.checks if not c.ok and not c.warn]
        warnings = [c for c in ctx.checks if not c.ok and c.warn]
        if warnings:
            print(f"\npreflight warnings: {len(warnings)} (review above)")
        if failures:
            print(f"preflight FAILED: {len(failures)} blocking problem(s):")
            for c in failures:
                print(f"  ✘ [{c.host}] {c.name}: {c.detail}")
            return False
        print(f"\npreflight passed: {len(ctx.checks)} checks, {len(warnings)} warning(s).")
        return True
