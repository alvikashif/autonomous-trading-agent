#!/bin/sh
# One image, several CapRover apps. AGENT_ROLE picks the process.
set -eu

case "${AGENT_ROLE:-}" in
  monitor)
    if [ "${AI_AUTO_TRADE:-false}" = "true" ]; then
      exec agent monitor --live
    else
      exec agent monitor --alert-only
    fi
    ;;
  dashboard)
    # serve() refuses a non-loopback bind unless DASHBOARD_PASSWORD is set.
    set -- agent dashboard --host 0.0.0.0 --port 8765
    if [ -n "${DASHBOARD_ALLOW_HOST:-}" ]; then
      set -- "$@" --allow-host "$DASHBOARD_ALLOW_HOST"
    fi
    exec "$@"
    ;;
  jobs)
    mkdir -p /app/data
    if [ "${AI_AUTO_TRADE:-false}" = "true" ]; then
      echo '{"ai_auto_trade": true, "broker_mode": "live"}' > /app/data/active_mode.json
    else
      echo '{"ai_auto_trade": false, "broker_mode": "paper"}' > /app/data/active_mode.json
    fi
    # Daily 14:45 IST, Mon-Fri (Power Hour: Live market execution, avoiding overnight AMO risk).
    # paper-run has its own guards (one deposit per month, rebalance only when due), so repeated runs are safe.
    while true; do
      now=$(date +%s)
      target=$(date -d "today 14:45" +%s)
      [ "$now" -ge "$target" ] && target=$(date -d "tomorrow 14:45" +%s)
      diff=$((target - now))
      echo "[jobs] Next automated scan & trade cycle scheduled in $((diff / 60)) minutes (at 14:45 IST, Power Hour live market)"
      sleep $diff
      if [ "$(date +%u)" -le 5 ]; then
        if [ "${AI_AUTO_TRADE:-false}" != "true" ]; then
          agent paper-run --deposit || echo "paper-run failed: $?"
        fi
        agent swing-run || echo "swing-run failed: $?"
        if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${DEEPSEEK_API_KEY:-}" ] || [ -n "${OPENAI_API_KEY:-}" ] || [ -n "${GEMINI_API_KEY:-}" ]; then
          agent ai-scan || echo "ai-scan failed: $?"
          if [ "${AI_AUTO_TRADE:-false}" = "true" ]; then
            agent ai-trade --live --yes || echo "ai-trade failed: $?"
          fi
        fi
      fi
    done
    ;;
  *)
    echo "AGENT_ROLE must be monitor, dashboard or jobs (got '${AGENT_ROLE:-}')" >&2
    exit 64
    ;;
esac
