"""adopt_newt: after a restore, bring the old installation's site connectors
under charmer, asking the operator for each host.

A restored database carries sites whose connectors (Newt, or since
Pangolin 1.23 the Pangolin CLI's `up site`) run on hosts this config has
never heard of. Their SSH details aren't in the database, so they come from
the operator, the same questions `charmer init` asks per agent (name, IP,
SSH). For each host charmer connects, finds the connector container
(fosrl/newt or fosrl/pangolin-cli, newt_ops.discover_connectors()), reads
its newtId/secret out of `docker inspect`, and checks them against this
Pangolin the way the connector itself does (get-token). Only credentials
this Pangolin accepts can be adopted, after a y/N per host.

Adopting pins the credentials in state (newt_id_<name>/newt_secret_<name>,
exactly what `newt` would have minted) and appends the agent to the config
file (the config stays the record; comments are kept, the result is
re-validated and rolled back if it doesn't load). It also adds the host to
this run's fleet. Nothing on the agent changes here: `newt`, next, replaces
the old container with charmer's bundle under the same credentials, so the
site, its resources and its targets stay as they are. No re-mint.

A connector charmer can't read (a systemd service, a Newt reading a
CONFIG_FILE) or whose credentials this Pangolin rejects is reported and
skipped. Hosts added here skipped preflight/base; `--replay preflight base`
covers them.
"""

from __future__ import annotations

import ipaddress
import re
import time

from ..config import ConfigError
from ..init_wizard import _ask, _ask_ssh, _ask_yn
from ..sshexec import NodeConn, prompt_node_credentials
from .base import Phase, PhaseContext, console
from .newt_ops import (NEWT_DIR, check_credentials, connector_units, discover_connectors,
                       image_tag, managed_newt_ids, newt_sites, unmanaged_sites,
                       write_agents_to_config)

DEFAULT_NEWT_TAG = "1.17.0"


def agent_name_for(site_name: str) -> str:
    """A config-friendly default agent name from a Pangolin site name."""
    slug = re.sub(r"[^a-z0-9-]+", "-", site_name.lower()).strip("-")
    return slug or "newt"


class AdoptNewtPhase(Phase):
    name = "adopt_newt"
    optional = True

    def __init__(self):
        self._adopted: list[str] = []

    def enabled(self, ctx: PhaseContext) -> bool:
        # restore_at is pinned only when a dump was actually loaded; restore
        # resets this phase to pending on every load (restore_phase.py).
        return ctx.restore_ran or "restore_at" in ctx.state.data["generated"]

    def plan(self, ctx: PhaseContext) -> list[str]:
        others = unmanaged_sites(ctx)
        if others is None:
            sites = "the restored database's site list couldn't be read"
        elif not others:
            sites = "every Newt site in the restored database is already managed by charmer"
        else:
            sites = (f"{len(others)} site(s) in the restored database not managed by charmer: "
                     + ", ".join(repr(n) for _, n in others))
        return [
            sites,
            "ask whether the old installation had site connectors to take over; per host: name, IP, "
            "SSH (the same questions as `charmer init`), nothing taken from the database",
            "connect, find the fosrl/newt or fosrl/pangolin-cli container, and check its newtId/secret "
            "against this Pangolin (get-token, loopback); ask before adopting each one",
            f"adopting: pin the credentials in state and append the agent to "
            f"{ctx.config_path or 'the config file'} (comments kept, re-validated); nothing on the "
            f"agent changes yet: `newt` then swaps the old container for charmer's {NEWT_DIR} bundle, "
            "same site, no re-mint",
        ]

    def apply(self, ctx: PhaseContext) -> None:
        if ctx.config_path is None:
            raise RuntimeError("adopt_newt needs the config file path to record adopted agents")
        self._adopted = []
        others = unmanaged_sites(ctx)
        remaining = [n for _, n in others or []]
        if remaining:
            console.print("[bold]sites from the old installation that charmer doesn't manage:[/bold] "
                          + ", ".join(repr(n) for n in remaining))
        while _ask_yn("Add a host running a site connector from the old installation",
                      default=bool(remaining)):
            site = self._onboard(ctx, remaining)
            if site in remaining:
                remaining.remove(site)
        if remaining:
            console.print("[yellow]still not managed by charmer: "
                          + ", ".join(repr(n) for n in remaining)
                          + ". Their connectors need a manual restart whenever gerbil restarts; "
                          "`charmer provision <config> --only adopt_newt` asks again.[/yellow]")
        if self._adopted:
            console.print(f"[dim]{', '.join(self._adopted)} skipped preflight/base: run "
                          "`charmer provision <config> --replay preflight base` to check and harden "
                          "them like the other hosts.[/dim]")

    def _onboard(self, ctx: PhaseContext, remaining: list[str]) -> str | None:
        """Ask for one host, adopt its connector. Returns the adopted site's
        name, or None if nothing was adopted (every reason is reported)."""
        cfg = ctx.cfg
        taken_names = {a.name for a in cfg.newt_agents}
        taken_ips = {a.ip for a in cfg.newt_agents} | {cfg.host_ip}

        default = agent_name_for(remaining[0]) if remaining else f"newt-{len(cfg.newt_agents) + 1}"
        name = _ask("  name (charmer's label for this host; the Pangolin site keeps its own name)", default)
        while not name or name in taken_names:
            name = _ask(f"  name ({name!r} is already a configured agent)" if name else "  name")
        ip = _ask("  IP")
        while not _is_ip(ip) or ip in taken_ips:
            ip = _ask("  IP (a valid address, not one of the configured hosts)")
        ssh_raw = _ask_ssh(f"  {name}")

        from ..config import _validate_ssh
        problems: list[str] = []
        ssh = _validate_ssh(ssh_raw, f"{name}.ssh", problems)
        if problems:
            ctx.record(name, "ssh settings", False, "; ".join(problems), warn=True)
            return None
        password, sudo_password = prompt_node_credentials(ip, ssh)
        conn = NodeConn(name, ip, ssh, password, sudo_password)
        conn.fleet = ctx.fleet
        try:
            conn.connect()
        except Exception as exc:  # noqa: BLE001
            ctx.record(name, "ssh", False, f"cannot reach {ip}: {exc}", warn=True)
            return None
        if conn.run("id -u").out != "0" and ssh.become and not conn.run("true", sudo=True).ok:
            ctx.record(name, "sudo", False, "sudo check failed", warn=True)
            conn.close()
            return None

        found = self._pick(ctx, conn)
        if found is None:
            conn.close()
            return None
        creds = found["creds"]
        site = dict(newt_sites(ctx) or []).get(creds["newt_id"], "?")
        endpoint_note = ""
        if creds["endpoint"] and creds["endpoint"].rstrip("/") != cfg.base_url:
            endpoint_note = f"; its endpoint {creds['endpoint']} becomes {cfg.base_url}"
        kind_note = " (a Pangolin CLI container: charmer runs fosrl/newt instead, same credentials)" \
            if found["kind"] == "cli" else ""
        console.print(f"  found {found['name']} ({found['image']}, {found['state']}), site {site!r}, "
                      f"credentials accepted by this Pangolin{kind_note}{endpoint_note}")

        old_tag = image_tag(found["image"]) if found["kind"] == "newt" else None
        tag = _ask("  Newt image tag charmer will pin (not 'latest')",
                   old_tag if old_tag not in (None, "latest") else DEFAULT_NEWT_TAG)
        while tag in ("", "latest"):
            tag = _ask("  Newt image tag (a pinned version)", DEFAULT_NEWT_TAG)
        if not _ask_yn(f"  Adopt it as agent {name!r}? `newt` will replace {found['name']} with "
                       f"charmer's bundle, same site", default=True):
            conn.close()
            return None

        agent = {"name": name, "ip": ip, "ssh": ssh_raw, "image_tag": tag,
                 "tun_device": "/dev/net/tun", "docker_socket": creds["docker_socket"]}
        try:
            new_cfg = write_agents_to_config(ctx.config_path, [agent])
        except ConfigError as exc:
            ctx.record(name, "added to config", False, "; ".join(exc.problems), warn=True)
            conn.close()
            return None

        g = ctx.state.data["generated"]
        g[f"newt_id_{name}"] = creds["newt_id"]
        g[f"newt_secret_{name}"] = creds["secret"]
        ctx.state.data.setdefault("adopted", {})[name] = {
            "container": found["id"], "container_name": found["name"],
            "working_dir": found["working_dir"], "image": found["image"],
            "site": site, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        ctx.state.save()
        cfg.newt_agents.append(next(a for a in new_cfg.newt_agents if a.name == name))
        ctx.fleet.conns.append(conn)
        self._adopted.append(name)
        ctx.record(name, "adopted", True, f"site {site!r}, credentials pinned, added to {ctx.config_path}")
        return site

    def _pick(self, ctx: PhaseContext, conn) -> dict | None:
        """The connector on `conn` whose credentials this Pangolin accepts
        and charmer doesn't already manage; asks if there's more than one."""
        node = conn.name
        found = discover_connectors(conn)
        if not found:
            units = connector_units(conn)
            detail = "no fosrl/newt or fosrl/pangolin-cli container on this host"
            if units:
                detail += (f"; systemd unit(s) {', '.join(units)} look like a service install, which "
                           "charmer can't read credentials from: stop/disable it and add the agent "
                           "by hand, or run the connector in Docker first")
            ctx.record(node, "site connector", False, detail, warn=True)
            return None

        managed = managed_newt_ids(ctx)
        usable = []
        for f in found:
            if not f["creds"]:
                ctx.record(node, f"{f['name']} ({f['image']})", False,
                           "no NEWT_ID/NEWT_SECRET or SITE_ID/SITE_SECRET in its env/args", warn=True)
            elif f["creds"]["newt_id"] in managed:
                ctx.record(node, f"{f['name']} ({f['image']})", False,
                           "its credentials already belong to a configured agent", warn=True)
            else:
                verdict = check_credentials(ctx, f["creds"]["newt_id"], f["creds"]["secret"])
                if verdict:
                    usable.append(f)
                else:
                    ctx.record(node, f"{f['name']} ({f['image']})", False,
                               "this Pangolin rejects its credentials (a site the restored database "
                               "doesn't have)" if verdict is False else
                               "couldn't check its credentials: pangolin didn't answer get-token", warn=True)
        if not usable:
            return None
        if len(usable) == 1:
            return usable[0]
        for i, f in enumerate(usable, 1):
            console.print(f"  {i}. {f['name']} ({f['image']}, {f['state']})")
        choice = _ask("  which one", "1")
        while not (choice.isdigit() and 1 <= int(choice) <= len(usable)):
            choice = _ask(f"  which one (1-{len(usable)})", "1")
        return usable[int(choice) - 1]

    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        g = ctx.state.data["generated"]
        for name in self._adopted:
            accepted = check_credentials(ctx, g[f"newt_id_{name}"], g[f"newt_secret_{name}"])
            ctx.record(name, "verify: adopted credentials accepted by pangolin", accepted is not False,
                       "" if accepted else ("rejected" if accepted is False else "couldn't check"),
                       warn=accepted is None)
            ok = ok and accepted is not False
        return ok


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False
