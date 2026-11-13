# charmer phases: what each does and what to watch

Read this when planning a run, explaining a plan to the operator, or deciding
between `--only` and `--replay`.

## preflight (read-only, no confirmation)
- Pangolin host: SSH and sudo, OS (Ubuntu 24.04 expected), disk, free ports
  `80`/`443` TCP and `51820`/`21820` UDP, and existing
  pangolin/gerbil/traefik/postgres containers (refused when
  `provision.refuse_existing`).
- Each Newt agent: SSH, OS (Ubuntu or Debian), Docker present, and a **real**
  TUN test (`ip tuntap add`, not just `test -c /dev/net/tun`).
- `pangolin_via_host_ip` agents: the private IP is RFC1918 and TCP 443 is
  reachable (warning only before the `pangolin` phase).
- Every host: Docker API on TCP (refused in production, warned in lab) and
  `ssh.socket`+`ssh.service` both owning the port (warning).
- It is state-aware: footprints of completed phases are expected on a resumed
  run.

## base
- Baseline packages and Docker CE from download.docker.com (Ubuntu or Debian
  repo picked from `/etc/os-release`). A host with `docker` but no
  `docker compose` fails rather than getting Docker CE forced over it.
- UFW: the Pangolin host allows ssh, 80, 443, 51820/udp and 21820/udp; agents
  allow ssh only (Newt is outbound-only).
- Opt-ins:
  - `monitor.ips` scopes ssh to an allow-list. It is asked once and pinned if
    unset, and the current SSH source IP is always added. UFW is additive:
    removing an IP later means `ufw delete` by hand.
  - `ssh.disable_password_auth` writes
    `/etc/ssh/sshd_config.d/01-charmer-hardening.conf`, validates with
    `sshd -T`, fixes an ssh.socket conflict, and confirms sshd is listening.
    It is refused together with `auth: password`.
  - `base.unattended_upgrades: false` masks unattended-upgrades and
    apt-daily-upgrade.timer.
  - `pangolin.host.hostname` sets the OS hostname and the `/etc/hosts`
    127.0.1.1 line. It is safe on a live site.
- `apt upgrade` is never run.

## pangolin
- Renders the official Compose layout verbatim: bridge networks,
  `network_mode: service:gerbil` on Traefik, and Gerbil publishing 80/443.
  Traefik does all TLS/ACME; nothing sits in front of it.
- Loopback-only publishes: `127.0.0.1:3001` (API), `127.0.0.1:3003`
  (integration API, on by default) and `127.0.0.1:5432` (Postgres, unless
  `postgres_loopback_port: null`). Verify fails if any of these bind beyond
  loopback.
- First run asks for `server.secret`. Press Enter to generate one, or paste
  the old install's secret when migrating. The value is pinned forever.
- With `smtp.enabled`, the SMTP password is asked for once and pinned.
- Re-running on a live stack restarts only what changed. A Gerbil change
  force-recreates Traefik and redials Newt agents.
- Verify: API on loopback, integration API, and an end-to-end request through
  the public interface.

## restore (optional)
- Needs `restore.postgres_dump` (`.sql.gz`) and `restore.destructive: true`.
  Otherwise, or if declined, it is recorded as skipped.
- Stops the stack, uploads and sha256-checks the dump, strips known
  version-skew `SET` lines, loads with `ON_ERROR_STOP=1`, deletes the dump from
  the host, then restarts and health-gates. On failure the stack is left
  **stopped**, not half-up.
- Patches this host's own Gerbil identity back onto its `exitNodes` row, so a
  dump from another deployment doesn't crash-loop Gerbil.
- Warns on any org `utilitySubnet` narrower than /20. With
  `restore.utility_subnet_prefix` set, it widens it when that is safe.
- Warns when a multi-exit-node dump has more tunnels than this exit node's
  address can hold.
- Redials managed Newt agents and lists unmanaged sites for manual restart.
- `--only restore` is the clean DB-only restore path.

## adopt_newt (only after a restore loaded a dump)
- Lists sites in the restored DB and asks for the SSH details of hosts
  running their connectors. Those details come from the operator, never from
  the database.
- Reads `newtId`/secret from the running container, checks them with
  get-token, then pins them and appends the agent to the config file (comments
  kept; rolled back if the file no longer loads).
- Skips systemd installs, CONFIG_FILE-based Newt, rejected credentials and
  duplicates.
- Afterwards: `charm provision CONFIG --replay preflight base`.

## newt
- Pinned credentials are checked with get-token first. A rejected pair is
  re-minted only if the operator says yes.
- New agents get credentials minted through the integration API, over SSH
  against loopback. The Root API key (only **Create Site** permission needed)
  and org ID are asked for once.
- Pushes `/opt/newt` with a pinned image and runs `docker compose up -d`.
- `tls.provider: self_signed` sets `SKIP_TLS_VERIFY=true` automatically.
- `pangolin_via_host_ip: true` adds an `extra_hosts` entry so the dashboard
  name resolves to `pangolin.host.ip` inside the container.
- Verify: each agent's `newt` container is running.

## handoff (read-only, no confirmation)
- Prints the dashboard URL and next steps (no secrets). Writes
  `config.<site>.monitor.yml` (mode 0600) for `monitor` and `logs`.

## Lifecycle commands (not pipeline phases)
- `shutdown`: stops `pangolin` only. Gerbil, Traefik and maintenance stay up.
  Traefik is force-recreated to avoid a stale network namespace.
- `start`: requires a prior graceful shutdown and redials agents if Gerbil had
  to restart.
- `clean`: reverses charmer's own footprint only. It never kills unrelated
  services (for example a foreign `wg0`) and does not delete Pangolin sites
  server-side. The state file is archived.
