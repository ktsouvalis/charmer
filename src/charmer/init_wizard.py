"""Interactive `charmer init` wizard: answers get materialized into a
reviewable `config.<site>.yml`, the same "answer once, review before
touching anything" shape as akropolis's `init`.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml

from .config import CONFIG_SCHEMA_VERSION, VALID_SSH_AUTH, VALID_TLS_PROVIDERS

# Shown in place of an empty `newt_agents: []` when the wizard is answered
# with 0 agents, restoring, or just not ready to wire up an agent yet,
# shouldn't mean re-running init later. Mirrors config.example.yml's block
# so filling it in is copy/uncomment/edit, not guesswork.
NEWT_AGENTS_EXAMPLE = """\
# No Newt agents yet, add them here whenever you're ready (see README
# "newt") and re-run `charmer provision`, no need to redo init. Each agent
# is a fully separate SSH target, never assumed to be a VM, LXC container,
# or physical host. Example:
# newt_agents:
#   - name: patras-edge
#     ip: 192.0.2.20
#     ssh:
#       user: root
#       auth: agent
#       key_file: null
#       port: 22
#       become: true
#     image_tag: "1.17.0"          # pinned, never "latest" (config.py refuses it)
#     tun_device: /dev/net/tun
#     docker_socket: false         # true only if this agent needs Docker-label-based routing
"""


def _ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _ask_yn(label: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    v = input(f"{label}{suffix}: ").strip().lower()
    if not v:
        return default
    return v in ("y", "yes")


def _valid_ip_or_cidr(v: str) -> bool:
    try:
        ipaddress.ip_network(v, strict=False)
        return True
    except ValueError:
        return False


def _ask_ssh(label: str) -> dict:
    auth = _ask(f"{label} SSH auth ({'/'.join(sorted(VALID_SSH_AUTH))})", "agent")
    while auth not in VALID_SSH_AUTH:
        print(f"  must be one of {sorted(VALID_SSH_AUTH)}")
        auth = _ask(f"{label} SSH auth", "agent")
    key_file = _ask("  SSH key file", "~/.ssh/id_ed25519") if auth == "key" else None
    user = _ask(f"{label} SSH user", "root")
    become = _ask_yn(f"{label} needs sudo (become)", default=(user != "root"))
    return {"user": user, "auth": auth, "key_file": key_file, "port": 22, "become": become}


def run_wizard(output: str | None = None) -> Path:
    site_name = _ask("Site name", "pangolin")
    environment = _ask("Environment (lab/production)", "lab")
    while environment not in ("lab", "production"):
        environment = _ask("Environment (lab/production)", "lab")

    print("\n-- Pangolin host --")
    host_ip = _ask("Pangolin host IP")
    ssh = _ask_ssh("Pangolin host")

    print("\n-- TLS (public boundary, see README 'Ingress') --")
    default_provider = "self_signed" if environment == "lab" else "acme"
    provider = _ask(f"TLS provider ({'/'.join(sorted(VALID_TLS_PROVIDERS))})", default_provider)
    while provider not in VALID_TLS_PROVIDERS or (environment == "production" and provider == "none"):
        if provider == "none":
            print("  'none' is refused for production sites")
        else:
            print(f"  must be one of {sorted(VALID_TLS_PROVIDERS)}")
        provider = _ask("TLS provider", default_provider)
    hostname_optional = provider == "self_signed"
    hostname_prompt = "Dashboard hostname" + (" (Enter to fall back to the host IP)" if hostname_optional else "")
    hostname = _ask(hostname_prompt)
    while not hostname and not hostname_optional:
        hostname = _ask("Dashboard hostname (required for provider " + provider + ")")

    tls: dict = {"provider": provider, "hostname": hostname}
    if provider == "acme":
        print("  Rehearsing first? Point this at your CA's staging directory instead "
              "(Let's Encrypt: https://acme-staging-v02.api.letsencrypt.org/directory) "
              "and switch to the production URL once you're ready for the real cert.")
        tls["acme"] = {
            "directory_url": _ask("ACME directory URL", "https://acme-v02.api.letsencrypt.org/directory"),
            "email": _ask("ACME account email"),
        }
    elif provider == "import":
        tls["import"] = {
            "fullchain": _ask("Path to fullchain.pem"),
            "privkey": _ask("Path to privkey.pem"),
        }

    print("\n-- Pangolin app --")
    database = _ask("Database (postgres/sqlite; sqlite is lab-only)",
                    "postgres" if environment == "production" else "postgres")
    base_domain = _ask("Base domain for published resources")
    while not base_domain:
        base_domain = _ask("Base domain for published resources (required: Pangolin CE "
                           "refuses to start without at least one domain configured)")

    postgres_loopback_port = None
    if database == "postgres":
        print("  Postgres is never published to the host by default: pangolin reaches it over the "
              "compose network, and `docker exec postgres psql` works on the host without it. "
              "Publishing it on 127.0.0.1 only (never off-host) is useful for tools that need a TCP "
              "port, e.g. a GUI client over `ssh -L`.")
        if _ask_yn("  Publish Postgres on 127.0.0.1 of the Pangolin host", default=False):
            port = _ask("  Loopback port", "5432")
            while not (port.isdigit() and 1 <= int(port) <= 65535 and int(port) not in (3001, 3003, 8091)):
                port = _ask("  Loopback port (1-65535, not 3001/3003/8091)", "5432")
            postgres_loopback_port = int(port)

    print("\n-- Maintenance page ('we'll be back', shown on the dashboard host during `charmer shutdown`) --")
    org_name = _ask("Organization name (Enter to skip)")
    maintenance_message = (f"{org_name}, θα επιστρέψουμε σε λίγο." if org_name
                           else "Θα επιστρέψουμε σε λίγο.")

    print("\n-- SMTP (optional: password reset / invite emails; Enter to skip) --")
    smtp_enabled = _ask_yn("Configure SMTP", default=False)
    smtp: dict = {"enabled": smtp_enabled}
    if smtp_enabled:
        smtp["host"] = _ask("  SMTP host")
        smtp["port"] = int(_ask("  SMTP port", "587") or "587")
        smtp["user"] = _ask("  SMTP user")
        smtp["no_reply"] = _ask("  \"From\" address", smtp["user"] or "")
        smtp["secure"] = _ask_yn("  Use implicit TLS/SSL (port 465)", default=False)
        smtp["tls_reject_unauthorized"] = _ask_yn("  Reject invalid TLS certs", default=True)
        print("  (the SMTP password is NOT stored here: `charmer provision` asks for it once, "
              "hidden, and pins it in local state)")

    print("\n-- Newt agents --")
    count = int(_ask("Number of Newt agents", "0") or "0")
    agents = []
    for i in range(1, count + 1):
        print(f"  agent {i}:")
        name = _ask(f"  name", f"newt-{i}")
        ip = _ask("  IP")
        agent_ssh = _ask_ssh(f"  {name}")
        image_tag = _ask("  Newt image tag (pinned, not 'latest')", "1.17.0")
        agents.append({
            "name": name, "ip": ip, "ssh": agent_ssh, "image_tag": image_tag,
            "tun_device": "/dev/net/tun", "docker_socket": False,
        })

    print("\n-- Admin/monitoring access (optional) --")
    monitor_ips_raw = _ask("IP(s)/CIDR(s) to scope ssh to on every host, comma-separated "
                           "(Enter to leave ssh open to all)")
    monitor_ips = [ip.strip() for ip in monitor_ips_raw.split(",") if ip.strip()]
    while any(not _valid_ip_or_cidr(ip) for ip in monitor_ips):
        bad = [ip for ip in monitor_ips if not _valid_ip_or_cidr(ip)]
        monitor_ips_raw = _ask(f"  invalid: {', '.join(bad)}, try again (Enter to skip)")
        monitor_ips = [ip.strip() for ip in monitor_ips_raw.split(",") if ip.strip()]

    config = {
        "site": {"name": site_name, "environment": environment, "config_version": CONFIG_SCHEMA_VERSION},
        "provision": {"state_file": f".state/{site_name}.json", "refuse_existing": True},
        "ssh": ssh,
        "pangolin": {
            "host": {"ip": host_ip},
            "tag": "1.22.0", "gerbil_tag": "1.5.0", "traefik_tag": "v3.7.12", "postgres_tag": "17",
            "database": database, "postgres_user": "pangolin", "base_domain": base_domain,
            **({"postgres_loopback_port": postgres_loopback_port} if postgres_loopback_port else {}),
        },
        "tls": tls,
        "maintenance": {"logo": None, "message": maintenance_message},
        "smtp": smtp,
        "newt_agents": agents,
        "restore": {"postgres_dump": None, "destructive": False},
        **({"monitor": {"ips": monitor_ips}} if monitor_ips else {}),
    }
    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    if not agents:
        rendered = rendered.replace("newt_agents: []\n", NEWT_AGENTS_EXAMPLE + "newt_agents: []\n")

    path = Path(output) if output else Path(f"config.{site_name}.yml")
    path.write_text(rendered)
    path.chmod(0o600)
    return path
