"""charmer logs: cluster-wide (host + every Newt agent) log viewer over
SSH, same `config.<site>.monitor.yml` as `charmer monitor`. `--save` writes
a plain-text report instead of printing to stdout.

Also resolves user<->resource connections from each Newt agent's own
ACCESS START/END log lines (Newt logs these itself at INFO level; Pangolin
CE has no server-side handler for the newt/access-log message that would
otherwise centralize them, see fosrl/pangolin#3695, so per-agent SSH is the
only way to get at them) into a CSV, cross-referenced against Pangolin's
Postgres via `docker exec postgres psql` on the host -- the same
credential-free local-socket trick restore_phase.py already uses, so a
rotated/unknown Postgres password never blocks this. Only agents listed in
this monitor.yml are reachable this way; hand-add extra `newt_agents:`
entries here (this file is plain, standalone YAML, not tied to charmer's
own provisioning state) to cover a Newt host this site didn't provision.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import re
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

# Newt (netstack2/access_log.go) logs these at INFO regardless of the
# level filter above; "ACCESS END" gains an optional "(reaped)"/
# "(shutdown)" annotation on some exit paths but the fields are the same.
_ACCESS_START_RE = re.compile(
    r"ACCESS START session=(?P<session>\S+) resource=(?P<resource>\d+) "
    r"proto=(?P<proto>\S+) src=(?P<src>\S+) dst=(?P<dst>\S+) time=(?P<time>\S+)"
)
_ACCESS_END_RE = re.compile(
    r"ACCESS END(?:\s*\([^)]*\))? session=(?P<session>\S+) resource=(?P<resource>\d+) "
    r"proto=(?P<proto>\S+) src=(?P<src>\S+) dst=(?P<dst>\S+) started=(?P<started>\S+) "
    r"ended=(?P<ended>\S+) duration=(?P<duration>\S+)"
)


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


def _newt_access_raw(conn: NodeConn, hours: int) -> str:
    """Unfiltered `newt` log: ACCESS START/END lines are INFO-level and
    would be dropped by any WARN-or-above --level filter."""
    r = conn.run(f"docker logs --since {hours}h newt 2>&1", timeout=30)
    return r.out


def parse_access_sessions(raw_log: str) -> list[dict]:
    """Pair ACCESS START/END lines by session id into complete session
    dicts. A START with no matching END (still-open session) is included
    with ended=None."""
    starts: dict[str, dict] = {}
    sessions = []
    for line in raw_log.splitlines():
        m = _ACCESS_START_RE.search(line)
        if m:
            starts[m["session"]] = m.groupdict()
            continue
        m = _ACCESS_END_RE.search(line)
        if m:
            starts.pop(m["session"], None)
            sessions.append({
                "session": m["session"], "resource_id": int(m["resource"]),
                "proto": m["proto"], "src": m["src"], "dst": m["dst"],
                "started": m["started"], "ended": m["ended"], "duration": m["duration"],
            })
    for session_id, s in starts.items():
        sessions.append({
            "session": session_id, "resource_id": int(s["resource"]),
            "proto": s["proto"], "src": s["src"], "dst": s["dst"],
            "started": s["time"], "ended": None, "duration": None,
        })
    return sessions


def _psql(postgres_user: str, sql: str) -> str:
    # Local-socket connection inside the container: no password needed
    # (same trick restore_phase.py uses), so a rotated/unknown Postgres
    # password never blocks this.
    return f"docker exec postgres psql -U {postgres_user} -d pangolin -tAc '{sql}'"


def build_lookup_maps(host: NodeConn, postgres_user: str) -> tuple[dict[int, str], dict[str, str]]:
    """Return (resourceId -> resource name, client subnet IP -> "user (client)")."""
    resource_map: dict[int, str] = {}
    r = host.run(_psql(postgres_user, 'select "resourceId", name from resources;'), timeout=30)
    if r.ok:
        for line in r.out.splitlines():
            rid, _, name = line.partition("|")
            if rid.strip().isdigit():
                resource_map[int(rid)] = name.strip()

    client_map: dict[str, str] = {}
    r = host.run(_psql(postgres_user,
                       'select c.subnet, c.name, u.name, u.email from clients c '
                       'left join "user" u on c."userId" = u.id;'), timeout=30)
    if r.ok:
        for line in r.out.splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            subnet, client_name, user_name, user_email = parts
            try:
                ip = str(ipaddress.ip_interface(subnet.strip()).ip)
            except ValueError:
                continue
            who = user_name.strip() or user_email.strip() or "unknown user"
            client_map[ip] = f"{who} ({client_name.strip()})"

    return resource_map, client_map


def format_session(session: dict, resource_map: dict[int, str], client_map: dict[str, str]) -> dict:
    src_ip = session["src"].rsplit(":", 1)[0]
    who = client_map.get(src_ip, session["src"])
    where = resource_map.get(session["resource_id"], f"resource#{session['resource_id']}")
    return {
        "started": session["started"], "ended": session["ended"] or "ongoing",
        "duration": session["duration"] or "-", "who": who, "where": where,
        "proto": session["proto"].upper(), "dst": session["dst"],
    }


def collect_access_rows(cfg: dict, host: NodeConn, agent_conns: list[NodeConn], hours: int) -> list[list[str]]:
    """Fetch+resolve access sessions from every reachable Newt agent. Only
    agents present in this monitor.yml are reachable this way -- an agent
    this site didn't provision (or one introduced by restoring a dump from
    elsewhere) has no SSH target here at all; add it to this file by hand
    if you want it covered."""
    if cfg.get("pangolin", {}).get("database", "postgres") != "postgres":
        return []  # sqlite is lab-only (see CLAUDE.md); no resolvable schema here
    postgres_user = cfg.get("pangolin", {}).get("postgres_user", "pangolin")
    resource_map, client_map = build_lookup_maps(host, postgres_user)

    rows: list[list[str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {conn.name: pool.submit(_newt_access_raw, conn, hours) for conn in agent_conns}
        for conn in agent_conns:
            try:
                raw = futures[conn.name].result()
            except Exception as exc:  # noqa: BLE001
                rows.append([conn.name, conn.ip, "", "", "", f"SSH error: {exc}", "", "", ""])
                continue
            sessions = parse_access_sessions(raw)
            formatted = [format_session(s, resource_map, client_map) for s in sessions]
            formatted.sort(key=lambda r: r["started"], reverse=True)
            for f in formatted:
                rows.append([conn.name, conn.ip, f["started"], f["ended"], f["duration"],
                            f["who"], f["where"], f["proto"], f["dst"]])
    return rows


def collect(cfg: dict, hours: int, level: str) -> tuple[str, list[list[str]]]:
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

    access_rows = collect_access_rows(cfg, host, agent_conns, hours)

    host.close()
    for conn in agent_conns:
        conn.close()

    header = (f"charmer log report: {cfg.get('site', '?')}\n"
             f"fetched: {datetime.now():%Y-%m-%d %H:%M:%S}  last {hours}h  level >= {level}\n")
    return header + "".join(sections), access_rows


def _write_access_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Agent", "Agent IP", "Started", "Ended", "Duration", "Who", "Where", "Proto", "Destination"])
        writer.writerows(rows)
    path.chmod(0o600)


def run(config_path: str, hours: int, save: str | None, level: str) -> None:
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    text, access_rows = collect(cfg, hours, level)

    if save:
        out = Path(save)
        if out.suffix != ".log":
            out = out.with_suffix(".log")
        out.write_text(text)
        print(f"wrote {out}")
        access_path = out.with_name(f"{out.stem}_access.csv")
    else:
        print(text)
        access_path = Path(f"charmer.logs.{cfg.get('site', 'site')}.{datetime.now():%Y%m%dT%H%M%S}.access.csv")

    _write_access_csv(access_path, access_rows)
    print(f"wrote {access_path} ({len(access_rows)} resolved access session(s), last {hours}h)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="charmer logs")
    parser.add_argument("config", help="config.<site>.monitor.yml (emitted by `charmer provision`'s handoff phase)")
    parser.add_argument("--last", type=int, default=24, metavar="HOURS")
    parser.add_argument("--level", choices=tuple(_GREP_LEVELS), default="warning")
    parser.add_argument("--save", metavar="FILE")
    args = parser.parse_args(argv)
    run(args.config, args.last, args.save, args.level)
    return 0
