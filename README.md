<p align="center">
<img src="./assets/charmer-logo-no-bg.png" width="30%" />
</p>

Provision a single self-hosted [Pangolin](https://pangolin.net) Community
Edition node and separate [Newt](https://docs.pangolin.net) site agents over
SSH: a reviewable config file, a resumable
plan/confirm/apply/verify phase pipeline, pinned-secret state, a full
transcript of every command run, and matching `monitor`/`logs` tooling.

Charmer is its own small app: one Pangolin
host, no etcd/Patroni/HAProxy/keepalived (official Pangolin CE has no
self-hosted HA at all, that's an Enterprise-only capability, see
[Roadmap](#roadmap)), plus however many independently-addressed Newt agents
you point it at.

## Stack

Charmer orchestrates the following open-source technologies over SSH; it
doesn't bundle, modify, or redistribute any of them.

| Component | Role | Link |
| :--- | :--- | :--- |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/pangolin.png" width="24" /> **Pangolin CE** | Identity-aware reverse proxy / dashboard | [pangolin.net](https://pangolin.net) |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/pangolin.png" width="24" /> **Newt** | Site agent, outbound-only tunnel to Pangolin | [docs.pangolin.net](https://docs.pangolin.net) |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/pangolin.png" width="24" /> **Gerbil** | WireGuard exit node, public ingress | [docs.pangolin.net](https://docs.pangolin.net) |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/traefik.png" width="24" /> **Traefik** | TLS/ACME termination, all routing | [traefik.io](https://traefik.io/) |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/postgresql.png" width="24" /> **PostgreSQL** | Database backend (production default) | [postgresql.org](https://www.postgresql.org/) |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/wireguard.png" width="24" /> **WireGuard** | The tunnel protocol under Gerbil/Newt | [wireguard.com](https://www.wireguard.com/) |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/docker.png" width="24" /> **Docker** | Container runtime | [docker.com](https://www.docker.com/) |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/python.png" width="24" /> **Python** | CLI automation engine | [python.org](https://www.python.org/) |
| <img src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/ubuntu-linux.svg" width="24" /> **Ubuntu** | Base OS (24.04) | [ubuntu.com](https://ubuntu.com/) |

*All trademarks are the property of their respective owners.*

## Status

| Phase | Status |
|---|---|
| preflight | read-only, implemented, verified (Docker-API-on-TCP + ssh.socket checks new in 0.9.0, unit-tested only) |
| base | implemented, verified |
| pangolin | implemented, verified, official Compose layout, exercised with `acme` (staging, then real Let's Encrypt production) |
| restore | implemented, verified, both the skipped path (no dump configured) and a real destructive restore |
| newt | implemented, verified, credentials minted automatically via the Pangolin API; agent provisioned, connected, and a private resource published through it and reached from outside |
| handoff | implemented, verified, read-only, emits the monitor config |

`shutdown` / `start` / `clean` lifecycle commands and `charmer monitor` /
`charmer logs` are implemented and have all been run end-to-end against a
real lab deployment, see [Verification status](#verification-status).

## Install

Download the single-file executable from the
[latest release](https://github.com/ktsouvalis/charmer/releases/latest):

```bash
sudo apt install python3-cryptography python3-bcrypt python3-nacl
curl -fLO https://github.com/ktsouvalis/charmer/releases/latest/download/charmer
chmod +x charmer
./charmer --version
```

That's the whole installation. The file is a self-contained
[zipapp](https://docs.python.org/3/library/zipapp.html) carrying charmer and
its pure-Python dependencies, no virtualenv, no `pip`, no root, nothing to
uninstall later. Drop it in `~/.local/bin` or `/usr/local/bin` if you want it
on `PATH`. Later, `charmer check-update` / `charmer update` / `charmer
whats-new` manage upgrades from there.

The three apt packages are paramiko's compiled dependencies; they're
deliberately *not* bundled, `zipimport` cannot load extension modules out of
a zip, and freezing a crypto library inside a release artifact is the wrong
posture for a tool that manages SSH credentials. `python3-cryptography` is
usually present already on a server install.

Prefer to run from source instead:

```bash
git clone https://github.com/ktsouvalis/charmer.git && cd charmer
python3 -m venv .venv && .venv/bin/pip install -e .
```

## Quickstart

```bash
charmer init                              # → config.<site>.yml

# read the file. this is the review-before-touching-anything step.

charmer provision config.<site>.yml --only preflight   # read-only
charmer provision config.<site>.yml                    # the full pipeline
```

(Running from source instead of the zipapp: use `.venv/bin/charmer` in place
of `charmer` above.)

Charmer keeps its state next to where you run it: `config.<site>.yml` and
`.state/<site>.json` (mode `0600`) resolve relative to the working
directory. Give each site its own directory, or set `provision.state_file`
explicitly.

## Commands

```
charmer init [-o FILE]              interactive wizard, writes config.<site>.yml

charmer provision CONFIG            run the phase pipeline (resumable)
  --only PHASE [PHASE...]              run only the named phase(s)
  --replay PHASE [PHASE...]            re-run specific completed phase(s)

charmer shutdown CONFIG             stop pangolin (postgres, newt agents, gerbil,
                                       traefik, and the maintenance page left
                                       running, see "Maintenance page")
charmer start CONFIG                start it again, refuses without a prior
                                       graceful shutdown

charmer clean CONFIG                tear the site down to a bare host (typed-name
                                       confirmation, every environment)
  --i-know-this-is-production          required additionally in production

charmer status CONFIG               phase progress + pinned state for a config file,
                                       local only (reads config + state file, no SSH)

charmer monitor CONFIG.monitor.yml  real-time health dashboard
charmer logs CONFIG.monitor.yml     cluster-wide log viewer (SSH)
  --last HOURS / --level LEVEL / --save FILE

charmer update                      install the latest release (zipapp binary only)
charmer check-update                check for a newer release without installing it
charmer whats-new [--all] [--version V]   show the changelog for the installed version
charmer licenses                    show third-party license info
```

`CONFIG` means `config.<site>.yml` for every command except `monitor` and
`logs`, which take `config.<site>.monitor.yml` instead, the separate file
`handoff` emits at the end of a successful `provision` run, scoped to
exactly what monitoring needs (host/agent IPs + SSH, no secrets).

## The phase model

Every phase runs **plan → confirm → apply → verify**:

- **plan** prints exactly what apply will do, before anything happens.
- **confirm**: `lab` sites ask `y/N`; `production` sites require typing the
  site name. Read-only phases (`preflight`, `handoff`) skip this.
- **apply** does the work, streaming per-host ✔/✘/⚠ lines, with a live
  status line for anything that takes a while (image pulls, health waits).
- **verify** is a health gate. A phase that applies but fails verify is
  marked `failed` and the runner **stops**; it never builds on an
  unhealthy foundation.

Progress is recorded in a per-site state file, so a re-run skips completed
phases and resumes at the frontier. `--replay PHASE` marks exactly the
named phase(s) pending; `--only PHASE` runs just the named phase(s)
regardless of prior status (useful for `--only preflight` on a live site,
or re-running `newt` after adding an agent).

`charmer status CONFIG` reads that state file (plus the config) and prints
each phase's status/timestamp, which secrets are pinned (names only, never
values), per-agent Newt credential-minting status, and the `restore`/`tls`
summary, without opening any SSH connection. Useful for "where did the last
run stop" without re-running `--only preflight` or waiting on `monitor` to
connect.

## Phases

### preflight *(read-only)*

SSH reachability + sudo, OS release (warn if not Ubuntu 24.04), free disk,
required ports free (`80`/`443` TCP for Gerbil/Traefik, `51820`/`21820` UDP
for Gerbil), and, if `provision.refuse_existing`, refuses a host that
already carries pangolin/gerbil/traefik/postgres containers. State-aware:
the footprint of already-completed phases is expected on a resumed run, not
a failure.

For each Newt agent: SSH reachability, OS release (Ubuntu or Debian is
fine; anything else is a warning, since `base` can only install Docker CE
from download.docker.com on those two), whether Docker is already present,
and a **real** TUN capability test: `ip tuntap add ... && ip link delete`
as root, not just `test -c /dev/net/tun`. A device node can exist and still
be blocked at the cgroup layer, which is exactly the failure mode of an
unprivileged LXC container without `/dev/net/tun` passed through. Charmer
never assumes an agent's host type (VM/LXC/physical; see `pangolin.net`
config comments and the config schema), but if the probe fails, the detail
line spells out the fix *if* it turns out to be an unprivileged LXC:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

added to the container's `<id>.conf` on the **hypervisor**, or make it
privileged / use a VM instead. Charmer has no SSH path to the hypervisor
to fix this itself.

On **every** host (Pangolin host and each Newt agent), two more read-only
checks, both found for real on Proxmox community-scripts "Docker LXC"
templates (Debian 13). Preflight only reports them and changes nothing:

- **Docker API on TCP:** a listener on `2375`/`2376`, dockerd listening on
  any other TCP port (its Swarm ports `2377`/`7946` excepted), or any
  `tcp://` entry in `/etc/docker/daemon.json`'s `hosts` or on dockerd's own
  `-H`/`--host` args. That template ships `"hosts": [..., "tcp://0.0.0.0:2375"]`
  with no TLS or auth, i.e. root on the host for anyone who can reach it
  (and, on a privileged container, on the hypervisor too). **Refused in
  `production`, a warning in `lab`.** The fix is yours: drop the `tcp://`
  entry and restart docker.
- **ssh.socket fighting sshd for the ssh port** (warning only): systemd
  owning the port through `ssh.socket` while `ssh.service` is *also*
  enabled. In that state any sshd reload/restart, including a routine
  openssh upgrade, dies with `fatal: Cannot bind any address.` and new
  logins fail while open sessions carry on as if nothing happened. `base`
  fixes this itself when `ssh.disable_password_auth` is on (below); by hand
  it's `systemctl disable --now ssh.socket && systemctl enable ssh.service
  && systemctl restart ssh.service`. Ubuntu 24.04's default socket
  activation (`ssh.socket` enabled, `ssh.service` disabled) is **not** this
  and isn't flagged.

### base

Baseline packages, Docker CE from Docker's own repo, on every host
(Pangolin host + every Newt agent). The repo is picked from
`/etc/os-release`'s `ID` (`download.docker.com/linux/ubuntu` or
`/linux/debian`, so Debian Newt agents work too); any other distro fails
that step with a clear message. A host that already has `docker` +
`docker compose` is left alone; one with `docker` but no `docker compose`
(e.g. the distro's own `docker.io`) is reported as a failure instead of
getting Docker CE installed over it, which apt would refuse anyway. The Pangolin host additionally gets
`chrony`. UFW: the Pangolin host gets `ssh`/`80`/`443` plus
`51820/udp`/`21820/udp` for Gerbil (host-published
wildcard, exactly as the official compose does it: its WireGuard handshake
and holepunch/relay ports need an explicit allow rule or every Newt/client
P2P connection fails with `HOLEPUNCH_MISSING`); Newt agents get `ssh` only. Newt only ever makes an
**outbound** connection to Pangolin, so there's nothing else to open.
`apt upgrade` is deliberately not run: package drift belongs to your
patching policy, not the provisioner.

UFW works inside LXC containers too, unprivileged ones included (verified
on a real unprivileged Debian 13 LXC, see [Verification
status](#verification-status)): `deny incoming` doesn't break Newt, since
Docker's own `FORWARD` rules come ahead of UFW's for container traffic.

**Unattended upgrades, optional `base.unattended_upgrades`:** defaults to
`true` (leave the OS's own `unattended-upgrades.service` +
`apt-daily-upgrade.timer` alone, today's behavior). Set it to `false` and
this phase masks both units on every host instead — not just disables, so a
package's postinst can't silently re-enable the timer on its own upgrade.
Same concern as `apt upgrade` never running automatically here, just on the
OS's own silent schedule instead of yours.

**SSH hardening, opt-in:** set `ssh.disable_password_auth: true` (top-level
`ssh:` block, and/or per-agent `newt_agents[].ssh:`) once you're confident
key/agent auth works against that host, and this phase locks its sshd to
key-only. `config.py` refuses the combination with `auth: password` at
config-load time, since that would lock you out on the very apply that
turns it on. Defaults to `false`; nothing changes unless you opt in.

What it does, in order, per host:

1. Writes `/etc/ssh/sshd_config.d/01-charmer-hardening.conf`:
   ```
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitRootLogin prohibit-password
   PubkeyAuthentication yes
   ```
   sshd keeps the **first** value it reads for an option, and reads
   `sshd_config.d/*.conf` in lexical order through the `Include` at the top
   of `sshd_config`, so the `01-` prefix is what makes it win over e.g.
   Ubuntu's `50-cloud-init.conf` (`PasswordAuthentication yes`) or a
   template's `PermitRootLogin yes`. `PermitRootLogin` is
   `prohibit-password`, never `no` (you may well be SSHing in as root with
   a key), and the line is left out entirely if the current effective value
   is already stricter (`no`/`forced-commands-only`), so charmer never
   loosens it. (Before 0.9.0 this was `60-charmer-key-only.conf`, which
   could lose to `50-cloud-init.conf`; it's removed.)
2. `sshd -t`, then checks `sshd -T`'s **effective** values. If either
   fails (e.g. a value set in `sshd_config` *before* its `Include` line
   still wins), the drop-in is removed again and nothing is reloaded.
3. If `ssh.socket` owns the ssh port alongside an enabled `ssh.service`
   (see preflight above), a reload would kill sshd, so this switches the
   host to plain `ssh.service` instead (`systemctl disable --now
   ssh.socket && systemctl enable ssh.service && systemctl restart
   ssh.service`). Otherwise, including Ubuntu 24.04's default socket
   activation, it's a plain `systemctl reload ssh`.
4. Confirms **sshd itself** (not only systemd) holds a listener on the ssh
   port and `ssh.service` is active. A reload job can report success while
   sshd dies right after re-exec, so neither `sshd -t` nor the reload's
   exit status proves anything here. If sshd isn't back, the phase fails
   loudly with the last journal lines and the fix; if that happened on the
   socket-switch path, `ssh.socket` is re-enabled first so new logins keep
   working as they did before. `verify()` repeats the effective-config and
   listener checks.

**SSH source scoping, opt-in:** set `monitor.ips` (a list of admin/monitoring
IPs or CIDRs) to scope `ssh` on every host (Pangolin and every Newt agent
alike) to that allow-list instead of leaving it open to anywhere, which is
what an empty/omitted list still does. Omit it and this phase asks
interactively instead (Enter to skip), pinning the answer in state so a
`--replay` never re-asks. This is deliberately unlike [Monitoring](#monitoring)
below: `charmer monitor`/`logs` already reuse whatever SSH access you have,
so there's no separate exposed port to scope: `monitor.ips` scopes SSH
itself, the one channel that access runs over. UFW here stays additive, not
reconciling: switching from no `monitor.ips` to a populated list clears the
old wide-open `ssh` rule first (otherwise it would coexist with, and defeat,
the new per-IP rules), but swapping one IP for another later leaves the old
IP's rule in place: clear it by hand (`ufw status numbered` + `ufw delete
<n>`) on the affected host.

`monitor.ips` is operator-typed and answers "what should stay allowed
long-term" (typically a separate admin/monitoring box), not "what IP is
charmer connecting from right now." If those diverge (you provision from
your own workstation but only list a separate monitoring host), this phase
still folds each host's own currently-connected source IP into its
allow-list automatically, alongside `monitor.ips`: read off sshd's
`$SSH_CLIENT` for the connection already in hand, so it costs nothing extra
and needs no config. Without it, the already-open connection survives the
apply that scopes ssh down (UFW doesn't tear down established connections),
so the run itself finishes looking clean, but the next connection attempt,
even charmer's own on a later `--only`/`--replay`, would otherwise time out
with no indication why.

**OS hostname, Pangolin host only, optional `pangolin.host.hostname`:**
same resolution shape as `monitor.ips` — config value first; if unset, an
interactive prompt the first time `base` runs against a site (Enter to
leave the host's current hostname alone), pinned in state so `--replay
base` never re-asks or drifts from what the file says. Runs `hostnamectl
set-hostname` and, if that succeeds, also rewrites `/etc/hosts`'s `127.0.1.1`
line to match (skipped otherwise): `hostnamectl` alone only updates
`/etc/hostname`, and a stale `127.0.1.1 <old-name>` line left behind is
exactly what makes `sudo` print `unable to resolve host <old-name>` on
every subsequent command — cosmetic, not a functional break, but cheap to
avoid outright.

Changing the OS hostname on an **already-running** site is safe: nothing
charmer renders or Pangolin itself needs is keyed off it. Pangolin's
`config.yml` (`base_url`/`dashboard_host`) and the self-signed/imported
cert's CN/SAN come from `tls.hostname`/`host_ip`, a DNS name or IP charmer
tracks separately; Gerbil/Traefik/Postgres talk to each other over Docker
Compose's own service-name DNS (`pangolin`, `gerbil`, `postgres`), which
has nothing to do with the host's `hostname(1)`; and charmer's own SSH
targeting is always `pangolin.host.ip`, never the hostname. The only actual
side effect anywhere in this stack is the `/etc/hosts` cosmetic warning
above, which this phase avoids by fixing it in the same step.

### pangolin

Renders and pushes the **official Compose layout verbatim**: bridge
networking on pangolin/gerbil/postgres, `network_mode: service:gerbil` on
traefik, Gerbil host-publishing `80`/`443` on the wildcard address, exactly
as
[docs.pangolin.net/self-host/manual/docker-compose](https://docs.pangolin.net/self-host/manual/docker-compose)
lays it out, plus `config.yml`. See [Ingress](#ingress) below for why
there's no reverse proxy or passthrough layer in front of any of this.
`server.secret` and the Postgres password are generated once and pinned in
state. Postgres, when used, is a plain
container on the compose bridge network, never published to the host by
default: reachable only from pangolin as `postgres:5432`. Setting
`pangolin.postgres_loopback_port` (asked by `charmer init`) additionally
publishes it as `127.0.0.1:<port>` on the Pangolin host, for tools that
need a TCP port (e.g. a GUI client over `ssh -L`); `docker exec postgres
psql` works without it. It's loopback-only by construction: there's no
knob for a wider bind, `preflight` checks the port is free, and this
phase's `verify()` fails if anything listens on it beyond loopback. SQLite
is accepted for `lab` only (`config.py` refuses it in production).

Charmer's own SSH-based tooling (this phase's `verify()`, and the `newt`
phase's calls into the integration API: see
[pangolin_api.py](src/charmer/pangolin_api.py)) needs to reach Pangolin's
API from the host over loopback, which the official layout never exposes
(it's bridge-internal only, reachable by Traefik as `pangolin:3001`). So
Pangolin's compose block additionally publishes `127.0.0.1:3001` always,
and `127.0.0.1:<port>` (`3003` by default, or whatever
`pangolin.integration_api.port` sets) when the integration API is on --
any Newt agents configured, or `pangolin.integration_api.enabled: true` --
both loopback bound, invisible off-host, and irrelevant to the official layout's own
container-to-container traffic.

Gerbil's `start_port` is hardcoded to `51820`, never a config knob: setting
it to `21820` (Gerbil's separate hole-punch/relay port) sends every
WireGuard handshake to the wrong listener: every site shows Disconnected
with `RELAY=true`, and Gerbil logs `No proxy mapping found` for every
packet. This is a real, previously-hit failure mode, not a hypothetical.

**`51820/udp` can be squatted by something charmer never touched.**
`preflight` only checks that port at the *start* of a run; on a reused host,
anything already bound to it (most concretely hit so far: a pre-existing,
unrelated `wg0` WireGuard interface with its own peers, nothing to do with
this deployment) makes `docker compose up` fail on Gerbil specifically
with `failed to bind host port 0.0.0.0:51820/udp: address already in use`,
after Postgres/Pangolin/maintenance have already come up healthy. Since
Gerbil's port is hardcoded (see above), this can't be worked around by
remapping the Docker host-side publish: the port has to actually be free.
Find the owner by hand (`sudo ss -ulnp | grep 51820`, `sudo wg show`,
`docker ps -a`, `systemctl list-timers`) and stop/disable it: `charmer
clean` deliberately will not do this for you, since it only reverses what
charmer itself provisioned (see `clean_phase.py`'s module docstring), and a
teardown command reaching outside its own footprint to kill an unrelated
host service on a shared box is exactly the kind of surprise it's designed
never to spring. Once the port is free, re-run `--only pangolin`: `docker
compose up -d` is idempotent, so the already-healthy containers are left
alone and only Gerbil/Traefik are retried.

Also renders whatever the configured `tls.provider` needs on Traefik's
side (see [Ingress](#ingress)) and turns on Pangolin's integration API
(`flags.enable_integration_api`, loopback-only) when any Newt agents are
configured, or unconditionally if `pangolin.integration_api.enabled: true`
is set (see "Newt credential automation" for that and
`pangolin.integration_api.port`).

**Pangolin has no OIDC config.yml key.** Confirmed against
docs.pangolin.net: setting up an external identity provider (Authentik,
Google, Azure, ...) is a **Server-Admin-dashboard-only** operation
(Identity Providers → Add Identity Provider) after the instance is up.
There is nothing to template here, and this repo doesn't pretend otherwise
by inventing a config key that doesn't exist.

**SMTP, optional:** Pangolin config.yml's real `email:` section (confirmed
against docs.pangolin.net; unlike OIDC, this key genuinely exists), used
for password reset / invite emails. Set `smtp.enabled: true` plus
`host`/`port`/`user`/`no_reply`/`secure`/`tls_reject_unauthorized` in the
config file; `smtp_pass` is deliberately **not** a config-file field: this
phase prompts for it once (hidden) the first time it runs and pins it in
local state, the same treatment as the Newt Root API key.

Verify: Pangolin's own API answers on loopback, (when Newt agents are
configured) so does the integration API, and an end-to-end request over the
actual public interface (Gerbil/Traefik) reaches Pangolin's API.

### restore *(optional)*

Runs **before** `newt` on purpose: see below. Set `restore.postgres_dump`
(a `.sql.gz`) and `restore.destructive: true` and the freshly-bootstrapped
database is replaced by a `pg_dump` from elsewhere. Without a dump
configured, or if you decline the confirmation, the phase records "skipped"
and the pipeline moves straight on to `newt`: it's optional by design, not
a required step you're expected to always answer.

When it runs: pangolin/gerbil/traefik stop first; the dump is uploaded and
sha256-verified; loaded with `ON_ERROR_STOP=1` (a couple of known
version-skew `SET` lines are stripped from the header first: a dump from a
newer `pg_dump` can carry GUCs an older server rejects); the dump is
**deleted from the host immediately after loading**, since it holds every
secret Pangolin has; the stack restarts and health-gates before the phase
is marked done. Any failure along the way leaves the stack stopped rather
than half-up on half-data.

Restoring a dump taken from a *different* Pangolin deployment overwrites
this host's `exitNodes` row (Gerbil's registered identity) with the source
deployment's `publicKey`/`reachableAt`. Gerbil's own WireGuard key on disk
(`/opt/pangolin/config/key`) is a host bind mount restore never touches, so
after the load it no longer matches what's in the database. Pangolin's own
reconciliation (`createExitNode()`) can't recover from this by itself: its
update path matches on `publicKey`, which can never equal the *incoming*
key when the stored row already belongs to someone else, so it silently
updates zero rows every time Gerbil retries, and Gerbil crash-loops trying
to parse the blank CIDR address it gets back. `restore` works around this
by snapshotting this host's own `exitNodeId`/`publicKey`/`reachableAt`
before the load and patching them back onto that same `exitNodeId` row
afterward, keyed on the primary key, not `publicKey`, which is exactly
what Pangolin's own logic can't do. Everything else on that row (address,
name) comes from the dump untouched. See `phases/restore_phase.py`.

**Org `utilitySubnet` after a restore.** Each org's `utilitySubnet` is the
range Pangolin hands out site-resource alias addresses from. Orgs created
before Pangolin 1.13 were migrated to a `/24` there, and a restore carries
that forward; 1.22 gives new orgs a `/20`. A `/24` eventually fails with
"No available subnets remaining in space". After the load, `restore`
reads every org's `utilitySubnet` and warns if one is narrower than `/20`.
With `restore.utility_subnet_prefix` set (e.g. `22`), it instead widens it
in place, while pangolin is still stopped, to the aligned block of that
size containing the current one, so every existing alias stays valid. It
does this only if the widened block overlaps neither any org's `subnet` nor
Gerbil's network (Pangolin's default `gerbil.subnet_group`, which charmer
never overrides, plus every `exitNodes.address`); otherwise it leaves the
value alone with a warning. It never narrows, and the dump just loaded is
the backup. Clients only get the wider route after they reconnect, and
anything real on your network inside the new range becomes unreachable for
connected clients (it's CGNAT space, so unlikely, but check).

**Migrating an existing (non-charmer) Pangolin deployment onto a fresh
charmer-provisioned host:** `server.secret` is what Pangolin uses to
encrypt/sign data that ends up in Postgres (sessions, 2FA, stored resource
passwords, ...). If the dump you're about to load was produced by a
*different* installation, restoring it onto a site whose own
`server.secret` doesn't match leaves that data undecryptable. The first
time the `pangolin` phase runs for a site, it asks (hidden input, before
rendering `config.yml`): Enter generates a new random secret, same as
before this prompt existed, for a normal fresh install; pasting the **old**
deployment's `server.secret` here instead pins that value in state
(`generated.pangolin_server_secret`) and Pangolin is rendered to use it
from the very first boot. Like every other pinned value, it's asked
**once ever** — get this one right before the `pangolin` phase's first
apply, since a later `--replay pangolin` reuses whatever got pinned, not a
second chance to answer differently. (The one thing not automated: getting
the old secret off the source deployment in the first place — read it out
of that install's own `config.yml`/`SERVER_SECRET` env var.)

Recreating gerbil above restarts its WireGuard process, which drops any
Newt agent that was already tunneled in. Since `newt` (below) skips itself
whenever a restore just ran, nothing else in the pipeline would otherwise
tell those agents to redial. So, for each configured `newt_agents` entry
that already has a bundle at `/opt/newt`, `restore` finishes by running a
plain `docker compose restart newt` on it — no credentials re-minted, no
compose file rewritten, no DB lookup: the container's own compose file
already carries the right `newtId`/secret from whenever it was first
provisioned. An agent with no bundle yet is skipped (nothing to restart);
one that doesn't come back is recorded as a warning, not a phase failure,
since the restore itself already succeeded by that point.

This only reaches agents charmer already has SSH access to, i.e. ones
listed in `newt_agents` in the config used for this run — it does no DB
lookup and prompts for nothing. With `newt_agents` empty (Newt managed
entirely outside charmer, a legitimate and common choice), this step is a
no-op: charmer has no visibility into those hosts and won't try to gain
any. Redialing them after a restore is then a manual step on whatever
system manages them, same as before this feature existed: on each such
agent, `docker compose down` then `docker compose up -d` (not just
`restart`) is the more reliable form — the same connection-recreation
gerbil itself just went through, which a plain `restart` doesn't always
reproduce for Newt's own reconnect logic. This applies to *any* Newt agent
outside this run's `newt_agents` list, whether or not charmer provisioned
it originally: e.g. an agent from a config that has since been trimmed, or
one that was always managed by hand.

`charmer provision config.yml --only restore` scopes a run to just this
phase regardless of `newt_agents`: `pangolin_phase` doesn't run, so
`config.yml` is never re-rendered/re-pushed and nothing else
force-recreates. That's the right way to do a DB-only restore whether or
not you use `newt_agents` at all.

### newt

Comes **after** `restore` in the pipeline, and is **skipped automatically**
whenever a restore actually loaded a dump this run: a restored database
may already carry Pangolin sites for these agents, and minting fresh ones
on top would create duplicates. The skip is scoped to that one
`charmer provision` invocation (`PhaseContext.restore_ran`, set only when
`restore` genuinely applies a dump: see `phases/base.py`/`phases/
restore_phase.py`), not persisted, so a later `--only newt` run for a
specific agent still works normally; reconcile against Server Admin ->
Sites on the dashboard first.

When it does run: for each configured agent, mint its Pangolin site
credentials via the integration API (see [Newt credential
automation](#newt-credential-automation)), push its Compose bundle (image
pinned: `config.py` refuses `latest`, since an unpinned Newt can outrun
the server-side Pangolin/Gerbil version and break compatibility silently),
`docker compose up -d`. Credentials are minted **once ever** per agent:
pinned in state, never re-created or rotated by a re-run. If the Root API
key / organization ID aren't pinned yet, the phase prints the one-time
manual bootstrap (`/auth/initial-setup`, create an org, mint a Root API
Key) before prompting for them.

Verify: each agent's `newt` container is in the `running` state.

Pangolin 1.23 introduced a unified **Pangolin CLI** (`fosrl/cli`) that
bundles Newt's site-connector code together with client/SSH/SCP/AI-gateway
tooling, and made it the dashboard's default for *new* sites. Newt itself
is unaffected: per Pangolin's own 1.23 release notes, "Newt will continue
to be provided in all of its current forms for the foreseeable future,"
and is called out as the right choice specifically "if you need the
smallest possible binary or container." Since `newt_agents` targets
arbitrary, possibly resource-constrained SSH hosts (see the LXC/TUN note
above), charmer deliberately stays on Newt directly rather than the
heavier all-in-one CLI: there's no client/SSH/SCP use case here that CLI
would add value for. Revisit only if Newt's own support status changes.

### handoff *(read-only)*

Like `preflight`, runs without the confirmation gate: nothing here is
irreversible. Prints the dashboard URL and next steps (no secrets), and
writes `config.<site>.monitor.yml` on the workstation (mode `0600`) for
`charmer monitor`/`charmer logs`.

## Ingress

**The constraint:** Pangolin's whole point is publishing arbitrary internal
resources under their own subdomains: new ones, minted whenever an admin
adds a resource, long after `charmer provision` has finished. An ingress that pre-decides which certificates exist can't serve
domains it doesn't know about yet. So: **Traefik does all TLS/ACME itself,
for the dashboard domain and every future resource domain, exactly as the
official Compose does it**: `network_mode: service:gerbil`, wildcard
`web`/`websecure` entryPoints, no changes from upstream in
`traefik-config.yml.j2` at all.

**No reverse proxy or passthrough layer in front of it.** Gerbil itself
host-publishes `80:80`/`443:443` on the wildcard address (Traefik shares
its netns via `network_mode: service:gerbil`, so it inherits whatever
Gerbil publishes), exactly as
[docs.pangolin.net/self-host/manual/docker-compose](https://docs.pangolin.net/self-host/manual/docker-compose)
lays it out, with zero deviation on the port-publishes. Traefik is the
literal first hop on the wire and the only thing that ever holds a
certificate. (Earlier revisions of this project ran a host nginx in front
of it as a bare-metal L4/SNI passthrough, and before that mistakenly tried
`network_mode: host` on pangolin/gerbil/traefik: neither is present here
anymore; see CHANGELOG.md. Following the official layout verbatim means
real client IPs already reach Traefik/Pangolin directly, no PROXY protocol
or trusted-IP unwrapping needed.)

## Maintenance page

`charmer shutdown` stops only `pangolin`: `gerbil`, `traefik` (and a tiny
new always-on `maintenance` container, both in
[pangolin-compose.yml.j2](src/charmer/templates/pangolin-compose.yml.j2))
are deliberately left running. Gerbil in particular has to stay up: Traefik
runs `network_mode: service:gerbil` (see "Ingress" above), so it borrows
Gerbil's network namespace and loopback port-publish wholesale instead of
having one of its own: stopping Gerbil would silently take Traefik's
connectivity down with it (it would still show as "running" in `docker
compose ps`, just unreachable), which defeats the maintenance page this
phase exists to serve. So every `shutdown` run unconditionally brings
Gerbil up with `--no-deps` (so that never implicitly restarts `pangolin`
too) and then force-recreates `traefik`: Docker never migrates a
`network_mode: service:X` container when `X` restarts, so a Traefik that
never stopped can be left silently pinned to Gerbil's dead old network
namespace, and there's no cheap way to tell from the outside whether that
happened. A brief, sub-second reachability blip on every deliberate
`shutdown` is the trade for not needing to detect that (see CHANGELOG for
why the earlier "only recreate traefik if gerbil was found down" version
was wrong). Traefik's own `errors` middleware
([traefik-dynamic-config.yml.j2](src/charmer/templates/traefik-dynamic-config.yml.j2))
is wired onto the dashboard's routers to catch 502/503/504 (exactly what
a dead `pangolin` backend produces) and fall back to a static "we'll be
back" page served by `maintenance` instead, optionally with your own logo
(`maintenance.logo` in the config, inlined as a data URI so the page needs
no other assets, see `config.example.yml`). `charmer init` asks for an
organization name and defaults `maintenance.message` to a Greek line built
from it (`{org}, θα επιστρέψουμε σε λίγο.`) instead of the English
fallback in `config.py`; it's plain text in the generated config file, so
edit it freely afterward.

**Scope, explicitly:** this covers the dashboard host only. Resource
subdomains aren't in charmer's static Traefik config: they're pushed live
by Pangolin's own backend via the `http` provider
([traefik-config.yml.j2](src/charmer/templates/traefik-config.yml.j2))
polling `127.0.0.1:3001`, which goes unreachable and stale the moment
`pangolin` stops. Charmer has no router to attach `errors` to for routes it
doesn't define, so visitors to resource subdomains during a `shutdown`
still see a hard failure, not the maintenance page. Closing that gap would
require Pangolin itself to support a maintenance mode: out of charmer's
hands.

`charmer start` brings `pangolin`/`gerbil` back (and is idempotent on
`traefik`/`maintenance`, so it self-heals either one if they'd gone down
for an unrelated reason too).

## Newt credential automation

Every Newt agent needs a Pangolin "site": an id + secret pair the Newt
container authenticates with. Doing this by hand (create a site in the
dashboard, copy the id/secret into the config) doesn't scale past one
agent and invites drift between what's configured and what's deployed, so
the `newt` phase mints it automatically:

```
GET  /org/{orgId}/pick-site-defaults   → a fresh {newtId, newtSecret, clientAddress}
PUT  /org/{orgId}/site                 → {name, type: "newt", newtId, secret} → the created site
```

confirmed against
[docs.pangolin.net/manage/common-api-routes](https://docs.pangolin.net/manage/common-api-routes).
Every call runs **over the existing SSH connection to the Pangolin host**,
against the integration API on loopback (`127.0.0.1:3003` by default): never
over the public internet, never routed through Traefik. It's charmer's
own tool for minting credentials, not a public interface, so it's
deliberately never exposed.

The port and whether it's on at all are optional config knobs
(`pangolin.integration_api.port`, default `3003`; `pangolin.integration_api.enabled`,
default unset) if you also want to hit the integration API yourself for your
own tooling, independent of whether any `newt_agents` are configured — set
`enabled: true` to turn it on regardless, or `false` to force it off (refused
at config-load time if `newt_agents` are configured, since the `newt` phase
needs it). Either way it's still only ever published loopback-only
(`127.0.0.1:<port>`) on the Pangolin host, same as today: charmer's own
managed Traefik config never routes it anywhere, so reaching it from off-host
is on you (e.g. an SSH tunnel), not something this repo wires up.

Pangolin CE has no way to seed an API key at deploy time so there is one
genuinely irreducible manual step: complete `/auth/initial-setup` in a
browser once, then mint a **Root API Key** (Server Admin → API Keys) once.
`charmer provision` asks for that key (hidden input) and your organization
ID (Organization Settings → General) the first time the `newt` phase runs,
pins both in state (`0600`, never written to the config file), and never
asks again: every agent after that is fully automatic.

The Root API Key only ever needs one permission: **Create Site**. That's
the single action gating both `pick-site-defaults` and `PUT .../site`
(confirmed against fosrl/pangolin's route table, `server/routers/
integration.ts`): nothing else charmer calls needs a broader key. The
"Site Provisioning Keys" category in the permission selector is a Fossorial
Commercial License feature (`server/private/routers/siteProvisioning`) with
no route wired into self-hosted CE's integration API at all; granting it is
a no-op there.

**`tls.provider: self_signed`:** Newt (a Go TLS client) validates the
dashboard's cert against the system trust store like anything else: a
self-signed leaf was never in it, so every agent fails with `x509:
certificate signed by unknown authority` and retries forever, while the
container itself stays "running" (so `verify()`'s running-state check alone
doesn't catch it: this looked like success). fosrl/newt does have a
`TLS_CLIENT_CAS` env var for exactly this, but it's a dead end in the
current release: in `websocket/client.go`'s `setupTLS()`, the block that
loads `CAFiles` into `RootCAs` is nested *inside* `if ClientCertFile != ""
&& ClientKeyFile != ""`, i.e. it only takes effect alongside a client
cert/key pair for mTLS, which charmer has no reason to set up. Set
`TLS_CLIENT_CAS` alone and `setupTLS()` falls through every branch and
returns `(nil, nil)`: the file gets read and logged, never applied to the
handshake. Confirmed against fosrl/newt's source, not a guess from the
symptom. The mechanism that *does* work standalone, checked independently
at every TLS call site (`getToken`, the provisioning POST, the WebSocket
dial), is `SKIP_TLS_VERIFY=true`, which the `newt` phase now sets for
every agent whenever `tls.provider: self_signed`. `acme`/`import` need no
such step: real CA chains Newt's default trust store already recognizes.
Because `newt`'s phase state was already marked `done` (the container was
"running", just crash-retry-looping) before this fix existed, picking it up
on an already-provisioned site needs `charmer provision <config> --only
newt` to force a re-apply.

## Monitoring

`charmer monitor`/`charmer logs` read `config.<site>.monitor.yml` (from
`handoff`), not the site's own config. Charmer's whole footprint is small enough
that the same SSH access `provision` used is what monitoring uses too:
`docker inspect` over SSH, not extra public HTTP ports (stub_status, stats
pages) opened just for a dashboard to poll. One less thing exposed to the
internet, one less UFW rule to keep in sync.

`charmer monitor` is a Textual TUI: a "PANGOLIN HOST" panel (per-container
health: Pangolin/Gerbil/Traefik/Postgres) and a "NEWT AGENTS" panel (each
agent's `newt` container state), each with a live status dot in its border
title, refreshed every `--interval` seconds (default 15). `r` forces an
immediate refresh, `q` quits. Internally the Pangolin host is a one-element
list rather than a hardcoded single host, so adding real multi-node
Pangolin support later is a config/loader change, not a UI rewrite.
`--once`, or stdout piped to something that isn't a terminal, prints a
single plain `rich` table snapshot instead (no Textual screen). `charmer
logs` pulls `docker logs` from every container on every host in parallel,
`--level`-filtered the same way `journalctl -p` filters, `--save` to write
a plain-text report instead of printing.

`charmer logs` also always writes `<site>_access.csv` (or
`<save-file>_access.csv` with `--save`): resolved user<->resource
connection history, one row per session (Agent, Agent IP, Started, Ended,
Duration, Who, Where, Proto, Destination). Source: each Newt agent's own
`ACCESS START`/`END` log lines (fetched unfiltered, bypassing `--level`,
since Newt logs these at INFO and Pangolin CE has no server-side handler
for the `newt/access-log` message that would otherwise centralize them —
see fosrl/pangolin#3695 — so per-agent SSH is the only way to reach this
data at all), cross-referenced against Pangolin's Postgres (`resources`,
`clients`, `user`) via `docker exec postgres psql` on the host — a local
Unix-socket connection (`initdb`'s default `local ... trust`, never
touched by the Docker entrypoint's TCP-only auth setup), so no Postgres
password is ever needed or stored for this. Only Newt agents listed in
that site's `newt_agents:` are reachable this way; since `monitor.yml` is
plain, standalone YAML (not tied to provisioning state), hand-add an entry
there to cover a Newt agent this site didn't provision itself (e.g. one
introduced by restoring a dump from elsewhere). SQLite deployments (lab-
only, see "Rules") get the plain per-agent log dump but no resolved CSV
rows: no Postgres to query.

## State file & secrets

One JSON state file per site (default `.state/<site>.json`, mode `0600`):
phase completion, and **pinned generate-once values**: the Pangolin server
secret, the Postgres password, the Root API key, each Newt agent's minted
id/secret, and (when `smtp.enabled`) the SMTP password. Pinning is what
makes re-runs safe: a completed phase can never be re-bootstrapped, and a
re-render can never rotate a password out from under a running stack.

- The site config file (`config.<site>.yml`) contains **no secrets** and is
  safe to commit: it's git-ignored by default anyway, except
  `config.example.yml`.
- SSH/sudo passwords, when used, are prompted at runtime and never stored.
- Generated secrets currently live **in plaintext** inside the state file.
  Acceptable for lab work; flagged as a hard requirement to fix (age/sops
  encryption or the OS keyring) before production use. `.state/` is
  git-ignored; keep it that way.
- Every command run on every host during `provision`/`clean`/`shutdown`/
  `start` is logged to a `0600` transcript file next to the state file,
  with best-effort secret redaction (see `transcript.py`'s docstring for
  exactly what is and isn't caught): useful for reconstructing what
  happened without re-reading the source or trusting memory months later.

## Verification status

Every phase has been exercised against config validation, template
rendering (every TLS/database combination: `pytest`), and the CLI's
connection/error-handling paths (a real SSH timeout against a placeholder
IP, config errors, unknown `--only`/`--replay` names).

Beyond that, the full pipeline has been run end-to-end against a real lab
deployment: `preflight` through `handoff` on a real Ubuntu host plus a real
Newt agent, `restore` exercised both without a dump configured (the skip
path) and with a real destructive restore, `tls.provider: acme` exercised
first against Let's Encrypt's staging directory and then against the real
production directory, the maintenance page checked both without and with a
custom `maintenance.logo`, the Newt agent provisioned and connected with a
private resource published through it and reached from outside, and
`shutdown`/`start`/`clean`/`monitor`/`logs` all run against that same live
site. Not yet exercised for real: multiple Newt agents in the same run,
`tls.provider: self_signed`/`import`, SQLite, the `monitor.ips`/
`base.unattended_upgrades` opt-ins, and `restore`'s
post-restore newt-agent redial (added after the fact, from a real-world
report of agents left disconnected post-restore; not yet run against a live
agent).

**Hardening on real Debian 13 LXC Newt agents** (three Proxmox
community-scripts "Docker LXC" containers, two privileged and one
unprivileged, provisioned outside charmer and hardened **by hand**):

- UFW (`0.36.2`, iptables `1.8.11` nf_tables) with `deny incoming` /
  `allow outgoing` / `deny routed` and ssh scoped to two admin IPs works
  inside the **unprivileged** container too. All three sites stayed Online
  and resources behind each agent kept loading.
- The sshd hardening path now in `base` was exercised step by step on all
  three: the `ssh.socket`/`ssh.service` conflict reproduced for real (a
  reload killed sshd's listener on every host), the socket-to-service
  switch fixed it, and the `01-` drop-in with `PermitRootLogin
  prohibit-password` was confirmed through `sshd -T`.
- The Docker-API-on-TCP finding (`tcp://0.0.0.0:2375` in `daemon.json`) was
  present on all three.

Charmer's own automation of these (`base`'s `ssh.disable_password_auth`,
the new preflight checks, the Debian Docker CE repo) is covered by unit
tests against canned `ss`/`systemctl`/`sshd -T` output, but has **not yet
run end-to-end against a live host**, and neither has the unchanged
reload path on Ubuntu 24.04's default socket activation.

## Roadmap

- HA is explicitly out of scope for the pipeline: official Pangolin CE has
  no self-hosted HA topology (it's an Enterprise-only capability). If that changes upstream, or if a
  warm-standby pattern similar to what a 3-node Pangolin HA deployment would need, becomes worth building by hand, it's a separate future project, not an assumption baked into this pipeline.
- `charmer clean`'s Newt-side teardown removes the agent's container and
  compose bundle but does not delete its Pangolin site server-side (the
  integration API can do this; not wired up yet).
- Encrypt secrets at rest in the state file (age/sops or an OS keyring)
  before any production use.

## License

Charmer itself is [MIT licensed](LICENSE).

Charmer orchestrates, but never bundles, modifies, or redistributes, the
following separately licensed projects:

- **Pangolin CE**, **Newt** and **Gerbil** ([fosrl/pangolin](https://github.com/fosrl/pangolin),
  [fosrl/newt](https://github.com/fosrl/newt), [fosrl/gerbil](https://github.com/fosrl/gerbil))
  are dual licensed under AGPL-3.0 and the Fossorial Commercial License.
  Charmer only pulls their published container images and drives them over
  SSH/Docker Compose and the integration API; it contains none of their code.
- **Traefik** is [MIT licensed](https://github.com/traefik/traefik/blob/master/LICENSE.md).
- **PostgreSQL** is under the [PostgreSQL License](https://www.postgresql.org/about/licence/),
  a permissive MIT-style license.
- **Docker Engine** is [Apache-2.0 licensed](https://github.com/moby/moby/blob/master/LICENSE).

Charmer's own Python dependencies (paramiko, PyYAML, Jinja2, rich,
cryptography, textual and their own dependencies) are each MIT/BSD/Apache-2.0,
except paramiko, which is LGPL-2.1. The `charmer` release zipapp bundles the
pure-Python ones (paramiko among them) with their source and license text
included; each release publishes a `THIRD_PARTY_LICENSES.md` alongside the
binary indexing every bundled package's license. `charmer licenses` prints
the same listing from whatever is actually installed/bundled in the copy
you're running.
