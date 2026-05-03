# Security policy

## Reporting a vulnerability

Please report suspected security vulnerabilities **privately** via
[GitHub Security Advisories][advisories].

[advisories]: https://github.com/CoreNovus/llm-gateway/security/advisories/new

Do **not** open a public GitHub issue for security reports.

We aim to acknowledge within 5 business days. Please include:

- A clear description of the vulnerability and its impact
- Reproduction steps or a minimal proof-of-concept
- Affected versions / commits / deployment shape (loopback vs. exposed)

If a public exploit is already in circulation, say so in the report —
we accelerate the disclosure window in that case.

## Disclosure policy

We follow a 90-day coordinated disclosure window:

1. Day 0 — report received, triage begins
2. Day 5 — acknowledgement + initial severity assessment shared with reporter
3. Day 90 — fix released, advisory published, reporter credited (if desired)

If the fix lands sooner, the advisory ships with the release. If
active exploitation is observed, the window is shortened by mutual
agreement with the reporter.

## Scope

**In scope:**

- Source code under `llm_gateway/`
- Container / systemd artefacts under `deploy/`
- Pinned dependencies in `pyproject.toml` / `poetry.lock`

**Out of scope** (please report to upstream):

- vLLM (`https://github.com/vllm-project/vllm`) — engine internals
- FastAPI / Starlette / httpx / pydantic / uvicorn — framework dependencies
- Operator-controlled configuration. Empty `BEARER_TOKEN` disables
  auth; this is documented behaviour for local dev and is gated by
  `__main__._require_bearer_token` on the production entry path.

## Known limitations (documented, not vulnerabilities)

The following are accepted trade-offs rather than bugs. Filing them
as vulnerabilities is welcome but the response will point back here:

- The `vllm_upstream_url` SSRF blocklist only checks IPv4 literals
  against a known-metadata-IP set. Hostnames that resolve to those
  IPs (DNS rebinding) are not caught at config-load time; the httpx
  transport must enforce this on its own if the threat model
  requires it.
- Chunked transfer-encoding requests are refused with 411 Length
  Required rather than length-counted at the ASGI layer. Honest JSON
  clients (httpx, openai-python, langchain-openai, curl) always set
  `Content-Length` so the practical impact is zero. Operators who
  must accept chunked uploads need a proxy in front that materialises
  `Content-Length`, or to extend `BodySizeLimitMiddleware` to a
  streaming ASGI implementation.
- The gateway binds to `127.0.0.1` by default and assumes an SSH
  tunnel as the network-level boundary. Lifting the bind to
  `0.0.0.0` without re-reading the threat model is an operator
  misconfiguration, not a gateway vulnerability.

## Hardening recommendations for operators

Beyond the in-process defences this package ships with, deploy-time
hardening lives in `deploy/`:

- Container hardening: `cap_drop: ALL`, `no-new-privileges`,
  `read_only` rootfs, non-root UID 1001.
- Systemd unit: same posture for non-container deploys.
- Operator-side `--limit-max-requests` / kernel-level limits cover
  HTTP-protocol-level abuse below this middleware's reach.
