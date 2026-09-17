"""clean: tear the site down to a bare host (reverse build order). Invoked
as its own subcommand with a typed-site-name confirmation gate in EVERY
environment, not just production (see cli.py); this is the one command
where "declined" should be the easy default, not `y/N`.

Packages (docker) and the hostname are deliberately left alone: apt state
belongs to the operator's patching policy, and removing data + config is
what makes the next `provision` honest.
"""

from __future__ import annotations

from .base import Phase, PhaseContext


class CleanPhase(Phase):
    name = "clean"

    def plan(self, ctx: PhaseContext) -> list[str]:
        lines = []
        if ctx.cfg.newt_agents:
            lines.append(f"stop + remove newt containers and /opt/newt on {len(ctx.cfg.newt_agents)} agent(s) "
                        "(their Pangolin sites are NOT deleted server-side; remove those from the dashboard "
                        "if desired)")
            lines.append("UFW reset on each agent too (ssh re-allowed before re-enable); docker, packages, "
                        "hostname, and any opted-in SSH key-only hardening are left alone, same as the host")
        lines += [
            "docker compose down -v for pangolin/gerbil/traefik/maintenance(/postgres); "
            "REMOVES ALL DATA, including the Postgres volume if present",
            "remove /opt/pangolin",
            "UFW reset (ssh re-allowed before re-enable)",
            "archive the local state file (not deleted; your paper trail of what was pinned)",
        ]
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        for conn in ctx.agents:
            r = conn.run("cd /opt/newt && docker compose down -v", sudo=True, timeout=60)
            conn.run("rm -rf /opt/newt", sudo=True)
            ctx.record(conn.name, "newt removed", r.ok, r.err if not r.ok else "")

            r = conn.run("ufw --force reset && ufw allow ssh && ufw --force enable", sudo=True)
            ctx.record(conn.name, "ufw reset", r.ok, r.err if not r.ok else "")

        host = ctx.host
        r = host.run("cd /opt/pangolin && docker compose down -v", sudo=True, timeout=120)
        ctx.record(host.name, "pangolin stack removed", r.ok, r.err if not r.ok else "")
        host.run("rm -rf /opt/pangolin", sudo=True)

        r = host.run("ufw --force reset && ufw allow ssh && ufw --force enable", sudo=True)
        ctx.record(host.name, "ufw reset", r.ok, r.err if not r.ok else "")

    def verify(self, ctx: PhaseContext) -> bool:
        r = ctx.host.run("test -d /opt/pangolin")
        clean = not r.ok
        ctx.record(ctx.host.name, "verify: /opt/pangolin removed", clean, "")
        return clean
