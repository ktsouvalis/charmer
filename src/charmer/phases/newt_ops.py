"""Newt-agent operations shared by pangolin, restore, adopt_newt, newt and
shutdown/start.

When does a Newt agent need a restart? Only when gerbil's WireGuard process
restarted, i.e. the gerbil container was recreated or restarted (a changed
gerbil tag, the whole-stack --force-recreate fallback, restore, a gerbil
that `shutdown`/`start` had to bring back up). The agent's tunnel is to that
process and goes stale (restore_phase.py hit this first). Pangolin, traefik,
postgres and the maintenance container restarting only drop Newt's
websocket control channel, which Newt redials itself every 3s
(fosrl/newt websocket/client.go), so they need nothing.

Charmer recreates the agents it manages (every configured agent with a
/opt/newt bundle: `docker compose down && up -d`) itself. Anything else can't be restarted from here:
announce_unmanaged() lists the sites in Pangolin's database whose newtId
isn't pinned for a configured agent (added later from the dashboard, or
carried over by a restore) so the operator can restart them by hand.

The newt table is the same for Newt and for the Pangolin CLI's `up site`
(1.23 added `agent` = newt|cli to it); `sites`/`newt` column names checked
against server/db/pg/schema/schema.ts at 1.22.2 and 1.23.0.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import yaml

from ..remote import wait_for
from .base import PhaseContext, console

NEWT_DIR = "/opt/newt"

# Images a site connector runs from. fosrl/newt reads NEWT_ID/NEWT_SECRET
# (or -id/-secret), fosrl/pangolin-cli reads SITE_ID/SITE_SECRET (or
# --id/--secret, see its entrypoint.sh); both take PANGOLIN_ENDPOINT.
CONNECTOR_IMAGES = {"fosrl/newt": "newt", "fosrl/pangolin-cli": "cli"}


# ---------------------------------------------------------------- pure logic
def connector_kind(image: str) -> str | None:
    """'newt' / 'cli' for a site-connector image reference, else None.
    Accepts registry prefixes and tags/digests: docker.io/fosrl/newt:1.17.0."""
    repo = image.split("@", 1)[0]
    last = repo.rsplit("/", 1)[-1]
    if ":" in last:
        repo = repo[: len(repo) - len(last)] + last.split(":", 1)[0]
    for name, kind in CONNECTOR_IMAGES.items():
        if repo == name or repo.endswith("/" + name):
            return kind
    return None


def image_tag(image: str) -> str | None:
    last = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else None


def parse_connector_credentials(config: dict) -> dict | None:
    """{newt_id, secret, endpoint, docker_socket} from a connector
    container's `docker inspect` .Config (Env + Cmd). None if no id/secret
    pair is in there, e.g. a Newt reading them from a CONFIG_FILE."""
    env = dict(e.split("=", 1) for e in (config.get("Env") or []) if "=" in e)
    flags: dict[str, str] = {}
    toks = list(config.get("Cmd") or [])
    for i, t in enumerate(toks):
        if not t.startswith("-"):
            continue
        key, eq, val = t.lstrip("-").partition("=")
        if not eq and i + 1 < len(toks) and not toks[i + 1].startswith("-"):
            val = toks[i + 1]
        flags.setdefault(key, val)
    newt_id = env.get("NEWT_ID") or env.get("SITE_ID") or flags.get("id", "")
    secret = env.get("NEWT_SECRET") or env.get("SITE_SECRET") or flags.get("secret", "")
    if not (newt_id and secret):
        return None
    return {"newt_id": newt_id, "secret": secret,
            "endpoint": env.get("PANGOLIN_ENDPOINT") or flags.get("endpoint", ""),
            "docker_socket": bool(env.get("DOCKER_SOCKET") or flags.get("docker-socket"))}


_NEWT_AGENTS_RE = re.compile(r"^newt_agents:[ \t]*(\[\])?[ \t]*(#.*)?$")


def append_newt_agents(text: str, agents: list[dict]) -> str:
    """Append `agents` to the config file's `newt_agents:` list, textually,
    so the operator's comments and layout survive. Handles `newt_agents:
    []`, an existing block list, and no key at all."""
    snippet = "".join(
        "\n".join("  " + line if line else line
                  for line in yaml.safe_dump([a], sort_keys=False, allow_unicode=True).splitlines()) + "\n"
        for a in agents)
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = _NEWT_AGENTS_RE.match(line.rstrip("\n"))
        if not m:
            continue
        if m.group(1):  # `newt_agents: []`
            lines[i] = "newt_agents:\n" + snippet
            return "".join(lines)
        # Block list: insert after its last indented line. A column-0
        # comment after that belongs to the next section, not this list.
        end = i
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if s.strip() == "" or s.startswith("#"):
                continue
            if s[0] in " \t-":
                end = j
                continue
            break
        if not lines[end].endswith("\n"):
            lines[end] += "\n"
        lines.insert(end + 1, snippet)
        return "".join(lines)
    return text + ("" if text.endswith("\n") or not text else "\n") + "newt_agents:\n" + snippet


def write_agents_to_config(config_path: Path, agents: list[dict]):
    """Append `agents` to the config file, re-validate it with load() and
    return the reloaded SiteConfig. On a validation failure the original
    file is put back and ConfigError propagates, so a bad answer never
    leaves a config that won't load."""
    from ..config import load

    original = config_path.read_text()
    config_path.write_text(append_newt_agents(original, agents))
    try:
        return load(config_path)
    except Exception:
        config_path.write_text(original)
        raise


# ------------------------------------------------------------ remote helpers
def check_credentials(ctx: PhaseContext, newt_id: str, secret: str) -> bool | None:
    """Ask this site's Pangolin whether it accepts a connector's newtId +
    secret, exactly the way the connector does on connect (POST
    /api/v1/auth/newt/get-token, getNewtToken.ts), over SSH against
    loopback 3001. True = 200, False = 400 (no such newt / wrong secret),
    None = couldn't tell (pangolin down, other status)."""
    body = json.dumps({"newtId": newt_id, "secret": secret})
    r = ctx.host.run(
        "curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "
        "http://127.0.0.1:3001/api/v1/auth/newt/get-token "
        "-H 'Content-Type: application/json' -H 'X-CSRF-Token: x-csrf-protection' "
        f"-d {shlex.quote(body)}")
    code = r.out.strip()
    if code == "200":
        return True
    if code in ("400", "401", "403", "404"):
        return False
    return None


def discover_connectors(conn) -> list[dict]:
    """Every site-connector container on `conn`'s host (any state):
    {id, name, image, kind, state, working_dir, creds}. Read-only."""
    fmt = ("{{.ID}}|{{.Image}}|{{.Names}}|{{.State}}|"
           '{{.Label "com.docker.compose.project.working_dir"}}')
    r = conn.run(f"docker ps -a --no-trunc --format {shlex.quote(fmt)}")
    found = []
    for line in r.out.splitlines() if r.ok else []:
        parts = line.strip().split("|")
        if len(parts) != 5 or not connector_kind(parts[1]):
            continue
        cid, image, name, state, wd = parts
        insp = conn.run(f"docker inspect --format '{{{{json .Config}}}}' {shlex.quote(cid)}")
        try:
            creds = parse_connector_credentials(json.loads(insp.out)) if insp.ok else None
        except json.JSONDecodeError:
            creds = None
        found.append({"id": cid, "name": name, "image": image, "kind": connector_kind(image),
                      "state": state, "working_dir": wd, "creds": creds})
    return found


def connector_units(conn) -> list[str]:
    """systemd services that look like a non-container connector (Newt's
    install script, or `pangolin service install site`). Only reported:
    charmer doesn't read credentials out of unit/env files."""
    r = conn.run("systemctl list-unit-files --no-legend --type=service 'newt*' 'pangolin*' 2>/dev/null")
    return [line.split()[0] for line in r.out.splitlines() if line.strip()] if r.ok else []


def newt_sites(ctx: PhaseContext) -> list[tuple[str, str]] | None:
    """(newtId, site name) for every Newt/CLI site in Pangolin's database,
    or None if it can't be read (sqlite, postgres down)."""
    if ctx.cfg.pangolin.database != "postgres":
        return None
    sql = 'select n."id", s."name" from newt n join sites s on s."siteId" = n."siteId" order by s."name"'
    r = ctx.host.run(f"docker exec postgres psql -U {ctx.cfg.pangolin.postgres_user} -d pangolin "
                     f"-tA -F '|' -c {shlex.quote(sql)}")
    if not r.ok:
        return None
    return [tuple(line.split("|", 1)) for line in r.out.splitlines() if "|" in line]  # type: ignore[misc]


def managed_newt_ids(ctx: PhaseContext) -> set[str]:
    g = ctx.state.data["generated"]
    return {g[f"newt_id_{a.name}"] for a in ctx.cfg.newt_agents if f"newt_id_{a.name}" in g}


def unmanaged_sites(ctx: PhaseContext) -> list[tuple[str, str]] | None:
    sites = newt_sites(ctx)
    if sites is None:
        return None
    managed = managed_newt_ids(ctx)
    return [s for s in sites if s[0] not in managed]


def announce_unmanaged(ctx: PhaseContext, why: str, hint: str = "") -> None:
    """Tell the operator which connectors charmer could NOT restart."""
    how = ("on each one's own host run `docker compose down && docker compose up -d` in its compose "
           "directory (more reliable than `restart`), or `systemctl restart <unit>` for a service install")
    hint = hint or ("To have charmer manage one (and restart it automatically next time), add its "
                    "host to newt_agents, or run `charmer provision <config> --only adopt_newt` "
                    "after a restore.")
    others = unmanaged_sites(ctx)
    if others is None:
        console.print(f"[yellow]{why}. Any Newt/site connector not in this config's newt_agents "
                      f"(added later from the dashboard, or carried over by a restore) was NOT "
                      f"restarted: {how}.[/yellow]")
        return
    if not others:
        return
    names = ", ".join(repr(n) for _, n in others)
    console.print(f"[yellow]{why}. {len(others)} site(s) are not managed by charmer and were NOT "
                  f"restarted: {names}. {how[0].upper() + how[1:]}. {hint}[/yellow]")


def redial(ctx: PhaseContext, conn) -> None:
    """Recreate `conn`'s existing newt container so it redials gerbil's new
    WireGuard process. No credentials touched, no compose file rewritten:
    an agent with no bundle yet is skipped, one that fails to come back is
    a warning, never a phase failure."""
    node = conn.name
    if not conn.run(f"test -f {NEWT_DIR}/docker-compose.yml").ok:
        ctx.record(node, "newt redial", True, f"no {NEWT_DIR} bundle here yet, nothing to restart")
        return
    # down + up, not `restart`: recreating the container is what reliably
    # reproduces the fresh connection gerbil just went through (README "restore").
    ctx.begin(node, "recreating newt", "down + up, to redial gerbil's restarted WireGuard process")
    r = conn.run(f"cd {NEWT_DIR} && docker compose down && docker compose up -d", timeout=120)
    if not r.ok:
        ctx.record(node, "newt redial", False, r.err, warn=True)
        return
    stable = wait_for(conn, f"cd {NEWT_DIR} && docker compose ps newt --format '{{{{.State}}}}'",
                      expect="running", timeout=60, interval=3)
    ctx.record(node, "newt redial", stable,
               "" if stable else "did not return to running; check docker compose logs newt on the agent",
               warn=not stable)


def redial_all(ctx: PhaseContext, why: str, hint: str = "") -> None:
    for conn in ctx.agents:
        redial(ctx, conn)
    announce_unmanaged(ctx, why, hint)


def gerbil_marker(conn) -> str:
    """Container id + state of gerbil: comparing it before/after a compose
    call tells whether gerbil was (re)started by it."""
    return conn.run("cd /opt/pangolin && docker compose ps -a gerbil --format '{{.ID}} {{.State}}'").out.strip()


def gerbil_bounced(before: str, after: str) -> bool:
    """Gerbil is running now as a different container, or wasn't running
    before. Not running now means there's nothing to redial to yet."""
    return before != after and after.endswith(" running")
