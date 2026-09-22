"""Site configuration: load, validate, and expose as typed objects.

The file is the record; prompts (in the phases that need them) are the
fallback for the handful of settings that have no safe file-wide default:
see ``phases/pangolin_phase.py``'s OIDC and SMTP-password handling.
Everything else is validated here, in one pass, so problems are reported
together instead of one SSH connection at a time.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_HOSTNAME_LABEL_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


def _valid_hostname(v: str) -> bool:
    return bool(v) and len(v) <= 253 and all(_HOSTNAME_LABEL_RE.match(label) for label in v.split("."))

VALID_ENVIRONMENTS = {"lab", "production"}
VALID_TLS_PROVIDERS = {"none", "self_signed", "acme", "import"}
VALID_SSH_AUTH = {"agent", "key", "password"}
VALID_DATABASES = {"postgres", "sqlite"}
VALID_MAINTENANCE_LOGO_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg"}

# Bump whenever a config-file change needs operator action to carry forward:
# a renamed/removed key, a default that would silently change behavior, a key
# that becomes required. `load()` refuses to run against a config whose
# site.config_version doesn't match this, pointing at CHANGELOG.md instead of
# guessing intent from a stale file. Record what each bump was about there.
CONFIG_SCHEMA_VERSION = 1

# TCP ports that must be free on the Pangolin host before provisioning
# (SSH excluded; preflight checks that separately). 80/443 are Gerbil's own
# public listeners (wildcard-published, Traefik riding along via
# network_mode: service:gerbil; see pangolin-compose.yml.j2 and README
# "Ingress"). Pangolin/Postgres never leave the compose network at all.
REQUIRED_FREE_TCP_PORTS = [80, 443]
# Gerbil's WireGuard ports, host-published wildcard, exactly as the
# official compose does it. See README "Ingress".
REQUIRED_FREE_UDP_PORTS = [51820, 21820]


class ConfigError(Exception):
    """Raised with a list of human-readable validation problems."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass
class SSHTarget:
    user: str = "root"
    auth: str = "agent"
    key_file: str | None = None
    port: int = 22
    become: bool = True
    # Opt-in, per-host: once this host's key/agent auth is proven (preflight
    # already gates the whole pipeline on that), the `base` phase locks sshd
    # to key-only. Refused at config-load time when auth == "password":
    # that combination would lock the operator out on the very apply that
    # turns it on.
    disable_password_auth: bool = False


@dataclass
class NewtAgent:
    name: str
    ip: str
    ssh: SSHTarget
    image_tag: str = "latest"
    tun_device: str = "/dev/net/tun"
    docker_socket: bool = False


@dataclass
class TLSConfig:
    provider: str = "self_signed"
    hostname: str = ""
    acme: dict = field(default_factory=dict)
    import_: dict = field(default_factory=dict)


@dataclass
class PangolinConfig:
    tag: str = "1.22.0"
    gerbil_tag: str = "1.5.0"
    traefik_tag: str = "v3.7.12"
    postgres_tag: str = "17"
    database: str = "postgres"
    postgres_user: str = "pangolin"
    base_domain: str | None = None


@dataclass
class MaintenanceConfig:
    """The Traefik-served "we'll be back" page shown for the dashboard host
    only (not resource subdomains, see README "Ingress") while `charmer
    shutdown` has pangolin/gerbil stopped. `logo` is a local file on the
    workstation, inlined as a data URI so the page needs no other assets."""

    logo: str | None = None
    message: str = "We'll be back shortly."


@dataclass
class SMTPConfig:
    """Pangolin config.yml's top-level `email:` section (confirmed against
    docs.pangolin.net; real schema key, unlike OIDC), used for password
    reset / invite / verification emails. `password` is deliberately not a
    field here: like the Newt Root API key, it is prompted once during the
    `pangolin` phase and pinned in state, never written to the config file
    (see pangolin_phase.py)."""

    enabled: bool = False
    host: str = ""
    port: int = 587
    user: str = ""
    no_reply: str = ""
    secure: bool = False
    tls_reject_unauthorized: bool = True


@dataclass
class SiteConfig:
    name: str
    environment: str
    config_version: int
    state_file: Path
    refuse_existing: bool
    host_ip: str
    host_hostname: str
    ssh: SSHTarget
    pangolin: PangolinConfig
    tls: TLSConfig
    maintenance: MaintenanceConfig
    smtp: SMTPConfig
    newt_agents: list[NewtAgent]
    restore_dump: str | None
    restore_destructive: bool
    monitor_ips: list[str]
    unattended_upgrades: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def dashboard_host(self) -> str:
        """The host/IP the dashboard and Traefik's cert are named for.

        Falls back to the Pangolin host's own IP, valid only for
        provider self_signed (config.py enforces that elsewhere), so the
        rest of the pipeline (Pangolin's own base_url, the cert's CN/SAN,
        the handoff landing card) all agree on one answer instead of
        drifting.
        """
        return self.tls.hostname or self.host_ip

    @property
    def public_scheme(self) -> str:
        return "http" if self.tls.provider == "none" else "https"

    @property
    def base_url(self) -> str:
        return f"{self.public_scheme}://{self.dashboard_host}"


def _get(d: dict, path: str, default=None):
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _validate_ssh(raw: dict, label: str, problems: list[str]) -> SSHTarget:
    auth = _get(raw, "auth", "agent")
    key_file = _get(raw, "key_file")
    disable_password_auth = bool(_get(raw, "disable_password_auth", False))
    target = SSHTarget(
        user=_get(raw, "user", "root"),
        auth=auth,
        key_file=key_file,
        port=int(_get(raw, "port", 22)),
        become=bool(_get(raw, "become", True)),
        disable_password_auth=disable_password_auth,
    )
    if auth not in VALID_SSH_AUTH:
        problems.append(f"{label}.auth must be one of {sorted(VALID_SSH_AUTH)}, got {auth!r}")
    if auth == "key":
        if not key_file:
            problems.append(f"{label}.auth is 'key' but {label}.key_file is not set")
        elif not Path(os.path.expanduser(key_file)).exists():
            problems.append(f"{label}.key_file does not exist: {key_file}")
    if disable_password_auth and auth == "password":
        problems.append(
            f"{label}.disable_password_auth is true but {label}.auth is 'password'. "
            "That combination would lock the operator out on the apply that turns it "
            "on; set auth to 'key' or 'agent' first, confirm SSH works, then enable this")
    return target


def load(path: str | Path) -> SiteConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError([f"Config file not found: {path}"])

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    raw_version = _get(raw, "site.config_version")
    if raw_version is None:
        raise ConfigError([
            "site.config_version is missing. This config predates charmer's "
            "config-version tracking. Check CHANGELOG.md for any config-affecting "
            "changes, update the file to match config.example.yml's current shape, "
            f"then add `config_version: {CONFIG_SCHEMA_VERSION}` to the site: block."
        ])
    if not isinstance(raw_version, int) or isinstance(raw_version, bool) or raw_version < 0:
        raise ConfigError([f"site.config_version must be a non-negative integer, got {raw_version!r}"])
    if raw_version < CONFIG_SCHEMA_VERSION:
        raise ConfigError([
            f"site.config_version is {raw_version}, charmer expects {CONFIG_SCHEMA_VERSION}. "
            "Config-affecting changes landed between those versions; check CHANGELOG.md, "
            f"update the file accordingly, then bump config_version to {CONFIG_SCHEMA_VERSION}."
        ])
    if raw_version > CONFIG_SCHEMA_VERSION:
        raise ConfigError([
            f"site.config_version is {raw_version}, this charmer only understands up to "
            f"{CONFIG_SCHEMA_VERSION}. This config was written for a newer charmer release."
        ])

    problems: list[str] = []

    # --- site ---
    name = _get(raw, "site.name")
    if not name:
        problems.append("site.name is required")
    environment = _get(raw, "site.environment", "lab")
    if environment not in VALID_ENVIRONMENTS:
        problems.append(f"site.environment must be one of {sorted(VALID_ENVIRONMENTS)}, got {environment!r}")

    # --- ssh (pangolin host) ---
    ssh = _validate_ssh(raw.get("ssh") or {}, "ssh", problems)

    # --- pangolin host + app ---
    host_ip = str(_get(raw, "pangolin.host.ip", ""))
    try:
        ipaddress.ip_address(host_ip)
    except ValueError:
        problems.append(f"pangolin.host.ip: invalid or placeholder IP: {host_ip!r}")

    # Optional: the OS-level hostname `base` sets on the Pangolin host
    # (hostnamectl + /etc/hosts), unrelated to tls.hostname/dashboard_host
    # (the DNS name Traefik's cert and Pangolin's base_url use) or host_ip
    # (what charmer connects to). Omit to leave the host's current hostname
    # untouched; `base` then falls back to an interactive prompt instead
    # (Enter to skip), same shape as monitor.ips. See README "base".
    host_hostname = str(_get(raw, "pangolin.host.hostname", "") or "").strip()
    if host_hostname and not _valid_hostname(host_hostname):
        problems.append(f"pangolin.host.hostname: invalid hostname: {host_hostname!r}")

    database = _get(raw, "pangolin.database", "postgres")
    if database not in VALID_DATABASES:
        problems.append(f"pangolin.database must be one of {sorted(VALID_DATABASES)}, got {database!r}")
    if environment == "production" and database == "sqlite":
        problems.append("sqlite is lab-only; production requires postgres")

    base_domain = _get(raw, "pangolin.base_domain")
    if not base_domain:
        problems.append(
            "pangolin.base_domain is required: Pangolin CE refuses to start "
            "('Validation error: At least one domain must be defined') without "
            "at least one domain configured; it cannot be added later from the "
            "dashboard before first boot")

    for tag_key in ("tag", "gerbil_tag", "traefik_tag", "postgres_tag"):
        raw_tag = _get(raw, f"pangolin.{tag_key}")
        if raw_tag is not None and not isinstance(raw_tag, str):
            problems.append(
                f"pangolin.{tag_key} must be quoted; YAML read it as the "
                f"{type(raw_tag).__name__} {raw_tag!r}, not a version string. "
                f"Quote it: pangolin.{tag_key}: \"{raw_tag}\"")

    pangolin = PangolinConfig(
        tag=str(_get(raw, "pangolin.tag", "1.22.0")),
        gerbil_tag=str(_get(raw, "pangolin.gerbil_tag", "1.5.0")),
        traefik_tag=str(_get(raw, "pangolin.traefik_tag", "v3.7.12")),
        postgres_tag=str(_get(raw, "pangolin.postgres_tag", "17")),
        database=database,
        postgres_user=_get(raw, "pangolin.postgres_user", "pangolin"),
        base_domain=base_domain,
    )

    # --- tls ---
    tls = TLSConfig(
        provider=_get(raw, "tls.provider", "self_signed"),
        hostname=_get(raw, "tls.hostname", ""),
        acme=_get(raw, "tls.acme", {}) or {},
        import_=_get(raw, "tls.import", {}) or {},
    )
    if tls.provider not in VALID_TLS_PROVIDERS:
        problems.append(f"tls.provider must be one of {sorted(VALID_TLS_PROVIDERS)}, got {tls.provider!r}")
    if tls.provider == "none" and environment == "production":
        problems.append("tls.provider 'none' is refused when site.environment is 'production'")
    hostname_optional = tls.provider == "self_signed"
    if tls.provider in {"self_signed", "acme", "import"} and not tls.hostname and not hostname_optional:
        problems.append(f"tls.hostname is required for provider {tls.provider!r}")
    if tls.provider == "acme":
        if not tls.acme.get("directory_url"):
            problems.append("tls.acme.directory_url is required for provider 'acme'")
        if not tls.acme.get("email"):
            problems.append("tls.acme.email is required for provider 'acme'")
    if tls.provider == "import":
        for k in ("fullchain", "privkey"):
            p = tls.import_.get(k)
            if not p:
                problems.append(f"tls.import.{k} is required for provider 'import'")
            elif not Path(os.path.expanduser(p)).exists():
                problems.append(f"tls.import.{k} does not exist: {p}")

    # --- newt agents ---
    agents_raw = raw.get("newt_agents") or []
    agents: list[NewtAgent] = []
    seen_names: set[str] = set()
    seen_ips: set[str] = set()
    for i, a in enumerate(agents_raw):
        aname = a.get("name") or f"newt-{i + 1}"
        aip = str(a.get("ip", ""))
        try:
            ipaddress.ip_address(aip)
        except ValueError:
            problems.append(f"newt_agents[{i}] ({aname}): invalid or placeholder IP: {aip!r}")
        if aname in seen_names:
            problems.append(f"newt_agents[{i}]: duplicate name {aname!r}")
        if aip in seen_ips or aip == host_ip:
            problems.append(f"newt_agents[{i}] ({aname}): IP {aip} collides with another host")
        seen_names.add(aname)
        seen_ips.add(aip)
        agent_ssh = _validate_ssh(a.get("ssh") or {}, f"newt_agents[{i}].ssh", problems)
        image_tag = a.get("image_tag", "latest")
        if image_tag in (None, "", "latest"):
            problems.append(
                f"newt_agents[{i}] ({aname}): image_tag must be a pinned version, not "
                "'latest' or empty; an unpinned Newt can outrun the server-side "
                "Pangolin/Gerbil version and break compatibility silently")
        agents.append(NewtAgent(
            name=aname, ip=aip, ssh=agent_ssh, image_tag=str(image_tag),
            tun_device=a.get("tun_device", "/dev/net/tun"),
            docker_socket=bool(a.get("docker_socket", False)),
        ))

    # --- maintenance page ---
    logo = _get(raw, "maintenance.logo")
    if logo:
        logo_path = Path(os.path.expanduser(str(logo)))
        if not logo_path.exists():
            problems.append(f"maintenance.logo does not exist: {logo}")
        elif logo_path.suffix.lower() not in VALID_MAINTENANCE_LOGO_SUFFIXES:
            problems.append(
                f"maintenance.logo must be one of {sorted(VALID_MAINTENANCE_LOGO_SUFFIXES)}, "
                f"got {logo_path.suffix!r}")
    maintenance = MaintenanceConfig(
        logo=logo,
        message=_get(raw, "maintenance.message", "We'll be back shortly."),
    )

    # --- smtp (Pangolin config.yml's `email:` section: password reset /
    # invite emails; smtp_pass is prompted + pinned in state at provision
    # time, never stored here; see pangolin_phase.py) ---
    smtp_enabled = bool(_get(raw, "smtp.enabled", False))
    smtp = SMTPConfig(
        enabled=smtp_enabled,
        host=_get(raw, "smtp.host", ""),
        port=int(_get(raw, "smtp.port", 587)),
        user=_get(raw, "smtp.user", ""),
        no_reply=_get(raw, "smtp.no_reply", ""),
        secure=bool(_get(raw, "smtp.secure", False)),
        tls_reject_unauthorized=bool(_get(raw, "smtp.tls_reject_unauthorized", True)),
    )
    if smtp_enabled:
        for field_name in ("host", "user", "no_reply"):
            if not getattr(smtp, field_name):
                problems.append(f"smtp.{field_name} is required when smtp.enabled is true")

    # --- restore ---
    restore_dump = _get(raw, "restore.postgres_dump")
    if restore_dump and not str(restore_dump).endswith(".sql.gz"):
        problems.append("restore.postgres_dump must be a .sql.gz PostgreSQL dump")

    # --- monitor (optional) ---
    # Admin/monitoring source IPs the `base` phase scopes ssh to, on both the
    # Pangolin host and every Newt agent, instead of leaving it open to
    # anywhere; see base_setup.py. Omit entirely (or leave the list empty)
    # to keep today's behavior (ssh open to all sources).
    monitor_ips_raw = _get(raw, "monitor.ips") or []
    monitor_ips: list[str] = []
    if not isinstance(monitor_ips_raw, list):
        problems.append("monitor.ips must be a list of IP addresses or CIDR ranges")
    else:
        for i, ip in enumerate(monitor_ips_raw):
            ip = str(ip).strip()
            try:
                ipaddress.ip_network(ip, strict=False)
            except ValueError:
                problems.append(f"monitor.ips[{i}]: invalid IP or CIDR: {ip!r}")
                continue
            monitor_ips.append(ip)

    # --- base (optional) ---
    # Defaults to True (leave the OS's own unattended-upgrades/apt-daily-
    # upgrade.timer alone) so an existing config's behavior doesn't change
    # under it; set base.unattended_upgrades: false to have `base` mask both
    # units instead, closing the same package-drift-outside-the-provisioner's-
    # control gap on the OS's own silent schedule. Purely additive and
    # optional, so no config_version bump.
    unattended_upgrades = bool(_get(raw, "base.unattended_upgrades", True))

    if problems:
        raise ConfigError(problems)

    state_file = Path(_get(raw, "provision.state_file", f".state/{name}.json"))

    return SiteConfig(
        name=name,
        environment=environment,
        config_version=raw_version,
        state_file=state_file,
        refuse_existing=bool(_get(raw, "provision.refuse_existing", True)),
        host_ip=host_ip,
        host_hostname=host_hostname,
        ssh=ssh,
        pangolin=pangolin,
        tls=tls,
        maintenance=maintenance,
        smtp=smtp,
        newt_agents=agents,
        restore_dump=restore_dump,
        restore_destructive=bool(_get(raw, "restore.destructive", False)),
        monitor_ips=monitor_ips,
        unattended_upgrades=unattended_upgrades,
        raw=raw,
    )
