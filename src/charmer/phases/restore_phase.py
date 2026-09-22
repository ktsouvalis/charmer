"""restore: optional, PostgreSQL `.sql.gz` dumps only (see config.py /
CLAUDE.md). Destructive by definition, so every step is gated: the stack
stops before anything touches the database, the dump is checksum-verified
after upload and deleted from the host immediately after loading (it holds
every secret Pangolin has), and a failed load leaves the stack stopped
rather than half-up on half-data.

Recreating gerbil (below) restarts its WireGuard process; any Newt agent
that was already tunneled in before this run goes stale and needs to
redial. This phase restarts each configured agent's *existing* `newt`
container for that reason alone (no fresh credentials minted, no DB
lookup needed: the running container already has the right newtId/secret
baked into its own compose file from whenever it was provisioned). An
agent with no `/opt/newt` bundle yet is skipped, not an error. This is
best-effort, not gating: an unreachable/not-yet-onboarded agent is
recorded as a warning, since the restore itself already succeeded and
`charmer monitor`/`logs` is where ongoing agent health belongs.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from ..remote import wait_for
from .base import Phase, PhaseContext, verify_public_reachable

DUMP_STAGING = "/tmp/charmer-restore.sql.gz"


def _wait_stable(conn, service: str, ctx: PhaseContext, seconds: int = 5) -> bool:
    """True if `service` reports docker state 'running' continuously for
    `seconds` (sampled once per second).

    A single sample isn't enough here: gerbil can report "Started" and then
    flip to "restarting" moments later (loading a WireGuard kernel module,
    or, after a restore, its persisted key no longer matching the
    freshly-loaded database), and traefik's `network_mode: service:gerbil`
    join is refused outright by Docker if it lands in that window. Waiting
    out a short dwell window before touching traefik at all is cheaper than
    detecting the failure after the fact.
    """
    cmd = f"cd /opt/pangolin && docker compose ps {service} --format '{{{{.State}}}}'"
    for i in range(seconds):
        r = conn.run(cmd)
        if not (r.ok and r.out.strip() == "running"):
            return False
        ctx.tick(f"confirming {service} is stable ({i + 1}/{seconds}s)")
        time.sleep(1)
    return True


class RestorePhase(Phase):
    name = "restore"
    optional = True

    def needs_confirm(self, ctx: PhaseContext) -> bool:
        # Nothing to confirm when there's no dump configured: apply() is a
        # genuine no-op in that case, and forcing a y/N here just gives the
        # operator a chance to accidentally halt the pipeline on a phase
        # that was never going to touch anything.
        return bool(ctx.cfg.restore_dump)

    def plan(self, ctx: PhaseContext) -> list[str]:
        dump = ctx.cfg.restore_dump
        if not dump:
            return ["no restore.postgres_dump configured: this phase will be SKIPPED"]
        lines = [
            f"restore {dump} onto the pangolin Postgres database: REPLACES all current data",
            "stop pangolin/gerbil/traefik first (Postgres stays up so it can be loaded into)",
            "upload, sha256-verify, load with ON_ERROR_STOP, delete the dump from the host",
            "reconcile this host's gerbil identity (publicKey/reachableAt) back onto the restored exitNodes row",
            "restart the stack and health-gate before declaring the phase done",
        ]
        if ctx.cfg.newt_agents:
            lines.append(
                f"restart the existing newt container (no re-mint, no compose changes) on "
                f"{len(ctx.cfg.newt_agents)} configured agent(s) with a bundle already in place, "
                "so they redial gerbil's recreated WireGuard process instead of sitting disconnected")
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        dump = cfg.restore_dump
        if not dump:
            return
        if not cfg.restore_destructive:
            raise ValueError("restore.postgres_dump is set but restore.destructive is not true, refusing")
        if cfg.pangolin.database != "postgres":
            raise ValueError("restore requires pangolin.database: postgres")

        local = Path(dump).expanduser()
        if not local.exists():
            raise FileNotFoundError(f"restore.postgres_dump not found: {local}")

        conn = ctx.host
        node = conn.name

        ctx.begin(node, "stopping pangolin stack", "pangolin/gerbil/traefik, postgres stays up")
        r = conn.run("cd /opt/pangolin && docker compose stop pangolin gerbil traefik", timeout=60, sudo=True)
        ctx.record(node, "pangolin stack stopped", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("failed to stop the pangolin stack before a destructive restore")

        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        ctx.begin(node, "uploading dump")
        conn.put(str(local), DUMP_STAGING)
        r = conn.run(f"sha256sum {DUMP_STAGING} | cut -d' ' -f1")
        ctx.record(node, "dump uploaded + checksum verified", r.out.strip() == digest,
                   "" if r.out.strip() == digest else "checksum mismatch: transfer corrupted")
        if r.out.strip() != digest:
            conn.run(f"rm -f {DUMP_STAGING}")
            raise RuntimeError("dump checksum mismatch after upload")

        psql = f"docker exec postgres psql -U {cfg.pangolin.postgres_user} -d pangolin"

        # Gerbil's WireGuard private key lives at /opt/pangolin/config/key, a
        # host bind mount a DB restore never touches, but the exitNodes row
        # that Pangolin matches it against on every /gerbil/get-config call
        # DOES get overwritten by the dump. A foreign publicKey/reachableAt
        # there (from whatever host the dump came from) makes get-config
        # unable to resolve this gerbil's identity, and it comes back with a
        # blank address, gerbil then panics trying to parse an empty CIDR
        # and crash-loops. Snapshot this host's own identity before the load
        # clobbers it, then patch it back on afterwards; the dump's address/
        # org/site data on that same row is left alone.
        pre = conn.run(f'{psql} -tAc \'select "exitNodeId", "publicKey", "reachableAt" '
                        f'from "exitNodes" order by "exitNodeId" limit 1\'')
        pre_identity = pre.out.strip().split("|") if pre.ok and pre.out.strip() else None

        # A dump written by a newer pg_dump can carry GUCs an older server
        # rejects (e.g. pg_dump 17 emitting `SET transaction_timeout = 0;`
        # against PostgreSQL 16), strip those specific header lines before
        # loading; ON_ERROR_STOP still governs everything else in the dump.
        load_cmd = (
            f"zcat {DUMP_STAGING} | sed -e '/^SET transaction_timeout/d' | "
            f"docker exec -i postgres psql -U {cfg.pangolin.postgres_user} -d pangolin -v ON_ERROR_STOP=1"
        )
        ctx.begin(node, "loading dump", "can take a while for large dumps")
        r = conn.run(load_cmd, timeout=1800, sudo=True)
        conn.run(f"rm -f {DUMP_STAGING}")  # always delete, it holds every secret pangolin has
        ctx.record(node, "dump loaded", r.ok, r.err.splitlines()[-1] if (not r.ok and r.err) else "")
        if not r.ok:
            raise RuntimeError("psql load failed: pangolin/gerbil/traefik intentionally left stopped; "
                               "fix the dump and --replay restore")

        if pre_identity and len(pre_identity) == 3:
            exit_id, pubkey, reachable_at = pre_identity
            reconcile = (f'{psql} -c "update \\"exitNodes\\" set \\"publicKey\\"=\'{pubkey}\', '
                         f'\\"reachableAt\\"=\'{reachable_at}\' where \\"exitNodeId\\"={exit_id}"')
            r = conn.run(reconcile, sudo=True)
            ctx.record(node, "gerbil identity reconciled onto restored exitNodes row", r.ok,
                       r.err if not r.ok else "")
            if not r.ok:
                raise RuntimeError("failed to reconcile exitNodes identity after restore: "
                                   "gerbil will crash-loop on a foreign key; fix manually and --replay restore")

        ctx.begin(node, "restarting pangolin")
        r = conn.run("cd /opt/pangolin && docker compose up -d --no-deps pangolin", timeout=120, sudo=True)
        ctx.record(node, "pangolin starting", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("failed to restart pangolin after restore")

        healthy = wait_for(conn, "cd /opt/pangolin && docker compose ps pangolin --format '{{.Health}}'",
                           expect="healthy", timeout=300, interval=5,
                           tick=lambda elapsed: ctx.tick(f"waiting for pangolin healthy ({int(elapsed)}s/300s)"))
        ctx.record(node, "pangolin healthy after restore", healthy, "")
        if not healthy:
            raise RuntimeError("pangolin did not become healthy after restore")

        # gerbil, then traefik, never together, see _wait_stable().
        ctx.begin(node, "restarting gerbil", "--no-deps, so pangolin doesn't get implicitly recreated")
        r = conn.run("cd /opt/pangolin && docker compose up -d --no-deps gerbil", timeout=60, sudo=True)
        ctx.record(node, "gerbil starting", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("failed to restart gerbil after restore")

        ctx.begin(node, "confirming gerbil is stable")
        stable = _wait_stable(conn, "gerbil", ctx)
        ctx.record(node, "gerbil stable", stable,
                   "" if stable else "gerbil kept restarting, check docker compose logs gerbil on the host")
        if not stable:
            raise RuntimeError("gerbil did not reach a stable running state after restore")

        # Force-recreate unconditionally: Docker never migrates a
        # network_mode: service:gerbil container to a fresh netns later, and
        # there's no cheap way to tell from the outside whether traefik's
        # current namespace pin is stale (see lifecycle.py/README
        # "Maintenance page" for the same reasoning).
        ctx.begin(node, "recreating traefik", "to guarantee it's on gerbil's current network namespace")
        r = conn.run("cd /opt/pangolin && docker compose up -d --no-deps --force-recreate traefik",
                     timeout=60, sudo=True)
        ctx.record(node, "traefik recreated", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("failed to restart traefik after restore")

        for agent_conn in ctx.agents:
            self._redial_newt(ctx, agent_conn)

        ctx.state.data["generated"]["restore_sha256"] = digest
        ctx.state.data["generated"]["restore_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        ctx.state.save()
        ctx.restore_ran = True

    def _redial_newt(self, ctx: PhaseContext, agent_conn) -> None:
        """Restart `agent_conn`'s existing newt container so it redials the
        gerbil process this phase just recreated. No credentials touched, no
        compose file rewritten: an agent with no bundle yet (never
        provisioned by this site, or genuinely new) is skipped, not an
        error; an agent that fails to come back is a warning, not a phase
        failure (see module docstring)."""
        node = agent_conn.name
        if not agent_conn.run("test -f /opt/newt/docker-compose.yml").ok:
            ctx.record(node, "newt redial", True, "no /opt/newt bundle here yet, nothing to restart")
            return
        ctx.begin(node, "restarting newt", "redialing gerbil's recreated WireGuard process")
        r = agent_conn.run("cd /opt/newt && docker compose restart newt", timeout=60)
        if not r.ok:
            ctx.record(node, "newt redial", False, r.err, warn=True)
            return
        stable = wait_for(agent_conn, "cd /opt/newt && docker compose ps newt --format '{{.State}}'",
                          expect="running", timeout=60, interval=3)
        ctx.record(node, "newt redial", stable,
                   "" if stable else "did not return to running; check docker compose logs newt on the agent",
                   warn=not stable)

    def verify(self, ctx: PhaseContext) -> bool:
        if not ctx.cfg.restore_dump:
            return True
        conn = ctx.host
        r = conn.run(
            "docker exec postgres psql -U " + ctx.cfg.pangolin.postgres_user +
            " -d pangolin -tAc \"select count(*) from pg_tables where schemaname='public'\"")
        ok = r.ok and r.out.strip().isdigit() and int(r.out.strip()) > 0
        ctx.record(conn.name, "verify: restored schema has tables", ok, r.out)
        # A healthy pangolin container says nothing about traefik, see
        # _wait_stable()/verify_public_reachable(); so this is the check
        # that actually catches a stack left half-up after restore.
        return ok and verify_public_reachable(ctx)
