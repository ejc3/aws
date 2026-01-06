# GitHub Actions Runner Auto-scaling
# Launches spot instances when jobs are queued, stops when idle

# ============================================
# Lambda Function for Webhook Handler
# ============================================

data "archive_file" "runner_webhook" {
  type        = "zip"
  output_path = "${path.module}/.terraform/runner-webhook.zip"

  source {
    content  = <<-EOF
      import json
      import boto3
      import os
      import hmac
      import hashlib

      ec2 = boto3.client('ec2', region_name='us-west-1')
      ssm = boto3.client('ssm', region_name='us-west-1')

      def get_user_data():
          """Fetch user_data from SSM Parameter Store"""
          param_name = os.environ.get('USER_DATA_PARAM', '/github-runner/user-data')
          resp = ssm.get_parameter(Name=param_name)
          return resp['Parameter']['Value']

      def get_latest_runner_ami():
          """Get the latest available AMI tagged with Purpose=github-runner"""
          response = ec2.describe_images(
              Owners=['self'],
              Filters=[
                  {'Name': 'tag:Purpose', 'Values': ['github-runner']},
                  {'Name': 'state', 'Values': ['available']}
              ]
          )
          if not response['Images']:
              return None
          # Sort by creation date, newest first
          images = sorted(response['Images'], key=lambda x: x['CreationDate'], reverse=True)
          return images[0]['ImageId']

      def verify_signature(payload, signature, secret):
          if not secret:
              return True  # Skip verification if no secret configured
          expected = 'sha256=' + hmac.new(
              secret.encode(), payload.encode(), hashlib.sha256
          ).hexdigest()
          return hmac.compare_digest(expected, signature)

      def get_running_runners():
          """Count running runner instances"""
          response = ec2.describe_instances(
              Filters=[
                  {'Name': 'tag:Role', 'Values': ['github-runner']},
                  {'Name': 'instance-state-name', 'Values': ['running', 'pending']}
              ]
          )
          count = sum(len(r['Instances']) for r in response['Reservations'])
          return count

      def launch_runner():
          """Launch a new spot runner instance with tags"""
          ami_id = get_latest_runner_ami()
          if not ami_id:
              raise Exception("No runner AMI found with Purpose=github-runner tag")
          response = ec2.run_instances(
              MinCount=1,
              MaxCount=1,
              ImageId=ami_id,
              InstanceType=os.environ['INSTANCE_TYPE'],
              KeyName='fcvm-ec2',
              SubnetId=os.environ['SUBNET_ID'],
              SecurityGroupIds=[os.environ['SECURITY_GROUP_ID']],
              IamInstanceProfile={'Name': os.environ['INSTANCE_PROFILE']},
              BlockDeviceMappings=[{
                  'DeviceName': '/dev/sda1',
                  'Ebs': {'VolumeSize': 100, 'VolumeType': 'gp3', 'DeleteOnTermination': True}
              }],
              UserData=get_user_data(),
              InstanceMarketOptions={
                  'MarketType': 'spot',
                  'SpotOptions': {'SpotInstanceType': 'one-time'}
              },
              TagSpecifications=[{
                  'ResourceType': 'instance',
                  'Tags': [
                      {'Key': 'Name', 'Value': 'github-runner-autoscale'},
                      {'Key': 'Role', 'Value': 'github-runner'}
                  ]
              }]
          )
          return response['Instances'][0]['InstanceId']

      def handler(event, context):
          # Parse webhook
          body = event.get('body', '{}')
          headers = event.get('headers', {})

          # Verify signature
          signature = headers.get('x-hub-signature-256', '')
          secret = os.environ.get('WEBHOOK_SECRET', '')
          if not verify_signature(body, signature, secret):
              return {'statusCode': 401, 'body': 'Invalid signature'}

          payload = json.loads(body)
          action = payload.get('action', '')

          # Only act on queued jobs
          if action != 'queued':
              return {'statusCode': 200, 'body': f'Ignoring action: {action}'}

          # Check current runner count
          max_runners = int(os.environ.get('MAX_RUNNERS', '3'))
          running = get_running_runners()

          if running >= max_runners:
              return {'statusCode': 200, 'body': f'Max runners ({max_runners}) reached'}

          # Launch new runner
          spot_id = launch_runner()
          return {
              'statusCode': 200,
              'body': f'Launched runner: {spot_id}'
          }
    EOF
    filename = "lambda_function.py"
  }
}

resource "aws_lambda_function" "runner_webhook" {
  count            = var.enable_github_runner ? 1 : 0
  filename         = data.archive_file.runner_webhook.output_path
  source_code_hash = data.archive_file.runner_webhook.output_base64sha256
  function_name    = "github-runner-webhook"
  role             = aws_iam_role.runner_lambda[0].arn
  handler          = "lambda_function.handler"
  runtime          = "python3.12"
  timeout          = 30

  environment {
    variables = {
      INSTANCE_TYPE     = var.github_runner_instance_type
      SUBNET_ID         = aws_subnet.runner[0].id
      SECURITY_GROUP_ID = aws_security_group.runner[0].id
      INSTANCE_PROFILE  = aws_iam_instance_profile.runner[0].name
      USER_DATA_PARAM   = aws_ssm_parameter.runner_user_data[0].name
      MAX_RUNNERS       = "5"
      WEBHOOK_SECRET    = var.github_webhook_secret
    }
  }

  tags = {
    Name = "github-runner-webhook"
  }
}

# ============================================
# IAM Role for Lambda
# ============================================

resource "aws_iam_role" "runner_lambda" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "runner_lambda" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-lambda-policy"
  role  = aws_iam_role.runner_lambda[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeImages",
          "ec2:DescribeInstances",
          "ec2:RunInstances",
          "ec2:StopInstances",
          "ec2:TerminateInstances",
          "ec2:CreateTags",
          "iam:PassRole",
          "cloudwatch:GetMetricStatistics"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:us-west-1:928413605543:parameter/github-runner/*"
      }
    ]
  })
}

# ============================================
# API Gateway for Webhook
# ============================================

resource "aws_apigatewayv2_api" "runner_webhook" {
  count         = var.enable_github_runner ? 1 : 0
  name          = "github-runner-webhook"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_stage" "runner_webhook" {
  count       = var.enable_github_runner ? 1 : 0
  api_id      = aws_apigatewayv2_api.runner_webhook[0].id
  name        = "$default"
  auto_deploy = true
}

resource "aws_apigatewayv2_integration" "runner_webhook" {
  count              = var.enable_github_runner ? 1 : 0
  api_id             = aws_apigatewayv2_api.runner_webhook[0].id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.runner_webhook[0].invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "runner_webhook" {
  count     = var.enable_github_runner ? 1 : 0
  api_id    = aws_apigatewayv2_api.runner_webhook[0].id
  route_key = "POST /webhook"
  target    = "integrations/${aws_apigatewayv2_integration.runner_webhook[0].id}"
}

resource "aws_lambda_permission" "runner_webhook" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_webhook[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.runner_webhook[0].execution_arn}/*/*"
}

# ============================================
# Variables
# ============================================

variable "enable_github_runner" {
  description = "Enable GitHub Actions runner infrastructure"
  type        = bool
  default     = true
}

variable "github_runner_instance_type" {
  description = "Instance type for GitHub runner"
  type        = string
  default     = "c7gd.metal"
}

variable "github_webhook_secret" {
  description = "GitHub webhook secret for signature verification"
  type        = string
  default     = ""
  sensitive   = true
}

# ============================================
# Outputs
# ============================================

output "runner_webhook_url" {
  description = "URL for GitHub webhook"
  value       = var.enable_github_runner ? "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook" : null
}

# ============================================
# Shared user data for runners
# ============================================

locals {
  # User data for pre-baked AMI - just set permissions and register runner
  # NOTE: Heredoc content starts at column 0 because <<-EOF only strips tabs, not spaces
  runner_user_data = <<-EOF
#!/bin/bash
set -euxo pipefail

# Console logging (for debugging via EC2 get-console-output)
cat >> /etc/rsyslog.d/50-console.conf << 'RSYSLOG'
*.emerg;*.alert;*.crit;*.err                    /dev/ttyS0
kern.*                                           /dev/ttyS0
RSYSLOG
systemctl restart rsyslog || true
echo "kernel.printk = 7 4 1 7" >> /etc/sysctl.conf
sysctl -w kernel.printk="7 4 1 7" || true

# Mount NVMe instance storage as btrfs at /mnt/fcvm-btrfs
# This is where fcvm expects its data (CoW reflinks require btrfs)
ROOT_DEV=$(lsblk -no PKNAME $(findmnt -no SOURCE /) | head -1)
NVME_DEV=$(lsblk -dn -o NAME,TYPE | awk '$2=="disk" && /^nvme/ {print $1}' | grep -v "^$ROOT_DEV$" | head -1)
if [ -n "$NVME_DEV" ]; then
  echo "Setting up NVMe as btrfs: /dev/$NVME_DEV"
  # Install btrfs-progs if not present (gives flexibility without AMI rebuild)
  which mkfs.btrfs || apt-get update && apt-get install -y btrfs-progs
  mkfs.btrfs -f /dev/$NVME_DEV
  mkdir -p /mnt/fcvm-btrfs
  mount /dev/$NVME_DEV /mnt/fcvm-btrfs
  chmod 1777 /mnt/fcvm-btrfs

  # Create fcvm directory structure
  mkdir -p /mnt/fcvm-btrfs/{kernels,rootfs,initrd,state,snapshots,vm-disks,cache,image-cache}
  chown -R ubuntu:ubuntu /mnt/fcvm-btrfs

  # Also set up containers and cargo on NVMe (separate ext4 partition would be better but single disk)
  mkdir -p /mnt/fcvm-btrfs/containers /mnt/fcvm-btrfs/cargo-target
  chown ubuntu:ubuntu /mnt/fcvm-btrfs/containers /mnt/fcvm-btrfs/cargo-target

  # Podman containers on NVMe
  mkdir -p /home/ubuntu/.local/share
  ln -sf /mnt/fcvm-btrfs/containers /home/ubuntu/.local/share/containers
  chown -R ubuntu:ubuntu /home/ubuntu/.local

  # Cargo target dir on NVMe
  echo 'export CARGO_TARGET_DIR=/mnt/fcvm-btrfs/cargo-target' >> /home/ubuntu/.bashrc
else
  echo "WARNING: No NVMe found - will use loopback btrfs (slower, limited space)"
fi

# Runtime permissions
chmod 666 /dev/kvm
[ -e /dev/userfaultfd ] || mknod /dev/userfaultfd c 10 126
chmod 666 /dev/userfaultfd
sysctl -w vm.unprivileged_userfaultfd=1
sysctl -w kernel.unprivileged_userns_clone=1 || true
iptables -P FORWARD ACCEPT || true

# Fix podman rootless - deduplicate subuid/subgid (AMI has duplicates)
sort -u /etc/subuid > /tmp/subuid && mv /tmp/subuid /etc/subuid
sort -u /etc/subgid > /tmp/subgid && mv /tmp/subgid /etc/subgid

# Enable linger so user processes survive SSH logout
loginctl enable-linger ubuntu

# SSH keys: jumpbox (fcvm-ec2) + dev servers can access runners
mkdir -p /home/ubuntu/.ssh
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINwtXjjTCVgT9OR3qrnz3zDkV2GveuCBlWFXSOBG2joe fcvm-ec2" >> /home/ubuntu/.ssh/authorized_keys
echo "${trimspace(tls_private_key.dev_to_runner.public_key_openssh)}" >> /home/ubuntu/.ssh/authorized_keys
chown -R ubuntu:ubuntu /home/ubuntu/.ssh
chmod 700 /home/ubuntu/.ssh
chmod 600 /home/ubuntu/.ssh/authorized_keys

# Start SSM agent (snap-based, kernel has squashfs)
snap start amazon-ssm-agent || true

# Get instance ID
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

# Register and start runner
cd /opt/actions-runner
PAT=$(aws ssm get-parameter --name /github-runner/pat --with-decryption --query 'Parameter.Value' --output text --region us-west-1 2>/dev/null || echo "")
if [ -n "$PAT" ] && [ "$PAT" != "placeholder" ]; then
  TOKEN=$(curl -s -X POST -H "Authorization: token $PAT" \
    https://api.github.com/repos/ejc3/fcvm/actions/runners/registration-token | jq -r '.token')
  sudo -u ubuntu ./config.sh --url https://github.com/ejc3/fcvm --token "$TOKEN" \
    --name "runner-$INSTANCE_ID" --labels self-hosted,Linux,ARM64 --unattended --replace
  ./svc.sh install ubuntu
  ./svc.sh start
fi
EOF
}

# SSM Parameter to store user_data (avoids Lambda 4KB env var limit)
resource "aws_ssm_parameter" "runner_user_data" {
  count = var.enable_github_runner ? 1 : 0
  name  = "/github-runner/user-data"
  type  = "String"
  tier  = "Advanced"
  value = base64encode(local.runner_user_data)
  tags = {
    Name = "github-runner-user-data"
  }
}

# ============================================
# Idle Runner Cleanup (runs every 5 minutes)
# ============================================

data "archive_file" "runner_cleanup" {
  type        = "zip"
  output_path = "${path.module}/.terraform/runner-cleanup.zip"

  source {
    content  = <<-EOF
      import boto3
      import urllib.request
      import json
      import os
      from datetime import datetime, timezone, timedelta

      ec2 = boto3.client('ec2', region_name='us-west-1')
      cloudwatch = boto3.client('cloudwatch', region_name='us-west-1')
      ssm = boto3.client('ssm', region_name='us-west-1')

      REPO = 'ejc3/fcvm'

      def get_github_pat():
          """Get GitHub PAT from SSM"""
          try:
              resp = ssm.get_parameter(Name='/github-runner/pat', WithDecryption=True)
              pat = resp['Parameter']['Value']
              print(f'SSM PAT value starts with: {pat[:10] if pat else "None"}...')
              if pat and pat != 'placeholder':
                  return pat
          except Exception as e:
              print(f'SSM get_parameter failed: {e}')
          return None

      def get_runners(pat):
          """Get all runners from GitHub, returns dict of name -> {id, busy}"""
          url = f'https://api.github.com/repos/{REPO}/actions/runners'
          req = urllib.request.Request(url, headers={
              'Authorization': f'token {pat}',
              'Accept': 'application/vnd.github.v3+json'
          })
          try:
              with urllib.request.urlopen(req) as resp:
                  data = json.loads(resp.read())
                  return {r['name']: {'id': r['id'], 'busy': r['busy']} for r in data.get('runners', [])}
          except Exception as e:
              print(f'Failed to get runners: {e}')
          return {}

      def deregister_runner(runner_id, pat):
          """Remove runner from GitHub by ID"""
          try:
              del_url = f'https://api.github.com/repos/{REPO}/actions/runners/{runner_id}'
              del_req = urllib.request.Request(del_url, method='DELETE', headers={
                  'Authorization': f'token {pat}',
                  'Accept': 'application/vnd.github.v3+json'
              })
              urllib.request.urlopen(del_req)
              return True
          except Exception as e:
              print(f'Failed to deregister runner {runner_id}: {e}')
          return False

      def get_instance_state(instance_id):
          """Check if instance exists and is running"""
          try:
              resp = ec2.describe_instances(InstanceIds=[instance_id])
              for res in resp['Reservations']:
                  for inst in res['Instances']:
                      return inst['State']['Name']
          except Exception:
              pass
          return None

      def handler(event, context):
          pat = get_github_pat()
          print(f'PAT available: {bool(pat)}')
          runners = get_runners(pat) if pat else {}
          print(f'Found {len(runners)} runners from GitHub')

          # Phase 1: Clean up orphaned GitHub runners (instances gone)
          orphans_cleaned = []
          for runner_name, runner_info in runners.items():
              if not runner_name.startswith('runner-i-'):
                  print(f'Skipping {runner_name} (not runner-i- pattern)')
                  continue
              instance_id = runner_name.replace('runner-', '')
              state = get_instance_state(instance_id)
              print(f'Runner {runner_name}: instance state={state}')
              # If instance doesn't exist or is terminated, deregister runner
              if state is None or state in ('terminated', 'shutting-down'):
                  print(f'Cleaning orphan: {runner_name} (state={state})')
                  if deregister_runner(runner_info['id'], pat):
                      orphans_cleaned.append(runner_name)

          # Phase 2: Find idle running instances to terminate
          response = ec2.describe_instances(
              Filters=[
                  {'Name': 'tag:Role', 'Values': ['github-runner']},
                  {'Name': 'tag:Name', 'Values': ['github-runner-autoscale']},
                  {'Name': 'instance-state-name', 'Values': ['running']}
              ]
          )

          terminated = []
          skipped_busy = []
          for reservation in response['Reservations']:
              for instance in reservation['Instances']:
                  instance_id = instance['InstanceId']
                  launch_time = instance['LaunchTime']
                  runner_name = f'runner-{instance_id}'

                  # Skip if launched less than 30 minutes ago
                  if datetime.now(timezone.utc) - launch_time < timedelta(minutes=30):
                      continue

                  # Skip if runner is busy (has active job)
                  runner_info = runners.get(runner_name, {})
                  if runner_info.get('busy', False):
                      skipped_busy.append(instance_id)
                      continue

                  # Check CPU utilization
                  metrics = cloudwatch.get_metric_statistics(
                      Namespace='AWS/EC2',
                      MetricName='CPUUtilization',
                      Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                      StartTime=datetime.now(timezone.utc) - timedelta(minutes=30),
                      EndTime=datetime.now(timezone.utc),
                      Period=300,
                      Statistics=['Average']
                  )

                  # If avg CPU < 5% for last 30 mins, terminate it
                  if metrics['Datapoints']:
                      avg_cpu = sum(d['Average'] for d in metrics['Datapoints']) / len(metrics['Datapoints'])
                      if avg_cpu < 5:
                          print(f'Terminating idle: {instance_id} (cpu={avg_cpu:.1f}%)')
                          # Deregister from GitHub first
                          if runner_info.get('id'):
                              deregister_runner(runner_info['id'], pat)
                          ec2.terminate_instances(InstanceIds=[instance_id])
                          terminated.append(instance_id)

          # Phase 3: Clean up stale AMI builder instances (> 2 hours old)
          ami_builder_terminated = []
          ami_response = ec2.describe_instances(
              Filters=[
                  {'Name': 'tag:Name', 'Values': ['ami-builder-temp']},
                  {'Name': 'instance-state-name', 'Values': ['running', 'pending']}
              ]
          )
          for reservation in ami_response['Reservations']:
              for instance in reservation['Instances']:
                  instance_id = instance['InstanceId']
                  launch_time = instance['LaunchTime']
                  age_hours = (datetime.now(timezone.utc) - launch_time).total_seconds() / 3600
                  if age_hours > 2:
                      print(f'Terminating stale AMI builder: {instance_id} (age={age_hours:.1f}h)')
                      ec2.terminate_instances(InstanceIds=[instance_id])
                      ami_builder_terminated.append(instance_id)

          return {'terminated': terminated, 'skipped_busy': skipped_busy, 'orphans_cleaned': orphans_cleaned, 'ami_builder_terminated': ami_builder_terminated}
    EOF
    filename = "lambda_function.py"
  }
}

resource "aws_lambda_function" "runner_cleanup" {
  count            = var.enable_github_runner ? 1 : 0
  filename         = data.archive_file.runner_cleanup.output_path
  source_code_hash = data.archive_file.runner_cleanup.output_base64sha256
  function_name    = "github-runner-cleanup"
  role             = aws_iam_role.runner_lambda[0].arn
  handler          = "lambda_function.handler"
  runtime          = "python3.12"
  timeout          = 60

  tags = {
    Name = "github-runner-cleanup"
  }
}

resource "aws_cloudwatch_event_rule" "runner_cleanup" {
  count               = var.enable_github_runner ? 1 : 0
  name                = "github-runner-cleanup"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "runner_cleanup" {
  count     = var.enable_github_runner ? 1 : 0
  rule      = aws_cloudwatch_event_rule.runner_cleanup[0].name
  target_id = "runner-cleanup"
  arn       = aws_lambda_function.runner_cleanup[0].arn
}

resource "aws_lambda_permission" "runner_cleanup" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_cleanup[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.runner_cleanup[0].arn
}

# SSM Parameter for GitHub PAT (set manually)
resource "aws_ssm_parameter" "github_runner_pat" {
  count = var.enable_github_runner ? 1 : 0
  name  = "/github-runner/pat"
  type  = "SecureString"
  value = "placeholder" # Set via: aws ssm put-parameter --name /github-runner/pat --value "ghp_xxx" --type SecureString --overwrite

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "github-runner-pat"
  }
}
