"""Thin SSH execution layer.

One `NodeConn` per host (the Pangolin host, or one Newt agent); `Fleet` is
just an ordered collection of them. Unlike a uniform akropolis-style
cluster, charmer's hosts are heterogeneously configured: each Newt agent
carries its own `ssh:` block, so `Fleet` takes pre-built connections
instead of stamping out one shared `SSHTarget` across every host.
"""

from __future__ import annotations

import getpass
import shlex
from dataclasses import dataclass

import paramiko

from .config import SSHTarget


def prompt_node_credentials(host_label: str, ssh: SSHTarget) -> tuple[str | None, str | None]:
    """Interactively prompt for whatever SSH/sudo password a NodeConn against
    `ssh` will need, same logic as cli.py's `_build_fleet`, factored out here
    so `monitor`/`logs` (which build NodeConns outside the provisioning
    pipeline, against a host they never authenticated to this run) get the
    same passwordless-sudo handling instead of silently failing every
    `sudo -n` check."""
    password = None
    if ssh.auth == "password":
        password = getpass.getpass(f"SSH password for {ssh.user}@{host_label}: ")
    sudo_password = None
    if ssh.become and ssh.user != "root":
        hint = "Enter = reuse SSH password" if password else "Enter = try passwordless sudo"
        sudo_password = getpass.getpass(f"sudo password for {ssh.user}@{host_label} ({hint}): ") or password
    return password, sudo_password


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class NodeConn:
    def __init__(self, name: str, ip: str, ssh: SSHTarget,
                 password: str | None = None, sudo_password: str | None = None):
        self.name = name
        self.ip = ip
        self.cfg = ssh
        self._password = password
        self._sudo_password = sudo_password
        self._client: paramiko.SSHClient | None = None
        self.fleet: "Fleet | None" = None  # set by Fleet.__init__; reaches its transcript

    def connect(self, timeout: float = 10.0) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # TOFU; known_hosts respected first
        client.load_system_host_keys()
        kwargs: dict = {
            "hostname": self.ip,
            "port": self.cfg.port,
            "username": self.cfg.user,
            "timeout": timeout,
            "auth_timeout": timeout,
            "allow_agent": self.cfg.auth in ("agent", "key"),
            "look_for_keys": self.cfg.auth in ("agent", "key"),
        }
        if self.cfg.auth == "key" and self.cfg.key_file:
            import os
            kwargs["key_filename"] = os.path.expanduser(self.cfg.key_file)
        if self.cfg.auth == "password":
            kwargs["password"] = self._password
            kwargs["allow_agent"] = False
            kwargs["look_for_keys"] = False
        client.connect(**kwargs)
        self._client = client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def run(self, cmd: str, sudo: bool | None = None, timeout: float = 30.0) -> Result:
        if self._client is None:
            self.connect()
        use_sudo = self.cfg.become if sudo is None else sudo
        feed_sudo_pw = False
        if use_sudo and self.cfg.user != "root":
            if self._sudo_password:
                # -S reads the password from stdin; -p '' keeps the prompt out of
                # stderr; -k forces a fresh authentication so a stale timestamp
                # can't make the stdin line leak into the command.
                cmd = f"sudo -S -k -p '' -- sh -c {shlex.quote(cmd)}"
                feed_sudo_pw = True
            else:
                cmd = f"sudo -n -- sh -c {shlex.quote(cmd)}"
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)  # type: ignore[union-attr]
        if feed_sudo_pw:
            stdin.write(self._sudo_password + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc = stdout.channel.recv_exit_status()
        result = Result(rc=rc, out=out.strip(), err=err.strip())
        if self.fleet is not None and self.fleet.transcript is not None:
            self.fleet.transcript.record(self.name, self.fleet.current_phase, cmd, result.rc, result.out, result.err)
        return result

    def put(self, local_path: str, remote_path: str, callback=None) -> None:
        """SFTP upload, for payloads too large for the base64 push_file pipe
        (SQL dumps, imported certs). Writes as the SSH user; chmod/chown
        afterwards via run(). `callback(transferred, total)` streams progress."""
        if self._client is None:
            self.connect()
        sftp = self._client.open_sftp()  # type: ignore[union-attr]
        try:
            sftp.put(local_path, remote_path, callback=callback)
        finally:
            sftp.close()
        if self.fleet is not None and self.fleet.transcript is not None:
            self.fleet.transcript.note(f"[{self.name}] ({self.fleet.current_phase}) SFTP put {local_path} -> {remote_path}")


class Fleet:
    """An ordered set of already-constructed connections, iterated in order."""

    def __init__(self, conns: list[NodeConn], transcript=None):
        self.transcript = transcript
        self.current_phase = ""  # set by run_phases()/lifecycle commands as phases start
        self.conns = conns
        for c in self.conns:
            c.fleet = self

    def __iter__(self):
        return iter(self.conns)

    def __len__(self) -> int:
        return len(self.conns)

    def close(self) -> None:
        for c in self.conns:
            c.close()
