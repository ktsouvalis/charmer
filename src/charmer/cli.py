"""charmer: provision a single self-hosted Pangolin CE node + separate
Newt agents over SSH.

    charmer init                       interactive wizard -> config.<site>.yml
    charmer provision config.yml       phase runner (resumable)
    charmer provision config.yml --only preflight
    charmer shutdown config.yml        stop pangolin (gerbil + traefik + maintenance page stay up)
    charmer start config.yml           start them again (refuses without a prior shutdown)
    charmer clean config.yml           tear the site down to a bare host
    charmer monitor config.<site>.monitor.yml   real-time health dashboard
    charmer logs config.<site>.monitor.yml      cluster-wide log viewer (SSH), --save to download
    charmer update                     install the latest release (zipapp binary only)
    charmer check-update               check for a newer release without installing it
    charmer whats-new                  show the changelog for the installed version
    charmer licenses                   show third-party license info
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from rich.markdown import Markdown

from . import __version__, changelog
from .config import ConfigError, SiteConfig, load
from .phases.base import PhaseContext, console, run_phases
from .phases.clean_phase import CleanPhase
from .phases.handoff_phase import HandoffPhase
from .phases.lifecycle import ShutdownPhase, StartPhase
from .phases.newt_phase import NewtPhase
from .phases.pangolin_phase import PangolinPhase
from .phases.base_setup import BasePhase
from .phases.preflight import PreflightPhase
from .phases.restore_phase import RestorePhase
from .sshexec import Fleet, NodeConn
from .state import State
from .update import check_for_update, check_update_now, self_update

PIPELINE = [
    PreflightPhase(), BasePhase(), PangolinPhase(),
    RestorePhase(), NewtPhase(), HandoffPhase(),
]


def _transcript_path(cfg: SiteConfig, command: str) -> Path:
    ts = time.strftime("%Y%m%dT%H%M%S")
    return cfg.state_file.parent / f"{cfg.name}.{command}.{ts}.transcript.log"


def _build_fleet(cfg: SiteConfig, transcript) -> Fleet:
    conns: list[NodeConn] = []

    password = None
    if cfg.ssh.auth == "password":
        password = getpass.getpass(f"SSH password for {cfg.ssh.user}@{cfg.host_ip}: ")
    sudo_password = None
    if cfg.ssh.become and cfg.ssh.user != "root":
        hint = "Enter = reuse SSH password" if password else "Enter = try passwordless sudo"
        sudo_password = getpass.getpass(f"sudo password for {cfg.ssh.user}@{cfg.host_ip} ({hint}): ") or password
    conns.append(NodeConn("pangolin-host", cfg.host_ip, cfg.ssh, password, sudo_password))

    for agent in cfg.newt_agents:
        a_password = None
        if agent.ssh.auth == "password":
            a_password = getpass.getpass(f"SSH password for {agent.ssh.user}@{agent.name}: ")
        a_sudo = None
        if agent.ssh.become and agent.ssh.user != "root":
            hint = "Enter = reuse SSH password" if a_password else "Enter = try passwordless sudo"
            a_sudo = getpass.getpass(f"sudo password for {agent.ssh.user}@{agent.name} ({hint}): ") or a_password
        conns.append(NodeConn(agent.name, agent.ip, agent.ssh, a_password, a_sudo))

    return Fleet(conns, transcript=transcript)


def _preauth(fleet: Fleet) -> bool:
    """Prove SSH + sudo work on every host BEFORE the first phase runs.

    Without this the credential is first exercised wherever a phase happens
    to need root, which for `restore` is right before the destructive part.
    A mistyped password there aborts a run that already reported progress.
    """
    bad: list[str] = []
    for conn in fleet:
        try:
            conn.connect()
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]cannot reach {conn.name} ({conn.ip}):[/red] {exc}")
            bad.append(conn.name)
            continue
        if conn.run("id -u").out == "0":
            continue
        if conn.cfg.become and not conn.run("true", sudo=True).ok:
            bad.append(conn.name)
    if bad:
        console.print(f"[red]SSH/sudo check failed on: {', '.join(bad)}[/red]")
        return False
    return True


def _load_or_die(config_path: str) -> SiteConfig:
    try:
        return load(config_path)
    except ConfigError as exc:
        console.print("[red]config problems:[/red]")
        for p in exc.problems:
            console.print(f"  ✘ {p}")
        raise SystemExit(2)


def _connected_command(config_path: str, command: str):
    """Shared setup for provision/shutdown/start: load config, open a
    transcript, build the fleet, pre-authenticate. Returns (cfg, state,
    fleet, transcript) or raises SystemExit(2) on failure."""
    from .transcript import Transcript

    cfg = _load_or_die(config_path)
    state = State(cfg.state_file, cfg.name)
    transcript = Transcript(_transcript_path(cfg, command))
    console.print(f"[dim]transcript: {transcript.path} (every command run on every host this "
                 "session, mode 0600)[/dim]")
    fleet = _build_fleet(cfg, transcript)
    if not _preauth(fleet):
        fleet.close()
        transcript.close()
        raise SystemExit(2)
    return cfg, state, fleet, transcript


def cmd_init(args: argparse.Namespace) -> int:
    from .init_wizard import run_wizard
    path = run_wizard(args.output)
    console.print(f"wrote {path} (mode 0600), read it, then: charmer provision {path} --only preflight")
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    all_names = {p.name for p in PIPELINE}
    for flag, names in (("--only", args.only), ("--replay", args.replay)):
        missing = set(names or []) - all_names
        if missing:
            console.print(f"[red]unknown phase(s) for {flag}: {', '.join(sorted(missing))}[/red]")
            return 2

    cfg, state, fleet, transcript = _connected_command(args.config, "provision")
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)
    if args.replay:
        for name in args.replay:
            state.mark_phase(name, "pending")
    phases = PIPELINE
    if args.only:
        selected = set(args.only)
        phases = [p for p in PIPELINE if p.name in selected]
    try:
        ok = run_phases(phases, ctx, replay=bool(args.only))
    finally:
        fleet.close()
        transcript.close()
    return 0 if ok else 1


def _lifecycle(args: argparse.Namespace, phase, command: str) -> int:
    cfg, state, fleet, transcript = _connected_command(args.config, command)
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)
    try:
        ok = run_phases([phase], ctx, replay=True)
    finally:
        fleet.close()
        transcript.close()
    return 0 if ok else 1


def cmd_shutdown(args: argparse.Namespace) -> int:
    return _lifecycle(args, ShutdownPhase(), "shutdown")


def cmd_start(args: argparse.Namespace) -> int:
    return _lifecycle(args, StartPhase(), "start")


def cmd_clean(args: argparse.Namespace) -> int:
    cfg = _load_or_die(args.config)
    if cfg.environment == "production" and not args.i_know_this_is_production:
        console.print("[red]refusing to clean a production site.[/red] If this really is a "
                      "teardown of production, add --i-know-this-is-production.")
        return 2

    from .transcript import Transcript

    state = State(cfg.state_file, cfg.name)
    transcript = Transcript(_transcript_path(cfg, "clean"))
    console.print(f"[dim]transcript: {transcript.path} (mode 0600)[/dim]")
    fleet = _build_fleet(cfg, transcript)
    if not _preauth(fleet):
        fleet.close()
        transcript.close()
        return 2
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)
    phase = CleanPhase()
    fleet.current_phase = phase.name
    transcript.note(f"phase: {phase.name}")

    console.rule("clean")
    console.print("[bold]plan:[/bold]")
    for line in phase.plan(ctx):
        console.print(f"  • {line}")
    console.print("[bold red]type the site name to tear it down:[/bold red]")
    if input("> ").strip() != cfg.name:
        console.print("[yellow]not confirmed, nothing touched.[/yellow]")
        fleet.close()
        transcript.close()
        return 1

    try:
        phase.apply(ctx)
        ok = phase.verify(ctx)
    finally:
        ctx.end_status()
        fleet.close()
        transcript.close()
    if ok:
        archived = cfg.state_file.with_suffix(f".cleaned-{time.strftime('%Y%m%dT%H%M%S')}.json")
        if cfg.state_file.exists():
            cfg.state_file.rename(archived)
            archived.chmod(0o600)
            console.print(f"[dim]state archived to {archived}[/dim]")
    return 0 if ok else 1


def cmd_monitor(args: argparse.Namespace) -> int:
    from .monitor.dashboard import run as monitor_run
    monitor_run(args.config, once=args.once, interval=args.interval)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    from .monitor.logs import run as logs_run
    logs_run(args.config, args.last, args.save, args.level)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    return self_update(__version__)


def cmd_check_update(args: argparse.Namespace) -> int:
    return check_update_now(__version__)


def cmd_licenses(args: argparse.Namespace) -> int:
    from . import licenses
    console.print(Markdown(licenses.report()))
    return 0


def cmd_whats_new(args: argparse.Namespace) -> int:
    text = changelog.load()
    if args.all:
        console.print(Markdown(text))
        return 0

    version = args.version or __version__
    entry = changelog.entry_for(version, text)
    if entry is None:
        console.print(f"[yellow]no changelog entry for {version}.[/yellow] "
                      "Run `charmer whats-new --all` for the full history.")
        return 1
    console.print(Markdown(entry))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="charmer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"charmer {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="interactive wizard -> write a site config file")
    p_init.add_argument("-o", "--output", help="output path (default: config.<site>.yml)")
    p_init.set_defaults(func=cmd_init)

    p_prov = sub.add_parser("provision", help="run the phase pipeline against a site")
    p_prov.add_argument("config", help="path to config.<site>.yml")
    p_prov.add_argument("--only", nargs="+", metavar="PHASE", help="run only the named phase(s)")
    p_prov.add_argument("--replay", nargs="+", metavar="PHASE", help="re-run specific completed phase(s)")
    p_prov.set_defaults(func=cmd_provision)

    p_shutdown = sub.add_parser("shutdown", help="stop pangolin (postgres, newt agents, gerbil, traefik, "
                                "and the maintenance page left running)")
    p_shutdown.add_argument("config")
    p_shutdown.set_defaults(func=cmd_shutdown)

    p_start = sub.add_parser("start", help="start pangolin/gerbil again, requires a prior graceful shutdown")
    p_start.add_argument("config")
    p_start.set_defaults(func=cmd_start)

    p_clean = sub.add_parser("clean", help="tear the site down to a bare host (typed-name confirmation)")
    p_clean.add_argument("config")
    p_clean.add_argument("--i-know-this-is-production", action="store_true",
                         help="required additionally when site.environment is production")
    p_clean.set_defaults(func=cmd_clean)

    p_mon = sub.add_parser("monitor", help="real-time health dashboard")
    p_mon.add_argument("config", help="config.<site>.monitor.yml (emitted by provision's handoff phase)")
    p_mon.add_argument("--once", action="store_true")
    p_mon.add_argument("--interval", type=int, default=15)
    p_mon.set_defaults(func=cmd_monitor)

    p_logs = sub.add_parser("logs", help="cluster-wide log viewer over SSH")
    p_logs.add_argument("config", help="config.<site>.monitor.yml (emitted by provision's handoff phase)")
    p_logs.add_argument("--last", type=int, default=24, metavar="HOURS")
    p_logs.add_argument("--level", default="warning", choices=["debug", "info", "warning", "error"])
    p_logs.add_argument("--save", metavar="FILE")
    p_logs.set_defaults(func=cmd_logs)

    p_update = sub.add_parser("update", help="download and install the latest charmer "
                              "release (zipapp binary only)")
    p_update.set_defaults(func=cmd_update)

    p_check_update = sub.add_parser("check-update", help="check (bypassing the cache) "
                                    "whether a newer charmer release exists, without "
                                    "installing it; exit 1 if one is available")
    p_check_update.set_defaults(func=cmd_check_update)

    p_licenses = sub.add_parser("licenses", help="show third-party license info for "
                                "every package actually bundled/installed right now")
    p_licenses.set_defaults(func=cmd_licenses)

    p_whats_new = sub.add_parser("whats-new", help="show the CHANGELOG.md entry for "
                                 "the installed version")
    p_whats_new.add_argument("--all", action="store_true",
                             help="show the full changelog instead of just this version")
    p_whats_new.add_argument("--version", metavar="VERSION",
                             help="show the entry for a specific version instead of "
                             "the one installed")
    p_whats_new.set_defaults(func=cmd_whats_new)

    args = parser.parse_args(argv)

    if args.command not in ("update", "check-update"):
        try:
            latest = check_for_update(__version__)
        except Exception:  # noqa: BLE001, a version check must never break a real command
            latest = None
        if latest:
            console.print(
                f"[yellow]a new charmer release is available: "
                f"{__version__} -> {latest}[/yellow] [dim](run `charmer update`)[/dim]"
            )

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
