"""Shared helpers for write-phases: template rendering, checksummed file push,
and poll-until-healthy waits."""

from __future__ import annotations

import base64
import hashlib
import re
import shlex
import time
from importlib import resources
from pathlib import Path

from jinja2 import Environment, StrictUndefined

from .sshexec import NodeConn

_env = Environment(undefined=StrictUndefined, keep_trailing_newline=True,
                    trim_blocks=True, lstrip_blocks=True)

# Pangolin prints this block to stdout on first boot, for as long as no
# server admin exists yet (see ensureSetupToken.ts upstream), scraped here
# so the operator doesn't have to go find it in `docker compose logs`
# themselves. Used by both handoff (always) and newt (when it needs to walk
# the operator through the org/API-key bootstrap too).
_SETUP_TOKEN_RE = re.compile(r"\bToken:\s*(\S+)\s*$")


def read_pangolin_setup_token(conn: NodeConn) -> str | None:
    """Read the one-time server-admin setup token off `docker compose logs
    pangolin` on the Pangolin host. Returns None if the container has no
    logs yet, or no unused token is present (already consumed, or never
    printed because a server admin already exists)."""
    r = conn.run("cd /opt/pangolin && docker compose logs --no-color pangolin")
    if not r.ok:
        return None
    token = None
    for line in r.out.splitlines():
        m = _SETUP_TOKEN_RE.search(line)
        if m:
            token = m.group(1)  # last match wins: the current, unused token
    return token


def render(template_name: str, **ctx) -> str:
    src = resources.files("charmer.templates").joinpath(template_name).read_text()
    return _env.from_string(src).render(**ctx)


def push_file(conn: NodeConn, content: str, remote_path: str,
              mode: str = "0644", owner: str | None = None) -> bool:
    """Write `content` to `remote_path`. Returns True if the file changed.

    Uploads via a shell base64 pipe (works under sudo/become too), compares
    sha256 first so unchanged files are a detected no-op that still
    converges mode/owner (an earlier run may have written this file with
    different perms).
    """
    digest = hashlib.sha256(content.encode()).hexdigest()
    r = conn.run(f"sha256sum {shlex.quote(remote_path)} 2>/dev/null | cut -d' ' -f1")
    if r.ok and r.out == digest:
        m = conn.run(f"stat -c %a {shlex.quote(remote_path)}")
        try:
            mode_drift = m.ok and int(m.out.strip(), 8) != int(mode, 8)
        except ValueError:
            mode_drift = False
        fix = []
        if mode_drift:
            fix.append(f"chmod {mode} {shlex.quote(remote_path)}")
        if owner:
            fix.append(f"chown {owner} {shlex.quote(remote_path)}")
        if fix:
            r2 = conn.run(" && ".join(fix))
            if not r2.ok:
                raise RuntimeError(f"[{conn.name}] failed to fix perms on {remote_path}: {r2.err}")
        return False

    b64 = base64.b64encode(content.encode()).decode()
    dirpath = remote_path.rsplit("/", 1)[0]
    cmd = (f"mkdir -p {shlex.quote(dirpath)} && "
           f"echo {shlex.quote(b64)} | base64 -d > {shlex.quote(remote_path)} && "
           f"chmod {mode} {shlex.quote(remote_path)}")
    if owner:
        cmd += f" && chown {owner} {shlex.quote(remote_path)}"
    r = conn.run(cmd, timeout=60)
    if not r.ok:
        raise RuntimeError(f"[{conn.name}] failed to write {remote_path}: {r.err}")
    return True


def push_binary(conn: NodeConn, local_path, remote_path: str, mode: str = "0644") -> bool:
    """Upload a LOCAL BINARY file (imported certs) to `remote_path` over SFTP.

    push_file() base64-encodes a str through the shell, which is fine for
    configs but wasteful/wrong for binaries. SFTP runs as the SSH user and
    cannot escalate, so the payload is staged in /tmp (world writable) and
    moved into place by a privileged run(), which also owns the mkdir, the
    final mode, and the ownership. Returns True iff the remote file changed.
    """
    import os

    local = Path(local_path).expanduser()
    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    r = conn.run(f"sha256sum {shlex.quote(remote_path)} 2>/dev/null | cut -d' ' -f1")
    if r.ok and r.out.strip() == digest:
        return False

    dirpath = remote_path.rsplit("/", 1)[0]
    staging = f"/tmp/.charmer-upload-{os.getpid()}-{local.name}"
    try:
        conn.put(str(local), staging)
    except OSError as exc:
        raise RuntimeError(f"[{conn.name}] SFTP upload of {local} to {staging} failed: {exc}") from exc
    r = conn.run(f"mkdir -p {shlex.quote(dirpath)} && "
                 f"mv {shlex.quote(staging)} {shlex.quote(remote_path)} && "
                 f"chmod {mode} {shlex.quote(remote_path)}", timeout=120)
    if not r.ok:
        conn.run(f"rm -f {shlex.quote(staging)}")
        raise RuntimeError(f"[{conn.name}] failed to install {remote_path}: {r.err}")

    r = conn.run(f"sha256sum {shlex.quote(remote_path)} | cut -d' ' -f1")
    if r.out.strip() != digest:
        raise RuntimeError(f"[{conn.name}] {remote_path}: checksum mismatch after upload, transfer corrupted")
    return True


def wait_for(conn: NodeConn, cmd: str, expect: str | None = None,
             timeout: float = 120.0, interval: float = 5.0, tick=None) -> bool:
    """Poll `cmd` over SSH until it exits 0 (and, if given, stdout contains
    `expect`). `tick(elapsed_seconds)` is called once per poll so the caller
    can keep a live "still waiting: 40s / 900s" line on screen instead of
    dead air."""
    start = time.monotonic()
    deadline = start + timeout
    while time.monotonic() < deadline:
        if tick:
            tick(time.monotonic() - start)
        r = conn.run(cmd, timeout=min(30, timeout))
        if r.ok and (expect is None or expect in r.out):
            return True
        time.sleep(interval)
    return False


def gen_password() -> str:
    import secrets as _s
    return _s.token_urlsafe(24)
