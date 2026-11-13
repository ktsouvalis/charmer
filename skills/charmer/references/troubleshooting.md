# charmer troubleshooting

Start every investigation with:
1. `charm status CONFIG` to find the failed phase and its recorded error.
2. The newest `<site>.*.transcript.log` next to the state file, which lists
   every command run (secrets best-effort redacted).
3. `charm logs config.<site>.monitor.yml --last 2 --level warning`.

| Symptom | Cause | Fix |
|---|---|---|
| Gerbil fails `failed to bind host port 0.0.0.0:51820/udp: address already in use` after other containers are healthy | Something else owns 51820/udp, often a pre-existing `wg0` | Find it (`sudo ss -ulnp \| grep 51820`, `sudo wg show`, `docker ps -a`) and stop it. Then `--only pangolin`. The port is hardcoded; do not remap it. charmer will not kill foreign services. |
| Every site shows Disconnected with `RELAY=true`; Gerbil logs `No proxy mapping found` | Gerbil `start_port` pointed at 21820 | charmer hardcodes 51820. If someone hand-edited `/opt/pangolin`, re-render with `--only pangolin`. |
| Agents disconnected after a restore, Gerbil upgrade or host reboot | WireGuard tunnels were tied to the old Gerbil process | Managed agents are redialed automatically. For each **unmanaged** site charmer lists, run `docker compose down && docker compose up -d` on its host (or `systemctl restart` for a service install). |
| Gerbil crash-loops after a restore | Dump's `exitNodes` row belongs to another deployment | The `restore` phase patches this. If restored by hand outside charmer, redo it with `--only restore`. |
| Newt running but never connects; logs `x509: certificate signed by unknown authority` | `tls.provider: self_signed` | Current charmer sets `SKIP_TLS_VERIFY=true`. On older sites run `--only newt` to re-apply. `TLS_CLIENT_CAS` alone does not work in Newt. |
| Preflight TUN probe fails on an agent | Often an unprivileged LXC without `/dev/net/tun` | On the hypervisor, add `lxc.cgroup2.devices.allow: c 10:200 rwm` and `lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file` to the container's conf, or use a privileged container or VM. Don't assume the host type; ask. |
| Preflight refuses: Docker API on TCP | `tcp://` in `/etc/docker/daemon.json` `hosts` or dockerd `-H` (common in Proxmox "Docker LXC" templates) | Remove the `tcp://` entry and restart docker. Unauthenticated root access is never acceptable. |
| sshd reload kills new logins (`Cannot bind any address`) | `ssh.socket` and an enabled `ssh.service` both own the port | `systemctl disable --now ssh.socket && systemctl enable ssh.service && systemctl restart ssh.service`. `base` does this itself when hardening. |
| Next SSH connection times out after `base` scoped ssh | Connecting from an IP not in `monitor.ips` | `base` auto-adds the source IP of that run. A new workstation needs adding to `monitor.ips` and `--replay base`, or console access. |
| Agent logs ~30 "Site N endpoint changed" per hour; SSH sessions through Pangolin drop | NAT hairpin: agent reaches the public IP through NAT that remaps source ports | Set `pangolin_via_host_ip: true` on that agent (needs `tls.hostname`), then `--only newt`. |
| "No available subnets remaining in space" | Org `utilitySubnet` is a legacy /24 | Set `restore.utility_subnet_prefix` (e.g. `20`) and redo the restore, or widen it by hand after a backup. |
| "No available subnets found for exit node" after a multi-exit-node restore | Too many tunnels for one exit node's address | Prune unused sites or clients, or widen the exit node network. That is the operator's decision. |
| `config.py` refuses the config | Unquoted tags (`1.22` is a YAML float), `latest` Newt tag, SQLite in production, `disable_password_auth` with `auth: password`, or integration API disabled while agents exist | Fix the config. These guards are intentional. |
| State file refused | It belongs to another tool (e.g. akropolis) or another site | Use the correct config/state pair. Never repoint by hand. |
| Maintenance page missing on resource subdomains during shutdown | By design: those routes come from Pangolin's live provider | Expected. Only the dashboard host is covered. |
