"""pangolin: render and push the official Pangolin + Gerbil + Traefik
Compose stack (bridge networking + `network_mode: service:gerbil` on
Traefik, matching docs.pangolin.net/self-host/manual/docker-compose
verbatim, Gerbil host-publishing 80/443 on the wildcard address exactly as
upstream does; no host-level reverse proxy in front of it), bring it up,
health-gate on Pangolin's own healthcheck.

Also owns everything Traefik needs to do its own TLS/ACME (self-signed
placeholder generation, imported-cert validation, or nothing at all for
`acme`, which issues lazily at runtime); Traefik is the one and only thing
holding certificates and terminating TLS.

Pangolin has no OIDC config.yml key: confirmed against docs.pangolin.net.
Identity-provider setup (Authentik/OIDC/Google/Azure) is a Server-Admin-
dashboard-only operation, post-deployment. Nothing here attempts to
template a key that does not exist; the handoff card points at Identity
Providers -> Add Identity Provider instead.

Also renders + pushes the "we'll be back" maintenance page and its own tiny
always-on static-file container (see pangolin-compose.yml.j2 and README
"Maintenance page"); Traefik's `errors` middleware falls back to it,
dashboard host only, whenever the pangolin container is unreachable, which
is exactly the state `charmer shutdown` (lifecycle.py) leaves it in on
purpose.

Real-world gotcha, hit on a reused host: 51820/udp can already be bound by
something charmer never provisioned (concretely: a pre-existing, unrelated
wg0 WireGuard interface with its own peers) even though preflight saw it
free at the start of the run; preflight only checks once, up front, not
continuously. That makes `docker compose up` fail on Gerbil specifically
("failed to bind host port 0.0.0.0:51820/udp: address already in use")
*after* Postgres/Pangolin/maintenance are already up healthy. Since
Gerbil's start_port is hardcoded (see pangolin-config.yml.j2), this can't
be fixed by remapping the Docker host-side publish; the port has to
actually be free. Find the real owner by hand (`ss -ulnp`, `wg show`,
`docker ps -a`, `systemctl list-timers` on the host) and stop it; `clean`
won't do this since it isn't charmer's to clean up. Then re-run `--only
pangolin`: `docker compose up -d` is idempotent, so it only retries
Gerbil/Traefik.
"""

from __future__ import annotations

import base64
import datetime as dt
import getpass
import ipaddress
import mimetypes
import os
import shlex
from urllib.parse import quote

from ..remote import gen_password, push_binary, push_file, render, wait_for
from .base import Phase, PhaseContext, verify_public_reachable
from .newt_ops import redial_all
from .restore_phase import _wait_stable

CONFIG_DIR = "/opt/pangolin/config"
CERT_DIR = f"{CONFIG_DIR}/traefik/certs"
FULLCHAIN = f"{CERT_DIR}/fullchain.pem"
PRIVKEY = f"{CERT_DIR}/privkey.pem"
MAINTENANCE_DIR = f"{CONFIG_DIR}/maintenance"

# Not a config knob: this is charmer's own always-on static page, not part
# of the official layout (see pangolin-compose.yml.j2); nothing external
# depends on its version the way Newt's does on Pangolin/Gerbil.
MAINTENANCE_TAG = "1.27-alpine"
# Loopback-only (bridge-published, not network_mode: host, see the compose
# template); arbitrary but clear of every other port this stack uses
# (80/443 gerbil/traefik, 3000-3004 pangolin/gerbil, 5432 postgres, 51820/21820 gerbil).
MAINTENANCE_PORT = 8091


# Order restarts run in: postgres before pangolin (which needs it), traefik
# last (it serves the maintenance page while pangolin restarts).
_RESTART_ORDER = ("postgres", "pangolin", "gerbil", "maintenance", "traefik")
_BROKEN_STATES = ("restarting", "exited", "dead", "created")

# service -> (container id, state, health), from `docker compose ps -a`.
Services = dict[str, tuple[str, str, str]]


def restart_pangolin_first(before: Services, pangolin_files: bool) -> bool:
    """Whether to `restart pangolin` BEFORE `docker compose up -d`.

    Traefik has `depends_on: pangolin: service_healthy`, and compose checks
    that on every `up`, even with traefik already running and unchanged: an
    unhealthy pangolin makes `up` fail ("dependency failed to start"), and
    can leave a recreated traefik created but not started. So a changed
    config.yml (which pangolin only reads at startup) or a pangolin that
    isn't healthy right now gets its restart first, while gerbil/traefik
    stay up and serve the maintenance page. Never on a first run.
    """
    if not before or "pangolin" not in before:
        return False
    _, state, health = before["pangolin"]
    return pangolin_files or state != "running" or health not in ("healthy", "")


def rollout_actions(before: Services, after: Services, traefik_files: bool) -> tuple[list[str], bool]:
    """Decide what still needs doing after a plain `docker compose up -d`.

    Compose already recreated every service whose *definition* changed (its
    config-hash label). It can't see bind-mounted file content: changed
    Traefik config/certs mean a `restart traefik` (the files are rewritten
    in place, so the existing mount already sees them). The maintenance
    page needs nothing, nginx reads index.html per request. A recreated
    postgres means a `restart pangolin`, so it reconnects cleanly.

    Returns (services to `restart`, in order; whether to force-recreate
    traefik). Traefik is force-recreated whenever gerbil was recreated or
    gets restarted: it lives in gerbil's network namespace (see lifecycle.py). A first run
    (nothing existed before) needs neither.
    """
    if not before:
        return [], False
    recreated = {svc for svc, (cid, _, _) in after.items() if before.get(svc, ("", "", ""))[0] != cid}
    gerbil_recreated = "gerbil" in recreated
    restart: set[str] = set()
    if "postgres" in recreated:
        restart.add("pangolin")
    if traefik_files:
        restart.add("traefik")
    # Recover anything left crash-looping by an earlier failed apply: it
    # could otherwise sit in Docker's restart backoff past the health wait.
    restart |= {svc for svc, (_, state, _) in after.items() if state in _BROKEN_STATES}
    restart -= recreated
    # A gerbil restart gets it a new network namespace too, same as a
    # recreate; either way traefik must be recreated, a restart won't do.
    recreate_traefik = gerbil_recreated or "gerbil" in restart
    if recreate_traefik:
        restart.discard("traefik")
    return [svc for svc in _RESTART_ORDER if svc in restart], recreate_traefik


class PangolinPhase(Phase):
    name = "pangolin"

    # ------------------------------------------------------------------ util
    def _image_tag(self, cfg) -> str:
        return f"postgresql-{cfg.pangolin.tag}" if cfg.pangolin.database == "postgres" else cfg.pangolin.tag

    def _ask_server_secret(self, ctx: PhaseContext) -> str:
        """Asked once, ever, the first time `pangolin` runs for this site
        (see _secrets()): Pangolin's server.secret is what encrypts/signs
        everything of its own that ends up in Postgres (sessions, 2FA,
        stored resource passwords). A fresh install has no reason to care
        what it is: Enter generates a random one, same as before this
        prompt existed. It only matters when restoring a dump produced by a
        *different* Pangolin deployment (README "restore"): that data was
        encrypted under the OLD deployment's secret, so this fresh site
        needs to be pinned to the SAME value before `pangolin` first
        renders config.yml, not patched in afterward by hand-editing the
        state file.
        """
        v = getpass.getpass(
            "Pangolin server.secret (hidden, pinned in state, never written to the config "
            "file). Leave blank to generate a new one (fresh install). Paste an EXISTING "
            "value only if you're about to restore a dump from a DIFFERENT Pangolin "
            "deployment onto this site: that dump's encrypted data (sessions/2FA/resource "
            "passwords) only decrypts under the secret it was written with: ")
        return v or gen_password()

    def _secrets(self, ctx: PhaseContext) -> dict[str, str]:
        g = ctx.state.get_or_generate
        secrets = {"server_secret": g("pangolin_server_secret", lambda: self._ask_server_secret(ctx))}
        if ctx.cfg.pangolin.database == "postgres":
            secrets["postgres_password"] = g("pangolin_postgres_password", gen_password)
        if ctx.cfg.smtp.enabled:
            def ask() -> str:
                v = getpass.getpass(
                    "SMTP password for "
                    f"{ctx.cfg.smtp.user}@{ctx.cfg.smtp.host} (hidden, pinned in state, "
                    "never written to the config file): ")
                while not v:
                    v = getpass.getpass("  (required) SMTP password: ")
                return v
            secrets["smtp_pass"] = g("pangolin_smtp_pass", ask)
        return secrets

    def _maintenance_context(self, ctx: PhaseContext) -> dict:
        logo = ctx.cfg.maintenance.logo
        logo_data_uri = None
        if logo:
            path = os.path.expanduser(logo)
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            data = base64.b64encode(open(path, "rb").read()).decode()
            logo_data_uri = f"data:{mime};base64,{data}"
        return {"message": ctx.cfg.maintenance.message, "logo_data_uri": logo_data_uri}

    def _tls_context(self, ctx: PhaseContext) -> dict:
        provider = ctx.cfg.tls.provider
        tls_enabled = provider != "none"
        cert_resolver = "letsencrypt" if provider == "acme" else None
        default_cert = provider in ("self_signed", "import")
        return {
            "tls_enabled": tls_enabled,
            "cert_resolver": cert_resolver,
            "acme_email": (ctx.cfg.tls.acme or {}).get("email", ""),
            "acme_directory_url": (ctx.cfg.tls.acme or {}).get("directory_url", ""),
            "default_cert_file": "/etc/traefik/certs/fullchain.pem" if default_cert else "",
            "default_key_file": "/etc/traefik/certs/privkey.pem" if default_cert else "",
        }

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        secret_note = ("" if "pangolin_server_secret" in ctx.state.data["generated"]
                      else ", you will be asked once (hidden input; Enter generates a new one, "
                           "or paste an existing server.secret when restoring a dump from a "
                           "different Pangolin deployment)")
        lines = [
            f"render + push the official Compose ({self._image_tag(cfg)}, bridge networking + "
            f"traefik as network_mode: service:gerbil) and config.yml on {ctx.host.name}",
            f"server secret: generated once (or provided by you), pinned in state, never printed{secret_note}",
        ]
        if cfg.pangolin.database == "postgres":
            lines.append("Postgres password: generated once, pinned in state, never printed")
            pg_port = cfg.pangolin.postgres_loopback_port
            if pg_port:
                lines.append("Postgres runs as a plain container on the compose bridge network, reachable "
                             f"from pangolin as `postgres:5432`, and published loopback-only on the host "
                             f"as 127.0.0.1:{pg_port} (pangolin.postgres_loopback_port), never off-host")
            else:
                lines.append("Postgres runs as a plain container on the compose bridge network, "
                             "never published to the host, reachable only from pangolin as `postgres:5432`")
        else:
            lines.append("SQLite (lab only); no Postgres container")
        provider = cfg.tls.provider
        if provider == "none":
            lines.append("tls: none; Traefik's `web` entrypoint only, plain HTTP")
        elif provider == "acme":
            lines.append(f"tls: acme; Traefik's own httpChallenge resolver, {cfg.tls.acme.get('directory_url', '?')}")
        elif provider == "self_signed":
            lines.append(f"tls: self_signed; 10y self-signed cert generated for {cfg.dashboard_host}, "
                         "used as Traefik's default certificate")
        else:
            lines.append(f"tls: import; validate {cfg.tls.import_.get('fullchain')} on this workstation "
                         "and place it as Traefik's default certificate")
        enable_api = cfg.pangolin.integration_api_enabled
        if enable_api is None:
            enable_api = bool(cfg.newt_agents)
        if enable_api:
            reason = (f"used by the newt phase to provision {len(cfg.newt_agents)} agent(s)"
                      if cfg.newt_agents else "pangolin.integration_api.enabled is set")
            lines.append(f"enable Pangolin's integration API (loopback-only, port "
                         f"{cfg.pangolin.integration_api_port}): {reason}")
        if cfg.smtp.enabled:
            note = "" if "pangolin_smtp_pass" in ctx.state.data["generated"] else ", you will be asked once (hidden input)"
            lines.append(f"config.yml email: section ({cfg.smtp.host}:{cfg.smtp.port}, user {cfg.smtp.user}): "
                         f"SMTP password pinned in state, never written to the config file{note}")
        lines.append(f"render + push the maintenance page (nginx:{MAINTENANCE_TAG}, loopback-only): "
                     "Traefik falls back to it on the dashboard host whenever pangolin is unreachable, "
                     "including during `charmer shutdown` (resource subdomains are not covered, see README 'Ingress')")
        lines.append("docker compose up -d (compose recreates only services whose definition changed), "
                     "with only what a changed bind-mounted file needs restarted (config.yml -> pangolin, "
                     "before the up; Traefik config/cert -> traefik; pangolin too if postgres was "
                     "recreated), traefik "
                     "force-recreated only if gerbil was recreated/restarted; health-gate on Pangolin's own healthcheck. "
                     "Gerbil/traefik stay up otherwise, so the maintenance page covers a pangolin restart")
        if cfg.newt_agents:
            lines.append(f"if gerbil gets recreated/restarted: recreate newt (down + up) on the {len(cfg.newt_agents)} "
                         "configured agent(s) so their tunnels redial (a pangolin/traefik/postgres restart "
                         "needs nothing, Newt reconnects its websocket itself); connectors charmer doesn't "
                         "manage are listed for a manual restart")
        else:
            lines.append("if gerbil gets recreated/restarted: list any Newt/site connectors in Pangolin's "
                         "database for a manual restart (none are managed by charmer)")
        return lines

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        conn = ctx.host
        node = conn.name
        sec = self._secrets(ctx)
        tlsctx = self._tls_context(ctx)
        enable_api = cfg.pangolin.integration_api_enabled
        if enable_api is None:
            enable_api = bool(cfg.newt_agents)
        integration_port = cfg.pangolin.integration_api_port

        dirs = f"{CONFIG_DIR}/traefik/logs {CONFIG_DIR}/letsencrypt {CERT_DIR} {MAINTENANCE_DIR}"
        if cfg.pangolin.database == "postgres":
            dirs += f" {CONFIG_DIR}/postgres"
        conn.run(f"mkdir -p {dirs}", sudo=True)

        cert_changed = False
        if cfg.tls.provider == "self_signed":
            cert_changed = self._generate_self_signed(ctx, conn)
        elif cfg.tls.provider == "import":
            cert_changed = self._import_cert(ctx, conn)

        postgres_conn_str = ""
        if cfg.pangolin.database == "postgres":
            # Container DNS name, not 127.0.0.1: postgres is a plain
            # compose-network service now, reachable from the pangolin
            # container by name, see pangolin-compose.yml.j2.
            postgres_conn_str = (f"postgresql://{quote(cfg.pangolin.postgres_user)}:"
                                 f"{quote(sec['postgres_password'])}@postgres:5432/pangolin")

        compose = render("pangolin-compose.yml.j2",
                         pangolin_image_tag=self._image_tag(cfg), gerbil_tag=cfg.pangolin.gerbil_tag,
                         traefik_tag=cfg.pangolin.traefik_tag, database=cfg.pangolin.database,
                         postgres_tag=cfg.pangolin.postgres_tag, postgres_user=cfg.pangolin.postgres_user,
                         postgres_password=sec.get("postgres_password", ""),
                         maintenance_tag=MAINTENANCE_TAG, maintenance_port=MAINTENANCE_PORT,
                         tls_enabled=tlsctx["tls_enabled"], enable_integration_api=enable_api,
                         integration_port=integration_port,
                         postgres_loopback_port=cfg.pangolin.postgres_loopback_port)
        app_config = render("pangolin-config.yml.j2",
                            base_url=cfg.base_url, dashboard_host=cfg.dashboard_host,
                            base_domain=cfg.pangolin.base_domain, server_secret=sec["server_secret"],
                            enable_integration_api=enable_api, integration_port=integration_port,
                            database=cfg.pangolin.database,
                            postgres_connection_string=postgres_conn_str,
                            smtp_enabled=cfg.smtp.enabled, smtp_host=cfg.smtp.host,
                            smtp_port=cfg.smtp.port, smtp_user=cfg.smtp.user,
                            smtp_pass=sec.get("smtp_pass", ""), smtp_no_reply=cfg.smtp.no_reply,
                            smtp_secure=cfg.smtp.secure,
                            smtp_tls_reject_unauthorized=cfg.smtp.tls_reject_unauthorized)
        traefik_config = render("traefik-config.yml.j2", **tlsctx)
        dynamic_config = render("traefik-dynamic-config.yml.j2", dashboard_host=cfg.dashboard_host,
                                maintenance_port=MAINTENANCE_PORT, **tlsctx)
        maintenance_page = render("maintenance.html.j2", **self._maintenance_context(ctx))

        c1 = push_file(conn, compose, "/opt/pangolin/docker-compose.yml", mode="0600")
        c2 = push_file(conn, app_config, f"{CONFIG_DIR}/config.yml", mode="0600")
        c3 = push_file(conn, traefik_config, f"{CONFIG_DIR}/traefik/traefik_config.yml", mode="0644")
        c4 = push_file(conn, dynamic_config, f"{CONFIG_DIR}/traefik/dynamic_config.yml", mode="0644")
        c5 = push_file(conn, maintenance_page, f"{MAINTENANCE_DIR}/index.html", mode="0644")
        changed = any((c1, c2, c3, c4, c5, cert_changed))
        ctx.record(node, "config rendered", True, "changed" if changed else "unchanged")

        # No "skip if already running" shortcut: `docker compose up -d` is itself
        # idempotent, and this phase only runs at all when the phase isn't already
        # marked done (first apply, --replay, or recovering from a prior failure;
        # see run_phases()). A container-count check here previously let a partial
        # failure (e.g. postgres/maintenance up, pangolin crash-looping on stale
        # config) look "already running" and skip straight to the health wait.
        #
        # No blanket --force-recreate either: that took down gerbil (the
        # 80/443 listener) and traefik on every change, so the maintenance
        # page couldn't show and every tunnel dropped. Compose recreates only
        # what changed in the compose file; restart_pangolin_first() and
        # rollout_actions() cover bind-mounted file changes and recovery.
        before = self._services(conn)
        if restart_pangolin_first(before, pangolin_files=c2):
            self._restart(ctx, conn, "pangolin", "gerbil/traefik stay up, serving the maintenance page")

        ctx.begin(node, "docker compose up", "image pull can take minutes on first run")
        r = conn.run("cd /opt/pangolin && docker compose up -d", timeout=1800)
        forced = False
        if not r.ok and before:
            # Last resort, the pre-0.10.1 behavior: recreate the whole stack.
            ctx.record(node, "docker compose up", False, f"{r.err}; falling back to --force-recreate", warn=True)
            ctx.begin(node, "docker compose up --force-recreate", "whole stack, brief outage")
            r = conn.run("cd /opt/pangolin && docker compose up -d --force-recreate", timeout=1800)
            before = {}  # everything is fresh now, nothing left to roll out
            forced = True
        ctx.record(node, "starting", r.ok, r.err if not r.ok else "")
        if not r.ok:
            self._dump_logs(conn)
            raise RuntimeError("docker compose up failed, see output above")

        after = self._services(conn)
        if before:
            recreated = sorted(svc for svc, (cid, _, _) in after.items() if before.get(svc, ("", "", ""))[0] != cid)
            ctx.record(node, "recreated by compose (definition changed)", True, ", ".join(recreated) or "none")
        restarts, recreate_traefik = rollout_actions(before, after, traefik_files=c3 or c4 or cert_changed)
        for svc in restarts:
            if svc == "pangolin" and before and after.get("postgres", ("",))[0] != before.get("postgres", ("",))[0]:
                pg_ok = wait_for(conn, "cd /opt/pangolin && docker compose ps postgres --format '{{.Health}}'",
                                 expect="healthy", timeout=120, interval=3,
                                 tick=lambda e: ctx.tick(f"waiting for the recreated postgres ({int(e)}s/120s)"))
                ctx.record(node, "recreated postgres healthy", pg_ok, "")
                if not pg_ok:
                    self._dump_logs(conn)
                    raise RuntimeError("recreated postgres never became healthy")
            self._restart(ctx, conn, svc)
        if recreate_traefik:
            ctx.begin(node, "confirming gerbil is stable")
            stable = _wait_stable(conn, "gerbil", ctx)
            ctx.record(node, "gerbil stable", stable, "" if stable else "gerbil kept restarting")
            if not stable:
                self._dump_logs(conn)
                raise RuntimeError("gerbil did not reach a stable running state")
            ctx.begin(node, "recreating traefik", "gerbil was recreated/restarted; traefik lives in its network namespace")
            r = conn.run("cd /opt/pangolin && docker compose up -d --no-deps --force-recreate traefik",
                         timeout=60)
            ctx.record(node, "traefik recreated", r.ok, r.err if not r.ok else "")
            if not r.ok:
                raise RuntimeError("failed to recreate traefik")

        healthy = wait_for(conn, "cd /opt/pangolin && docker compose ps pangolin --format '{{.Health}}'",
                           expect="healthy", timeout=600, interval=5,
                           tick=lambda elapsed: ctx.tick(f"waiting for pangolin healthy ({int(elapsed)}s/600s)"))
        ctx.record(node, "pangolin healthy", healthy, "" if healthy else "never became healthy")
        if not healthy:
            self._dump_logs(conn)
            raise RuntimeError(f"{node}: pangolin never became healthy")

        # Only a restarted gerbil strands Newt tunnels (see newt_ops.py);
        # recreate_traefik is exactly "gerbil was recreated or restarted".
        # After the health wait: a redialing Newt needs pangolin to answer.
        if recreate_traefik or forced:
            redial_all(ctx, "gerbil was restarted, so every Newt tunnel had to redial")

    def _services(self, conn) -> Services:
        r = conn.run("cd /opt/pangolin 2>/dev/null && docker compose ps -a "
                     "--format '{{.Service}}|{{.ID}}|{{.State}}|{{.Health}}'")
        out: Services = {}
        for line in r.out.splitlines() if r.ok else []:
            parts = line.strip().split("|")
            if len(parts) == 4:
                out[parts[0]] = (parts[1], parts[2], parts[3])
        return out

    def _restart(self, ctx: PhaseContext, conn, svc: str, detail: str = "") -> None:
        ctx.begin(conn.name, f"restarting {svc}", detail)
        r = conn.run(f"cd /opt/pangolin && docker compose restart {svc}", timeout=120)
        ctx.record(conn.name, f"{svc} restarted", r.ok, r.err if not r.ok else "")
        if not r.ok:
            self._dump_logs(conn)
            raise RuntimeError(f"failed to restart {svc}")

    def _dump_logs(self, conn) -> None:
        for svc in ("pangolin", "gerbil", "traefik"):
            tail = conn.run(f"cd /opt/pangolin && docker compose logs --tail 40 {svc}").out
            print(f"\n--- {svc} (last 40 lines) ---\n{tail}\n")

    # ------------------------------------------------------------ providers
    def _generate_self_signed(self, ctx: PhaseContext, conn) -> bool:
        node = conn.name
        cn = ctx.cfg.dashboard_host
        r = conn.run(
            f"test -s {FULLCHAIN} && test -s {PRIVKEY} && "
            f"openssl x509 -in {FULLCHAIN} -noout -checkend 2592000 && "
            f"openssl x509 -in {FULLCHAIN} -noout -ext subjectAltName | grep -q {shlex.quote(cn)}",
            sudo=True)
        if r.ok:
            ctx.record(node, "self-signed cert", True, "existing cert matches and is valid >30d, kept")
            return False

        try:
            ipaddress.ip_address(cn)
            sans = [f"IP:{cn}"]
        except ValueError:
            sans = [f"DNS:{cn}"]
        if f"IP:{ctx.cfg.host_ip}" not in sans:
            sans.append(f"IP:{ctx.cfg.host_ip}")
        san = ",".join(sans)
        cmd = (f"cd {CERT_DIR} && openssl req -x509 -nodes -days 3650 -newkey rsa:2048 "
              f"-keyout privkey.pem -out fullchain.pem -subj '/O=charmer/CN={cn}' "
              f"-addext 'subjectAltName={san}' && chmod 600 privkey.pem")
        ctx.begin(node, "generating self-signed cert", "10y")
        r = conn.run(cmd, timeout=60, sudo=True)
        ctx.record(node, "self-signed cert generated", r.ok, r.err.splitlines()[-1] if (not r.ok and r.err) else "")
        if not r.ok:
            raise RuntimeError("openssl generation failed")
        return True

    def _import_cert(self, ctx: PhaseContext, conn) -> bool:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cfg = ctx.cfg
        node = conn.name
        chain_path = os.path.expanduser(cfg.tls.import_["fullchain"])
        key_path = os.path.expanduser(cfg.tls.import_["privkey"])
        chain_bytes = open(chain_path, "rb").read()
        key_bytes = open(key_path, "rb").read()

        cert = x509.load_pem_x509_certificate(chain_bytes)
        key = serialization.load_pem_private_key(key_bytes, password=None)
        spki = lambda k: k.public_bytes(  # noqa: E731
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        match = spki(cert.public_key()) == spki(key.public_key())
        ctx.record("workstation", "private key matches certificate", match, "" if match else "SubjectPublicKeyInfo mismatch")
        if not match:
            raise RuntimeError("privkey does not match fullchain: wrong file pair?")

        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            san = []
        covered = cfg.dashboard_host in san or any(
            h.startswith("*.") and cfg.dashboard_host.endswith(h[1:]) for h in san)
        ctx.record("workstation", f"SAN covers {cfg.dashboard_host}", covered, f"SANs: {san}")
        if not covered:
            raise RuntimeError("certificate SAN does not cover the configured dashboard hostname")

        expiry = cert.not_valid_after_utc
        days_left = (expiry - dt.datetime.now(dt.timezone.utc)).days
        ctx.record("workstation", "certificate validity", days_left > 0,
                   f"expires {expiry.date()} ({days_left} days)", warn=(0 < days_left <= 30))
        if days_left <= 0:
            raise RuntimeError("certificate is already expired")

        c1 = push_binary(conn, chain_path, FULLCHAIN, mode="0644")
        c2 = push_binary(conn, key_path, PRIVKEY, mode="0600")
        ctx.record(node, "cert pushed", True, CERT_DIR)
        ctx.state.data["generated"]["tls_cert_expiry"] = expiry.date().isoformat()
        ctx.state.save()
        return c1 or c2

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        conn = ctx.host
        node = conn.name
        ok = True
        r = conn.run("curl -sk -o /dev/null -w '%{http_code}' http://127.0.0.1:3001/api/v1/")
        api_ok = r.out in ("200", "204")
        ctx.record(node, "verify: pangolin API responds", api_ok, f"HTTP {r.out}")
        ok = ok and api_ok

        r = conn.run("cd /opt/pangolin && docker compose ps maintenance --format '{{.State}}'")
        maint_ok = r.out.strip() == "running"
        ctx.record(node, "verify: maintenance page container running", maint_ok, r.out or r.err)
        ok = ok and maint_ok

        enable_api = ctx.cfg.pangolin.integration_api_enabled
        if enable_api is None:
            enable_api = bool(ctx.cfg.newt_agents)
        if enable_api:
            port = ctx.cfg.pangolin.integration_api_port
            r = conn.run(f"curl -sk -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/v1/")
            api3_ok = r.out not in ("", "000")
            ctx.record(node, "verify: integration API reachable", api3_ok, f"HTTP {r.out}")
            ok = ok and api3_ok

        pg_port = ctx.cfg.pangolin.postgres_loopback_port
        if pg_port:
            # Every listener on that port must be loopback: catches a
            # hand-edited compose (or anything else) exposing it wider.
            r = conn.run(f"ss -Htln 'sport = :{pg_port}' | awk '{{print $4}}'")
            addrs = [a for a in r.out.split() if a]
            pg_ok = bool(addrs) and all(a.startswith(("127.", "[::1]")) for a in addrs)
            ctx.record(node, f"verify: postgres published loopback-only on {pg_port}", pg_ok,
                       ", ".join(addrs) if addrs else "nothing listening")
            ok = ok and pg_ok

        # End-to-end over the actual public interface (Gerbil's wildcard
        # 80/443 publish, Traefik riding along via network_mode:
        # service:gerbil, see pangolin-compose.yml.j2 and README
        # "Ingress"). Gerbil/Traefik only start once pangolin's own
        # healthcheck flips (depends_on: condition: service_healthy above),
        # so right after that they can still be a few seconds from
        # listening; poll briefly instead of failing on the first attempt.
        ok = ok and verify_public_reachable(ctx)
        return ok
