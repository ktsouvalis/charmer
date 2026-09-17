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


class PangolinPhase(Phase):
    name = "pangolin"

    # ------------------------------------------------------------------ util
    def _image_tag(self, cfg) -> str:
        return f"postgresql-{cfg.pangolin.tag}" if cfg.pangolin.database == "postgres" else cfg.pangolin.tag

    def _secrets(self, ctx: PhaseContext) -> dict[str, str]:
        g = ctx.state.get_or_generate
        secrets = {"server_secret": g("pangolin_server_secret", gen_password)}
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
        lines = [
            f"render + push the official Compose ({self._image_tag(cfg)}, bridge networking + "
            f"traefik as network_mode: service:gerbil) and config.yml on {ctx.host.name}",
            "server secret / Postgres password: generated once, pinned in state, never printed",
        ]
        if cfg.pangolin.database == "postgres":
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
        if cfg.newt_agents:
            lines.append(f"enable Pangolin's integration API (loopback-only, port 3003): "
                         f"used by the newt phase to provision {len(cfg.newt_agents)} agent(s)")
        if cfg.smtp.enabled:
            note = "" if "pangolin_smtp_pass" in ctx.state.data["generated"] else ", you will be asked once (hidden input)"
            lines.append(f"config.yml email: section ({cfg.smtp.host}:{cfg.smtp.port}, user {cfg.smtp.user}): "
                         f"SMTP password pinned in state, never written to the config file{note}")
        lines.append(f"render + push the maintenance page (nginx:{MAINTENANCE_TAG}, loopback-only): "
                     "Traefik falls back to it on the dashboard host whenever pangolin is unreachable, "
                     "including during `charmer shutdown` (resource subdomains are not covered, see README 'Ingress')")
        lines.append("docker compose up -d, health-gate on Pangolin's own healthcheck (image pull can take minutes)")
        return lines

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        conn = ctx.host
        node = conn.name
        sec = self._secrets(ctx)
        tlsctx = self._tls_context(ctx)
        enable_api = bool(cfg.newt_agents)

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
                         tls_enabled=tlsctx["tls_enabled"], enable_integration_api=enable_api)
        app_config = render("pangolin-config.yml.j2",
                            base_url=cfg.base_url, dashboard_host=cfg.dashboard_host,
                            base_domain=cfg.pangolin.base_domain, server_secret=sec["server_secret"],
                            enable_integration_api=enable_api, database=cfg.pangolin.database,
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
        ctx.begin(node, "docker compose up", "image pull can take minutes on first run")
        # --force-recreate when config content changed: compose only recreates a
        # container when the *service definition* changes, not when a bind-mounted
        # file's content does. Without this, a container already crash-looping on
        # stale config (e.g. from a previous failed apply) can sit mid Docker
        # restart-backoff and never pick up the fix within this run's health wait.
        force = " --force-recreate" if changed else ""
        r = conn.run(f"cd /opt/pangolin && docker compose up -d{force}", timeout=1800)
        ctx.record(node, "starting", r.ok, r.err if not r.ok else "")
        if not r.ok:
            self._dump_logs(conn)
            raise RuntimeError("docker compose up failed, see output above")

        healthy = wait_for(conn, "cd /opt/pangolin && docker compose ps pangolin --format '{{.Health}}'",
                           expect="healthy", timeout=600, interval=5,
                           tick=lambda elapsed: ctx.tick(f"waiting for pangolin healthy ({int(elapsed)}s/600s)"))
        ctx.record(node, "pangolin healthy", healthy, "" if healthy else "never became healthy")
        if not healthy:
            self._dump_logs(conn)
            raise RuntimeError(f"{node}: pangolin never became healthy")

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

        if bool(ctx.cfg.newt_agents):
            r = conn.run("curl -sk -o /dev/null -w '%{http_code}' http://127.0.0.1:3003/v1/")
            api3_ok = r.out not in ("", "000")
            ctx.record(node, "verify: integration API reachable", api3_ok, f"HTTP {r.out}")
            ok = ok and api3_ok

        # End-to-end over the actual public interface (Gerbil's wildcard
        # 80/443 publish, Traefik riding along via network_mode:
        # service:gerbil, see pangolin-compose.yml.j2 and README
        # "Ingress"). Gerbil/Traefik only start once pangolin's own
        # healthcheck flips (depends_on: condition: service_healthy above),
        # so right after that they can still be a few seconds from
        # listening; poll briefly instead of failing on the first attempt.
        ok = ok and verify_public_reachable(ctx)
        return ok
