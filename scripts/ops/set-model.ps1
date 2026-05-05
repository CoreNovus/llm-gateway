# Atomic vLLM model swap on a running llm-gateway EC2 host.
#
# Pairs with the rest of the day-to-day ops loop:
#
#     setup-ssh.ps1            (one-time per laptop)
#     fix-and-start.ps1   →    set-model.ps1   →   develop / experiment / bench
#                                                     │
#     restore-idle-protection.ps1 -StopNow  ←─────────┘
#
# Use cases:
#   - Switch to a different model size / family without redeploying.
#   - Run an ablation across vLLM flags (``--gpu-memory-utilization``,
#     ``--max-model-len``, ``--tool-call-parser``) and roll back via the
#     auto-archived backup.
#   - Pre-pull weights into the host HF cache before a measurement run
#     (``-PrePull``) so model-load latency does not pollute timings.
#   - Drive an automated benchmark from any external orchestrator that
#     can shell out to PowerShell — the script is non-interactive and
#     returns deterministic exit codes.
#
# Threat model (in priority order):
#   T1. Argument injection — every string parameter is validated against
#       a strict whitelist (see ``param`` block) BEFORE it crosses the SSH
#       boundary. Even if validation were defeated, all values are
#       interpolated through ``ConvertTo-Json`` for transport, then
#       single-quoted in the remote bash heredoc, so shell-metacharacter
#       payloads cannot escape.
#   T2. Compose file corruption — edits are atomic (write to .new, run
#       ``docker compose config -q``, mv on success; restore .bak on any
#       failure). The originals are kept under
#       ``/opt/llm-gateway/deploy/.swap-history/`` so an operator can
#       diff or roll back manually.
#   T3. Source-code mutation creep — this script DOES NOT edit
#       ``llm_gateway/models/registry.py``. Before any swap it runs a
#       READ-ONLY pre-flight that searches the deployed registry for
#       the requested ``served_model_name`` (``grep -F``). If the name
#       is not registered, exit 1 with a pointer to open a registry PR.
#       Mutating Python source over SSH would defeat the gateway's
#       normal code-review path.
#   T4. Concurrent operators — a per-host advisory lock
#       (``flock -n /var/lock/llm-gateway-set-model.lock``) prevents two
#       swaps overlapping. Without it the second swap could read a
#       half-edited compose file.
#   T5. Bearer-token leakage — the script never reads .env, never logs
#       it, and never echoes it; the gateway already gates /v1 routes
#       with bearer auth and we don't touch that surface.
#
# Idempotency: re-running with the currently-loaded model is a no-op
# (skip compose edit, skip container recreate, just verify ``/ready``
# is 200). Operators and external orchestrators can therefore call this
# script without first checking the current state.
#
# Usage:
#   pwsh -NoProfile -File set-model.ps1 `
#       -Model               'Qwen/Qwen2.5-7B-Instruct-AWQ' `
#       -ServedModelName     'selfhost-qwen' `
#       -Quantization        'awq' `
#       -ToolCallParser      'hermes' `
#       -GpuMemoryUtilization 0.9 `
#       -MaxModelLen          8192
#
#   # Pre-pull weights without swapping (Phase 6.b warm-up):
#   pwsh -NoProfile -File set-model.ps1 -PrePull -Model '...' -ServedModelName '...' \
#       -Quantization 'awq' -ToolCallParser 'hermes' -GpuMemoryUtilization 0.9 -MaxModelLen 8192

[CmdletBinding()]
param(
    # Hugging Face repo path. Two non-empty path segments separated by
    # one slash, optional ``:tag``. Leading/trailing slashes and ``..``
    # are rejected by the regex.
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}/[A-Za-z0-9][A-Za-z0-9._\-]{0,63}(:[A-Za-z0-9._\-]{1,32})?$')]
    [string]$Model,

    # Gateway-side stable id. Must match a key registered in
    # ``llm_gateway/models/registry.py`` — verified before any edit.
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z][a-z0-9-]{1,63}$')]
    [string]$ServedModelName,

    [Parameter(Mandatory)]
    [ValidateSet('awq', 'gptq', 'none')]
    [string]$Quantization,

    [Parameter(Mandatory)]
    [ValidateSet('hermes', 'llama3_json', 'mistral', 'none')]
    [string]$ToolCallParser,

    [Parameter(Mandatory)]
    [ValidateRange(0.1, 0.95)]
    [double]$GpuMemoryUtilization,

    [Parameter(Mandatory)]
    [ValidateRange(512, 131072)]
    [int]$MaxModelLen,

    # Pre-pull mode: download weights to /models/hf-cache without
    # touching the running service. Idempotent — second call hits the
    # cache. Used by Phase 6.b warm-up so HF-pull latency falls outside
    # the bench measurement window.
    [switch]$PrePull,

    # Discovery / connection — same shape as the other ops scripts.
    [string]$Environment = 'dev',
    [string]$InstanceId,
    [string]$Eip,
    [ValidatePattern('^[a-z]{2}-[a-z]+-\d$')]
    [string]$Region = 'ap-northeast-1',
    [ValidatePattern('^[a-z][a-z0-9_-]{0,31}$')]
    [string]$Ec2User = 'ubuntu',
    [string]$KeyPath = (Join-Path $HOME ".ssh\id_ed25519_vllm"),

    # Compose-file path on the remote host. Locked to /opt/llm-gateway/
    # by default; overridable for tests against a non-standard layout.
    [ValidatePattern('^/[A-Za-z0-9._/\-]{1,128}\.yml$')]
    [string]$ComposePath = '/opt/llm-gateway/deploy/docker-compose.yml',

    # Registry source path on remote — only READ to verify the
    # served_name. Never edited. Locked by ValidatePattern.
    [ValidatePattern('^/[A-Za-z0-9._/\-]{1,160}\.py$')]
    [string]$RegistryPath = '/opt/llm-gateway/llm_gateway/models/registry.py',

    # /ready poll budget. Hard-capped to 600s — the contract reserves
    # 5 min, this leaves a 100 s margin for SSH and edit overhead.
    [ValidateRange(60, 600)]
    [int]$ReadyTimeoutSeconds = 300
)

# Exit codes — stable contract for external automation (see README §
# "Exit codes"). Adding a new code is a breaking change for any caller
# that branches on values; prefer reusing one of the existing five.
Set-Variable -Option Constant -Name EXIT_OK             -Value 0
Set-Variable -Option Constant -Name EXIT_COMPOSE_FAILED -Value 1
Set-Variable -Option Constant -Name EXIT_RESTART_FAILED -Value 2
Set-Variable -Option Constant -Name EXIT_NOT_HEALTHY    -Value 3
Set-Variable -Option Constant -Name EXIT_SSH_FAILED     -Value 4

$ErrorActionPreference = 'Stop'
$AWS = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"

function Write-Stage {
    param([string]$Message)
    # All operator-facing logs go to stderr — keeps stdout clean for
    # any caller that wants to capture machine-readable output (none
    # produced today, but reserves the channel).
    [Console]::Error.WriteLine("[set-model] $Message")
}

function Fail {
    param([int]$Code, [string]$Message)
    [Console]::Error.WriteLine("[set-model] FAIL ($Code): $Message")
    exit $Code
}

# ---------------------------------------------------------------------
# 0. Discover instance + EIP via tags (same pattern as setup-ssh.ps1)
# ---------------------------------------------------------------------
if (-not $InstanceId) {
    Write-Stage "Discovering instance via tag:application=vllm-serving + tag:environment=$Environment"
    $InstanceId = (& $AWS ec2 describe-instances `
        --filters "Name=tag:application,Values=vllm-serving" "Name=tag:environment,Values=$Environment" `
                  "Name=instance-state-name,Values=running" `
        --region $Region `
        --query 'Reservations[0].Instances[0].InstanceId' --output text 2>$null).Trim()
    if ([string]::IsNullOrWhiteSpace($InstanceId) -or $InstanceId -eq 'None') {
        Fail $EXIT_SSH_FAILED "No RUNNING vllm-serving instance for environment=$Environment in $Region. Start it first via fix-and-start.ps1, or pass -InstanceId."
    }
    if ($InstanceId -notmatch '^i-[0-9a-f]{8,17}$') {
        Fail $EXIT_SSH_FAILED "Discovered instance id has unexpected shape: '$InstanceId'"
    }
}
if (-not $Eip) {
    $Eip = (& $AWS ec2 describe-instances --instance-ids $InstanceId --region $Region `
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>$null).Trim()
    if ([string]::IsNullOrWhiteSpace($Eip) -or $Eip -eq 'None') {
        Fail $EXIT_SSH_FAILED "Instance $InstanceId has no public IP. Pass -Eip explicitly."
    }
}
if ($Eip -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
    # Same defensive check as setup-ssh.ps1 — refuse to author SSH args
    # against a non-IPv4 endpoint (DNS injection / typosquat surface).
    Fail $EXIT_SSH_FAILED "Public IP has non-IPv4 shape: '$Eip' — refusing to SSH"
}
Write-Stage "Target: $InstanceId @ $Eip ($Region)"

# ---------------------------------------------------------------------
# 1. Build SSH args (hardened — same flags as the existing scripts)
# ---------------------------------------------------------------------
if (-not (Test-Path $KeyPath)) {
    Fail $EXIT_SSH_FAILED "SSH key not found at $KeyPath. Run setup-ssh.ps1 first."
}
$sshArgs = @(
    '-i', $KeyPath,
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'IdentitiesOnly=yes',
    '-o', 'PasswordAuthentication=no',
    '-o', 'BatchMode=yes',          # never prompt for password — fail loud instead
    '-o', 'ConnectTimeout=10',
    "$Ec2User@$Eip"
)

# ---------------------------------------------------------------------
# 2. Marshal validated parameters into a single JSON blob.
#
# Every string lands in the remote shell exactly once, single-quoted,
# inside a heredoc. Validation regexes already exclude shell
# metacharacters; JSON transport adds defence in depth (no raw
# interpolation into the heredoc body — the remote awk reads the JSON
# file).
# ---------------------------------------------------------------------
$payload = [ordered]@{
    model                  = $Model
    served_model_name      = $ServedModelName
    quantization           = $Quantization
    tool_call_parser       = $ToolCallParser
    gpu_memory_utilization = $GpuMemoryUtilization
    max_model_len          = $MaxModelLen
    pre_pull               = [bool]$PrePull
    compose_path           = $ComposePath
    registry_path          = $RegistryPath
    ready_timeout_seconds  = $ReadyTimeoutSeconds
} | ConvertTo-Json -Compress

# ---------------------------------------------------------------------
# 3. Remote orchestrator. Single-quoted heredoc — NO local
# interpolation. The bash script reads the JSON via stdin (fd 3) and
# uses ``jq`` to extract validated fields.
# ---------------------------------------------------------------------
$remoteScript = @'
#!/bin/bash
set -euo pipefail

# Read JSON payload from fd 3.
PAYLOAD="$(cat <&3)"

# jq is part of the bootstrap; bail loud if it is somehow missing.
command -v jq >/dev/null || { echo "[set-model] jq missing on remote host" >&2; exit 1; }
command -v docker >/dev/null || { echo "[set-model] docker missing on remote host" >&2; exit 1; }
command -v flock >/dev/null || { echo "[set-model] flock missing on remote host" >&2; exit 1; }

j() { printf '%s' "$PAYLOAD" | jq -r "$1"; }

MODEL="$(j '.model')"
SERVED="$(j '.served_model_name')"
QUANT="$(j '.quantization')"
PARSER="$(j '.tool_call_parser')"
GMU="$(j '.gpu_memory_utilization')"
MAXLEN="$(j '.max_model_len')"
PREPULL="$(j '.pre_pull')"
COMPOSE="$(j '.compose_path')"
REGISTRY="$(j '.registry_path')"
READY_TIMEOUT="$(j '.ready_timeout_seconds')"

# Re-validate on the remote side — defence in depth against a
# compromised laptop sending malformed JSON to the host.
[[ "$MODEL"   =~ ^[A-Za-z0-9][A-Za-z0-9._/-]+(:[A-Za-z0-9._-]+)?$ ]] || { echo "[set-model] bad model: $MODEL" >&2; exit 1; }
[[ "$SERVED"  =~ ^[a-z][a-z0-9-]+$                                ]] || { echo "[set-model] bad served_name: $SERVED" >&2; exit 1; }
[[ "$QUANT"   =~ ^(awq|gptq|none)$                                ]] || { echo "[set-model] bad quantization" >&2; exit 1; }
[[ "$PARSER"  =~ ^(hermes|llama3_json|mistral|none)$              ]] || { echo "[set-model] bad parser" >&2; exit 1; }
[[ "$GMU"     =~ ^0\.[1-9][0-9]?$                                 ]] || { echo "[set-model] bad gpu_memory_utilization" >&2; exit 1; }
[[ "$MAXLEN"  =~ ^[1-9][0-9]{2,5}$                                ]] || { echo "[set-model] bad max_model_len" >&2; exit 1; }

[[ -f "$COMPOSE" ]]  || { echo "[set-model] compose file missing: $COMPOSE" >&2; exit 1; }
[[ -f "$REGISTRY" ]] || { echo "[set-model] registry file missing: $REGISTRY" >&2; exit 1; }

# T4 — advisory lock so two operators do not corrupt the compose file.
exec 9>/var/lock/llm-gateway-set-model.lock
if ! flock -n 9; then
  echo "[set-model] another swap is in progress (lock held)" >&2
  exit 1
fi

# T3 — verify, do not mutate. The registry pre-flight checks the literal
# served_name. ``grep -F`` is fixed-string so no regex injection from
# $SERVED, and ``-q`` keeps the operator log clean.
if ! grep -F -q "served_name=\"$SERVED\"" "$REGISTRY"; then
  echo "[set-model] served_name '$SERVED' is NOT registered in $REGISTRY." >&2
  echo "[set-model] Open a separate llm-gateway PR adding the ModelDefinition before swapping." >&2
  exit 1
fi
echo "[set-model] STEP 1/5: served_name '$SERVED' present in registry" >&2

# ──────────────────────────────────────────────────────────────────────
# Pre-pull mode short-circuit
# ──────────────────────────────────────────────────────────────────────
if [[ "$PREPULL" == "true" ]]; then
  echo "[set-model] PrePull mode — fetching weights, no swap" >&2
  CACHE_DIR="${LLM_GATEWAY_HF_CACHE_DIR:-/models/hf-cache}"
  mkdir -p "$CACHE_DIR"
  # Run the vLLM image with the same model arg + an immediate exit so
  # the HF cache is populated. Avoids requiring host-side pip / hf-cli.
  if ! sudo docker run --rm \
        -v "$CACHE_DIR:/root/.cache/huggingface" \
        --entrypoint python3 \
        vllm/vllm-openai:latest \
        -c "from huggingface_hub import snapshot_download as d; d('$MODEL')" >&2; then
    echo "[set-model] pre-pull failed" >&2
    exit 1
  fi
  echo "[set-model] PrePull OK for $MODEL" >&2
  exit 0
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 2/5: idempotency — skip everything if the compose already
# matches the requested state.
# ──────────────────────────────────────────────────────────────────────
already_matching() {
  # All comparisons are line-anchored substring matches. We don't parse
  # YAML — we look for the exact ``- --flag=<value>`` lines in the vllm
  # service block.
  local needle
  needle="    - --model=$MODEL"
  grep -F -q "$needle" "$COMPOSE" || return 1
  needle="    - --served-model-name=$SERVED"
  grep -F -q "$needle" "$COMPOSE" || return 1
  needle="    - --max-model-len=$MAXLEN"
  grep -F -q "$needle" "$COMPOSE" || return 1
  needle="    - --gpu-memory-utilization=$GMU"
  grep -F -q "$needle" "$COMPOSE" || return 1

  # quantization / parser are conditional flags — match presence-or-absence.
  if [[ "$QUANT" == "none" ]]; then
    grep -F -q "    - --quantization=" "$COMPOSE" && return 1
  else
    grep -F -q "    - --quantization=$QUANT" "$COMPOSE" || return 1
  fi
  if [[ "$PARSER" == "none" ]]; then
    grep -F -q "    - --tool-call-parser=" "$COMPOSE" && return 1
  else
    grep -F -q "    - --tool-call-parser=$PARSER" "$COMPOSE" || return 1
  fi
  return 0
}

if already_matching; then
  echo "[set-model] STEP 2/5: compose already matches request — skipping edit + restart" >&2
  echo "[set-model] STEP 5/5: verifying /ready" >&2
  if curl -fsS --max-time 5 http://127.0.0.1:8000/ready >/dev/null 2>&1; then
    echo "[set-model] OK (idempotent no-op)" >&2
    exit 0
  fi
  echo "[set-model] compose matches but /ready not 200 — falling through to restart" >&2
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 2/5: atomic compose edit.
# Strategy:
#   1. Copy compose -> .new (same content)
#   2. awk rewrites the vllm-service command list in .new
#   3. ``docker compose -f .new config -q`` validates structure
#   4. Backup original to /opt/llm-gateway/deploy/.swap-history/
#   5. mv .new -> compose (atomic on same filesystem)
# Any error in 2-4 leaves the original compose untouched.
# ──────────────────────────────────────────────────────────────────────
echo "[set-model] STEP 2/5: editing $COMPOSE" >&2
HISTORY_DIR="$(dirname "$COMPOSE")/.swap-history"
sudo mkdir -p "$HISTORY_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
NEW="$COMPOSE.new.$TS"
BAK="$HISTORY_DIR/docker-compose.yml.bak.$TS"

sudo cp -p "$COMPOSE" "$NEW"
sudo cp -p "$COMPOSE" "$BAK"

# AWK rewrite. State machine:
#   - Track when we are inside the ``vllm:`` service's ``command:`` block
#   - Replace ``- --flag=...`` lines whose flag matches one of our six
#   - For optional flags (--quantization, --tool-call-parser):
#       * If new value == "none" → drop the line (filter it out)
#       * If new value != "none" and no line exists → append after
#         --max-model-len line (deterministic insertion point)
#
# We do NOT touch any other line. ``--enable-prefix-caching`` and
# ``--max-num-seqs`` are preserved exactly as they were.
sudo env \
    M="$MODEL" S="$SERVED" Q="$QUANT" P="$PARSER" \
    GMU="$GMU" MAXLEN="$MAXLEN" \
    awk '
BEGIN {
  in_vllm = 0; in_command = 0;
  printed_quant = 0; printed_parser = 0;
  q_done = (ENVIRON["Q"] == "none");
  p_done = (ENVIRON["P"] == "none");
}
# Service header: enter vllm block when we see "  vllm:" at the top level.
/^  [a-zA-Z0-9_-]+:[[:space:]]*$/ {
  in_vllm = ($0 == "  vllm:");
  in_command = 0;
}
# Command list start: capture only inside vllm block.
in_vllm && /^[[:space:]]+command:[[:space:]]*$/ { in_command = 1; print; next }
# Sibling key at same depth as `command:` ends the list.
in_command && /^[[:space:]]{4}[a-zA-Z]/ { in_command = 0 }

# Inside the vllm command list, rewrite recognised flags.
in_vllm && in_command && /^[[:space:]]+-[[:space:]]+--model=/                 { print "      - --model=" ENVIRON["M"]; next }
in_vllm && in_command && /^[[:space:]]+-[[:space:]]+--served-model-name=/     { print "      - --served-model-name=" ENVIRON["S"]; next }
in_vllm && in_command && /^[[:space:]]+-[[:space:]]+--max-model-len=/         {
  print "      - --max-model-len=" ENVIRON["MAXLEN"];
  # After --max-model-len, append optional flags if they were not
  # previously present in the file (we cannot re-scan, so we rely on
  # the post-pass below for completeness; this branch only handles
  # the in-place rewrite).
  next
}
in_vllm && in_command && /^[[:space:]]+-[[:space:]]+--gpu-memory-utilization=/ { print "      - --gpu-memory-utilization=" ENVIRON["GMU"]; next }
in_vllm && in_command && /^[[:space:]]+-[[:space:]]+--quantization=/ {
  if (ENVIRON["Q"] == "none") { next }            # drop
  print "      - --quantization=" ENVIRON["Q"];
  printed_quant = 1; next
}
in_vllm && in_command && /^[[:space:]]+-[[:space:]]+--tool-call-parser=/ {
  if (ENVIRON["P"] == "none") { next }            # drop
  print "      - --tool-call-parser=" ENVIRON["P"];
  printed_parser = 1; next
}
{ print }
' "$NEW" | sudo tee "$NEW.tmp" >/dev/null
sudo mv -f "$NEW.tmp" "$NEW"

# Post-pass: insert any optional flags that were not in the original
# compose. Insertion point: immediately after the --max-model-len line
# (deterministic and easy to reason about).
ensure_flag() {
  local flag="$1" value="$2"
  if [[ "$value" == "none" ]]; then return; fi
  if grep -F -q "      - --$flag=" "$NEW"; then return; fi
  sudo awk -v F="$flag" -v V="$value" '
    /^      - --max-model-len=/ { print; print "      - --" F "=" V; next }
    { print }
  ' "$NEW" | sudo tee "$NEW.tmp" >/dev/null
  sudo mv -f "$NEW.tmp" "$NEW"
}
ensure_flag "quantization"     "$QUANT"
ensure_flag "tool-call-parser" "$PARSER"

# Validate: docker compose must accept the new file. ``config -q`` is
# read-only and prints nothing on success.
if ! sudo docker compose -f "$NEW" config -q; then
  echo "[set-model] new compose failed docker validation; original untouched" >&2
  sudo rm -f "$NEW" "$NEW.tmp"
  exit 1
fi

# Sanity: every requested value must be present after the rewrite.
verify_present() {
  local needle="$1"
  if ! grep -F -q "$needle" "$NEW"; then
    echo "[set-model] post-edit verification missing: $needle" >&2
    sudo rm -f "$NEW"
    exit 1
  fi
}
verify_present "      - --model=$MODEL"
verify_present "      - --served-model-name=$SERVED"
verify_present "      - --max-model-len=$MAXLEN"
verify_present "      - --gpu-memory-utilization=$GMU"
[[ "$QUANT"  != "none" ]] && verify_present "      - --quantization=$QUANT"
[[ "$PARSER" != "none" ]] && verify_present "      - --tool-call-parser=$PARSER"

# Atomic swap.
sudo mv -f "$NEW" "$COMPOSE"
echo "[set-model] STEP 2/5: compose updated; backup at $BAK" >&2

# ──────────────────────────────────────────────────────────────────────
# STEP 3/5: bring the new container up. We use ``docker compose up -d``
# which respects ``depends_on`` + healthchecks; this also recreates
# only changed services.
# ──────────────────────────────────────────────────────────────────────
echo "[set-model] STEP 3/5: docker compose up -d (recreate vllm)" >&2
if ! sudo docker compose -f "$COMPOSE" up -d --no-deps --force-recreate vllm >&2; then
  echo "[set-model] docker compose up failed; restoring backup" >&2
  sudo cp -p "$BAK" "$COMPOSE"
  exit 2
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 4/5: docker container healthy.
# ──────────────────────────────────────────────────────────────────────
echo "[set-model] STEP 4/5: waiting for vllm container healthy (up to ${READY_TIMEOUT}s)" >&2
DEADLINE=$(( $(date +%s) + READY_TIMEOUT ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
  HS="$(sudo docker inspect --format '{{json .State.Health}}' llm-gateway-vllm 2>/dev/null || echo '{}')"
  STATUS="$(printf '%s' "$HS" | jq -r '.Status // "unknown"')"
  case "$STATUS" in
    healthy)   break ;;
    unhealthy) echo "[set-model] vllm container reported unhealthy" >&2; exit 3 ;;
    *)         sleep 5 ;;
  esac
done
if [[ "$STATUS" != "healthy" ]]; then
  echo "[set-model] vllm container not healthy within ${READY_TIMEOUT}s (last status: $STATUS)" >&2
  exit 3
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 5/5: gateway /ready returning 200. Container healthy is necessary
# but not sufficient — the gateway has its own readiness gate.
# ──────────────────────────────────────────────────────────────────────
echo "[set-model] STEP 5/5: verifying gateway /ready" >&2
DEADLINE=$(( $(date +%s) + 30 ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
  if curl -fsS --max-time 5 http://127.0.0.1:8000/ready >/dev/null 2>&1; then
    echo "[set-model] OK swap to $SERVED ($MODEL) complete" >&2
    exit 0
  fi
  sleep 2
done
echo "[set-model] /ready did not return 200 within 30s after container healthy" >&2
exit 3
'@

# ---------------------------------------------------------------------
# 4. Push payload + script over SSH. Layout:
#     fd 0 (stdin) ← bash script
#     fd 3         ← JSON payload
# We use ``ssh ... bash -s 3<<<JSON`` semantics by streaming both.
# ---------------------------------------------------------------------
Write-Stage "STEP 0/5: SSH to $Eip"
$transport = @"
exec 3< <(printf '%s' '$($payload.Replace("'", "'\''"))')
$remoteScript
"@

# Pipe transport into ssh; SSH executes ``bash -s`` which reads stdin
# and runs it. The exec line creates fd 3 from a process substitution
# that emits the JSON payload — bash inside the heredoc reads it via
# ``cat <&3``.
$transport | & ssh @sshArgs 'bash -s'
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    Write-Stage "DONE swap to $ServedModelName"
    exit $EXIT_OK
}

# Map remote exit code to the contract code. The remote script already
# exits with the right value for steps it owns; preserve that.
switch ($rc) {
    1 { Fail $EXIT_COMPOSE_FAILED "remote bash reported a compose-edit failure" }
    2 { Fail $EXIT_RESTART_FAILED "remote bash reported docker compose up failed" }
    3 { Fail $EXIT_NOT_HEALTHY    "remote bash reported container/gateway not healthy" }
    255 { Fail $EXIT_SSH_FAILED   "ssh transport failed (exit 255 — connection refused, key rejected, host down)" }
    default { Fail $EXIT_SSH_FAILED "remote bash exited $rc (unmapped — check stderr)" }
}
