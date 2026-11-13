---
name: charmer
description: Operate charmer (the `charm` CLI), which provisions a self-hosted Pangolin CE node plus Newt site agents over SSH. Use for planning, reviewing config, running or resuming provisioning, adding Newt agents, restoring/migrating a Pangolin database, upgrades, shutdown/start, teardown, and troubleshooting charmer-managed sites.
---

# charmer

charmer turns one fresh Ubuntu 24.04 host into a Pangolin Community Edition
node (Pangolin, Gerbil, Traefik, PostgreSQL in the official Docker Compose
layout) and turns any number of separate SSH hosts into Newt site agents. It
runs from the operator's workstation; nothing is pre-installed on the targets.

Upstream: https://github.com/ktsouvalis/charmer. The README of the installed
version is the authority. This skill describes behaviour as of charmer 0.16.0;
if `charm --version` is newer, read `charm whats-new` before relying on
details here.

## First: establish the deployment context

This skill is organisation-agnostic. Never assume hostnames, IPs, domains,
site names or paths. Before doing anything site-specific:

1. Find the site config, `config.<site>.yml`. Look in the working directory,
   then ask the operator. Its directory also holds `.state/<site>.json` and
   transcript logs, because charmer resolves relative paths against the
   config file's directory.
2. Read it in full. Note `site.environment` (`lab` or `production`),
   `tls.provider`, `pangolin.database`, and the `newt_agents` list.
3. Run `charm status config.<site>.yml` (local only, no SSH) to see which
   phases are done, failed or pending, and which secrets are pinned.
4. Check the version: `charm --version`, and `charm check-update`.

If there is no config yet, the task is a new site. Start with `charm init`.

## How charmer works

Every phase runs **plan → confirm → apply → verify**. Lab sites confirm with
y/N; production sites require typing the site name. A phase that fails verify
is marked `failed` and the pipeline stops. Progress is recorded in the state
file, so a re-run resumes where it stopped.

Pipeline order: `preflight` (read-only) → `base` → `pangolin` → `restore`
(optional) → `adopt_newt` (only after a restore) → `newt` → `handoff`
(read-only, writes `config.<site>.monitor.yml`).

- `--only PHASE...` runs just those phases regardless of status.
- `--replay PHASE...` marks completed phases pending and re-runs them.

See `references/phases.md` for what each phase does and its gotchas.

## Rules for the agent

- **The operator runs interactive commands.** `provision`, `shutdown`,
  `start`, `clean`, `init` and `adopt_newt` prompt for confirmations,
  passwords and API keys. Prepare the exact command and let the operator run
  it in their own terminal (in Claude Code they can prefix it with `!`). Never
  pipe `yes` into it, script around the typed-name gate, or answer a
  production confirmation on the operator's behalf.
- **Preflight first.** Before any apply on a new or changed site, run
  `charm provision CONFIG --only preflight`. It is read-only and safe even
  against production.
- **Review before apply.** Walk the operator through the config and the
  printed plan. The config is the single source of truth. Edit it, don't work
  around it.
- **Treat the state file as a secret store.** `.state/<site>.json` holds
  plaintext secrets (Pangolin server secret, Postgres password, Root API key,
  Newt credentials, SMTP password). Never print, paste, commit or upload it.
  Use `charm status` to inspect it; it shows secret names only.
- **Pinned values are set once.** The server secret, DB password, Root API
  key and minted Newt credentials are generated or asked for once and reused
  forever. A replay never rotates them. If the operator is migrating from
  another Pangolin install, the old `server.secret` must be pasted at the
  `pangolin` phase's first prompt, because there is no second chance short of
  `clean`.
- **Don't hand-edit what charmer renders** on the hosts (`/opt/pangolin`,
  `/opt/newt`, the sshd drop-in, UFW rules). Change the config and `--only`
  or `--replay` the phase, or the next run will overwrite or fight the edit.
- **Destructive commands need explicit intent:** `clean`, `restore` with
  `restore.destructive: true`, and `--replay` on `pangolin` or `restore` for
  a live production site. Restate what will be lost and confirm before
  preparing the command.
- **Don't invent config keys.** Pangolin has no OIDC key in `config.yml`;
  identity providers are configured in the dashboard (Server Admin →
  Identity Providers). If unsure whether a key exists, check
  `config.example.yml` at the installed version's tag.

## Common tasks

**Install the CLI**
```bash
sudo apt install python3-cryptography python3-bcrypt python3-nacl
curl -fLO https://github.com/ktsouvalis/charmer/releases/latest/download/charm
chmod +x charm && ./charm --version
```
Upgrades: `charm check-update`, then `charm update` (zipapp only; source
installs use `git pull`).

**New site**
1. `charm init` writes `config.<site>.yml`. Review it with the operator.
2. `charm provision config.<site>.yml --only preflight`. Fix every ✘.
3. `charm provision config.<site>.yml` runs the full pipeline.
4. At the `newt` phase, if agents are configured, the operator first
   completes `/auth/initial-setup` in a browser, creates an org, and mints a
   Root API key with only the **Create Site** permission. charmer then asks
   for that key and the org ID once.

**Resume after a failure.** Run `charm status CONFIG`, read the failed phase's
error and the latest `*.transcript.log` next to the state file, fix the cause,
then re-run `charm provision CONFIG`. It resumes at the failed phase.

**Add a Newt agent.** Append an entry to `newt_agents` (pin `image_tag`; never
`latest`). Then:
`charm provision CONFIG --only preflight base newt`.

**Upgrade Pangolin, Gerbil, Traefik or Postgres.** Bump the quoted tag in
`pangolin:`, take a database backup first, then
`charm provision CONFIG --replay pangolin`. Changing `gerbil_tag` recreates
Gerbil; charmer then redials managed Newt agents and lists unmanaged ones the
operator must restart by hand.

**Restore or migrate a database.** Set `restore.postgres_dump` (`.sql.gz`,
PostgreSQL only) and `restore.destructive: true`. For a DB-only restore use
`charm provision CONFIG --only restore`. For connectors from the old install,
`adopt_newt` runs next and asks for their SSH details. Afterwards run
`--replay preflight base` for adopted hosts. See `references/phases.md`.

**Maintenance window.** `charm shutdown CONFIG` stops only Pangolin; Traefik
keeps serving a maintenance page on the dashboard host. Resource subdomains
are not covered. `charm start CONFIG` brings it back and refuses without a
prior graceful shutdown.

**Monitoring.** `charm monitor config.<site>.monitor.yml` and
`charm logs config.<site>.monitor.yml [--last H] [--level L] [--save FILE]`.
These take the **monitor** file from `handoff`, not the site config.

**Tear down.** `charm clean CONFIG` (typed-name confirmation; production also
needs `--i-know-this-is-production`). It reverses only what charmer created.
It does not delete Newt sites server-side in Pangolin.

## Troubleshooting

See `references/troubleshooting.md` for known failure modes and fixes:
squatted `51820/udp`, agents stuck disconnected, `x509` errors with
self-signed TLS, TUN failures on unprivileged LXC, sshd lock-out guards, and
NAT hairpin endpoint churn.
