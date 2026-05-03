# Revoke any SG :22 inbound rules added by setup-ssh.ps1.
#
# Idempotent - reports what got revoked.
#
# Keeps:
#   - The dedicated key file (~/.ssh/id_ed25519_vllm[.pub]) - reuse next time.
#   - The key inside ~ubuntu/.ssh/authorized_keys on the EC2 - harmless
#     once port 22 is closed; cleared on stack destroy.
#
# Usage:
#   .\teardown-ssh.ps1                        # discovers dev instance via tags
#   .\teardown-ssh.ps1 -Environment prod
#   .\teardown-ssh.ps1 -InstanceId i-abc -Region us-east-1

param(
    [string]$Environment = 'dev',
    [string]$InstanceId,
    [string]$Region = 'ap-northeast-1'
)

$ErrorActionPreference = 'Stop'
$AWS = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"

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

$sgId = (& $AWS ec2 describe-instances `
    --instance-ids $InstanceId `
    --region $Region `
    --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' `
    --output text).Trim()
Write-Host ">>> SG: $sgId" -ForegroundColor Cyan

$rules = & $AWS ec2 describe-security-group-rules `
    --filters "Name=group-id,Values=$sgId" "Name=ip-protocol,Values=tcp" `
    --region $Region `
    --query "SecurityGroupRules[?FromPort==``22`` && ToPort==``22`` && IsEgress==``false``].SecurityGroupRuleId" `
    --output text

if ([string]::IsNullOrWhiteSpace($rules)) {
    Write-Host ">>> No :22 inbound rules found - already torn down." -ForegroundColor DarkGray
    return
}

$ruleIds = $rules -split '\s+' | Where-Object { $_ }
Write-Host ">>> Revoking $($ruleIds.Count) rule(s): $ruleIds" -ForegroundColor Cyan

& $AWS ec2 revoke-security-group-ingress `
    --group-id $sgId `
    --security-group-rule-ids $ruleIds `
    --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) { throw "revoke-security-group-ingress failed" }

Write-Host "Done. Port 22 closed." -ForegroundColor Green
