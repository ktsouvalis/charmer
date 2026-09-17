# CLAUDE.md

## Project

Charmer provisions one self-hosted Pangolin Community Edition node and
separate Newt agents over SSH, in the [akropolis](https://github.com/ktsouvalis/akropolis)
pattern (plan/confirm/apply/verify phases, pinned-secret state, a
transcript log, resumable pipeline) — but as its own app, not a port: no
topology switch, no etcd/Patroni/HAProxy/keepalived. HA is a roadmap line,
not part of the current pipeline, because official Pangolin CE has no
self-hosted HA at all (Enterprise-only).

See `README.md` for the full design writeup (phase-by-phase, the ingress
design and why it isn't a copy of akropolis's nginx pattern, Newt credential
automation, state/secrets posture) and `CHANGELOG.md` for what changed and
why. Keep both current when the design changes — they are the source of
truth for "where things stand," not this file.

## Layout

- `src/charmer/config.py`, `state.py`, `sshexec.py`, `remote.py`,
  `transcript.py` — the engine (config load/validate, pinned-secret state,
  the SSH layer, template rendering + checksummed push, the audit log).
- `src/charmer/phases/base.py` — `Phase`/`PhaseContext`/`run_phases`.
  `src/charmer/phases/*.py` — the real phases: `preflight`, `base_setup`
  (phase name `base`), `pangolin_phase`, `newt_phase`, `restore_phase`,
  `handoff_phase`, plus `lifecycle.py` (shutdown/start) and
  `clean_phase.py`.
- `src/charmer/pangolin_api.py` — the integration-API client used only by
  the `newt` phase, called over the existing SSH connection against
  loopback, never over the public internet.
- `src/charmer/templates/*.j2` — Jinja2 templates for every rendered file
  (Pangolin Compose/config.yml, Traefik static+dynamic config, the Newt
  Compose bundle).
- `src/charmer/monitor/dashboard.py`, `monitor/logs.py` — `charmer monitor`
  / `charmer logs`, SSH-based (no separate public monitoring ports).
- `src/charmer/init_wizard.py`, `cli.py` — `charmer init` and the top-level
  argparse wiring.

## Rules

- Follow the official Pangolin self-hosted Docker Compose layout verbatim:
  bridge networking on pangolin/gerbil/postgres, `network_mode:
  service:gerbil` on traefik, Gerbil host-publishing 80/443 on the wildcard
  address directly, exactly as docs.pangolin.net's compose does. No service
  uses `network_mode: host`, and there is no host-level reverse proxy or L4
  passthrough in front of it — see README "Ingress" before changing
  anything here.
- Traefik owns all TLS/ACME itself, for the dashboard domain and every
  future resource subdomain, and is the direct first hop on the public
  interface — nothing sits in front of it. Do not reintroduce nginx or any
  other reverse proxy/passthrough layer.
- Pangolin has no OIDC config.yml key. Don't reintroduce one — identity
  provider setup is dashboard-only (Server Admin → Identity Providers),
  confirmed against docs.pangolin.net.
- PostgreSQL is the production default; SQLite is lab-only.
- Restore is optional and accepts PostgreSQL `.sql.gz` dumps only.
- Handoff is read-only and must never prompt.
- Newt targets are generic SSH hosts; do not infer VM/LXC/physical type.
  If TUN capability fails, describe the LXC-side fix conditionally ("if
  this is an unprivileged LXC...") — never assert the host's type.
- Newt image tags must be pinned; `config.py` refuses `"latest"`.
- Newt credentials are minted automatically via the Pangolin integration
  API (see `pangolin_api.py` and README "Newt credential automation"), not
  pasted in by hand — the Root API key and org ID are the one prompted,
  state-pinned exception.
- Keep local state, generated secrets, and the transcript log mode `0600`.
- `charmer monitor` and `charmer logs` are the two migrated-utility names;
  don't reintroduce the old `create_`/`normalize_` tools or the plain
  print-loop stubs this rebuild replaced.
