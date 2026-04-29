#!/usr/bin/env bash
# One-shot installer — run as root on the EC2 after `git clone` into
# /opt/llm-gateway. Wires the systemd unit and the
# idle-shutdown cron; idempotent.

set -euo pipefail

REPO_ROOT="${LLM_GATEWAY_REPO_ROOT:-/opt/llm-gateway}"
DEPLOY_DIR="$REPO_ROOT/deploy"

if [ "$(id -u)" -ne 0 ]; then
    echo "Must run as root (systemd + cron writes)." >&2
    exit 1
fi

if [ ! -f "$DEPLOY_DIR/docker-compose.yml" ]; then
    echo "ERROR: expected $DEPLOY_DIR/docker-compose.yml — clone the repo first" >&2
    exit 1
fi

if [ ! -f "$DEPLOY_DIR/.env" ]; then
    echo "ERROR: $DEPLOY_DIR/.env missing." >&2
    echo "       cp $DEPLOY_DIR/.env.example $DEPLOY_DIR/.env" >&2
    echo "       and populate BEARER_TOKEN (openssl rand -hex 32)." >&2
    exit 1
fi

# 1. systemd unit
install -m 644 "$DEPLOY_DIR/systemd/llm-gateway.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable llm-gateway.service

# 2. Idle-shutdown cron — primary cost-control guardrail.
install -m 644 "$DEPLOY_DIR/scripts/cron/llm-gateway-idle-shutdown" /etc/cron.d/

# 3. State directory the idle script writes to.
# 0700 — the dir holds /var/lib/llm-gateway/gpu_idle_count, written
# and read only by root via the cron. World-read serves no purpose.
install -d -m 0700 /var/lib/llm-gateway

cat <<'NEXT'

Installed.

Start:    systemctl start llm-gateway
Status:   systemctl status llm-gateway
Logs:     journalctl -u llm-gateway -f
Smoke:    curl http://127.0.0.1:8000/health  (from this host or via SSH tunnel)

Idle shutdown: /etc/cron.d/llm-gateway-idle-shutdown — 10-minute cadence,
60 minutes of < 5% GPU utilisation triggers `systemctl poweroff`.
NEXT
