# `llm-gateway` — OpenAI-compatible API gateway in front of vLLM

A thin FastAPI service that sits in front of `vllm/vllm-openai` (vendor Docker
container) on a self-hosted GPU host. Owns the cross-cutting gateway concerns
so the engine container stays as opaque vendor code:

- Bearer auth + per-key token-bucket rate-limit
- Multi-model registry + routing
- OpenAI-compat `POST /v1/chat/completions` + `GET /v1/models`
- Streaming SSE forwarding
- Tool-call parser registry per model family
- Circuit breaker + graceful shutdown against a dead upstream
- Prometheus `/metrics` (matched-route labels — bounded cardinality)
- `/health` (process) + `/ready` (vLLM upstream reachable + model loaded)

vLLM stays opaque — best-of-breed CUDA-level work, no rewrite. This package
is the seam where gateway cross-cutting concerns live.

## Topology

```
SSH tunnel: ssh -L 8000:127.0.0.1:8000 user@host
   ↓
[llm_gateway gateway,         127.0.0.1:8000]   ← this package
   ↓ httpx proxy
[vllm/vllm-openai container,  127.0.0.1:18000]  ← vendor Docker
   ↓
GPU + Qwen2.5-7B-Instruct-AWQ
```

Both bound to `127.0.0.1`. The SSH tunnel is the auth boundary; no public LLM
endpoint.

## Folder map

```
llm_gateway/
├── app.py             # FastAPI factory (create_app(settings, backend))
├── config.py          # pydantic-settings — env-driven config
├── api/               # HTTP endpoints (/health, /ready, /v1/*)
├── inference/         # InferenceBackend Protocol + NoopBackend stub
│                      # + VLLMHTTPBackend + CircuitBreakerBackend
├── middleware/        # auth / rate-limit / body-limit / logging / metrics
├── observability/     # Prometheus metrics + /metrics router
├── tool_calling/      # parser enum + pre-flight validator
└── models/            # ModelDefinition registry (no weights — metadata only)
```

## Local development

```bash
poetry install
poetry run pytest tests/unit/ -v        # unit tests
poetry run pyright llm_gateway/         # type check
poetry run ruff check llm_gateway/      # lint
poetry run black --check llm_gateway/   # format

# Run with NoopBackend (useful for smoking /health and /ready):
poetry run python -m llm_gateway
curl -s http://127.0.0.1:8000/health
curl -s -i http://127.0.0.1:8000/ready
```

See [`deploy/README.md`](deploy/README.md) for the production deploy.

## Configuration

Settings load from environment variables (or `.env` next to the gateway).
The most-relevant keys:

| Variable                     | Required   | Default                     | Purpose                                        |
|------------------------------|------------|-----------------------------|------------------------------------------------|
| `BEARER_TOKEN`               | yes (prod) | `""` (auth disabled in dev) | Bearer header clients send                     |
| `VLLM_UPSTREAM_URL`          | no         | `http://127.0.0.1:18000`    | vLLM container URL                             |
| `RATE_LIMIT_RPM`             | no         | `60`                        | Per-key requests/minute cap                    |
| `MAX_REQUEST_BODY_BYTES`     | no         | `1048576` (1 MiB)           | Reject larger bodies with 413                  |
| `CIRCUIT_BREAKER_ENABLED`    | no         | `true`                      | Open after 5 consecutive upstream failures     |
| `LOG_LEVEL`                  | no         | `INFO`                      | stdlib logging level                           |

Full set: [`llm_gateway/config.py`](llm_gateway/config.py).

## Security defaults

What the gateway protects against out of the box:

- Loopback bind (`127.0.0.1`); never set `0.0.0.0` in production.
- Bearer-token auth with constant-time comparison.
- Per-key token-bucket rate-limit with LRU + TTL eviction (bounded memory).
- Request body size cap (1 MiB default; 413 above).
- Cloud metadata-service IPs (IMDS, ECS task metadata, Alibaba) rejected
  at config load — gateway cannot be misconfigured into an SSRF relay.
- Container hardening: `cap_drop: ALL`, `no-new-privileges`, `read_only`
  rootfs, non-root UID 1001 (see `deploy/`).
- Access logger redacts `authorization` / `bearer` / `api_key` / `token` /
  `secret` / `password` `extra={…}` fields automatically.

## Ops scripts

PowerShell helpers for running the gateway on a single AWS EC2 GPU host
with idle-shutdown cost guardrails live in
[`scripts/ops/`](scripts/ops/) — `setup-ssh.ps1`, `fix-and-start.ps1`,
`restore-idle-protection.ps1`, `teardown-ssh.ps1`. Tag-based instance
discovery (`tag:application=vllm-serving + tag:environment=<env>`) means
zero hardcoded IDs. See [`scripts/ops/README.md`](scripts/ops/README.md)
for the operator workflow + IAM permissions list.

## License

MIT — see [`LICENSE`](LICENSE).
