#!/bin/sh
# One image, several CapRover apps. AGENT_ROLE picks the process.
set -eu

case "${AGENT_ROLE:-}" in
  monitor)
    exec agent monitor --alert-only
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
    # Daily 16:00 IST, Mon-Fri. paper-run has its own guards (one deposit per
    # month, rebalance only when due), so repeated runs are safe.
    while true; do
      now=$(date +%s)
      target=$(date -d "today 16:00" +%s)
      [ "$now" -ge "$target" ] && target=$(date -d "tomorrow 16:00" +%s)
      sleep $((target - now))
      if [ "$(date +%u)" -le 5 ]; then
        agent paper-run --deposit || echo "paper-run failed: $?"
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
