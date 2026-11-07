# Running 24/7 and viewing the dashboard remotely

All of this is PAPER trading. The dashboard is read-only and the monitor never buys.

## What "24/7" can and cannot mean
- NSE trades Mon-Fri 09:15-15:30 IST. The monitor runs all day, checks holdings every 60 s while the
  market is open, and only writes a heartbeat (every 5 min) when it is closed.
- It does not buy or sell for profit all day. Backtests showed frequent trading loses to costs.
  The only automatic action is an optional paper stop-loss (`portfolio.stop_loss_pct`, default OFF).
- Exchange holidays are not known to the monitor; quotes simply stay unchanged.

## 1. Local (laptop)
Two terminals in `D:\Kashif\trading`:
```
.venv\Scripts\agent monitor          # add --alert-only to never sell
.venv\Scripts\agent dashboard        # http://127.0.0.1:8765
```
A laptop sleeps and drops WiFi. For all-day running: keep it plugged in and set sleep to "Never"
when plugged in (Windows Settings > System > Power). Or use a VPS (section 3).

Start automatically at logon (run once in PowerShell, adjust nothing else):
```
$py = "D:\Kashif\trading\.venv\Scripts\python.exe"
$a1 = New-ScheduledTaskAction -Execute $py -Argument "-m agent.cli monitor" -WorkingDirectory "D:\Kashif\trading"
$a2 = New-ScheduledTaskAction -Execute $py -Argument "-m agent.cli dashboard" -WorkingDirectory "D:\Kashif\trading"
$t  = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "AgentMonitor"   -Action $a1 -Trigger $t
Register-ScheduledTask -TaskName "AgentDashboard" -Action $a2 -Trigger $t
```

## 2. Remote view over a VPN (recommended: Tailscale)
Never port-forward the dashboard or put it on the public internet.
1. Install Tailscale on the laptop (or VPS) and on your phone; sign in to the same account.
2. Find the laptop's Tailscale IP (`tailscale ip -4`, looks like 100.x.y.z) and device name.
3. Set a strong password, then bind the dashboard to the Tailscale IP only:
```
$env:DASHBOARD_PASSWORD = "<long random password>"
.venv\Scripts\agent dashboard --host 100.x.y.z --allow-host <device-name>
```
4. On your phone (Tailscale on) open `http://100.x.y.z:8765`. Username is ignored, password is the one above.
The dashboard refuses to start on a non-loopback address without `DASHBOARD_PASSWORD`, rejects unknown
Host headers, and has no write endpoints. HTTP inside the Tailscale tunnel is encrypted by the VPN.
Treat the password like any other secret: do not commit it or paste it into chat.

## 3. VPS (the real 24/7 option)
Same code on a small Linux VPS (copy the folder, `pip install -e .`, copy `config.yaml` and `data/`).
Run monitor and dashboard under `systemd`, dashboard bound to 127.0.0.1 or the Tailscale IP, firewall closed
to everything except SSH. Needed before any live trading anyway (Groww API needs a static IP).
Use `agent fetch-fundamentals` and `agent screen` quarterly, and `agent paper-run --deposit` monthly.
