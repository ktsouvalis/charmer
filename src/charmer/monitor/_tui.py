"""The Textual App behind `charmer monitor`. Split out of dashboard.py so
that importing dashboard.py (e.g. from `charmer logs`, which shares its
config loader) never requires Textual's screen machinery to spin up in a
non-tty context; `dashboard.run()` only imports this module on the
interactive path.
"""

from __future__ import annotations

import locale
import os
from datetime import datetime

from textual import work
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Footer, Static

from .dashboard import AgentStatus, HostStatus, MonitorNode, _gather


def _terminal_supports_unicode() -> bool:
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = os.environ.get(var, "")
        if val and "utf" in val.lower():
            return True
    try:
        return "utf" in (locale.getpreferredencoding(False) or "").lower()
    except Exception:  # noqa: BLE001
        return False


_BULLET = "●" if _terminal_supports_unicode() else "*"
OK = f"[bold green]{_BULLET}[/]"
DOWN = f"[bold red]{_BULLET}[/]"
WARN = f"[bold yellow]{_BULLET}[/]"
GREY = f"[dim white]{_BULLET}[/]"


def failures_to_dot(failures: int) -> str:
    if failures >= 2:
        return DOWN
    if failures >= 1:
        return WARN
    return OK


CSS = """
Screen {
    background: #0d1117;
    color: #e6edf3;
}

#title {
    content-align: center middle;
    background: #161b22;
    color: #58a6ff;
    text-style: bold;
    height: 1;
    padding: 0 2;
}

#statusbar {
    height: 1;
    content-align: center middle;
    padding: 0 2;
    margin-bottom: 1;
}

.panel {
    border: solid #30363d;
    border-title-color: #58a6ff;
    border-title-style: bold;
    padding: 0 1;
    margin: 0 1 1 1;
    height: auto;
    background: #161b22;
    width: 1fr;
}

Footer {
    background: #161b22;
    color: #8b949e;
}
"""


class HostPanel(Static):
    data: reactive[list] = reactive(list)

    def render_content(self) -> str:
        if not self.data:
            return "  [dim]Checking...[/]"
        lines: list[str] = []
        for host in self.data:
            host_dot = OK if host.ok else DOWN
            host_color = "green" if host.ok else "red"
            lines.append(f"  {host_dot} [bold {host_color}]{host.name}[/]")
            for svc in host.services:
                if svc.up is None:
                    dot, color, word = GREY, "dim white", "absent"
                elif svc.up:
                    dot, color, word = OK, "green", "up"
                else:
                    dot, color, word = DOWN, "red", "down"
                detail = f"  [dim]{svc.detail}[/]" if svc.detail else ""
                lines.append(f"      {dot} [{color}]{svc.name:<10}[/] {word}{detail}")
        return "\n".join(lines)

    def watch_data(self, data: list) -> None:
        self.update(self.render_content())


class AgentPanel(Static):
    data: reactive[list] = reactive(list)

    def render_content(self) -> str:
        if not self.data:
            return "  [dim]Checking...[/]"
        lines = []
        for agent in self.data:
            if agent.up:
                lines.append(f"  {OK} [bold green]{agent.name:<20}[/] [green]UP[/]  [dim]{agent.detail}[/]")
            else:
                lines.append(f"  {DOWN} [bold red]{agent.name:<20}[/] [red]DOWN[/]  [dim]{agent.detail}[/]")
        return "\n".join(lines)

    def watch_data(self, data: list) -> None:
        self.update(self.render_content())


class StatusBar(Static):
    last_refresh: reactive[str] = reactive("")
    status_dot: reactive[str] = reactive(GREY)

    def __init__(self, *args, interval: int, config_path: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._interval = interval
        self._config_path = config_path

    def render_content(self) -> str:
        ts = self.last_refresh or "n/a"
        return (
            f"  {self.status_dot}    "
            f"[dim]Last refresh: {ts}   "
            f"Auto-refresh: {self._interval}s   "
            f"Config: {self._config_path}[/]"
        )

    def watch_last_refresh(self, _: str) -> None:
        self.update(self.render_content())

    def watch_status_dot(self, _: str) -> None:
        self.update(self.render_content())


def build_app(cfg: dict, config_path: str, pangolin_nodes: list[MonitorNode],
              newt_nodes: list[MonitorNode], interval: int) -> App:
    site = cfg.get("site", "charmer")
    dashboard_url = cfg.get("dashboard_url", "")
    title_text = f"{site}: {dashboard_url}" if dashboard_url else site

    class DashboardApp(App):
        CSS = CSS
        TITLE = title_text
        BINDINGS = [
            ("r", "refresh_now", "Refresh"),
            ("q", "quit", "Quit"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._refreshing = False

        def compose(self) -> ComposeResult:
            yield Static(f"  {GREY}  {title_text}", id="title")
            yield StatusBar(id="statusbar", interval=interval, config_path=config_path)
            yield HostPanel("  [dim]Checking...[/]", id="panel-host", classes="panel")
            if newt_nodes:
                yield AgentPanel("  [dim]Checking...[/]", id="panel-newt", classes="panel")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#panel-host").border_title = f" {GREY}  PANGOLIN HOST  "
            if newt_nodes:
                self.query_one("#panel-newt").border_title = f" {GREY}  NEWT AGENTS  "
            self.set_interval(interval, self.action_refresh_now)
            self.action_refresh_now()

        @work(thread=True)
        def action_refresh_now(self) -> None:
            # Coarse lock: skip an overlapping tick rather than running two
            # SSH checks against the same NodeConn from different threads at
            # once (paramiko clients aren't safe for that).
            if self._refreshing:
                return
            self._refreshing = True
            try:
                hosts, agents = _gather(pangolin_nodes, newt_nodes)
                ts = datetime.now().strftime("%H:%M:%S")
                self.call_from_thread(self._apply_updates, hosts, agents, ts)
            finally:
                self._refreshing = False

        def _apply_updates(self, hosts: list[HostStatus], agents: list[AgentStatus], ts: str) -> None:
            self.query_one("#panel-host", HostPanel).data = hosts
            host_fail = sum(h.failed for h in hosts)
            self.query_one("#panel-host").border_title = f" {failures_to_dot(host_fail)}  PANGOLIN HOST  "

            agent_fail = 0
            if newt_nodes:
                self.query_one("#panel-newt", AgentPanel).data = agents
                agent_fail = sum(1 for a in agents if not a.up)
                self.query_one("#panel-newt").border_title = f" {failures_to_dot(agent_fail)}  NEWT AGENTS  "

            central_dot = failures_to_dot(host_fail + agent_fail)
            self.query_one("#title", Static).update(f"  {central_dot}  {title_text}")
            sb = self.query_one("#statusbar", StatusBar)
            sb.status_dot = central_dot
            sb.last_refresh = ts

    return DashboardApp()
