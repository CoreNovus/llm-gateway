# Restore the cost-control safeguards that fix-and-start.ps1 disabled.
# Run when you're done developing for the day.
#
# What this restores:
#   1. Re-enable the CloudWatch idle backstop alarm action.
#   2. Move /etc/cron.d/llm-gateway-idle-shutdown.disabled back to
#      /etc/cron.d/llm-gateway-idle-shutdown.
#   3. Optionally stop the instance now (to lock in the savings).
#
# Usage:
#   .\restore-idle-protection.ps1
#   .\restore-idle-protection.ps1 -StopNow
#   .\restore-idle-protection.ps1 -Environment prod -StopNow

param(
    [switch]$StopNow,
    [string]$Environment = 'dev',
    [string]$InstanceId,
    [string]$Eip,
    [string]$Region   = 'ap-northeast-1',
    [string]$Ec2User  = 'ubuntu',
    [string]$KeyPath  = (Join-Path $HOME ".ssh\id_ed25519_vllm"),
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
}

# ---------------------------------------------------------------------
# 1. Re-enable CloudWatch alarm action
# ---------------------------------------------------------------------
$alarmName = (& $AWS cloudwatch describe-alarms --region $Region `
    --query "MetricAlarms[?contains(AlarmName,'$AlarmNameContains')].AlarmName | [0]" `
    --output text).Trim()
if ($alarmName -and $alarmName -ne 'None') {
    Write-Host ">>> Re-enabling alarm actions: $alarmName" -ForegroundColor Cyan
    & $AWS cloudwatch enable-alarm-actions --alarm-names $alarmName --region $Region | Out-Null
    Write-Host "    OK." -ForegroundColor Green
} else {
    Write-Host ">>> No alarm matching '$AlarmNameContains' - skipping" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------
# 2. Re-enable instance-side idle cron (only if instance is running)
# ---------------------------------------------------------------------
$state = (& $AWS ec2 describe-instances `
    --instance-ids $InstanceId --region $Region `
    --query 'Reservations[0].Instances[0].State.Name' --output text).Trim()
Write-Host ">>> Instance state: $state" -ForegroundColor Cyan

if ($state -eq 'running' -and $Eip -and $Eip -ne 'None') {
    $sshArgs = @(
        '-i', $KeyPath,
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'IdentitiesOnly=yes',
        '-o', 'PasswordAuthentication=no',
        '-o', 'ConnectTimeout=15',
        "$Ec2User@$Eip"
    )
    $restoreScript = @'
if [ -f /etc/cron.d/llm-gateway-idle-shutdown.disabled ]; then
  sudo mv /etc/cron.d/llm-gateway-idle-shutdown.disabled /etc/cron.d/llm-gateway-idle-shutdown
  echo "cron re-enabled"
else
  echo "(cron file not found - already enabled or never disabled)"
fi
ls -la /etc/cron.d/llm-gateway-idle-shutdown* 2>&1
'@
    Write-Host ">>> Re-enabling idle cron via SSH" -ForegroundColor Cyan
    $restoreScript | & ssh @sshArgs 'bash -s'
} else {
    Write-Host "    Instance not running - cron file is in EBS, will be restored on next start." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------
# 3. Optionally stop the instance
# ---------------------------------------------------------------------
if ($StopNow -and $state -eq 'running') {
    Write-Host ">>> Stopping instance now (-StopNow flag)" -ForegroundColor Cyan
    & $AWS ec2 stop-instances --instance-ids $InstanceId --region $Region `
        --query 'StoppingInstances[0].CurrentState.Name' --output text
    Write-Host "    Stop initiated." -ForegroundColor Green
}

Write-Host ""
Write-Host "Idle protection restored." -ForegroundColor Green
