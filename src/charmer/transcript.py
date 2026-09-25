"""transcript: a full, timestamped record of every command run on every host
during a `provision`/`clean`/`shutdown`/`start` invocation, plus its output.

Exists so an operator (or an auditor) can reconstruct exactly what charmer
did to a machine without re-reading the source or trusting memory of what
happened months ago. One file per invocation, named after the site, the
command, and the run's start time, so successive runs never overwrite each
other's record. Lives on the WORKSTATION only; nothing here is written to
a host.

Wiring: NodeConn.run() (sshexec.py) is the single choke point every remote
command already passes through, so that is where every command/output pair
is captured; no phase has to opt in or remember to log anything itself.

Secrets. What IS covered and what is NOT:
Commands routinely embed the very secrets charmer generates: the Pangolin
root API key in `curl -H 'Authorization: Bearer ...'`, the server secret /
Postgres password / OIDC client secret in KEY=value assignments, a Newt
secret in a JSON body (`"secret": "..."`, the get-token check), and the
base64 blob push_file()/push_binary() pipe through `base64 -d` to write
rendered files (compose, config.yml, certs) whose CONTENT is secret-bearing
even though the shell command itself is generic. redact() catches all four
patterns. It does NOT catch a secret embedded in free-form prose that
doesn't match one of those shapes; this is best-effort, not a guarantee,
which is why the file is written 0600 like the state file, and the handoff
landing card says so explicitly rather than implying it's safe to hand
around casually.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # curl -H 'Authorization: Bearer <token>' ...
    (re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE), r"\1<redacted>"),
    # KEY=value assignments where KEY looks like a secret
    (re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|SECRET|TOKEN|PASS|KEY)[A-Za-z0-9_]*)=\S+"),
     r"\1=<redacted>"),
    # JSON bodies, e.g. newt_ops.check_credentials()'s {"newtId": ..., "secret": ...}
    (re.compile(r'("[A-Za-z_]*(?:secret|Secret|password|Password|token|Token)"\s*:\s*")[^"]*(")'),
     r"\1<redacted>\2"),
    # push_file()/push_binary() write rendered files via
    # `echo '<base64>' | base64 -d > path`; the encoded blob typically *is*
    # the secret (a whole config.yml/.env), so it is collapsed on sight.
    (re.compile(r"(echo )'[A-Za-z0-9+/=]{40,}'( \| base64 -d)"),
     r"\1'<base64 payload redacted>'\2"),
]


def redact(text: str) -> str:
    """Best-effort secret redaction; see module docstring for exact scope."""
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class Transcript:
    """One append-only file per `provision`/`clean`/`shutdown`/`start` run.

    Flat and chronological (not one file per host); grep for `[patras-edge]`
    to filter by host, or `(newt)` to filter by phase.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")
        self.path.chmod(0o600)
        self._f.write(f"\n=== charmer run started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        self._f.flush()

    def record(self, host: str, phase: str, cmd: str, rc: int, out: str, err: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._f.write(f"\n[{ts}] [{host}] ({phase or '-'}) $ {redact(cmd)}\n")
        for line in redact(out).splitlines():
            self._f.write(f"  out| {line}\n")
        for line in redact(err).splitlines():
            self._f.write(f"  err| {line}\n")
        self._f.write(f"  rc={rc}\n")
        # Flushed on every write: a run that crashes or is killed mid-phase
        # should still leave a usable trail up to the point it stopped.
        self._f.flush()

    def note(self, text: str) -> None:
        """Free-form marker line: phase boundaries, SFTP transfers, etc."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._f.write(f"\n[{ts}] --- {text} ---\n")
        self._f.flush()

    def close(self) -> None:
        if self._f and not self._f.closed:
            self._f.write(f"\n=== charmer run ended {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            self._f.close()
