"""newt. Provision every configured Newt agent: mint its Pangolin site
credentials via the integration API, install and start its Docker Compose
bundle.

Credential minting runs once per agent, ever: `pick-site-defaults` + `PUT
.../site` (pangolin_api.py) are called only the first time an agent's
credentials aren't already pinned in state; a re-run (or --replay) never
re-creates the site or rotates its secret out from under a running tunnel.

The Pangolin Root API Key and organization ID are the one irreducible
manual step (see pangolin_api.py's module docstring and README "Newt
credential automation"): prompted once, hidden, pinned in state, never
written to the config file, same treatment as any other secret charmer
handles. They're asked for only when an agent actually needs a new site.

Credentials already pinned (minted earlier, or adopted by adopt_newt) are
checked against Pangolin first (get-token, newt_ops.check_credentials()):
after a restore they may belong to a database that's gone. A rejected
pair, or an unpinned agent right after a restore (whose dump may already
hold a site for it), is only (re)minted after a y/N; declining skips that
agent. An adopted agent's old connector container is removed just before
charmer's bundle starts, so two connectors never run with one identity.
"""

from __future__ import annotations

import getpass
import time

from ..init_wizard import _ask_yn
from ..pangolin_api import PangolinAPIError, create_newt_site, pick_site_defaults
from ..remote import push_file, read_pangolin_setup_token, render, wait_for
from .base import Phase, PhaseContext, console
from .newt_ops import NEWT_DIR, check_credentials


class NewtPhase(Phase):
    name = "newt"

    def __init__(self):
        self._skipped: set[str] = set()

    def enabled(self, ctx: PhaseContext) -> bool:
        return bool(ctx.cfg.newt_agents)

    # ------------------------------------------------------------------ util
    def _root_key(self, ctx: PhaseContext) -> str:
        def ask() -> str:
            v = getpass.getpass(
                "Pangolin Root API key (Server Admin -> API Keys on the dashboard, "
                "after completing /auth/initial-setup; needs at least the 'Create Site' "
                "permission: everything this phase does over the integration API needs it; "
                "hidden, pinned in state): ")
            while not v:
                v = getpass.getpass("  (required) Root API key: ")
            return v
        return ctx.state.get_or_generate("pangolin_root_api_key", ask)

    def _org_id(self, ctx: PhaseContext) -> str:
        def ask() -> str:
            v = input("Pangolin organization ID (Organization Settings -> General on the dashboard): ").strip()
            while not v:
                v = input("  (required) organization ID: ").strip()
            return v
        return ctx.state.get_or_generate("pangolin_org_id", ask)

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        agents = ctx.cfg.newt_agents
        g = ctx.state.data["generated"]
        unpinned = [a.name for a in agents if f"newt_id_{a.name}" not in g]
        lines = [
            "credentials already pinned in state (minted earlier, or adopted by adopt_newt) are "
            "checked against Pangolin and reused, never re-minted; a rejected pair is re-minted "
            "only if you say so",
        ]
        if unpinned:
            lines.append(f"mint Pangolin site credentials for {', '.join(unpinned)} via the integration "
                         "API (loopback-only on the Pangolin host, see pangolin_api.py)"
                         + ("; a restore just ran, so you'll be asked per agent first (its dump may "
                            "already have a site for it)" if ctx.restore_ran else ""))
            if "pangolin_root_api_key" not in g:
                lines.append("Root API key not yet pinned: you will be asked once, only if a site gets "
                             "minted (hidden input); needs at least the 'Create Site' permission")
            if "pangolin_org_id" not in g:
                lines.append("organization ID not yet pinned: you will be asked once, only if a site "
                             "gets minted")
        for name, info in ctx.state.data.get("adopted", {}).items():
            if not info.get("retired") and any(a.name == name for a in agents):
                lines.append(f"{name}: remove the adopted connector container {info['container_name']} "
                             "just before charmer's bundle starts (same credentials, one connector)")
        if ctx.cfg.tls.provider == "self_signed":
            lines.append("tls: self_signed; every agent gets SKIP_TLS_VERIFY=true, since fosrl/newt's "
                        "TLS_CLIENT_CAS only takes effect alongside a client cert/key (mTLS), not "
                        "standalone for trusting a self-signed dashboard cert (see newt_phase.py)")
        for agent in agents:
            lines.append(f"install + start the Newt Compose bundle on {agent.name} ({agent.ip}, "
                        f"image pinned to {agent.image_tag})")
        return lines

    def _announce_prereqs(self, ctx: PhaseContext) -> None:
        missing = ("pangolin_root_api_key" not in ctx.state.data["generated"]
                   or "pangolin_org_id" not in ctx.state.data["generated"])
        if not missing:
            return
        step1 = f"open {ctx.cfg.base_url}/auth/initial-setup and create the server admin account"
        token = read_pangolin_setup_token(ctx.host)
        if token:
            step1 += f" (setup token, read off `docker compose logs pangolin` on the host: {token})"
        else:
            step1 += (" (needs the one-time setup token Pangolin printed to its own logs on first "
                      "boot; not found just now, already used, or `sudo docker compose logs "
                      "pangolin` on the host to look it up)")
        console.print(
            "[bold]Newt agents need a Pangolin org + Root API key; Pangolin CE has no way to seed "
            "these at deploy time, so this is a one-time manual step if you haven't done it yet:[/bold]\n"
            f"  1. {step1}\n"
            "  2. create (or open) an Organization, then note its ID under Organization Settings -> General\n"
            "  3. Server Admin -> API Keys -> generate a Root API Key, granting it at least the "
            "'Create Site' permission (everything this phase does over the integration API needs it)\n"
            "You'll be asked for the org ID and the key next (hidden input; pinned in state, "
            "never written to the config file)."
        )

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        self._skipped = set()
        skip_tls_verify = ctx.cfg.tls.provider == "self_signed"
        g = ctx.state.data["generated"]

        for agent_cfg, conn in zip(ctx.cfg.newt_agents, ctx.agents):
            node = conn.name
            id_key = f"newt_id_{agent_cfg.name}"
            secret_key = f"newt_secret_{agent_cfg.name}"

            ask_first = ""
            if id_key in g:
                accepted = check_credentials(ctx, g[id_key], g[secret_key])
                if accepted is False:
                    ask_first = (f"Pangolin rejects the credentials pinned for {agent_cfg.name} (its site "
                                 "was deleted, or a restore replaced the database)")
                else:
                    ctx.record(node, "pangolin site credentials", True, "reusing pinned credentials"
                               + ("" if accepted else " (couldn't check them: pangolin didn't answer)"))
            elif ctx.restore_ran:
                ask_first = (f"no credentials pinned for {agent_cfg.name}, and the restored database may "
                             "already have a site for it (adopt_newt takes over a running connector)")
            if ask_first and not _ask_yn(f"{ask_first}. Mint a NEW Pangolin site for it", default=False):
                ctx.record(node, "pangolin site credentials", False,
                           f"{ask_first}; not minted, agent skipped", warn=True)
                self._skipped.add(node)
                continue
            if ask_first or id_key not in g:
                newt_id, newt_secret = self._mint(ctx, agent_cfg.name, node)
            else:
                newt_id, newt_secret = g[id_key], g[secret_key]

            compose = self._render(agent_cfg, ctx.cfg.base_url, newt_id, newt_secret, skip_tls_verify)
            conn.run(f"mkdir -p {NEWT_DIR}", sudo=True)
            changed = push_file(conn, compose, f"{NEWT_DIR}/docker-compose.yml", mode="0600")
            self._retire_adopted(ctx, agent_cfg.name, conn)

            running = conn.run(f"cd {NEWT_DIR} && docker compose ps --status running --format '{{{{.Name}}}}'").out
            if not running or changed:
                ctx.begin(node, "docker compose up")
                r = conn.run("cd /opt/newt && docker compose up -d", timeout=300)
                ctx.record(node, "newt starting", r.ok, r.err if not r.ok else "")
                if not r.ok:
                    raise RuntimeError(f"{node}: docker compose up failed")

            stable = wait_for(conn, "cd /opt/newt && docker compose ps newt --format '{{.State}}'",
                              expect="running", timeout=60, interval=3)
            ctx.record(node, "newt container running", stable, "")
            if not stable:
                tail = conn.run("cd /opt/newt && docker compose logs --tail 40 newt").out
                print(f"\n--- newt ({node}, last 40 lines) ---\n{tail}\n")
                raise RuntimeError(f"{node}: newt container did not stay running")

    def _mint(self, ctx: PhaseContext, agent_name: str, node: str) -> tuple[str, str]:
        """Create a new Pangolin site for `agent_name`, pin and return its
        (newtId, secret). Asks for the Root API key / org ID if unpinned."""
        self._announce_prereqs(ctx)
        root_key = self._root_key(ctx)
        org_id = self._org_id(ctx)
        try:
            port = ctx.cfg.pangolin.integration_api_port
            defaults = pick_site_defaults(ctx.host, root_key, org_id, port=port)
            newt_id = defaults["newtId"]
            newt_secret = defaults["newtSecret"]
            site = create_newt_site(ctx.host, root_key, org_id, agent_name, newt_id, newt_secret, port=port)
        except PangolinAPIError as exc:
            ctx.record(node, "pangolin site created", False, str(exc))
            raise RuntimeError(f"minting credentials for {agent_name} failed: {exc}") from exc
        g = ctx.state.data["generated"]
        g[f"newt_id_{agent_name}"] = newt_id
        g[f"newt_secret_{agent_name}"] = newt_secret
        ctx.state.data.get("adopted", {}).pop(agent_name, None)  # a new identity, nothing to retire
        ctx.state.save()
        ctx.record(node, "pangolin site created", True, f"site id {site.get('siteId', '?')}")
        return newt_id, newt_secret

    def _retire_adopted(self, ctx: PhaseContext, agent_name: str, conn) -> None:
        """Remove the connector container adopt_newt took the credentials
        from, right before charmer's bundle starts with them. Only that one
        container: its compose project may hold other services."""
        info = ctx.state.data.get("adopted", {}).get(agent_name)
        if not info or info.get("retired"):
            return
        node = conn.name
        if info.get("working_dir") != NEWT_DIR:
            r = conn.run(f"docker rm -f {info['container']}", timeout=60)
            gone = r.ok or "No such container" in r.err
            ctx.record(node, f"adopted connector {info['container_name']} removed", gone, r.err if not gone else "")
            if not gone:
                raise RuntimeError(f"{node}: couldn't remove the adopted connector {info['container_name']}; "
                                   "two connectors with the same credentials would fight over the site")
            if info.get("working_dir"):
                ctx.record(node, "old compose file", False,
                           f"{info['working_dir']} still defines {info['container_name']}: remove it there, "
                           "or a `docker compose up` in that directory brings back a second connector "
                           "with the same credentials", warn=True)
        info["retired"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        ctx.state.save()

    def _render(self, agent_cfg, pangolin_endpoint: str, newt_id: str, newt_secret: str,
               skip_tls_verify: bool) -> str:
        return render("newt-compose.yml.j2", image_tag=agent_cfg.image_tag,
                      pangolin_endpoint=pangolin_endpoint, newt_id=newt_id, newt_secret=newt_secret,
                      tun_device=agent_cfg.tun_device, docker_socket=agent_cfg.docker_socket,
                      skip_tls_verify=skip_tls_verify)

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        for conn in ctx.agents:
            if conn.name in self._skipped:
                continue
            r = conn.run("cd /opt/newt && docker compose ps newt --format '{{.State}}'")
            running = r.out.strip() == "running"
            ctx.record(conn.name, "verify: newt container running", running, r.out or r.err)
            ok = ok and running
        return ok
