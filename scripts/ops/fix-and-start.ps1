# One-shot fix-and-start for the llm-gateway + vLLM stack on EC2.
#
# What this script does (all idempotent):
#   1. Make sure the EC2 is running.
#   2. Disable the CloudWatch idle backstop alarm action (so it doesn't
#      stop the box mid-startup).
#   3. SSH in:
#      a. Disable the idle-shutdown cron (mv to .disabled).
#      b. sed-fix the systemd unit (--no-color -> --ansi never) for
#         compatibility with docker compose v2.x. Safe no-op if already
#         on a fresh systemd unit.
#      c. systemctl daemon-reload + start llm-gateway.service.
#      d. Tail journal until "Application startup complete." or fail.
#   4. From the EC2, smoke /health, /ready, /v1/chat/completions.
#   5. Print the SSH tunnel command + restore command.
#
# Usage:
#   .\fix-and-start.ps1                  # discovers dev instance via tags
#   .\fix-and-start.ps1 -Environment prod
#   .\fix-and-start.ps1 -InstanceId i-abc -Eip 1.2.3.4 -Region us-east-1

param(
    [string]$Environment = 'dev',
    [string]$InstanceId,
    [string]$Eip,
    [string]$Region   = 'ap-northeast-1',
    [string]$Ec2User  = 'ubuntu',
    [string]$KeyPath  = (Join-Path $HOME ".ssh\id_ed25519_vllm"),
    [string]$ServedModelName = 'selfhost-qwen',
    [string]$AlarmNameContains = 'VLLMIdleBackstop'
)

$ErrorActionPreference = 'Stop'
$AWS = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"

# ---------------------------------------------------------------------
# 0. Discover instance + EIP via tags
# ---------------------------------------------------------------------
if (-not $InstanceId) {
    $InstanceId = (& $AWS ec2 describe-instances `
        --filters "Name=tag:application,Values=vllm-serving" "Name=tag:environment,Values=$Environment" `
                  "Name=instance-state-name,Values=running,stopped,stopping,starting" `
        --region $Region `
        --query 'Reservations[0].Instances[0].InstanceId' --output text).Trim()
    if ([string]::IsNullOrWhiteSpace($InstanceId) -or $InstanceId -eq 'None') {
        throw "No vllm-serving instance found for environment=$Environment in $Region. Pass -InstanceId explicitly."
    }
}
if (-not $Eip) {
    $Eip = (& $AWS ec2 describe-instances --instance-ids $InstanceId --region $Region `
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text).Trim()
    if ([string]::IsNullOrWhiteSpace($Eip) -or $Eip -eq 'None') {
        throw "Instance $InstanceId has no public IP. Pass -Eip explicitly."
    }
}

# ---------------------------------------------------------------------
# 1. Start instance if needed
# ---------------------------------------------------------------------
$state = (& $AWS ec2 describe-instances `
    --instance-ids $InstanceId --region $Region `
    --query 'Reservations[0].Instances[0].State.Name' --output text).Trim()
Write-Host ">>> Instance state: $state" -ForegroundColor Cyan
if ($state -ne 'running') {
    if ($state -eq 'stopped') {
        & $AWS ec2 start-instances --instance-ids $InstanceId --region $Region | Out-Null
    }
    & $AWS ec2 wait instance-status-ok --instance-ids $InstanceId --region $Region
    Write-Host "    Instance OK." -ForegroundColor Green
}

# ---------------------------------------------------------------------
# 2. Disable CloudWatch idle alarm (action only - the alarm itself stays
#    so we can re-enable in restore-idle-protection.ps1).
# ---------------------------------------------------------------------
$alarmName = (& $AWS cloudwatch describe-alarms --region $Region `
    --query "MetricAlarms[?contains(AlarmName,'$AlarmNameContains')].AlarmName | [0]" `
    --output text).Trim()
if ($alarmName -and $alarmName -ne 'None') {
    Write-Host ">>> Disabling alarm actions: $alarmName" -ForegroundColor Cyan
    & $AWS cloudwatch disable-alarm-actions --alarm-names $alarmName --region $Region | Out-Null
    Write-Host "    Disabled (will re-enable in restore script)." -ForegroundColor Green
} else {
    Write-Host ">>> No alarm matching '$AlarmNameContains' - skipping" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------
# 3. SSH: disable cron + sed fix + start + watch journal
# ---------------------------------------------------------------------
$sshArgs = @(
    '-i', $KeyPath,
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'IdentitiesOnly=yes',
    '-o', 'PasswordAuthentication=no',
    '-o', 'ConnectTimeout=15',
    "$Ec2User@$Eip"
)

$fixScript = @'
set -e
echo === 3a. disable idle cron ===
if [ -f /etc/cron.d/llm-gateway-idle-shutdown ]; then
  sudo mv /etc/cron.d/llm-gateway-idle-shutdown /etc/cron.d/llm-gateway-idle-shutdown.disabled
  echo "moved to .disabled"
else
  echo "(already disabled or missing)"
fi
echo
echo === 3b. sed fix systemd unit (compose v1->v2 compat) ===
sudo cp /etc/systemd/system/llm-gateway.service /etc/systemd/system/llm-gateway.service.bak 2>/dev/null || true
sudo sed -i 's|docker compose --no-color|docker compose --ansi never|g' /etc/systemd/system/llm-gateway.service
sudo grep -E '^Exec(StartPre|Start|Stop)=' /etc/systemd/system/llm-gateway.service
echo
echo === 3c. daemon-reload + start ===
sudo systemctl daemon-reload
sudo systemctl reset-failed llm-gateway.service 2>/dev/null || true
sudo systemctl start llm-gateway.service
echo "start command issued"
echo
echo === 3d. follow journal until ready or fail ===
sudo timeout 1200 bash -c '
  while true; do
    if sudo journalctl -u llm-gateway.service --since "20 min ago" --no-pager | grep -q "Application startup complete"; then
      echo "GATEWAY_READY"
      exit 0
    fi
    if sudo systemctl is-failed --quiet llm-gateway.service; then
      echo "SERVICE_FAILED"
      sudo journalctl -xeu llm-gateway.service --no-pager | tail -40
      exit 1
    fi
    STATE=$(sudo systemctl is-active llm-gateway.service)
    echo "[$(date +%H:%M:%S)] state=$STATE"
    sleep 15
  done
'
echo
echo === final ===
sudo systemctl is-active llm-gateway.service
sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo === DONE ===
'@

Write-Host ">>> SSH: applying fix and starting service (10-20 min for first boot)" -ForegroundColor Cyan
$fixScript | & ssh @sshArgs 'bash -s'
if ($LASTEXITCODE -ne 0) {
    Write-Host "SSH script ended with non-zero exit code $LASTEXITCODE" -ForegroundColor Red
    Write-Host "Run restore-idle-protection.ps1 to re-arm CW alarm + cron when done." -ForegroundColor Yellow
    throw "Fix failed"
}

# ---------------------------------------------------------------------
# 4. Smoke test from inside the EC2 (no tunnel needed).
# ---------------------------------------------------------------------
Write-Host ""
Write-Host ">>> Smoke test from EC2 host" -ForegroundColor Cyan
$smokeScript = @"
TOKEN=`$(grep "^BEARER_TOKEN=" /opt/llm-gateway/deploy/.env | cut -d= -f2)
echo === health ===
curl -fsS http://127.0.0.1:8000/health
echo
echo === ready ===
curl -fsS http://127.0.0.1:8000/ready
echo
echo === chat completion ===
curl -fsS -H "Authorization: Bearer `$TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/v1/chat/completions \
  -d '{"model":"$ServedModelName","messages":[{"role":"user","content":"reply with the word ok"}],"max_tokens":8}'
echo
"@
$smokeScript | & ssh @sshArgs 'sudo bash -s'

Write-Host ""
Write-Host "llm-gateway is up." -ForegroundColor Green
Write-Host ""
Write-Host "Open the local tunnel in another PowerShell window:" -ForegroundColor Yellow
Write-Host "  ssh -i `"$KeyPath`" -L 8000:127.0.0.1:8000 -N $Ec2User@$Eip"
Write-Host ""
Write-Host "Point your client at http://127.0.0.1:8000/v1 with the bearer token" -ForegroundColor Yellow
Write-Host "from your Secrets Manager (any OpenAI-compatible client works)."
Write-Host ""
Write-Host "When you finish development, restore protection:" -ForegroundColor Yellow
Write-Host "  .\restore-idle-protection.ps1 -Environment $Environment -StopNow"
