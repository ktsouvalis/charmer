# Changelog

## [0.11.0] - 2026-09-25

- **Newt agents are redialed whenever gerbil restarts, and only then.**
  An agent's WireGuard tunnel is to gerbil's process. A pangolin, traefik
  or postgres restart only drops Newt's websocket, which Newt redials
  itself. So `pangolin` (gerbil recreated/restarted, or the
  `--force-recreate` fallback), `restore` and `start` (including a gerbil
  that `shutdown` had to bring back up) now recreate the newt container
  (`docker compose down && docker compose up -d`, the form README already
  recommended over `restart`) on every configured agent once pangolin is
  healthy. Before this, only `restore` did it, with a plain `restart`. Each
  of these also names every site in Pangolin's database that charmer
  doesn't manage, so you know which connectors to restart by hand. README
  "Which restarts need the Newt agents redialed" has the table.
- **New `adopt_newt` phase, only after a restore.** It lists the restored
  sites charmer doesn't manage, then asks for the host of each old
  connector (name, IP, SSH, the same questions as `charmer init`, nothing
  taken from the database). It finds the `fosrl/newt` or
  `fosrl/pangolin-cli` container there, checks its `newtId`/secret against
  this Pangolin via `get-token`, and on your y pins them in state and
  appends the agent to the config file (comments kept, re-validated,
  rolled back if invalid). `newt` then swaps the old container for
  charmer's bundle under the same credentials: same site, no re-mint.
- **`newt` no longer skips itself after a restore.** Pinned credentials are
  checked first. A rejected pair, or an unpinned agent right after a
  restore, is minted only after a y/N; declining skips that agent. The Root
  API key / org ID are asked for only when a site is actually minted.
- `charmer status` shows adopted agents; the transcript also redacts
  `"secret": "..."` JSON bodies.
- None of this has run against live agents yet: unit tests plus a
  simulated run against fake hosts. No `config_version` bump: no config
  key changed.

## [0.10.1] - 2026-09-25

- **Re-running `pangolin` on a live stack no longer takes the whole stack
  down.** Any changed file used to trigger `docker compose up -d
  --force-recreate`, which recreated gerbil (the 80/443 listener) and
  traefik too: no maintenance page during the rollout, and every Newt
  tunnel dropped. Found adding `postgres_loopback_port` to a production
  site. Now a plain `up -d` recreates only services whose compose
  definition changed. Bind-mounted file changes get a targeted `restart`:
  `config.yml` restarts pangolin *before* the `up` (compose fails an `up`
  while pangolin is unhealthy, because of traefik's `service_healthy`
  dependency, even with traefik untouched), and Traefik config/certs
  restart traefik. A recreated postgres restarts pangolin once it's
  healthy. Traefik is force-recreated only if gerbil was recreated or
  restarted. Anything crash-looping is restarted. If the `up` still fails,
  it falls back to the old whole-stack `--force-recreate`. Compose behavior
  verified locally against Docker Compose v5.5.1; not yet exercised on a
  real Pangolin host.

## [0.10.0] - 2026-09-25

- **New optional `pangolin.postgres_loopback_port`.** Publishes the postgres
  container as `127.0.0.1:<port>:5432` on the Pangolin host, for host-side
  tools that need a TCP port (e.g. a GUI client over `ssh -L`). Off by
  default, so the official never-published layout is unchanged; optional,
  so no `config_version` bump. `charmer init` explains it and asks.
  Loopback-only by construction (no knob for a wider bind); refused
  without `database: postgres` or on 3001/8091/the integration API port.
  `preflight` now checks that port is free on the Pangolin host when it's
  set (a host-level Postgres on 5432 would otherwise only fail at `docker
  compose up`), and `pangolin`'s `verify()` fails if anything listens on it
  beyond loopback. A publish added by hand to
  `/opt/pangolin/docker-compose.yml` is overwritten the next time the
  `pangolin` phase runs; set this key instead.
- **`restore` now checks every org's `utilitySubnet`, and can widen it.**
  Orgs from before Pangolin 1.13 were migrated to a `/24`, which a restore
  carries forward and which runs out ("No available subnets remaining in
  space"); 1.22 gives new orgs a `/20`, so fresh installs aren't affected.
  After the load, a range narrower than `/20` is a warning. With the new
  optional `restore.utility_subnet_prefix` it's widened in place (pangolin
  still stopped) to the aligned supernet of that size, keeping existing
  aliases valid, but only if that overlaps neither an org `subnet` nor
  Gerbil's network (default `gerbil.subnet_group` plus `exitNodes.address`);
  otherwise it's left alone with a warning. Never narrows, never fails the
  phase. Clients get the wider route on reconnect.

## [0.9.0] - 2026-09-23

Fixes for what `base`/`preflight` would have gotten wrong on Proxmox
community-scripts "Docker LXC" Newt agents (Debian 13), found while
hardening three of them by hand. No `config_version` bump: no config key
was added, renamed or made required. `ssh.disable_password_auth: true`
does now also set `PermitRootLogin prohibit-password` (unless the current
value is already stricter), so re-running `base` on a host that already had
it on will tighten root login too.

- **`ssh.disable_password_auth` no longer kills sshd on hosts where
  `ssh.socket` owns the ssh port alongside an enabled `ssh.service`.** In
  that state a `systemctl reload ssh` makes sshd re-exec, fail with `fatal:
  Cannot bind any address.` and exit (the reload job can still report
  success), so new logins break while open sessions carry on. This
  happened on all three hosts. `base` now detects it before touching sshd
  and switches the host to plain `ssh.service` (`systemctl disable --now
  ssh.socket && systemctl enable ssh.service && systemctl restart
  ssh.service`); if sshd doesn't come up, it re-enables `ssh.socket` and
  fails loudly. Ubuntu 24.04's default socket activation (`ssh.service`
  disabled) isn't a conflict and still just gets a reload.
- **The sshd drop-in is now `01-charmer-hardening.conf`** (was
  `60-charmer-key-only.conf`, removed on the next run). sshd keeps the
  first value it reads and reads drop-ins in lexical order, so the old name
  could lose to e.g. Ubuntu's `50-cloud-init.conf` (`PasswordAuthentication
  yes`). It now also sets `PubkeyAuthentication yes` and `PermitRootLogin
  prohibit-password` (never `no`, and never loosening an already-stricter
  value). Its effective values are checked through `sshd -T` before
  anything is reloaded; the drop-in is removed again if they don't hold.
- **`base` apply and `verify()` now confirm sshd itself is listening** on
  the ssh port (not only systemd) and `ssh.service` is active, after the
  reload/restart. `sshd -t` passing and the reload's exit status proved
  neither.
- **`preflight` reports a Docker API reachable over TCP on every host**
  (listener on 2375/2376, dockerd on any other TCP port except Swarm's
  2377/7946, or a `tcp://` host in `daemon.json` / dockerd's args). The
  template shipped `tcp://0.0.0.0:2375` with no TLS or auth. Refused in
  `production`, a warning in `lab`, report only.
- **`preflight` warns about the `ssh.socket`/`ssh.service` conflict on
  every host**, whether or not `disable_password_auth` is on: any sshd
  restart, including an openssh upgrade, hits it.
- **Debian Newt agents: `base`'s Docker CE repo is no longer hardcoded to
  Ubuntu.** It's picked from `/etc/os-release`'s `ID`
  (`download.docker.com/linux/{ubuntu,debian}`); other distros fail that
  step clearly. Hosts with `docker` but no `docker compose` are reported
  instead of getting Docker CE installed over them. Baseline packages drop
  `lsb-release`/`apt-transport-https` (unused) and use `gnupg` instead of
  the transitional `gnupg2`. `preflight` now reports each agent's OS
  (warning unless Ubuntu/Debian); the Pangolin host's "warn if not Ubuntu
  24.04" is unchanged. The agent "docker present" line now correctly says
  `base` installs Docker, not `newt`.
- README "Verification status": UFW verified inside a real unprivileged
  Debian 13 LXC, and the sshd hardening procedure exercised by hand on real
  Debian 13 LXCs. Charmer's automation of it is unit-tested, not yet run
  end-to-end.

## [0.8.0] - 2026-09-22

- **New optional `pangolin.integration_api.enabled` / `.port`.** The
  integration API used to be all-or-nothing: on at a hardcoded `3003`
  whenever `newt_agents` were configured, off otherwise. Now
  `integration_api.enabled: true` turns it on independent of `newt_agents`
  (for your own tooling against it) and `.port` overrides `3003`; both are
  optional and default to today's behavior (auto, `3003`), so no
  `config_version` bump. Setting `enabled: false` while `newt_agents` are
  configured is refused at config-load time, since the `newt` phase needs
  the API to mint their credentials. Still always published loopback-only
  (`127.0.0.1:<port>`) on the Pangolin host either way; charmer's own
  Traefik config never routes it anywhere, so reaching it from off-host
  (e.g. an SSH tunnel) is on you. See README "Newt credential automation".

## [0.7.0] - 2026-09-22

- **`charmer logs` now resolves user<->resource connection history**,
  writing `<site>_access.csv` (or `<save-file>_access.csv` with `--save`)
  alongside the existing text report: one row per session (Agent, Agent
  IP, Started, Ended, Duration, Who, Where, Proto, Destination). Source is
  each Newt agent's own `ACCESS START`/`END` log lines, fetched unfiltered
  (bypassing `--level`, since Newt logs these at INFO and Pangolin CE has
  no server-side handler for the `newt/access-log` message that would
  otherwise centralize them — see fosrl/pangolin#3695), cross-referenced
  against Pangolin's Postgres (`resources`, `clients`, `user`) via `docker
  exec postgres psql` on the host: a local Unix-socket connection, so no
  Postgres password is needed or stored for this (`initdb`'s default
  `local ... trust`, never touched by the Docker entrypoint's TCP-only
  auth setup). `handoff` now also writes `pangolin.database`/
  `pangolin.postgres_user` into `config.<site>.monitor.yml` so `logs`
  knows how to run the query; SQLite deployments (lab-only) get the plain
  per-agent dump but no resolved CSV rows. Only Newt agents already listed
  in that site's `newt_agents:` are reachable this way — `monitor.yml` is
  plain, standalone YAML, so hand-add an entry there to cover an agent
  this site didn't provision itself. No config.yml keys changed, no
  `config_version` bump. See README "Monitoring".

## [0.6.0] - 2026-09-22

- **New optional `base.unattended_upgrades` — mask the OS's own
  unattended-upgrades.** Defaults to `true` (leave
  `unattended-upgrades.service` + `apt-daily-upgrade.timer` alone, today's
  behavior); set to `false` and `base` masks both units on every host
  (Pangolin host and every Newt agent), not just disables, so a package's
  own postinst can't silently re-enable the timer on its next upgrade. Same
  reasoning as `apt upgrade` never running automatically here — package
  drift belongs to your patching policy, not the provisioner — just closing
  the same gap on the OS's own silent schedule. Purely additive and
  optional, like `monitor.ips`/`pangolin.host.hostname` before it, so no
  `config_version` bump. See README "base".

## [0.5.0] - 2026-09-22

- **`pangolin` now offers to reuse an existing `server.secret` instead of
  always generating a new one.** The first time the phase runs for a site,
  it asks (hidden input, before `config.yml` is first rendered): Enter
  generates a random secret as before; pasting an existing value instead
  pins that one. Meant for migrating a dump from a *different* Pangolin
  deployment onto a fresh charmer-provisioned site — that data was
  encrypted/signed under the old secret, so this site needs to match it
  from the first boot, not have it patched in after the fact. Replaces the
  0.3.0 README workaround of hand-editing `.state/<site>.json`'s
  `generated.pangolin_server_secret` directly; that file should no longer
  need manual edits for this. Like every pinned value, it's asked **once
  ever** — get it right before `pangolin`'s first apply, since a later
  `--replay pangolin` reuses whatever's already pinned. See README
  "restore".

## [0.4.0] - 2026-09-22

- **New optional `pangolin.host.hostname` — sets the Pangolin host's OS
  hostname.** Same resolution shape as `monitor.ips`: config value first,
  else `base` asks interactively the first time it runs against a site
  (Enter to leave the current hostname alone), pinned in state so `--replay
  base` never re-asks or drifts from what the file says. Runs `hostnamectl
  set-hostname` and syncs `/etc/hosts`'s `127.0.1.1` line to match, avoiding
  the classic `sudo: unable to resolve host <old-name>` cosmetic warning
  that `hostnamectl` alone leaves behind. Safe to use against an
  already-running site: nothing charmer renders or Pangolin itself needs is
  keyed off the OS hostname (see README "base"). Purely additive and
  optional, like `monitor.ips` before it, so no `config_version` bump.
  `charmer status` also surfaces the resolved hostname when one is set.

## [0.3.0] - 2026-09-22

- **New `charmer status CONFIG` command.** Reads the config file plus its
  `.state/<site>.json` and prints per-phase status/timestamps, which secrets
  are pinned (names only, values never shown), per-agent Newt credential-
  minting status, and the `restore`/`tls` summary — purely local, no SSH
  connection opened. Answers "where did the last run stop" without
  `--only preflight` or waiting on `monitor` to connect. See README
  "The phase model".

- **README: documented two operational gotchas from a real production
  migration** (the same one behind 0.2.0's Newt-redial fix): restoring a
  dump from a *different* Pangolin deployment needs that old deployment's
  `server.secret` pre-seeded into `.state/<site>.json`'s
  `generated.pangolin_server_secret` before the `pangolin` phase first
  runs, otherwise data encrypted/signed under the old secret (sessions,
  2FA, stored resource passwords) is unreadable once restored under a
  freshly-generated one; and Newt agents outside this run's `newt_agents`
  list (never charmer-provisioned, or trimmed from a later config) need a
  manual `docker compose down` + `up -d` after a restore recreates gerbil,
  not just `restart`. See README "restore".

## [0.2.0] - 2026-09-22

- **`restore` now redials already-provisioned Newt agents after loading a
  dump.** Recreating gerbil/traefik (below) restarts gerbil's WireGuard
  process; a Newt agent that was already tunneled in before the run goes
  stale and previously stayed that way until an operator manually ran
  `docker compose restart` on it, since `newt` itself is skipped whenever a
  restore just ran (its own credentials-aren't-re-minted guard). `restore`
  now does a lightweight `docker compose restart newt` on each configured
  agent that already has a bundle at `/opt/newt` — no credentials touched,
  no DB lookup, since a Newt container's compose file already carries the
  right `newtId`/secret from whenever it was first provisioned. An agent
  with no bundle yet is skipped (this is the no-op path whenever
  `newt_agents` is empty, e.g. Newt managed entirely outside charmer); a
  restart that doesn't come back is a warning, not a phase failure. Surfaced
  by a real-world production migration report: an existing, non-charmer
  Pangolin deployment restored into a fresh charmer-provisioned stack via
  `restore.postgres_dump`, whose already-running Newt agents went dark
  after the pipeline recreated gerbil, with nothing left to tell them to
  redial. See README "restore".

## [0.1.0] - 2026-09-17

- **First full pipeline run against a real lab deployment.** `preflight`
  through `handoff` on a real Ubuntu host plus a real Newt agent, `restore`
  exercised both without a dump configured and with a real destructive
  restore, `tls.provider: acme` exercised against Let's Encrypt staging and
  then production, the maintenance page checked without and with a custom
  logo, the Newt agent provisioned/connected with a private resource
  published and reached from outside, and `shutdown`/`start`/`clean`/
  `monitor`/`logs` all run against that same site. See README "Verification
  status" for what's still unexercised.

- **`restore` now reconciles Gerbil's exit-node identity after loading a
  dump from a different Pangolin deployment.** A dump's `exitNodes` row
  overwrites this host's `publicKey`/`reachableAt` with the source
  deployment's, but Gerbil's WireGuard key on disk
  (`/opt/pangolin/config/key`) is an untouched host bind mount — the two go
  out of sync, and Pangolin's own `createExitNode()` can't self-heal it
  (its update path matches `WHERE publicKey = <incoming key>`, which never
  matches a foreign row, so it silently no-ops forever). Gerbil then
  crash-loops trying to parse the blank CIDR address it gets back from
  `/api/v1/gerbil/get-config`. `restore` now snapshots this host's own
  exit-node identity before the load and patches it back afterward, keyed
  on `exitNodeId` rather than `publicKey`. Confirmed against a real
  cross-deployment restore and the upstream `createExitNode.ts`/
  `getConfig.ts` source. See README "restore".

- **`handoff` now surfaces Pangolin's one-time server-admin setup token
  directly, instead of only pointing at `/auth/initial-setup`.** The scrape
  (`docker compose logs pangolin`, looking for the `Token: ...` line Pangolin
  prints on first boot) previously lived only in the `newt` phase's
  first-run-bootstrap announcement — which never runs when `newt_agents` is
  empty, e.g. a Pangolin-only lab deploy. That left no path to the token at
  all besides the operator knowing to go check `docker compose logs`
  themselves. Moved the scrape into a shared `read_pangolin_setup_token()`
  in `remote.py`; both phases use it now. See README "handoff"/"newt".

- **`base`'s `monitor.ips` ssh scoping now always keeps charmer's own
  current connection alive, in addition to whatever `monitor.ips` says.**
  `monitor.ips` answers "what should stay allowed long-term" (typically a
  separate admin/monitoring box), not "what IP is charmer connecting from
  right now" — an operator provisioning from their own workstation while
  only listing a separate monitoring host's IP would get UFW-locked out the
  moment scoping applied. Because UFW doesn't tear down the already-open
  connection, the run itself finished looking clean, and the token gap
  above compounded it: no setup-token announcement (no `newt_agents`
  configured) plus no ssh access afterward to go find it by hand. `base`
  now reads each host's own connecting IP off `$SSH_CLIENT` for the
  connection already in hand and folds it into that host's allow-list
  automatically — no config needed. See README "base".

- **Newt's default pinned image tag (init wizard + `config.example.yml`)
  bumped 1.16.0 -> 1.17.0.**

- **Evaluated and declined switching `newt` agents to the new Pangolin
  CLI.** Pangolin 1.23 introduced a unified `fosrl/cli` (Docker image
  `fosrl/pangolin-cli`, `pangolin up site`) that bundles Newt's own
  site-connector code with client/SSH/SCP/AI-gateway tooling, and made it
  the dashboard's default for new sites. Confirmed against Pangolin's 1.23
  release notes that Newt itself is unaffected — "Newt will continue to be
  provided in all of its current forms for the foreseeable future," and is
  the recommended choice specifically when the smallest binary/container
  footprint matters. `newt_agents` targets arbitrary, possibly
  resource-constrained SSH hosts, so charmer stays on Newt directly rather
  than adding the heavier all-in-one CLI for features (client/SSH/SCP) it
  has no use for. See README "newt" phase section.

- **Removed the host nginx passthrough layer — Traefik/Gerbil are now the
  direct public first hop, exactly matching the official Docker Compose
  layout with zero port-publish deviation.** Previously Gerbil's `80`/`443`
  Docker publishes were narrowed to `127.0.0.1` so a bare-metal nginx
  `stream{}` block could own the real public interface and passthrough-
  forward to Traefik, wrapping every connection in the PROXY protocol so
  Traefik/Pangolin didn't log every visitor as `127.0.0.1`. That whole layer
  — `nginx_phase.py`, `templates/nginx.conf.j2`, the `base` phase's
  `nginx-full` install, and the `nginx` pipeline phase/preflight/monitor/
  logs plumbing around it — is gone. `pangolin-compose.yml.j2` now
  host-publishes Gerbil's `80`/`443` on the wildcard address, matching
  [docs.pangolin.net/self-host/manual/docker-compose](https://docs.pangolin.net/self-host/manual/docker-compose)
  verbatim; `traefik-config.yml.j2` drops the `proxyProtocol.trustedIPs`
  entrypoint config that only existed to unwrap nginx's PROXY-protocol
  header. The end-to-end reachability check the `nginx` phase used to run
  (curl the public interface, expect it to reach Pangolin's API) moved into
  `pangolin_phase.py`'s own `verify()`, since Gerbil/Traefik now own that
  interface directly. `charmer monitor`'s "PANGOLIN HOST" panel and
  `charmer logs` no longer check/pull nginx's systemd state and journal —
  there's no nginx unit left on the host. See README "Ingress".

- **New optional `monitor.ips` — scopes ssh to an admin/monitoring
  allow-list.** Previously the `base` phase always ran `ufw allow ssh`
  (open to anywhere) on the Pangolin host and every Newt agent, with no way
  to restrict it — unlike akropolis, which prompts for a `monitor.ip` and
  punches a UFW hole for it. Porting that mechanism as-is would have been a
  no-op: akropolis's `monitor.ip` opens extra *polling* ports (Patroni/etcd/
  HAProxy stats, nginx stub_status) for a separate external monitoring tool
  with only HTTP reachability, and charmer has none of those — `charmer
  monitor`/`logs` already run entirely over the operator's existing SSH
  access (see README "Monitoring"), so there's no second port to punch
  through. `monitor.ips` instead scopes SSH itself: an optional list of
  IPs/CIDRs, resolved config-first then via an interactive prompt (Enter to
  skip, pinned in state so `--replay` never re-asks) exactly like
  `ssh.disable_password_auth`'s resolution. Backward compatible — an
  omitted or empty list keeps today's open-to-anywhere `ufw allow ssh`.
  See README "base".

- **`charmer shutdown` no longer stops Gerbil.** It was stopping both
  `pangolin` and `gerbil`, but Traefik runs `network_mode: service:gerbil`
  (see README "Ingress") — it borrows Gerbil's network namespace and
  loopback port-publish wholesale instead of having one of its own.
  Stopping Gerbil silently took Traefik's connectivity down with it:
  Traefik and the `maintenance` container kept showing as "running" in
  `docker compose ps`, but were completely unreachable, so the shutdown
  phase's own verify step (curl the dashboard host, expect the maintenance
  page) failed every time, and the run stopped in a half-applied state
  that `charmer start` then correctly refused to build on top of ("did not
  last complete gracefully"). `apply()` now stops only `pangolin`.
  Recovering a site already left in that half-applied state (Gerbil down
  from a prior buggy run) took three attempts to get right, each surfacing
  the next issue: (1) `docker compose up -d gerbil ...` without `--no-deps`
  silently started `pangolin` back up too — both Gerbil and Traefik
  `depends_on: pangolin: condition: service_healthy` — and blocked on its
  healthcheck for up to ~2.5min with no progress output, which looked like
  a hang; fixed with `--no-deps` plus `ctx.begin()` status lines. (2)
  Docker doesn't migrate a `network_mode: service:X` container when X
  restarts — a Traefik that never stopped stayed pinned to Gerbil's dead
  old network namespace after Gerbil came back up, so the maintenance-page
  curl kept failing (`rc=7` connection-refused, then `rc=35`
  TLS-handshake-failure once Gerbil's port-publish came back but Traefik
  still wasn't listening on it); first fixed by force-recreating `traefik`
  but only inside an `if gerbil was found down` branch. (3) That condition
  was itself the bug: a *previous* half-successful run had already brought
  Gerbil back up (so it no longer read as "down") while leaving Traefik
  stale from that same restart, so the guarded fix silently didn't fire on
  a site in exactly that state. There's no cheap way to detect "is traefik
  actually still attached to gerbil's current namespace" from the outside,
  so the condition is gone — `apply()` now unconditionally runs
  `up -d --no-deps gerbil maintenance` then
  `up -d --no-deps --force-recreate traefik` before stopping `pangolin`,
  every time. `verify()`'s maintenance-page check is now a short poll
  (`remote.wait_for`, 30s) rather than one curl, since traefik needs a
  moment to rebind right after being recreated.

- **Compose networking corrected: dropped `network_mode: host` everywhere.**
  The claim that docs.pangolin.net's compose uses `network_mode: host` on
  pangolin/gerbil/traefik was wrong — verified directly against the current
  page, which uses plain bridge networking on pangolin/gerbil/postgres and
  `network_mode: service:gerbil` on traefik, with Gerbil itself publishing
  `51820/21820/udp` (wildcard) and `80/443` (also wildcard, upstream).
  Charmer now matches that almost exactly; the one remaining deviation is
  narrowing Gerbil's `80`/`443` publish to `127.0.0.1` so host nginx can own
  the real public interface on those port numbers (see README "Ingress") —
  a bind-address change on an existing Docker port-publish, not a
  networking-mode change. Traefik's own `entryPoints` go back to the
  wildcard address (matching upstream) since the loopback restriction now
  lives in Gerbil's port-publish instead. Postgres drops the
  `listen_addresses=127.0.0.1` workaround (unneeded — it's never published
  to the host at all now) and pangolin's Postgres connection string /
  Traefik's HTTP-provider endpoint / dynamic-config service URLs move from
  `127.0.0.1:<port>` to the container DNS names (`postgres`, `pangolin`,
  `maintenance`) the bridge network provides. Pangolin's compose block
  additionally loopback-publishes `3001` (always) and `3003` (when Newt
  agents are configured) so charmer's own SSH+curl verify/integration-API
  tooling keeps working without host networking.
  **Caveat, read before applying to a live host:** an earlier version of
  this project tried a bridge-networking substitution and reverted it after
  it broke Gerbil's WireGuard hole punching (see the `network_mode: host`
  entry lower in this file). That was against the pre-rebuild scaffold, not
  this implementation, and this one matches upstream's actual current
  compose (cap_add, explicit UDP ports, `reachableAt`/`remoteConfig` all
  included) — but it has **not** been verified end-to-end against a real
  NAT'd Newt agent doing an actual hole punch (not just relay fallback).
  Test against a disposable lab node before trusting this on production.
  Gerbil's `wg0` interface also now lives inside Gerbil's own container
  network namespace instead of the host's, which should incidentally fix
  the key-drift-after-recreate issue this change was originally prompted
  by (a stale host-level `wg0` surviving `docker compose up -d
  --force-recreate` across unrelated config changes) — also unverified.

Full rebuild, replacing the initial Copilot-generated scaffold with a real
engine in the [akropolis](https://github.com/ktsouvalis/akropolis) pattern:
plan/confirm/apply/verify phases, a transcript log, pinned-secret state,
`config_version` schema gating, and actual remote provisioning (the old
`newt` phase only ever wrote a local file — it never touched a Newt agent).

- `site.config_version: 1` is the baseline schema this changelog tracks
  from. Bump it whenever a config-file change needs operator action to
  carry forward (a renamed/removed key, a default that would silently
  change behavior, a key that becomes required) — see `CONFIG_SCHEMA_VERSION`
  in `src/charmer/config.py`.
- Compose now follows the official self-hosted layout exactly
  (`network_mode: host` on pangolin/gerbil/traefik) instead of the earlier
  bridge-networking substitution, which broke Gerbil's WireGuard hole
  punching.
- Ingress: Traefik keeps all TLS/ACME itself (loopback-bound entryPoints,
  otherwise unmodified) so Pangolin's dynamically-created resource
  subdomains keep working; host nginx is a pure L4/SNI passthrough with
  PROXY protocol, not a TLS terminator — see README "Ingress".
- Newt agent credentials are minted automatically via Pangolin's
  integration API (`pick-site-defaults` + `PUT .../site`) instead of being
  pasted in by hand — see README "Newt credential automation".
- `restore` now runs before `newt` in the pipeline (was: `newt` then
  `restore`), and `newt` skips itself automatically whenever a restore
  actually loaded a dump this run — a restored database can already carry
  Pangolin sites for these agents, and minting fresh ones on top would
  create duplicates (`PhaseContext.restore_ran`, scoped to one `provision`
  invocation). When no restore runs, `newt` now prints the one-time manual
  bootstrap steps (`/auth/initial-setup`, create an org, mint a Root API
  Key) before prompting for the org ID / Root API key, instead of prompting
  cold. Also fixed `pangolin_api.py` building the integration-API URL with
  `shlex.quote(org_id)` (shell-quoting, not URL-encoding) — an org ID with
  a space produced a literal malformed URL and every call failed.
- Removed a fictional `pangolin.oidc` config.yml section: confirmed against
  docs.pangolin.net that identity-provider setup is dashboard-only, no
  config key exists for it.
- Newt's default pinned image tag (init wizard + `config.example.yml`) bumped
  1.5.0 -> 1.16.0. (Gerbil's own `pangolin.gerbil_tag` default is unrelated
  and unchanged — a separate component's version.)
- Added `ssh.disable_password_auth` (per-host: the top-level `ssh:` block and
  each `newt_agents[].ssh:` block) — opt-in, defaults to `false`. Once an
  operator is confident key/agent auth works, the `base` phase locks that
  host's sshd to key-only via a drop-in, `sshd -t`-validated before reload.
  `config.py` refuses the combination with `auth: password`, which would
  lock the operator out on the very apply that turns it on. UFW's inbound
  posture for Newt agents (deny-all except ssh — they only ever connect
  *out* to Pangolin) was already in place; nothing changed there.
- Added Pangolin config.yml's real `email:` section (confirmed against
  docs.pangolin.net) as a new `smtp:` config block, for password
  reset/invite emails. `smtp_pass` is never written to the config file —
  same treatment as the Newt Root API key: prompted once (hidden) the first
  time the `pangolin` phase runs and pinned in local state.
- `charmer init` now also asks for an organization name and defaults
  `maintenance.message` to a Greek "we'll be back" line built from it
  (`{org} — θα επιστρέψουμε σε λίγο.`) instead of the English fallback —
  editable in the generated config file like any other `maintenance.*` key.
- Fixed `charmer start`: it ran `docker compose up` and the pangolin-healthy
  wait with no live status line at all (every other phase that can run long
  — `pangolin`, `newt`, `restore` — calls `ctx.begin()` first). `shutdown`
  and `clean`'s own steps are quick stop/down calls, consistent with how
  the rest of the pipeline already treats those (no spinner elsewhere for a
  plain stop/down either), so they were left as-is.
- Fixed `pangolin_api.py`'s curl invocation relying on bash's `$'...'`
  ANSI-C quoting for `-w $'\n%{http_code}'` — `NodeConn.run()` always wraps
  sudo'd commands as `sh -c '...'` (sshexec.py), and under plain POSIX `sh`
  that syntax isn't special, so a literal `$` leaked into the response body
  and broke JSON parsing on every call. Replaced with a control-byte
  sentinel passed through a plain single-quoted argument, which every POSIX
  shell preserves literally.
- `newt` phase: when `tls.provider: self_signed`, every Newt agent now gets
  `SKIP_TLS_VERIFY=true` — previously agents retried `x509: certificate
  signed by unknown authority` forever while looking "running" enough to
  pass `verify()`. fosrl/newt's `TLS_CLIENT_CAS` looked like the correct,
  non-blunt fix but turned out to be a dead end in the current release: it
  only takes effect alongside a client cert/key for mTLS, never standalone
  (confirmed against `websocket/client.go`'s `setupTLS()`) — see README
  "Newt credential automation". Agents already deployed before this fix
  need `charmer provision <config> --only newt` to pick it up.
- `newt` phase now prints the Pangolin setup token (scraped from `docker
  compose logs pangolin`) directly in the one-time manual-step instructions
  instead of sending the operator to go find it themselves.
- `charmer monitor` / `charmer logs` are real now (SSH-based; no separate
  public monitoring ports needed), replacing the print-loop stubs.
- Added `charmer shutdown` / `start` / `clean` lifecycle commands.
- `charmer shutdown` now leaves `traefik` (and a new always-on `maintenance`
  container) running instead of stopping it too, so Traefik's own `errors`
  middleware can fall back to a "we'll be back" page on the dashboard host —
  optionally with your own logo (`maintenance.logo`/`maintenance.message`
  in the config). Dashboard-only: resource subdomains are routed by
  Pangolin's own HTTP provider, not charmer's static Traefik config, so
  they're unaffected and still see a hard failure during a shutdown. See
  README "Maintenance page".
- `pangolin.base_domain` is now required at config-load time instead of an
  optional "set it later from the dashboard" field: Pangolin CE actually
  refuses to boot at all without at least one domain configured
  (`Validation error: At least one domain must be defined`), so the old
  default silently produced an unhealthy `pangolin` container on first
  apply. `pangolin_phase.py` also now dumps `pangolin`/`gerbil`/`traefik`
  logs on a `docker compose up` failure, not just on the healthcheck-timeout
  path, so this kind of app-level crash is visible instead of just
  Compose's dependency-failed error.
- `pangolin_phase.py` now passes `--force-recreate` to `docker compose up -d`
  whenever the rendered config actually changed. Compose only recreates a
  container when the *service definition* changes, not when a bind-mounted
  file's content does — so a `pangolin` container already crash-looping on
  stale config (e.g. from a previous failed apply, sitting mid Docker
  restart-backoff) could survive a config fix untouched and never pick it
  up within that run's health-gate wait, reproducing the exact same error
  a second time even though the new `config.yml` was already on disk.
  Also dropped the phase's "skip `docker compose up -d` if any container is
  already running" shortcut — `docker compose up -d` is idempotent, and the
  shortcut let a partial failure (e.g. postgres/maintenance healthy,
  pangolin crash-looping) look like "nothing to do" and skip straight to
  the health wait instead of ever retrying pangolin.
- `charmer monitor` is now a real Textual TUI (`src/charmer/monitor/_tui.py`)
  instead of a `rich.Live`-refreshed table: a dark panel-and-dot layout with
  live-updating border titles, a status bar, and `r`/`q` bindings, in the
  same visual idiom as this rebuild's predecessor
  (`ktsouvalis/pangolin`'s `monitor.py`). Still SSH-only (`docker inspect`,
  `systemctl is-active` — see README "Monitoring" for why), still reads
  `config.<site>.monitor.yml`. Internally, checks are now built around a
  `MonitorNode`/`HostStatus`/`AgentStatus` model with the Pangolin host kept
  as a one-element list rather than a single hardcoded host, so a future
  multi-node Pangolin rollout only needs a config-loader change, not a UI
  rewrite. `--once` and non-tty runs (CI, piped output) still fall back to
  a single plain `rich` table snapshot instead of launching the TUI. Added
  `textual` as a new dependency.
- Fixed `base` phase's UFW rule on the Pangolin host: it opened `ssh`/`80`/
  `443` but never `51820/udp`/`21820/udp`, even though `preflight` already
  checked those two ports were locally free (`REQUIRED_FREE_UDP_PORTS` in
  `config.py`) and Gerbil binds them directly via `network_mode: host`.
  With UFW's default-deny-incoming policy left in place, every inbound
  WireGuard handshake and holepunch/relay packet from a Newt agent or
  client was silently dropped at the host firewall — visible client-side as
  Olm's `HOLEPUNCH_MISSING` ("Unable to coordinate client P2P connection").
  `base_setup.py` now adds `ufw allow 51820/udp` and `ufw allow 21820/udp`
  alongside the existing TCP rules.
