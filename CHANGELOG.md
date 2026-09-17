# Changelog

## [0.1.0] - 2026-09-17

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
