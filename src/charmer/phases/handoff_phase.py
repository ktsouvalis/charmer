"""handoff: the last phase, and the only one besides preflight that touches
nothing on any host. Read-only, never prompts: prints the landing card and
emits `config.<site>.monitor.yml` on the workstation for `charmer monitor`/
`charmer logs`, filled from the site config + pinned state so the two tools
never drift from what was actually provisioned.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..remote import read_pangolin_setup_token
from .base import Phase, PhaseContext


class HandoffPhase(Phase):
    name = "handoff"
    read_only = True

    def plan(self, ctx: PhaseContext) -> list[str]:
        return [
            "print the dashboard URL and next steps (no secrets)",
            f"emit config.{ctx.cfg.name}.monitor.yml for `charmer monitor`/`charmer logs`",
        ]

    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        monitor_cfg = {
            "site": cfg.name,
            "dashboard_url": cfg.base_url,
            "pangolin": {
                "ip": cfg.host_ip,
                "ssh": {"user": cfg.ssh.user, "auth": cfg.ssh.auth,
                        "key_file": cfg.ssh.key_file, "port": cfg.ssh.port, "become": cfg.ssh.become},
            },
            "newt_agents": [
                {"name": a.name, "ip": a.ip,
                 "ssh": {"user": a.ssh.user, "auth": a.ssh.auth,
                         "key_file": a.ssh.key_file, "port": a.ssh.port, "become": a.ssh.become}}
                for a in cfg.newt_agents
            ],
        }
        path = Path(f"config.{cfg.name}.monitor.yml")
        path.write_text(yaml.safe_dump(monitor_cfg, sort_keys=False))
        path.chmod(0o600)
        ctx.state.data["generated"]["monitor_config_path"] = str(path)
        ctx.state.save()

        print(f"\ndashboard: {cfg.base_url}")
        token = read_pangolin_setup_token(ctx.host)
        if token:
            print(f"if you haven't already: complete first-run setup at /auth/initial-setup "
                  f"(one-time setup token: {token})")
        else:
            print("if you haven't already: complete first-run setup at /auth/initial-setup "
                  "(needs the one-time setup token Pangolin printed to its own logs on first boot; "
                  "not found just now, already used, or `sudo docker compose logs pangolin` on the "
                  "host to look it up)")
        print("external identity provider (optional, dashboard-only; no config.yml key for this): "
              "Server Admin -> Identity Providers -> Add Identity Provider")
        if cfg.newt_agents:
            print("Newt agent credentials were minted automatically via the integration API "
                 "(see the newt phase); nothing further needed per agent.")
        print(f"monitor config: {path}")
        print(f"next: charmer monitor {path}")
        print(f"      charmer logs {path}")

    def verify(self, ctx: PhaseContext) -> bool:
        raw_path = ctx.state.data["generated"].get("monitor_config_path", "")
        if not raw_path:
            return False
        path = Path(raw_path)
        if not path.exists():
            return False
        data = yaml.safe_load(path.read_text()) or {}
        return bool(data.get("pangolin")) and "dashboard_url" in data
