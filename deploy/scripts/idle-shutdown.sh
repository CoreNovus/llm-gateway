#!/usr/bin/env bash
# Power off the host if the GPU has been < 5% utilised for 60 minutes.
# Primary cost-control defence on a pay-as-you-use cloud box. Pair
# with the cron file in deploy/scripts/cron/ — 10-minute cadence, 6
# consecutive idle samples (≈ 60 min) trigger shutdown.

set -euo pipefail

STATE_DIR="/var/lib/llm-gateway"
IDLE_FILE="$STATE_DIR/gpu_idle_count"
THRESHOLD="${LLM_GATEWAY_IDLE_SHUTDOWN_SAMPLES:-6}"   # 6 × 10min = 60min
UTIL_THRESHOLD="${LLM_GATEWAY_IDLE_UTIL_THRESHOLD:-5}"

mkdir -p "$STATE_DIR"

# nvidia-smi must be on PATH; if it isn't, the box is misconfigured —
# fail loud rather than silently never shutting down.
util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits \
       | head -n1 \
       | tr -d '[:space:]')

if [ "$util" -lt "$UTIL_THRESHOLD" ]; then
    count=$(cat "$IDLE_FILE" 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > "$IDLE_FILE"

    if [ "$count" -ge "$THRESHOLD" ]; then
        minutes=$((count * 10))
        logger -t llm-gateway-idle "GPU idle ${minutes}min (≥ threshold) — powering off"
        # systemctl poweroff is more portable than /sbin/shutdown across
        # Ubuntu / Debian / Amazon Linux — both call into the same
        # systemd target.
        /usr/bin/systemctl poweroff
    fi
else
    echo 0 > "$IDLE_FILE"
fi
