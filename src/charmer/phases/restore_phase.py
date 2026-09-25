"""restore: optional, PostgreSQL `.sql.gz` dumps only (see config.py /
CLAUDE.md). Destructive by definition, so every step is gated: the stack
stops before anything touches the database, the dump is checksum-verified
after upload and deleted from the host immediately after loading (it holds
every secret Pangolin has), and a failed load leaves the stack stopped
rather than half-up on half-data.

Recreating gerbil (below) restarts its WireGuard process; any Newt agent
that was already tunneled in before this run goes stale and needs to
redial. This phase recreates each configured agent's *existing* `newt`
container (down + up) for that reason alone (newt_ops.redial(): no fresh
credentials minted, no compose changes). An agent with no `/opt/newt` bundle yet is
skipped, not an error. This is best-effort, not gating: an agent that
doesn't come back is recorded as a warning, since the restore itself
already succeeded and `charmer monitor`/`logs` is where ongoing agent
health belongs. Every other site in the restored database is listed for a
manual restart; adopt_newt (the next phase) offers to bring their hosts
under charmer.

After the load, every org's `utilitySubnet` (the range Pangolin hands out
site-resource alias addresses from) is checked. Orgs created before
Pangolin 1.13 were migrated to a /24 there (scriptsPg/1.13.0.ts), which a
restore carries forward; 1.22 gives new orgs a /20 (`orgs.
utility_subnet_group`). A /24 runs out ("No available subnets remaining in
space") once enough resources exist. With `restore.utility_subnet_prefix`
set, a narrower range is widened in place to the aligned supernet of that
size, which keeps every existing alias valid, but only if the result
overlaps neither any org's `subnet` nor Gerbil's network (config.yml's
`gerbil.subnet_group`, Pangolin's default since charmer doesn't set it,
plus every `exitNodes.address`). Otherwise it's left alone with a warning.
The just-loaded dump is the pre-change backup. Clients only pick up the
wider route when they reconnect.
"""

from __future__ import annotations

import hashlib
import ipaddress
import shlex
import time
from pathlib import Path

from ..remote import wait_for
from .base import Phase, PhaseContext, verify_public_reachable
from .newt_ops import redial_all

DUMP_STAGING = "/tmp/charmer-restore.sql.gz"

# Pangolin 1.22's own defaults (server/lib/readConfigFile.ts): the
# utilitySubnet new orgs get, and gerbil.subnet_group, which charmer's
# config.yml never overrides (see pangolin-config.yml.j2).
PANGOLIN_DEFAULT_UTILITY_PREFIX = 20
PANGOLIN_DEFAULT_GERBIL_SUBNET_GROUP = "100.89.137.0/20"


def widen_utility_subnet(current: str, prefix: int, org_subnets: list[str],
                         gerbil_nets: list[str]) -> tuple[str | None, str]:
    """Return (new CIDR or None, reason). None means leave `current` as is:
    already at least that wide, unparseable, or the widened block would
    overlap an org subnet / Gerbil's network. Pure, so it's unit-tested
    without a host."""
    try:
        cur = ipaddress.ip_network(current, strict=False)
    except ValueError:
        return None, f"unparseable utilitySubnet {current!r}, left alone"
    if cur.prefixlen <= prefix:
        return None, f"{cur} is already /{cur.prefixlen}, at least /{prefix}"
    new = cur.supernet(new_prefix=prefix)
    for label, nets in (("org subnet", org_subnets), ("Gerbil network", gerbil_nets)):
        for n in nets:
            try:
                other = ipaddress.ip_network(n, strict=False)
            except ValueError:
                continue
            if other.version == new.version and new.overlaps(other):
                return None, f"widening {cur} to {new} would overlap {label} {other}, left alone"
    return str(new), f"{cur} -> {new}"


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
        ]
        target = ctx.cfg.restore_utility_subnet_prefix
        if target is None:
            lines.append(f"check every org's utilitySubnet and warn if narrower than Pangolin's "
                         f"/{PANGOLIN_DEFAULT_UTILITY_PREFIX} default (restore.utility_subnet_prefix unset: "
                         "nothing is changed)")
        else:
            lines.append(f"widen any org's utilitySubnet narrower than /{target} to the aligned /{target} "
                         "containing it (existing aliases stay valid), only if that overlaps neither an "
                         "org subnet nor Gerbil's network; otherwise leave it and warn")
        lines += [
            "restart the stack and health-gate before declaring the phase done",
        ]
        if ctx.cfg.newt_agents:
            lines.append(
                f"recreate (down + up) the existing newt container (no re-mint, no compose changes) on "
                f"{len(ctx.cfg.newt_agents)} configured agent(s) with a bundle already in place, "
                "so they redial gerbil's recreated WireGuard process instead of sitting disconnected")
        lines.append("list every Newt/site connector in the restored database that charmer doesn't "
                     "manage, for a manual restart (adopt_newt, next, offers to take them over)")
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

        self._utility_subnets(ctx, conn, psql)

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

        ctx.state.data["generated"]["restore_sha256"] = digest
        ctx.state.data["generated"]["restore_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        ctx.state.save()
        ctx.restore_ran = True
        # A fresh database means fresh unmanaged sites: offer adoption again.
        ctx.state.mark_phase("adopt_newt", "pending")

        # Last, after the restore is recorded: a redial failure is only a warning.
        redial_all(ctx, "the restore recreated gerbil, so every Newt tunnel had to redial",
                   hint="The adopt_newt phase, next, asks for the host of each connector from the "
                        "old installation (IP/SSH) so charmer can take it over without re-minting.")

    def _utility_subnets(self, ctx: PhaseContext, conn, psql: str) -> None:
        """Check (and, with restore.utility_subnet_prefix, widen) every
        restored org's utilitySubnet; see the module docstring. Runs while
        pangolin is still stopped, so nothing allocates from the old range
        mid-change. Never fails the phase: the restore itself succeeded."""
        node = conn.name
        target = ctx.cfg.restore_utility_subnet_prefix
        r = conn.run(f"{psql} -tA -F '|' -c " + shlex.quote(
            'select "orgId", coalesce("subnet", \'\'), coalesce("utilitySubnet", \'\') '
            'from orgs order by "orgId"'))
        if not r.ok:
            ctx.record(node, "utilitySubnet check", False, r.err or "query failed", warn=True)
            return
        orgs = [line.split("|") for line in r.out.splitlines() if line.count("|") == 2]
        org_subnets = [o[1] for o in orgs if o[1]]
        g = conn.run(f"{psql} -tAc " + shlex.quote('select "address" from "exitNodes"'))
        gerbil_nets = [PANGOLIN_DEFAULT_GERBIL_SUBNET_GROUP] + (
            [a for a in g.out.split() if a] if g.ok else [])

        for org_id, _subnet, utility in orgs:
            label = f"org {org_id}: utilitySubnet"
            if not utility:
                ctx.record(node, label, True, "not set, left alone")
                continue
            if target is None:
                try:
                    narrow = ipaddress.ip_network(utility, strict=False).prefixlen > PANGOLIN_DEFAULT_UTILITY_PREFIX
                except ValueError:
                    narrow = False
                detail = utility + (f", narrower than Pangolin's /{PANGOLIN_DEFAULT_UTILITY_PREFIX} "
                                    "default for new orgs; set restore.utility_subnet_prefix to widen it "
                                    "on the next restore" if narrow else "")
                ctx.record(node, label, not narrow, detail, warn=narrow)
                continue
            new, reason = widen_utility_subnet(utility, target, org_subnets, gerbil_nets)
            if new is None:
                blocked = "overlap" in reason or "unparseable" in reason
                ctx.record(node, label, not blocked, reason, warn=blocked)
                continue
            lit = lambda v: "'" + v.replace("'", "''") + "'"  # noqa: E731
            sql = (f'update orgs set "utilitySubnet" = {lit(new)} '
                   f'where "orgId" = {lit(org_id)} and "utilitySubnet" = {lit(utility)}')
            upd = conn.run(f"{psql} -v ON_ERROR_STOP=1 -c " + shlex.quote(sql), sudo=True)
            ctx.record(node, f"{label} widened", upd.ok,
                       f"{reason}; clients pick up the wider route on reconnect" if upd.ok else upd.err,
                       warn=not upd.ok)

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
