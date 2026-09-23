"""Pangolin integration-API client for minting Newt agent credentials.

No akropolis equivalent: Authentik's bootstrap token is env-seeded at
container start, but Pangolin CE has no way to seed an API key at deploy
time; the operator mints one Root API Key by hand, once, via Server Admin ->
API Keys after completing /auth/initial-setup (see phases/newt_phase.py). It
needs at least the 'Create Site' permission; pick_site_defaults() and
create_newt_site() below are the only calls this module makes, and both are
scoped under that permission. From then on this module drives it.

Every call here runs over the EXISTING SSH connection to the Pangolin host
and hits the integration API on loopback (`127.0.0.1:{port}`, `3003` unless
`pangolin.integration_api.port` overrides it, see config.py), never over the
public internet, and never through Traefik. The integration API is
charmer's own tool for minting agent credentials, not a public interface,
so it is deliberately never routed through the public TLS boundary (see
README "Ingress"). This also means charmer itself never needs network
reachability to the API independent of the SSH connection it already has,
and the root key never leaves the host it's used on except to live (pinned)
in local state.

Route paths and the `flags.enable_integration_api` / `server.integration_port`
config.yml keys are confirmed against docs.pangolin.net (self-host/advanced/
integration-api, manage/common-api-routes) as of the version this was
written against. If Pangolin's API changes shape, `API_PREFIX` below is the
one thing to check first: verify against `https://<dashboard>/v1/docs`
(the Swagger UI the integration API itself serves) if a call starts failing
with 404 rather than 401/403.
"""

from __future__ import annotations

import json
import shlex
from urllib.parse import quote

from .sshexec import NodeConn

INTEGRATION_PORT = 3003
API_PREFIX = "/v1"

# NodeConn.run() wraps every sudo'd command as `sudo -- sh -c '<cmd>'`
# (sshexec.py), always POSIX sh, never bash, regardless of the host's login
# shell. So the curl -w argument below must not rely on bash's `$'...'`
# ANSI-C quoting to get a literal newline: under sh that syntax isn't
# special, `$` leaks through verbatim and corrupts the JSON payload. A
# control byte that can never appear in a JSON response, passed through a
# plain single-quoted argument (preserved literally by every POSIX shell),
# sidesteps the whole issue.
_STATUS_SEP = "\x1e"


class PangolinAPIError(RuntimeError):
    pass


def _call(conn: NodeConn, root_key: str, method: str, path: str, body: dict | None = None,
          port: int = INTEGRATION_PORT) -> dict:
    data_flag = ""
    if body is not None:
        data_flag = f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))}"
    cmd = (
        f"curl -sS -X {method} http://127.0.0.1:{port}{API_PREFIX}{path} "
        f"-H {shlex.quote('Authorization: Bearer ' + root_key)} "
        f"-w {shlex.quote(_STATUS_SEP + '%{http_code}')} {data_flag}"
    )
    r = conn.run(cmd, timeout=30)
    if not r.ok:
        raise PangolinAPIError(f"curl failed calling {method} {path}: {r.err or r.out}")
    if _STATUS_SEP not in r.out:
        raise PangolinAPIError(f"{method} {path}: malformed curl output (no status separator): {r.out}")
    payload, _, status = r.out.rpartition(_STATUS_SEP)
    status = status.strip()
    if not status.startswith("2"):
        raise PangolinAPIError(f"{method} {path} -> HTTP {status}: {payload}")
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PangolinAPIError(f"{method} {path}: non-JSON response: {payload}") from exc


def pick_site_defaults(conn: NodeConn, root_key: str, org_id: str, port: int = INTEGRATION_PORT) -> dict:
    """GET /org/{orgId}/pick-site-defaults -> a fresh {newtId, newtSecret, clientAddress}."""
    resp = _call(conn, root_key, "GET", f"/org/{quote(org_id, safe='')}/pick-site-defaults", port=port)
    return resp.get("data", resp)


def create_newt_site(conn: NodeConn, root_key: str, org_id: str, name: str,
                      newt_id: str, newt_secret: str, port: int = INTEGRATION_PORT) -> dict:
    """PUT /org/{orgId}/site -> the created site, echoing newtId/secret back
    so the caller can confirm the server accepted the exact credentials it
    was asked to use (rather than trusting the response blindly)."""
    body = {"name": name, "type": "newt", "newtId": newt_id, "secret": newt_secret}
    resp = _call(conn, root_key, "PUT", f"/org/{quote(org_id, safe='')}/site", body, port=port)
    return resp.get("data", resp)
