# Deploy on the existing CapRover host (paper trading)

Supersedes VPS_PLAN.md. Nothing below has been run yet except two read-only SSH checks.

## What I found (read-only)
- SSH works over the Tailscale IP: `ssh opc@100.127.177.5` (or `captain-new.goose-dragon.ts.net`).
  From Git Bash, the bare name `captain-new` resolves to the public IP, where port 22 is closed. Use the IP or the full ts.net name.
- Host: OCI, **aarch64 (ARM)**, Docker Swarm 29.x, Tailscale running, ~4.7 GB RAM free, ~25 GB disk free.
  The image must be arm64. `python:3.12-slim` is multi-arch and pandas/numpy/yfinance have arm64 wheels, so this is fine.
- About 19 other services run there. This plan adds two or three apps and touches none of them.
- I did not inspect other apps' settings or env (they may hold secrets).

## Target layout
| CapRover app | Role | Public route |
|---|---|---|
| `agent-monitor` | `agent monitor --alert-only` | none (no HTTP) |
| `agent-dashboard` | `agent dashboard` on 0.0.0.0:8765 | HTTP only, tailnet-only (see 3) |
| `agent-jobs` (optional) | cron: monthly `paper-run --deposit` | none |

One repo, one Dockerfile, one image. An `AGENT_ROLE` env var picks the command (CapRover builds each app from the same repo).
Shared state: host directory `/srv/agent-data` mounted at `/app/data` in every app. The data is NOT in git or the image.

## 1. Repo changes (I make these after you approve)
- `Dockerfile` (python:3.12-slim, non-root user, `pip install .`, `TZ=Asia/Kolkata`), `entrypoint.sh` (switch on `AGENT_ROLE`), `captain-definition`.
- `.gitignore` and `.dockerignore`: exclude `.venv`, `data/` state and caches (`paper_state.json`, `monitor_status.json`, `prices_cache.csv`, `fundamentals.csv`), `__pycache__`.
- `config.docker.yaml` or env-driven paths so `data/` resolves to `/app/data`.
- `git init`, first commit. Not pushed. You create the private GitHub repo and push.

## 2. Host prep (you approve, then I run over SSH, or you run it)
```bash
sudo mkdir -p /srv/agent-data && sudo chown 10001:10001 /srv/agent-data && sudo chmod 750 /srv/agent-data
```
Seed from the laptop (over Tailscale): `config` stays in the image, `data/*.csv` and `paper_state.json` are copied once to `/srv/agent-data`.
Backup: a nightly host cron copying `paper_state.json` to a dated file. Tell me if the host already has a backup routine to join.

## 3. Dashboard reachable only through Tailscale
You said the hostname gets an A record pointing at the Tailscale IP `100.127.177.5`. That works, with these settings:
1. CapRover app `agent-dashboard`: enable HTTP, container port **8765**, custom domain = your hostname. **Do not enable HTTPS** (Let's Encrypt cannot validate an address the internet cannot reach). Traffic inside the tailnet is already WireGuard-encrypted; the browser only shows "not secure".
2. The app also gets CapRover's default `agent-dashboard.<root-domain>` name, and that wildcard likely points at the public IP. So add this under the app's Nginx config so only tailnet addresses get through:
   ```nginx
   allow 100.64.0.0/10;
   deny all;
   ```
   Anything coming via the public IP or Cloudflare tunnel then gets 403.
3. App env: `AGENT_ROLE=dashboard`, `DASHBOARD_PASSWORD=<you type it in CapRover; never in chat or git>`, `DASHBOARD_ALLOW_HOST=<your hostname>`. The app already refuses to start on a non-loopback bind without the password and rejects unknown Host headers.
4. Verify: tailnet device opens the hostname and gets a password prompt; from mobile data with Tailscale off, both the hostname and the default CapRover name fail or return 403.

## 4. Monitor and jobs
- `agent-monitor`: `AGENT_ROLE=monitor`, no HTTP, mount `/srv/agent-data:/app/data`. Memory cap about 256 MB.
- `agent-jobs` (optional): supercronic with `paper-run --deposit` on the first trading days of each month (guards make repeats safe).
  The quarterly data refresh (`fetch-fundamentals`, `screen`) stays manual via `docker exec`, because the screen needs your review before the next rebalance (due 2027-02-06).
- Only the server owns `paper_state.json`. Stop the laptop dashboard and monitor after cutover.

## 5. GitHub webhook auto-deploy (private repo)
1. In each app: Deployment, Method 3 (Deploy from GitHub). Enter the repo URL, branch `main`. CapRover shows an SSH public key: add it as a **read-only deploy key** in the GitHub repo settings.
2. CapRover shows the app's webhook URL. Add it in GitHub, Settings, Webhooks, content type JSON, event push only. Each app has its own URL, so a push triggers two or three builds, about 2-3 minutes each.
3. The webhook only works if GitHub can reach your CapRover captain endpoint. If the captain dashboard is tailnet-only, GitHub cannot call it; see question 3 below.
4. Branch protection on `main` is worth turning on: a push there rebuilds what runs on your shared host.

## 6. Verify after first deploy
1. Dashboard shows the seeded equity and deposits (about ₹4,973 equity, ₹5,000 deposited).
2. Monitor status file updates every 5 minutes closed / 60 s open.
3. Restart the host or the services; all return alone (`docker service ls`).
4. Push a trivial commit; webhook rebuilds and `/app/data` is untouched.
5. `docker stats` shows the apps within their caps; other apps unaffected.

## Risks
- Shared host: the dashboard password is the only barrier between tailnet devices and this data. Tailscale ACLs can narrow which devices reach port 80 on this node.
- Rebuilds on a host with 25 GB free: old images pile up. CapRover can prune unused images.
- Yahoo may rate-limit this IP. Test `fetch-fundamentals --limit 5` once.
- No exchange-holiday calendar in the monitor yet.

## What I need from you
1. Hostname for the dashboard, and confirmation the A record points at 100.127.177.5.
2. GitHub repo name (private) once you create it, and whether you want the optional `agent-jobs` app.
3. Is the CapRover captain endpoint (the webhook target) reachable from the public internet, or tailnet-only?
4. OK for me to run the host prep (sudo mkdir/chown under /srv) and copy `data/` over SSH.
5. Does the host already have a backup routine?
