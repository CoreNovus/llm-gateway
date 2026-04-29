# Deploy artefacts

Production deploy of `llm-gateway` on any GPU host (validated on an AWS
g6.2xlarge with an NVIDIA L4). Pure config — the operator clones the
repo, populates `.env`, runs `install.sh`, then `systemctl start
llm-gateway`.

## Topology

```
SSH tunnel (-L 8000:127.0.0.1:8000)
   ↓
[host 127.0.0.1:8000]                           ← only entry point
   ↓
[llm-gateway gateway container, port 8000]         ← this repo's FastAPI
   ↓ http://vllm:8000   (docker bridge, internal only)
[vllm container, port 8000, NO host publish]    ← vendor image
   ↓
GPU + Qwen2.5-7B-Instruct-AWQ
```

No public LLM endpoint anywhere. SG opens 22 from operator IP only;
SSH is the auth boundary. The gateway's bearer-token check is
defense-in-depth.

## One-time setup (per box)

```bash
# 1. Clone into /opt
sudo git clone https://github.com/CoreNovus/llm-gateway /opt/llm-gateway

# 2. Populate the bearer token + tuning
cd /opt/llm-gateway/deploy
sudo cp .env.example .env
sudo "$EDITOR" .env       # set BEARER_TOKEN=$(openssl rand -hex 32)

# 3. Install systemd unit + idle-shutdown cron
sudo ./scripts/install.sh

# 4. Start the stack
sudo systemctl start llm-gateway
```

vLLM cold-loads Qwen 7B AWQ in ~60-120s. Check progress:

```bash
journalctl -u llm-gateway -f          # docker compose logs
docker compose -f /opt/llm-gateway/deploy/docker-compose.yml ps
curl -fsS http://127.0.0.1:8000/ready    # 200 once vLLM is healthy + gateway up
```

## From your laptop

```bash
# Open the tunnel (replace user/host with your own;
# AWS example uses `ec2-user@<eip>`).
ssh -L 8000:127.0.0.1:8000 user@host

# Smoke
TOKEN=$(grep BEARER_TOKEN /opt/llm-gateway/deploy/.env | cut -d= -f2)
curl -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -X POST http://127.0.0.1:8000/v1/chat/completions \
     -d '{"model":"selfhost-qwen","messages":[{"role":"user","content":"hi"}],"max_tokens":32}'

# Scrape metrics (no token required — SSH tunnel is the boundary)
curl -s http://127.0.0.1:8000/metrics | head -30
```

## Cost guardrails (already wired)

* **Idle shutdown cron** — `/etc/cron.d/llm-gateway-idle-shutdown`, every
  10 minutes; 60 minutes of < 5% GPU utilisation triggers
  `systemctl poweroff`. Override thresholds in `idle-shutdown.sh`
  via `LLM_GATEWAY_IDLE_SHUTDOWN_SAMPLES` / `LLM_GATEWAY_IDLE_UTIL_THRESHOLD`.
* **Circuit breaker** — open after 5 consecutive upstream failures,
  fail-fast for 30s, then probe. Saves quota and engine churn during
  vLLM crashes.
* **Rate limit** — 60 rpm per bearer token (configurable via
  `RATE_LIMIT_RPM`).

## Rolling a new bearer token

```bash
NEW=$(openssl rand -hex 32)
sudo sed -i "s|^BEARER_TOKEN=.*|BEARER_TOKEN=$NEW|" /opt/llm-gateway/deploy/.env
sudo systemctl restart llm-gateway      # zero-downtime not required; SSH client retries
```

## Switching to a different model

1. Edit `command:` block in `docker-compose.yml`:
   * change `--model=...` and `--served-model-name=...`
   * change `--tool-call-parser=...` to match the family
2. Add the same `served_name` to `llm_gateway/models/registry.py` so
   `/v1/models` exposes it and the tool-call pre-flight validator
   accepts it.
3. `sudo systemctl restart llm-gateway`.

The first start downloads the new weights to `/models/hf-cache` on
the host's persistent data volume. Subsequent starts load from disk.

## Stop / start (manual, no shutdown)

```bash
sudo systemctl stop  llm-gateway    # drain + stop containers, keep instance up
sudo systemctl start llm-gateway
```

To stop the instance entirely (so AWS charges nothing for compute):

```bash
sudo /usr/bin/systemctl poweroff   # or wait for the idle cron
```

The data volume preserves model weights across stop/start. Bring the
instance back up with whatever start script your host operator uses;
the systemd unit auto-starts the stack on next boot.
