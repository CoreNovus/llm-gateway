# Operator scripts (AWS EC2, PowerShell)

Day-to-day workflow for an `llm-gateway` instance running on a single EC2
GPU host with idle-shutdown cost guardrails. Designed to incur near-zero
fixed cost — the box only runs while you're actively developing.

```
setup-ssh.ps1            (one-time per laptop)
   ↓
fix-and-start.ps1   →   ssh -L 8000:...   →   develop   →   restore-idle-protection.ps1 -StopNow
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

# 5. Done for the day - restore idle protection + stop instance
.\restore-idle-protection.ps1 -StopNow

# 6. Done for a long while - revoke the SG :22 inbound rule
.\teardown-ssh.ps1
```

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
