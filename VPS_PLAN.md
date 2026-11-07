# VPS plan (paper trading, 24/7)

Nothing here is installed or run for you. Every command is for you to run on the VPS.

## 0. Is it worth it yet?
- Paid VPS in India is about Rs 400-600/month. Your monthly contribution is Rs 5,000. That is 8-12% of each deposit, every month, forever. It would erase any edge.
- The monitor only alerts. Paper mode needs nothing running between market hours.
- Worth it when: (a) you go live and Groww needs a static IP, or (b) portfolio is large enough that Rs 500/month is noise (roughly Rs 5 lakh+).
- Free option to try first: Oracle Cloud Always Free (Mumbai region, ARM VM). Free tier capacity is often unavailable and accounts can be reclaimed if idle, so keep a copy of `data/` on the laptop.
- Cheaper alternative: laptop plugged in, sleep off, Task Scheduler (REMOTE_ACCESS.md section 1).

## 1. Choose
- Provider: Oracle Cloud (OCI) Always Free. Cost: Rs 0.
- Region: Mumbai (India West). Home region cannot be changed later.
- Shape: VM.Standard.E2.1.Micro (1 vCPU, 1 GB) or Ampere A1 Flex if capacity exists. Ubuntu 24.04 LTS.
- Yahoo Finance sometimes rate-limits datacenter IPs. Test `agent fetch-fundamentals --limit 5` early. If blocked, run the data refresh on the laptop and copy `data/` over.

## 2. Harden (first 15 minutes)
1. Create a non-root user, e.g. `agent`, with sudo. Add your SSH public key.
2. `/etc/ssh/sshd_config`: `PermitRootLogin no`, `PasswordAuthentication no`. Restart ssh (keep a second session open while testing).
3. Install Tailscale first, join your account, and confirm you can SSH over the Tailscale IP from the laptop and phone:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   Tailscale needs no inbound port. It connects outbound (UDP 41641, or relay over 443).
4. Only after step 3 works from a second terminal, make Tailscale the ONLY way in. Everything (SSH, dashboard) is reachable only on `tailscale0`:
   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw allow in on tailscale0
   sudo ufw enable
   ```
   Then in the OCI console (Networking > VCN > Security List, and any Network Security Group) delete the ingress rule for TCP 22, leaving no public ingress at all. Ubuntu images on OCI also carry iptables rules; ufw sits on top, so verify with step 6.
5. `sudo apt install unattended-upgrades`. fail2ban is unnecessary once nothing public listens.
6. Verify from outside Tailscale (phone on mobile data, Tailscale off): `ssh agent@<public-ip>` times out, and port 8765 times out. With Tailscale on, both work.
7. Lockout recovery: if Tailscale breaks, there is no public SSH. Use the OCI console: Compute > Instance > Console connection (serial console), or temporarily re-add the TCP 22 ingress rule. Do not close port 22 until step 3 is proven, and keep `tailscaled` enabled at boot (`sudo systemctl enable tailscaled`).
8. Optional: disable key expiry for this machine in the Tailscale admin console, or the node drops off after about 180 days and locks you out.

## 3. Deploy the code
```bash
sudo apt install python3-venv python3-pip rsync
# from the laptop (PowerShell, adjust host):
#   scp -r D:\Kashif\trading agent@<vps-tailscale-name>:~/trading
cd ~/trading
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/agent monitor --once     # smoke test
```
Do not copy `.venv`, it is Windows-built. Copy `src`, `data`, `config.yaml`, `pyproject.toml`.

Single source of truth: pick ONE machine that owns `data/paper_state.json`. If the VPS runs `paper-run`, stop the laptop scheduled tasks, or the two states diverge.

## 4. Secrets
```bash
sudo install -m 600 -o agent -g agent /dev/null /etc/agent.env
echo 'DASHBOARD_PASSWORD=<long random password>' | sudo tee /etc/agent.env >/dev/null
```
Never commit it or paste it into chat.

## 5. systemd units
`/etc/systemd/system/agent-monitor.service`
```ini
[Unit]
Description=Paper monitor
After=network-online.target
Wants=network-online.target

[Service]
User=agent
WorkingDirectory=/home/agent/trading
ExecStart=/home/agent/trading/.venv/bin/agent monitor --alert-only
Restart=always
RestartSec=30
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/agent/trading/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
`/etc/systemd/system/agent-dashboard.service` (replace `100.x.y.z` and device name)
```ini
[Unit]
Description=Read-only dashboard
After=network-online.target tailscaled.service
Wants=tailscaled.service

[Service]
User=agent
WorkingDirectory=/home/agent/trading
EnvironmentFile=/etc/agent.env
ExecStart=/home/agent/trading/.venv/bin/agent dashboard --host 100.x.y.z --allow-host <device-name>
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ReadOnlyPaths=/home/agent/trading
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
Enable: `sudo systemctl daemon-reload && sudo systemctl enable --now agent-monitor agent-dashboard`.
Logs: `journalctl -u agent-monitor -f`.

Start `--alert-only` while paper trading. Drop it only if you set `portfolio.stop_loss_pct` on purpose.

## 6. Scheduled jobs (systemd timers or cron, VPS timezone set to Asia/Kolkata)
- Monthly, 1st trading week, after market close: `agent paper-run --deposit` (guards make repeats safe).
- Quarterly, before the rebalance date (next: 2027-02-06): `agent fetch-fundamentals`, then `agent screen`, then review, then `paper-run`.
- Daily: back up `data/paper_state.json` off the box (`rsync` to laptop over Tailscale).

## 7. Verify
1. Phone on Tailscale opens `http://100.x.y.z:8765`, asks for password, shows equity.
2. From outside Tailscale, the port is closed: `nmap -p 8765 <public-ip>` shows filtered.
3. Reboot VPS; both services return on their own.
4. Dashboard "monitor alive" stays green across a market day.
5. Kill the monitor process; systemd restarts it.

## 8. Before live trading (later, separate work)
- Groww API terms: static IP whitelist (the VPS public IP), rate limits, daily token refresh.
- Implement `GrowwBroker` forced to delivery (CNC) only. Start with a tiny amount.
- Wire the daily-loss kill switch into the live path.
- Add an NSE holiday calendar to the monitor.
- Scholar review of holdings.

## Gaps this plan does not close
- Monitor knows no exchange holidays.
- Single VPS, no failover. A dead monitor on a paper account costs nothing; on a live account it would matter.
