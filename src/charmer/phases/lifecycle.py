"""shutdown / start: ad hoc operational commands, not part of the
`provision` pipeline.

`shutdown` stops only pangolin; Postgres, every Newt agent, gerbil, and
traefik (plus its tiny maintenance-page container) keep running;
deliberately left up so Traefik's `errors` middleware can fall back to the
"we'll be back" page on the dashboard host (see pangolin_phase.py and
README "Maintenance page"). Gerbil in particular MUST stay up: traefik runs
`network_mode: service:gerbil`, i.e. it borrows gerbil's network namespace
and loopback port-publish wholesale rather than having its own; stopping
gerbil silently takes traefik's connectivity down with it, which defeats
the maintenance page this phase exists to serve (traefik keeps "running"
per `docker compose ps`, but nothing can reach it). That coverage is
dashboard-only: resource subdomains come from Pangolin's own HTTP
provider, which goes stale the moment pangolin is unreachable, so visitors
there still see a hard failure. `start` refuses unless `shutdown` last
completed gracefully, so it can't be used to "start" a site that was never
actually provisioned.
"""

from __future__ import annotations

import shlex

from ..remote import wait_for
from .base import Phase, PhaseContext

MAINTENANCE_MARKER = 'name="charmer-maintenance"'


class ShutdownPhase(Phase):
    name = "shutdown"

    def plan(self, ctx: PhaseContext) -> list[str]:
        return [
            "stop the pangolin container (postgres, newt agents, gerbil, traefik, and the "
            "maintenance page keep running; gerbil must stay up for traefik's network to work, "
            "see README 'Ingress')",
            "traefik gets force-recreated first (brief, sub-second reachability blip) to guarantee "
            "it's attached to gerbil's current network namespace, regardless of history",
            "visitors to the dashboard host now see the maintenance page instead of a connection "
            "reset; resource subdomains are not covered (see README 'Ingress')",
        ]

    def apply(self, ctx: PhaseContext) -> None:
        conn = ctx.host
        # gerbil/maintenance first, `--no-deps` throughout: gerbil and
        # traefik both `depends_on: pangolin: condition: service_healthy`,
        # and without `--no-deps` a plain `up` would silently start pangolin
        # back up too and block on its healthcheck (up to ~2.5min) before
        # doing anything else: looks exactly like a hang, no progress shown.
        ctx.begin(conn.name, "ensuring gerbil/maintenance are up", "--no-deps, so pangolin stays down")
        r = conn.run("cd /opt/pangolin && docker compose up -d --no-deps gerbil maintenance",
                     timeout=120, sudo=True)
        ctx.record(conn.name, "gerbil/maintenance up", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("failed to ensure gerbil/maintenance are up")
        # Traefik's `network_mode: service:gerbil` pins it to gerbil's
        # network namespace at container-start time, and Docker never
        # migrates that later: if gerbil was ever recreated/restarted since
        # traefik last started (including by the `up` just above, or by an
        # earlier run of this same command), traefik is left silently
        # attached to gerbil's dead old namespace: still "running" per
        # `docker compose ps`, completely unreachable on the wire. There's
        # no cheap way to detect that staleness from the outside, so rather
        # than guess, unconditionally force-recreate traefik every time;
        # a sub-second blip during a deliberate maintenance operation is a
        # fine price for guaranteed correctness.
        ctx.begin(conn.name, "recreating traefik", "to guarantee it's on gerbil's current network namespace")
        r = conn.run("cd /opt/pangolin && docker compose up -d --no-deps --force-recreate traefik",
                     timeout=60, sudo=True)
        ctx.record(conn.name, "traefik recreated", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("failed to recreate traefik")
        r = conn.run("cd /opt/pangolin && docker compose stop pangolin", timeout=60, sudo=True)
        ctx.record(conn.name, "pangolin stopped", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("failed to stop pangolin")

    def verify(self, ctx: PhaseContext) -> bool:
        conn = ctx.host
        r = conn.run("cd /opt/pangolin && docker compose ps --status running --format '{{.Name}}'")
        stopped = "pangolin" not in r.out
        ctx.record(conn.name, "verify: pangolin stopped", stopped, r.out)

        still_up = all(n in r.out for n in ("gerbil", "traefik", "maintenance"))
        ctx.record(conn.name, "verify: gerbil/traefik/maintenance still running", still_up, r.out)

        scheme = "https" if ctx.cfg.tls.provider != "none" else "http"
        port = 443 if scheme == "https" else 80
        host = ctx.cfg.dashboard_host
        curl_cmd = (f"curl -sk --max-time 10 --resolve {shlex.quote(host)}:{port}:127.0.0.1 "
                    f"{scheme}://{host}:{port}/")
        # A short poll, not a single shot: traefik was just (re)created above
        # and needs a moment to finish binding/loading its TLS config.
        page_ok = wait_for(conn, curl_cmd, expect=MAINTENANCE_MARKER, timeout=30, interval=2,
                           tick=lambda elapsed: ctx.tick(f"waiting for the maintenance page ({int(elapsed)}s/30s)"))
        ctx.record(conn.name, "verify: dashboard serves the maintenance page", page_ok, "")

        return stopped and still_up and page_ok


class StartPhase(Phase):
    name = "start"

    def plan(self, ctx: PhaseContext) -> list[str]:
        return ["start pangolin (gerbil, traefik, and the maintenance page were never stopped); "
               "refuses unless `shutdown` last completed gracefully"]

    def apply(self, ctx: PhaseContext) -> None:
        if ctx.state.phase_status("shutdown") != "done":
            raise RuntimeError("refusing: `charmer shutdown` for this site did not last complete gracefully; "
                               "nothing to safely start back up")
        conn = ctx.host
        # `up -d` on all four is idempotent; gerbil/traefik/maintenance are
        # already up and this just no-ops for them, but it's what makes
        # `start` self-healing if any of them happened to be down too (host
        # reboot, etc.) rather than assuming shutdown's invariant always held.
        ctx.begin(conn.name, "docker compose up", "pangolin: gerbil/traefik/maintenance already running")
        r = conn.run("cd /opt/pangolin && docker compose up -d pangolin gerbil traefik maintenance",
                     timeout=300, sudo=True)
        ctx.record(conn.name, "stack starting", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("failed to start the pangolin stack")
        ctx.begin(conn.name, "waiting for pangolin healthy")
        healthy = wait_for(conn, "cd /opt/pangolin && docker compose ps pangolin --format '{{.Health}}'",
                           expect="healthy", timeout=300, interval=5,
                           tick=lambda elapsed: ctx.tick(f"waiting for pangolin healthy ({int(elapsed)}s/300s)"))
        ctx.record(conn.name, "pangolin healthy", healthy, "")
        if not healthy:
            raise RuntimeError("pangolin did not become healthy after starting")
        ctx.state.mark_phase("shutdown", "reversed")

    def verify(self, ctx: PhaseContext) -> bool:
        r = ctx.host.run("curl -sk -o /dev/null -w '%{http_code}' http://127.0.0.1:3001/api/v1/")
        ok = r.out in ("200", "204")
        ctx.record(ctx.host.name, "verify: pangolin API responds", ok, f"HTTP {r.out}")
        return ok
