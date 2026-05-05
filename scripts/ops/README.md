# Operator scripts (AWS EC2, PowerShell)

Day-to-day workflow for an `llm-gateway` instance running on a single EC2
GPU host with idle-shutdown cost guardrails. Designed to incur near-zero
fixed cost — the box only runs while you're actively developing.

```
setup-ssh.ps1            (one-time per laptop)
   ↓
fix-and-start.ps1   →   ssh -L 8000:...   →   develop                           →   restore-idle-protection.ps1 -StopNow
                                                  ↑
                                          set-model.ps1 (swap loaded model — bench / experiment)
```

## Prerequisites

- AWS CLI v2 on Windows at `C:\Program Files\Amazon\AWSCLIV2\aws.exe`
- An `llm-gateway` EC2 instance deployed via the CDK stack pattern, tagged:
  - `application=vllm-serving`
  - `environment=<dev|prod|...>`
- A CloudWatch alarm whose name contains `VLLMIdleBackstop` wired as the
  idle backstop (the script disables/re-enables its actions)
- `/etc/cron.d/llm-gateway-idle-shutdown` on the instance (idle cron)
- Bearer token in Secrets Manager (the bootstrap helper reads it; not
  used directly by these scripts)

## Discovery

All four scripts default to `-Environment dev` and discover the instance
+ EIP via tags. To run against a different env or pin explicit values:

```powershell
.\fix-and-start.ps1 -Environment prod
.\fix-and-start.ps1 -InstanceId i-1234 -Eip 1.2.3.4 -Region us-east-1
```

## Usage

```powershell
# 1. One-time per laptop (or after teardown): generate key, open SG :22, push key
.\setup-ssh.ps1

# 2. Daily: start instance, fix systemd unit, start service, smoke-test
.\fix-and-start.ps1

# 3. Open SSH tunnel in a separate PowerShell window
ssh -i $env:USERPROFILE\.ssh\id_ed25519_vllm -L 8000:127.0.0.1:8000 -N ubuntu@<EIP>

# 4. Use the gateway at http://127.0.0.1:8000/v1 (any OpenAI-compatible client)

# 4b. (Bench / experiment) Swap the loaded model atomically:
.\set-model.ps1 `
    -Model               'Qwen/Qwen2.5-7B-Instruct-AWQ' `
    -ServedModelName     'selfhost-qwen' `
    -Quantization        'awq' `
    -ToolCallParser      'hermes' `
    -GpuMemoryUtilization 0.9 `
    -MaxModelLen          8192

# 5. Done for the day - restore idle protection + stop instance
.\restore-idle-protection.ps1 -StopNow

# 6. Done for a long while - revoke the SG :22 inbound rule
.\teardown-ssh.ps1
```

## set-model.ps1 — atomic model swap

Drop a different model into the running vLLM container without a
redeploy. Useful for trying a new model size or family, sweeping vLLM
flags (`--gpu-memory-utilization`, `--max-model-len`,
`--tool-call-parser`) for an ablation, or driving an external benchmark
that wants to iterate models on a single host.

The script is non-interactive and returns deterministic exit codes, so
it composes cleanly with any orchestrator that can shell out to
PowerShell (manual operator session, CI job, benchmark runner, …).

### Threat model

1. **Argument injection** — every parameter is whitelist-validated client-side
   (`ValidatePattern` / `ValidateSet` / `ValidateRange`). Validation re-runs on
   the remote host as defence in depth. Values transit as a single JSON blob
   via fd 3 to the remote `bash -s`; the remote script reads them with `jq`,
   so they never enter the shell-tokenisation surface.
2. **Compose-file corruption** — the edit is atomic: `awk` rewrites a
   copy, `docker compose config -q` validates the result, then `mv` swaps
   it in. Originals are kept under `/opt/llm-gateway/deploy/.swap-history/`
   for forensic diff / manual rollback.
3. **No source-code mutation** — the script does **not** edit
   `llm_gateway/models/registry.py`. Instead, it pre-flight verifies the
   target `served_model_name` exists in the deployed registry (read-only
   `grep -F`); unknown names exit code 1 with a pointer to open a
   registry PR. Mutating Python source over SSH would defeat code review.
4. **Concurrent operators** — `flock -n /var/lock/llm-gateway-set-model.lock`
   ensures two parallel swaps cannot interleave compose edits.
5. **Bearer-token leakage** — the script never reads `.env`, never logs
   secrets, and runs no traffic against the bearer-gated `/v1` surface.

### Exit codes (contract-pinned)

| Code | Meaning |
|------|---------|
| 0 | compose edited (or already matched) + container healthy + `/ready` 200 |
| 1 | compose edit failed, registry pre-flight failed, or remote validation failed |
| 2 | `docker compose up` failed |
| 3 | container not healthy or `/ready` not 200 within budget |
| 4 | SSH connection / key / discovery failed |

### Idempotency

Re-running with the currently-loaded model is a no-op: the script reads
the existing compose, finds every requested flag already in place,
verifies `/ready` is 200, and exits 0 without touching anything.

### Pre-pull mode

`-PrePull` runs the vLLM image headless and downloads weights to
`/models/hf-cache` without restarting the live service. Used by
operators warming the cache before a benchmark run starts so HF-pull
latency does not corrupt cold-start measurements.

```powershell
.\set-model.ps1 -PrePull `
    -Model 'Qwen/Qwen2.5-14B-Instruct-AWQ' -ServedModelName 'selfhost-qwen-14b' `
    -Quantization 'awq' -ToolCallParser 'hermes' `
    -GpuMemoryUtilization 0.9 -MaxModelLen 8192
```

### Prerequisite: register the served_name first

`set-model.ps1` deliberately **refuses** to swap to a `served_model_name`
that is not in `llm_gateway/models/registry.py` on the deployed
instance. To add a new registry entry, open a separate llm-gateway PR
that appends a `ModelDefinition` literal to `DEFAULT_REGISTRY`. Once
that PR is merged and re-deployed, `set-model.ps1` will accept the new
name.

This separation keeps registry mutations under code review (covered by
the gateway's existing tests + slice-E pre-flight) rather than ad-hoc
SSH edits.

## Notes

- `fix-and-start.ps1` includes a `sed` patch that rewrites
  `docker compose --no-color` → `docker compose --ansi never` in the
  systemd unit for compatibility with Compose v2.x. Safe no-op if your
  unit is already correct.
- `setup-ssh.ps1` opens port 22 only to **your current public IP /32**
  (resolved via `checkip.amazonaws.com` and IPv4-validated). The rule
  persists until you run `teardown-ssh.ps1`.
- The SSH key pair (`~/.ssh/id_ed25519_vllm[.pub]`) is generated once and
  reused across runs. EC2 Instance Connect pushes the public key into
  `authorized_keys` on first connect; subsequent connects are direct SSH.
- All four scripts are idempotent — safe to re-run.

## Required IAM permissions (operator)

```
ec2:DescribeInstances
ec2:DescribeSecurityGroups
ec2:DescribeSecurityGroupRules
ec2:AuthorizeSecurityGroupIngress
ec2:RevokeSecurityGroupIngress
ec2:StartInstances
ec2:StopInstances
ec2-instance-connect:SendSSHPublicKey
cloudwatch:DescribeAlarms
cloudwatch:DisableAlarmActions
cloudwatch:EnableAlarmActions
```
