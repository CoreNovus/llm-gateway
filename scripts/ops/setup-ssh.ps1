# One-time SSH setup for an llm-gateway EC2 instance.
#
# Generates a dedicated ed25519 key, opens an SG :22 inbound rule scoped
# to your current public IP /32, pushes the public key via EC2 Instance
# Connect (60s TTL), and persists it into authorized_keys for ongoing use.
#
# Instance discovery is tag-based so you don't have to hardcode IDs:
#   tag:application = vllm-serving
#   tag:environment = $Environment   (default: dev)
#
# Usage:
#   .\setup-ssh.ps1                        # discovers dev instance via tags
#   .\setup-ssh.ps1 -Environment prod
#   .\setup-ssh.ps1 -InstanceId i-abc -Eip 1.2.3.4 -Region us-east-1   # explicit
#
# Pair with teardown-ssh.ps1 when you're done with the dev box for a while.

param(
    [string]$Environment = 'dev',
    [string]$InstanceId,
    [string]$Eip,
    [string]$Region   = 'ap-northeast-1',
    [string]$Ec2User  = 'ubuntu',
    [string]$KeyPath  = (Join-Path $HOME ".ssh\id_ed25519_vllm")
)

$ErrorActionPreference = 'Stop'
$AWS = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"
$KEY_PUB = "$KeyPath.pub"
$KEY_COMMENT = "vllm-dev-$env:USERNAME"

# ---------------------------------------------------------------------
# 0. Discover instance + EIP via tags (unless explicitly passed)
# ---------------------------------------------------------------------
if (-not $InstanceId) {
    Write-Host ">>> Discovering instance via tag:application=vllm-serving + tag:environment=$Environment" -ForegroundColor Cyan
    $InstanceId = (& $AWS ec2 describe-instances `
        --filters "Name=tag:application,Values=vllm-serving" "Name=tag:environment,Values=$Environment" `
                  "Name=instance-state-name,Values=running,stopped,stopping,starting" `
        --region $Region `
        --query 'Reservations[0].Instances[0].InstanceId' --output text).Trim()
    if ([string]::IsNullOrWhiteSpace($InstanceId) -or $InstanceId -eq 'None') {
        throw "No vllm-serving instance found for environment=$Environment in $Region. Pass -InstanceId explicitly."
    }
    Write-Host "    InstanceId: $InstanceId"
}
if (-not $Eip) {
    $Eip = (& $AWS ec2 describe-instances --instance-ids $InstanceId --region $Region `
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text).Trim()
    if ([string]::IsNullOrWhiteSpace($Eip) -or $Eip -eq 'None') {
        throw "Instance $InstanceId has no public IP. Pass -Eip or attach an EIP first."
    }
    Write-Host "    Eip: $Eip"
}

# Refuse to proceed if instance is in a state where SSH won't work.
$state = (& $AWS ec2 describe-instances `
    --instance-ids $InstanceId --region $Region `
    --query 'Reservations[0].Instances[0].State.Name' --output text).Trim()
Write-Host ">>> Instance state: $state" -ForegroundColor Cyan
switch ($state) {
    'running'  { }
    'stopped'  {
        Write-Host "    Starting instance..." -ForegroundColor Yellow
        & $AWS ec2 start-instances --instance-ids $InstanceId --region $Region | Out-Null
        & $AWS ec2 wait instance-status-ok --instance-ids $InstanceId --region $Region
    }
    default { throw "Refusing to handle instance in state '$state'" }
}

# ---------------------------------------------------------------------
# 1. Generate dedicated SSH key (skip if exists)
# ---------------------------------------------------------------------
if (-not (Test-Path $KeyPath)) {
    Write-Host ">>> Generating dedicated key $KeyPath" -ForegroundColor Cyan
    ssh-keygen -t ed25519 -f $KeyPath -N '""' -C $KEY_COMMENT | Out-Host
} else {
    Write-Host ">>> Key already exists at $KeyPath (reusing)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------
# 2. Get current public IP and current SG id
# ---------------------------------------------------------------------
Write-Host ">>> Resolving your current public IP and the vLLM SG id" -ForegroundColor Cyan
$myIp = (Invoke-RestMethod -Uri https://checkip.amazonaws.com -TimeoutSec 5).Trim()
if ($myIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
    throw "checkip.amazonaws.com returned non-IPv4 response: '$myIp' - refusing to author SG rule"
}
Write-Host "    Your IP: $myIp"

$sgId = (& $AWS ec2 describe-instances `
    --instance-ids $InstanceId `
    --region $Region `
    --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' `
    --output text).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sgId)) {
    throw "Cannot resolve SG id for $InstanceId"
}
Write-Host "    SG id:   $sgId"

# Idempotency: skip if a rule already covers this exact IP.
$existing = & $AWS ec2 describe-security-group-rules `
    --filters "Name=group-id,Values=$sgId" `
    --region $Region `
    --query "SecurityGroupRules[?IpProtocol=='tcp' && FromPort==``22`` && ToPort==``22`` && CidrIpv4=='$myIp/32' && IsEgress==``false``].SecurityGroupRuleId" `
    --output text
if (-not [string]::IsNullOrWhiteSpace($existing)) {
    Write-Host ">>> SG rule already allows 22/tcp from $myIp/32 (rule $existing)" -ForegroundColor DarkGray
} else {
    Write-Host ">>> Adding SG inbound 22/tcp from $myIp/32" -ForegroundColor Cyan
    $ipPerm = "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$myIp/32}]"
    & $AWS ec2 authorize-security-group-ingress `
        --group-id $sgId `
        --ip-permissions $ipPerm `
        --region $Region | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "authorize-security-group-ingress failed" }
}

# ---------------------------------------------------------------------
# 3. Push public key via EC2 Instance Connect (60s TTL)
# ---------------------------------------------------------------------
Write-Host ">>> Pushing public key via EC2 Instance Connect (60s TTL)" -ForegroundColor Cyan
$pubKey = Get-Content $KEY_PUB -Raw
& $AWS ec2-instance-connect send-ssh-public-key `
    --instance-id $InstanceId `
    --instance-os-user $Ec2User `
    --ssh-public-key $pubKey `
    --region $Region `
    --output json | Out-Null
if ($LASTEXITCODE -ne 0) { throw "send-ssh-public-key failed" }
Write-Host "    OK - have 60 seconds before that key is rotated out"

# ---------------------------------------------------------------------
# 4. SSH in once and persist the key to authorized_keys
# ---------------------------------------------------------------------
Write-Host ">>> Persisting key into ~$Ec2User/.ssh/authorized_keys" -ForegroundColor Cyan
$sshArgs = @(
    '-i', $KeyPath,
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'IdentitiesOnly=yes',
    '-o', 'PasswordAuthentication=no',
    '-o', 'ConnectTimeout=10',
    "$Ec2User@$Eip"
)

$keyLine = $pubKey.Trim()
$persistCmd = 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && ' +
              "grep -qxF '$keyLine' ~/.ssh/authorized_keys || " +
              "printf '%s\n' '$keyLine' | tee -a ~/.ssh/authorized_keys | head -c 0 && " +
              'echo PERSIST_OK_done'

& ssh @sshArgs $persistCmd
if ($LASTEXITCODE -ne 0) {
    throw "Persist SSH failed - if you see Permission denied, the EIC TTL likely expired; rerun this script"
}

# ---------------------------------------------------------------------
# 5. Verify - run a few cheap diagnostic commands
# ---------------------------------------------------------------------
Write-Host ">>> Verify connectivity + GPU + bootstrap status" -ForegroundColor Cyan
$verifyCmd = @'
echo === host ===
whoami; hostname; uname -r
echo === gpu ===
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1 | head -1
echo === bootstrap script ===
ls -la /usr/local/bin/llm-gateway-bootstrap 2>&1
echo === models mount ===
df -h /models 2>&1 | tail -2
'@
$verifyCmd | & ssh @sshArgs 'bash -s'

Write-Host ""
Write-Host "SSH ready. Daily driver:" -ForegroundColor Green
Write-Host "  ssh -i `"$KeyPath`" $Ec2User@$Eip"
Write-Host ""
Write-Host "When done with the dev box for a while, revoke the SG rule:" -ForegroundColor Yellow
Write-Host "  .\teardown-ssh.ps1 -Environment $Environment"
