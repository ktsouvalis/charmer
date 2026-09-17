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
handles.
"""

from __future__ import annotations

import getpass

from ..pangolin_api import PangolinAPIError, create_newt_site, pick_site_defaults
from ..remote import push_file, read_pangolin_setup_token, render, wait_for
from .base import Phase, PhaseContext, console


class NewtPhase(Phase):
    name = "newt"

    def enabled(self, ctx: PhaseContext) -> bool:
        if not ctx.cfg.newt_agents:
            return False
        if ctx.restore_ran:
            console.print(
                "[yellow]skipping newt: the restore phase just replaced Postgres with a dump that "
                "may already contain sites for these agents. Check Server Admin -> Sites on the "
                "dashboard: for any agent that genuinely needs a fresh site, clear its "
                "newt_id_<agent>/newt_secret_<agent> from state and run "
                "`charmer provision <config> --only newt`.[/yellow]"
            )
            return False
        return True

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
        lines = [
            f"mint Pangolin site credentials for {len(agents)} agent(s) via the integration API "
            "(loopback-only on the Pangolin host, see pangolin_api.py); credentials already "
            "pinned in state from a previous run are reused, never re-minted",
        ]
        if "pangolin_root_api_key" not in ctx.state.data["generated"]:
            lines.append("Root API key not yet pinned: you will be asked once (hidden input); "
                        "needs at least the 'Create Site' permission")
        if "pangolin_org_id" not in ctx.state.data["generated"]:
            lines.append("organization ID not yet pinned: you will be asked once")
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
        self._announce_prereqs(ctx)
        root_key = self._root_key(ctx)  # may prompt, before any agent is touched
        org_id = self._org_id(ctx)
        skip_tls_verify = ctx.cfg.tls.provider == "self_signed"

        for agent_cfg, conn in zip(ctx.cfg.newt_agents, ctx.agents):
            node = conn.name
            id_key = f"newt_id_{agent_cfg.name}"
            secret_key = f"newt_secret_{agent_cfg.name}"

            if id_key in ctx.state.data["generated"]:
                newt_id = ctx.state.data["generated"][id_key]
                newt_secret = ctx.state.data["generated"][secret_key]
                ctx.record(node, "pangolin site credentials", True, "reusing previously minted credentials")
            else:
                try:
                    defaults = pick_site_defaults(ctx.host, root_key, org_id)
                    newt_id = defaults["newtId"]
                    newt_secret = defaults["newtSecret"]
                    site = create_newt_site(ctx.host, root_key, org_id, agent_cfg.name, newt_id, newt_secret)
                except PangolinAPIError as exc:
                    ctx.record(node, "pangolin site created", False, str(exc))
                    raise RuntimeError(f"minting credentials for {agent_cfg.name} failed: {exc}") from exc
                ctx.state.data["generated"][id_key] = newt_id
                ctx.state.data["generated"][secret_key] = newt_secret
                ctx.state.save()
                ctx.record(node, "pangolin site created", True, f"site id {site.get('siteId', '?')}")

            compose = self._render(agent_cfg, ctx.cfg.base_url, newt_id, newt_secret, skip_tls_verify)
            conn.run("mkdir -p /opt/newt", sudo=True)
            changed = push_file(conn, compose, "/opt/newt/docker-compose.yml", mode="0600")

            running = conn.run("cd /opt/newt && docker compose ps --status running --format '{{.Name}}'").out
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
            r = conn.run("cd /opt/newt && docker compose ps newt --format '{{.State}}'")
            running = r.out.strip() == "running"
            ctx.record(conn.name, "verify: newt container running", running, r.out or r.err)
            ok = ok and running
        return ok
