"""charmer monitor: real-time TUI dashboard for one Pangolin host + its
Newt agents.

Built on Textual, in the same visual idiom as this rebuild's predecessor
(ktsouvalis/pangolin's monitor.py: dark panel-and-dot layout, live-updating
border titles, a status bar, r/q bindings), adapted to charmer's own
checks. Deliberately smaller in scope than that predecessor's HA-cluster
dashboard: charmer's whole footprint is one host plus a handful of agents,
and, unlike akropolis's cluster, whose ak-monitor deliberately runs from a
separate machine with only HTTP reachability, the operator running this
already has the same SSH access `provision` used. So every check here goes
straight over SSH (`docker inspect`) instead of opening extra public HTTP
ports (nginx stub_status, HAProxy stats) purely for a dashboard to poll;
one less thing exposed to the internet, and one less UFW rule to keep in
sync.

Reads `config.<site>.monitor.yml`, emitted by the `handoff` phase, not the
site's own `config.<site>.yml`.

`pangolin_nodes` is built as a single-element list even though charmer
provisions exactly one Pangolin host today: every panel/check below
iterates it rather than assuming len() == 1, so a future multi-node
Pangolin rollout (HA is a roadmap line, see CLAUDE.md/README) is a
config-loader change, not a UI rewrite. `newt_agents` is already a list for
the same reason, since charmer supports many Newt agents per host today.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from ..config import SSHTarget
from ..sshexec import NodeConn, prompt_node_credentials

DEFAULT_INTERVAL = 15
CONTAINERS = ("pangolin", "gerbil", "traefik", "postgres")


def load_monitor_config(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def _ssh_target(d: dict) -> SSHTarget:
    return SSHTarget(user=d["user"], auth=d["auth"], key_file=d.get("key_file"),
                     port=int(d.get("port", 22)), become=bool(d.get("become", True)))


@dataclass
class MonitorNode:
    name: str
    conn: NodeConn


def _gather_credentials(cfg: dict) -> dict[str, tuple[str | None, str | None]]:
    """Prompt once per distinct host for whatever SSH/sudo password its
    NodeConn will need, same accounts `charmer provision` already
    authenticated against, but the monitor config never carries secrets, so
    this session has to ask again instead of assuming passwordless sudo.
    Done up front, before the Textual app takes over the terminal, since
    getpass needs a plain stdin/stdout it won't have once the TUI screen is
    active."""
    creds: dict[str, tuple[str | None, str | None]] = {}
    creds["pangolin-host"] = prompt_node_credentials("pangolin-host", _ssh_target(cfg["pangolin"]["ssh"]))
    for agent in cfg.get("newt_agents", []):
        creds[agent["name"]] = prompt_node_credentials(agent["name"], _ssh_target(agent["ssh"]))
    return creds


def build_nodes(cfg: dict, creds: dict[str, tuple[str | None, str | None]]) -> tuple[list[MonitorNode], list[MonitorNode]]:
    host_password, host_sudo_password = creds["pangolin-host"]
    pangolin_nodes = [MonitorNode(
        name="pangolin-host",
        conn=NodeConn("pangolin-host", cfg["pangolin"]["ip"], _ssh_target(cfg["pangolin"]["ssh"]),
                      host_password, host_sudo_password),
    )]
    newt_nodes = [
        MonitorNode(name=agent["name"],
                    conn=NodeConn(agent["name"], agent["ip"], _ssh_target(agent["ssh"]), *creds[agent["name"]]))
        for agent in cfg.get("newt_agents", [])
    ]
    return pangolin_nodes, newt_nodes


def _check(conn: NodeConn, cmd: str) -> tuple[bool, str]:
    try:
        r = conn.run(cmd, timeout=10)
        return r.ok, (r.out or r.err).strip()
    except Exception as exc:  # noqa: BLE001
        # Force a fresh handshake on the next check instead of retrying a
        # connection that may be half-dead (dropped socket, expired auth).
        conn.close()
        return False, str(exc)


@dataclass
class ServiceStatus:
    name: str
    up: bool | None  # None = absent / check produced no detail
    detail: str = ""


@dataclass
class HostStatus:
    name: str
    services: list[ServiceStatus] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # `up is None` means "absent" (e.g. no postgres container in
        # SQLite lab mode), expected, not a failure. Only `up is False`
        # (checked and found down) counts against the host.
        return all(s.up is not False for s in self.services)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.services if s.up is False)


@dataclass
class AgentStatus:
    name: str
    up: bool
    detail: str = ""


def check_pangolin_host(node: MonitorNode) -> HostStatus:
    services = []
    for svc in CONTAINERS:
        ok, detail = _check(
            node.conn,
            f"docker inspect --format '{{{{.State.Health.Status}}}}' {svc} 2>/dev/null || "
            f"docker inspect --format '{{{{.State.Status}}}}' {svc} 2>/dev/null")
        if not ok or not detail:
            services.append(ServiceStatus(svc, None, "absent" if ok else detail))
            continue
        services.append(ServiceStatus(svc, detail in ("healthy", "running"), detail))
    return HostStatus(node.name, services)


def check_newt_agent(node: MonitorNode) -> AgentStatus:
    ok, detail = _check(node.conn, "cd /opt/newt && docker compose ps newt --format '{{.State}}'")
    running = ok and detail.strip() == "running"
    return AgentStatus(node.name, running, detail or "unreachable")


def _gather(pangolin_nodes: list[MonitorNode], newt_nodes: list[MonitorNode]) -> tuple[list[HostStatus], list[AgentStatus]]:
    with ThreadPoolExecutor(max_workers=max(1, len(pangolin_nodes) + len(newt_nodes))) as ex:
        host_futs = [ex.submit(check_pangolin_host, n) for n in pangolin_nodes]
        agent_futs = [ex.submit(check_newt_agent, n) for n in newt_nodes]
        hosts = [f.result() for f in host_futs]
        agents = [f.result() for f in agent_futs]
    return hosts, agents


# ---------------------------------------------------------------------------
# Non-interactive fallback: `--once`, or stdout isn't a terminal. Textual
# needs a real tty; CI/pipe usage gets a single plain snapshot instead.
# ---------------------------------------------------------------------------

def render_snapshot_table(cfg: dict, hosts: list[HostStatus], agents: list[AgentStatus]) -> Table:
    table = Table(title=f"{cfg.get('site', 'charmer')}: {cfg.get('dashboard_url', '')}")
    table.add_column("Host")
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("Detail")
    for host in hosts:
        for svc in host.services:
            if svc.up is None:
                status = "[dim]absent[/dim]"
            else:
                status = "[green]UP[/green]" if svc.up else "[red]DOWN[/red]"
            table.add_row(host.name, svc.name, status, svc.detail)
    for agent in agents:
        status = "[green]UP[/green]" if agent.up else "[red]DOWN[/red]"
        table.add_row(agent.name, "newt", status, agent.detail)
    return table


def run(config_path: str, once: bool = False, interval: int = DEFAULT_INTERVAL) -> None:
    cfg = load_monitor_config(config_path)
    creds = _gather_credentials(cfg)
    pangolin_nodes, newt_nodes = build_nodes(cfg, creds)

    console = Console()
    if once or not console.is_terminal:
        hosts, agents = _gather(pangolin_nodes, newt_nodes)
        console.print(render_snapshot_table(cfg, hosts, agents))
        for node in (*pangolin_nodes, *newt_nodes):
            node.conn.close()
        return

    from ._tui import build_app
    app = build_app(cfg, config_path, pangolin_nodes, newt_nodes, interval)
    try:
        app.run()
    finally:
        for node in (*pangolin_nodes, *newt_nodes):
            node.conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="charmer monitor")
    parser.add_argument("config", help="config.<site>.monitor.yml (emitted by `charmer provision`'s handoff phase)")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = parser.parse_args(argv)
    run(args.config, once=args.once, interval=args.interval)
    return 0
