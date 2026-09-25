"""Phase framework.

Every phase runs plan -> confirm -> apply -> verify:

  plan    - describe exactly what will happen, before anything happens.
  confirm - lab: y/N; production: type the site name; read-only phases skip this.
  apply   - do it.
  verify  - health-gate; a phase that applies but fails verify is FAILED, and
            the runner stops (never proceeds onto an unhealthy foundation).

The runner is resumable: completed phases are skipped unless --replay.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from ..config import SiteConfig
from ..remote import wait_for
from ..sshexec import Fleet, NodeConn
from ..state import State

console = Console()


@dataclass
class Check:
    """One named check result inside a phase (used heavily by preflight)."""

    host: str
    name: str
    ok: bool
    detail: str = ""
    warn: bool = False  # ok=False + warn=True -> warning, not failure


@dataclass
class PhaseContext:
    cfg: SiteConfig
    state: State
    fleet: Fleet  # conns[0] is always the Pangolin host; conns[1:] are Newt agents, in config order
    checks: list[Check] = field(default_factory=list)
    # Set by restore_phase when it actually loads a dump (not the no-op /
    # declined case): newt_phase then asks before minting a site for an
    # agent with nothing pinned (the dump may already have one), and
    # adopt_newt runs. Scoped to this one `charmer provision` invocation.
    restore_ran: bool = False
    # The config file this run loaded; adopt_newt appends adopted agents to it.
    config_path: Path | None = None
    _status: object = field(default=None, repr=False)
    _status_text: str = field(default="", repr=False)

    @property
    def host(self) -> NodeConn:
        return self.fleet.conns[0]

    @property
    def agents(self) -> list[NodeConn]:
        return self.fleet.conns[1:]

    def agent_conn(self, name: str) -> NodeConn:
        for c in self.agents:
            if c.name == name:
                return c
        raise KeyError(f"no such newt agent: {name}")

    # --- live progress --------------------------------------------------------
    # A phase announces what it is ABOUT to do; record() reports how it went.
    # Without this, a 10-minute image pull looks exactly like a hang.
    def begin(self, host: str, name: str, detail: str = "") -> None:
        self.end_status()
        self._status_text = f"({host}) {name}" + (f" - {detail}" if detail else "")
        if console.is_terminal:
            try:
                self._status = console.status(f"[cyan]{self._status_text}[/cyan]", spinner="dots")
                self._status.start()
                return
            except Exception:  # noqa: BLE001 - fall through to the plain line
                self._status = None
        # piped/CI output: a live spinner renders nothing there
        console.print(f"  ... {self._status_text}", markup=False, highlight=False)

    def tick(self, detail: str) -> None:
        """Update the live line in place (e.g. elapsed/timeout during a wait)."""
        if self._status is not None:
            self._status.update(f"[cyan]{self._status_text} - {detail}[/cyan]")

    def end_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def record(self, host: str, name: str, ok: bool, detail: str = "", warn: bool = False) -> Check:
        self.end_status()
        c = Check(host=host, name=name, ok=ok, detail=detail, warn=warn)
        self.checks.append(c)
        mark = "[green]✔[/green]" if ok else ("[yellow]⚠[/yellow]" if warn else "[red]✘[/red]")
        console.print(f"  {mark} ({host}) {name}" + (f" - {detail}" if detail else ""), markup=True, highlight=False)
        return c


def verify_public_reachable(ctx: PhaseContext) -> bool:
    """End-to-end check that traefik/gerbil actually serve the public
    interface. A healthy `pangolin` container says nothing about traefik,
    since it depends only on pangolin's own healthcheck, not on gerbil/
    traefik (see pangolin-compose.yml.j2). In particular, traefik's
    `network_mode: service:gerbil` join can be refused by Docker outright if
    gerbil is mid-restart when traefik starts (Docker never migrates the
    netns later either); this is the check that catches a stack left
    silently half-up after that race, used by both the initial bring-up and
    a post-restore restart.
    """
    conn = ctx.host
    node = conn.name
    scheme = "https" if ctx.cfg.tls.provider != "none" else "http"
    port = 443 if scheme == "https" else 80
    curl = (f"curl -sk -o /dev/null -w '%{{http_code}}' --max-time 10 "
            f"{scheme}://{ctx.cfg.host_ip}:{port}/api/v1/")
    wait_for(conn, f'code=$({curl}); [ -n "$code" ] && [ "$code" != 000 ]',
             timeout=30, interval=3,
             tick=lambda elapsed: ctx.tick(f"waiting for the public interface ({int(elapsed)}s/30s)"))
    r = conn.run(curl)
    reached = r.out not in ("", "000")
    ctx.record(node, "verify: end-to-end over the public interface", reached, f"HTTP {r.out}")
    return reached


class Phase(ABC):
    name: str = "unnamed"
    read_only: bool = False
    # OPTIONAL phases are the ones the pipeline can legitimately run without:
    # declining them is a choice, not an abort (restore with no dump set is
    # the only one today). A REQUIRED phase declined still stops the runner.
    optional: bool = False

    def enabled(self, ctx: PhaseContext) -> bool:
        return True

    @abstractmethod
    def plan(self, ctx: PhaseContext) -> list[str]:
        """Return human-readable lines describing what apply() will do."""

    @abstractmethod
    def apply(self, ctx: PhaseContext) -> None: ...

    @abstractmethod
    def verify(self, ctx: PhaseContext) -> bool: ...

    def needs_confirm(self, ctx: PhaseContext) -> bool:
        return not self.read_only


def _confirm(cfg: SiteConfig, phase: Phase, ctx: PhaseContext) -> bool:
    if not phase.needs_confirm(ctx):
        return True
    if cfg.environment == "production":
        console.print(
            f"[bold red]PRODUCTION[/bold red] site [bold]{cfg.name}[/bold] - "
            f"type the site name to apply phase [bold]{phase.name}[/bold]:"
        )
        return input("> ").strip() == cfg.name
    answer = input(f"Apply phase '{phase.name}'? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def run_phases(phases: list[Phase], ctx: PhaseContext, replay: bool = False) -> bool:
    for phase in phases:
        if not phase.enabled(ctx):
            continue

        status = ctx.state.phase_status(phase.name)
        if status == "done" and not replay:
            console.print(f"[dim]phase {phase.name}: already done - skipping (use --replay to re-run)[/dim]")
            continue

        console.rule(f"phase: {phase.name}")
        ctx.checks.clear()
        ctx.fleet.current_phase = phase.name
        if ctx.fleet.transcript is not None:
            ctx.fleet.transcript.note(f"phase: {phase.name}")

        console.print("[bold]plan:[/bold]")
        for line in phase.plan(ctx):
            console.print(f"  • {line}")

        if not _confirm(ctx.cfg, phase, ctx):
            if phase.optional:
                console.print(f"[yellow]not confirmed - skipping optional phase '{phase.name}' and continuing.[/yellow]")
                ctx.state.mark_phase(phase.name, "skipped")
                continue
            console.print("[yellow]not confirmed - stopping.[/yellow]")
            ctx.state.mark_phase(phase.name, "declined")
            return False

        try:
            phase.apply(ctx)
        except Exception as exc:  # noqa: BLE001 - surface everything, then stop
            ctx.end_status()
            console.print(f"[red]apply failed:[/red] {exc}")
            ctx.state.mark_phase(phase.name, "failed", {"error": str(exc)})
            return False
        finally:
            ctx.end_status()

        if phase.verify(ctx):
            ctx.state.mark_phase(phase.name, "done")
            console.print(f"[green]phase {phase.name}: OK[/green]")
        else:
            ctx.end_status()
            ctx.state.mark_phase(phase.name, "failed")
            console.print(f"[red]phase {phase.name}: verify failed - stopping.[/red]")
            return False
        ctx.end_status()
    return True
