"""charmer logs: cluster-wide (host + every Newt agent) log viewer over
SSH, same `config.<site>.monitor.yml` as `charmer monitor`. `--save` writes
a plain-text report instead of printing to stdout.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yaml

from ..config import SSHTarget
from ..sshexec import NodeConn, prompt_node_credentials

# each level includes everything at or above it in severity; "debug"
# disables filtering entirely.
_GREP_LEVELS = {
    "debug": None,
    "info": r"(INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)",
    "warning": r"(WARN|WARNING|ERROR|CRITICAL|FATAL)",
    "error": r"(ERROR|CRITICAL|FATAL)",
}


def _ssh_target(d: dict) -> SSHTarget:
    return SSHTarget(user=d["user"], auth=d["auth"], key_file=d.get("key_file"),
                     port=int(d.get("port", 22)), become=bool(d.get("become", True)))


def _docker_logs(conn: NodeConn, container: str, hours: int, level: str) -> str:
    cmd = f"docker logs --since {hours}h {container} 2>&1"
    pattern = _GREP_LEVELS[level]
    if pattern:
        cmd += f" | grep -iE '{pattern}'"
    r = conn.run(cmd, timeout=30)
    return r.out or "(no matching logs)"


def collect(cfg: dict, hours: int, level: str) -> str:
    host_password, host_sudo_password = prompt_node_credentials("pangolin-host", _ssh_target(cfg["pangolin"]["ssh"]))
    host = NodeConn("pangolin-host", cfg["pangolin"]["ip"], _ssh_target(cfg["pangolin"]["ssh"]),
                    host_password, host_sudo_password)
    agent_conns = []
    for a in cfg.get("newt_agents", []):
        a_password, a_sudo_password = prompt_node_credentials(a["name"], _ssh_target(a["ssh"]))
        agent_conns.append(NodeConn(a["name"], a["ip"], _ssh_target(a["ssh"]), a_password, a_sudo_password))

    jobs: list[tuple[str, object]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for svc in ("pangolin", "gerbil", "traefik", "postgres"):
            jobs.append((f"pangolin-host/{svc}", pool.submit(_docker_logs, host, svc, hours, level)))
        for conn in agent_conns:
            jobs.append((f"{conn.name}/newt", pool.submit(_docker_logs, conn, "newt", hours, level)))

        sections = []
        for label, future in jobs:
            try:
                sections.append(f"\n=== {label} ===\n{future.result()}\n")
            except Exception as exc:  # noqa: BLE001
                sections.append(f"\n=== {label} ===\n(unreachable: {exc})\n")

    host.close()
    for conn in agent_conns:
        conn.close()

    header = (f"charmer log report: {cfg.get('site', '?')}\n"
             f"fetched: {datetime.now():%Y-%m-%d %H:%M:%S}  last {hours}h  level >= {level}\n")
    return header + "".join(sections)


def run(config_path: str, hours: int, save: str | None, level: str) -> None:
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    text = collect(cfg, hours, level)
    if save:
        out = Path(save)
        if out.suffix != ".log":
            out = out.with_suffix(".log")
        out.write_text(text)
        print(f"wrote {out}")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="charmer logs")
    parser.add_argument("config", help="config.<site>.monitor.yml (emitted by `charmer provision`'s handoff phase)")
    parser.add_argument("--last", type=int, default=24, metavar="HOURS")
    parser.add_argument("--level", choices=tuple(_GREP_LEVELS), default="warning")
    parser.add_argument("--save", metavar="FILE")
    args = parser.parse_args(argv)
    run(args.config, args.last, args.save, args.level)
    return 0
