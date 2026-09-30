"""Per-site provisioning state.

Records which phases completed and pins values that must be generated
exactly once per site's lifetime (the Pangolin server secret, the Postgres
password, the OIDC client secret if configured, the Pangolin root API key,
each Newt agent's minted id/secret, ...). Re-running a completed phase must
be a no-op or an explicit diff, never a re-bootstrap or a rotated secret
pulled out from under a running stack.

TODO(security): generated secrets are stored plaintext in the state file for
now. Before any production use, wrap `generated` in age/sops encryption or
move it to the OS keyring; see README "State file & secrets".
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


TOOL = "charmer"

# Every phase name charmer has ever written under "phases". akropolis uses the
# same state-file shape (and the same `.state/<site>.json` default), so a file
# without the "tool" stamp (written before it existed) is only accepted if its
# phase names are all charmer's; akropolis's etcd/patroni/nginx/... are not.
KNOWN_PHASES = frozenset({"preflight", "base", "pangolin", "restore", "adopt_newt",
                          "newt", "handoff", "shutdown", "start", "clean"})


class State:
    def __init__(self, path: Path, site_name: str):
        self.path = Path(path)
        self.site_name = site_name
        self.data: dict = {"tool": TOOL, "site": site_name, "phases": {}, "generated": {}}
        if self.path.exists():
            with open(self.path) as f:
                self.data = json.load(f)
            tool = self.data.get("tool")
            foreign = sorted(set(self.data.get("phases", {})) - KNOWN_PHASES)
            if tool is not None and tool != TOOL:
                raise RuntimeError(
                    f"State file {self.path} was written by {tool!r}, not charmer. "
                    "Refusing to read or overwrite another tool's state.")
            if tool is None and foreign:
                raise RuntimeError(
                    f"State file {self.path} has phases charmer never writes ({', '.join(foreign)}): "
                    "it looks like another tool's state (akropolis uses the same layout). "
                    "Refusing to read or overwrite it.")
            if self.data.get("site") != site_name:
                raise RuntimeError(
                    f"State file {self.path} belongs to site {self.data.get('site')!r}, "
                    f"not {site_name!r}. Refusing to mix state between sites."
                )
            self.data["tool"] = TOOL  # stamp pre-existing charmer files on their next save

    # --- phases -------------------------------------------------------------
    def phase_status(self, name: str) -> str:
        return self.data["phases"].get(name, {}).get("status", "pending")

    def mark_phase(self, name: str, status: str, detail: dict | None = None) -> None:
        entry = self.data["phases"].setdefault(name, {})
        entry["status"] = status
        entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if detail:
            entry.update(detail)
        self.save()

    # --- generated-once values -----------------------------------------------
    def get_or_generate(self, key: str, generator: Callable[[], str]) -> str:
        """Return the pinned value for `key`, generating and pinning it on first use."""
        if key not in self.data["generated"]:
            self.data["generated"][key] = generator()
            self.save()
        return self.data["generated"][key]

    # --- io -------------------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        tmp.replace(self.path)
        self.path.chmod(0o600)  # state may contain pinned secrets: owner-only
