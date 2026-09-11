# nextjs-dev.tf
#
# Small dev box for running a few `next dev` servers at once, each reachable on a
# persistent, Gmail-gated URL under dev.colton-games.com via a Cloudflare Tunnel.
#
# WHY A SEPARATE BOX from fcvm-metal-arm: the metal boxes are for Firecracker/KVM and get
# reaped by the 12h idle auto-stop. These dev servers are meant to stay up so the URLs
# keep working, and they need none of the nested-virt machinery.
#
# NO INBOUND WEB PORTS. Cloudflare Tunnel dials OUT from this box, so nothing listens
# publicly: `next dev` binds 127.0.0.1 and cloudflared forwards to it. The security group
# opens SSH and Eternal Terminal for management and nothing else, so the dev servers
# cannot be reached except through Cloudflare Access (Google login + allowlist).
#
# LEAST PRIVILEGE. This box deliberately does NOT use aws_iam_role.dev_server -- that role
# can send SSM commands to runners, stop/start EC2, read the GitHub PAT, publish to
# CodeArtifact and call Bedrock. A box serving web content to other people should not hold
# any of that. It gets its own role below with exactly two permissions: read its own setup
# script from S3, and read the one Cloudflare tunnel secret.

# RUNS PERSISTENTLY. Deliberately absent from dev-auto-stop-lambda.tf's INSTANCE_IDS
# (which reaps only the two metal boxes after 12h idle) -- these URLs are meant to keep
# working, so the box must not be reaped for being quiet. Cost is kept down by size and
# spot pricing instead: t4g.large spot is roughly $12/month running 24/7.
variable "enable_nextjs_dev" {
  description = "Run the Next.js dev box (stays up; not covered by the idle auto-stop)."
  type        = bool
  default     = true
}

variable "nextjs_instance_type" {
  description = "t4g.large: 2 vCPU / 8GB Graviton, burstable. Four accounts, each with a `next dev` plus Claude and Codex; the 4GB swapfile absorbs compile spikes rather than carrying steady state."
  type        = string
  # 8GB, raised from t4g.medium (4GB) on 2026-08-16. At 4GB the kernel paged to /swapfile,
  # swap-in reads pinned the root volume at its 125MB/s ceiling, and with the disk saturated
  # sshd could not complete a handshake and BOTH cloudflared tunnels dropped to zero
  # connections. A session then hit the OOM killer at a 6.7GB peak.
  default = "t4g.large"
}

variable "nextjs_volume_size" {
  # Extended from 100 to 200 on 2026-09-11 when the shared root filled again. Dolphin and
  # the other users keep their projects, node_modules, browser downloads, and caches here.
  description = "Root volume GB. Holds the projects and their node_modules."
  type        = number
  default     = 200
}

# ---------------------------------------------------------------------------------
# Least-privilege instance role: setup script + tunnel secret, nothing else.
# ---------------------------------------------------------------------------------
resource "aws_iam_role" "nextjs_dev" {
  name = "nextjs-dev-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  tags = { Name = "nextjs-dev-role" }
}

# SSM on every host, without exception. When sshd stopped completing its handshake on this
# box there was no second way in: no way to read load, kill a runaway, or even confirm what
# was wrong. The agent was already running (Ubuntu ships it as a snap) -- this policy is the
# piece that was missing, so it could never register.
resource "aws_iam_role_policy_attachment" "nextjs_dev_ssm" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.ssm_managed_instance.arn

  lifecycle {
    create_before_destroy = true
  }
}

# Read metadata only, never another copy of either secret payload in state.
# cloudflare-tunnel-* also matches Terraform's control-plane API token.
data "aws_secretsmanager_secret" "nextjs_connector" {
  for_each = toset(concat(
    ["cloudflare-tunnel-credentials"],
    local.dolphin_enabled ? ["cloudflare-dolphin-tunnel-credentials"] : [],
  ))
  name = each.value
}

resource "aws_iam_instance_profile" "nextjs_dev" {
  name = "nextjs-dev-profile"
  role = aws_iam_role.nextjs_dev.name
}

resource "aws_iam_role_policy" "nextjs_dev" {
  name = "nextjs-dev-policy"
  role = aws_iam_role.nextjs_dev.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadOwnSetupScript"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.dev_scripts.arn}/user-data/nextjs.sh"
      },
      {
        # Connector credentials and the dev-only hop key. The separate Access
        # service-token resources and their consumers are deliberately unchanged.
        Sid    = "ReadOwnSecrets"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = concat([
          data.aws_secretsmanager_secret.nextjs_connector["cloudflare-tunnel-credentials"].arn,
          # Hop key for reaching the other dev servers. Grants nothing beyond them: its
          # public half is never installed on the jumpbox. See dev-hop-key.tf.
          aws_secretsmanager_secret.dev_hop.arn,
          ], local.dolphin_enabled ? [
          data.aws_secretsmanager_secret.nextjs_connector["cloudflare-dolphin-tunnel-credentials"].arn,
        ] : [])
      },
      {
        # devhop-refresh resolves the other dev servers' private IPs at boot, because they
        # are spot instances and a reclaim changes the address. Read-only, and Describe*
        # cannot be resource-scoped in EC2.
        Sid      = "DescribeInstancesForHopAliases"
        Effect   = "Allow"
        Action   = "ec2:DescribeInstances"
        Resource = "*"
      }
    ]
  })
}

resource "aws_security_group" "nextjs_dev" {
  name        = "nextjs-dev"
  description = "Next.js dev box: management access only, web traffic arrives via Cloudflare Tunnel"
  vpc_id      = local.vpc_id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "SSH"
  }

  ingress {
    from_port   = 2022
    to_port     = 2022
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Eternal Terminal"
  }

  # Deliberately NO 3000-3999 / 80 / 443 ingress: cloudflared connects outbound and
  # proxies to 127.0.0.1, so opening web ports would only create a way to bypass Access.
  # ipv6_cidr_blocks is NOT optional here. The subnet assigns IPv6 addresses and the route
  # table has ::/0 to the IGW, so the box believes it has IPv6 and DNS hands back AAAA
  # records first -- but with no IPv6 egress rule every outbound v6 SYN was dropped by this
  # security group. Symptoms were slow and confusing rather than obviously "no network":
  # `aws` calls sat in SYN-SENT retransmitting for 60-90s before falling back, and
  # cloudflared logged "failed to dial to edge with quic" against 2606:4700:a0::8.
  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "All outbound, v4 and v6 (incl. the tunnel to Cloudflare)"
  }

  tags = { Name = "nextjs-dev" }
}

resource "aws_instance" "nextjs_dev" {
  count = var.enable_nextjs_dev ? 1 : 0

  ami                    = var.firecracker_ami # Ubuntu 24.04 arm64, same as the ARM dev box
  instance_type          = var.nextjs_instance_type
  key_name               = var.firecracker_key_name
  subnet_id              = aws_subnet.subnet_a.id
  vpc_security_group_ids = [aws_security_group.nextjs_dev.id]
  iam_instance_profile   = aws_iam_instance_profile.nextjs_dev.name

  # ON-DEMAND, deliberately. This box ran as spot until 2026-07-25, when it was reclaimed
  # SIX times in one day and then could not come back at all:
  #   spot request status: capacity-not-available
  #   "There is no Spot capacity available that matches your request."
  # The kids' URLs were simply down, with no ETA. Spot placement score for this shape was
  # 3/10 in every US region and for every alternative instance type (t4g/m7g/m6g/c7g/c6g,
  # even x86 t3), so neither moving region nor changing family was a way out.
  #
  # The systemd units still matter and are not made redundant by this: on-demand instances
  # reboot for host maintenance and kernel updates too. This changes how OFTEN the box goes
  # away, not whether it comes back cleanly.
  #
  # Sized down from large to medium at the same time, so the durable option costs about
  # what the unreliable one did (~$29/mo vs ~$24/mo spot) rather than $58/mo.

  root_block_device {
    volume_size           = var.nextjs_volume_size
    volume_type           = "gp3"
    delete_on_termination = false
    encrypted             = true
  }

  user_data = base64encode(<<-BOOTSTRAP
    #!/bin/bash
    # Thin bootstrap: the real script is published to S3 (local.nextjs_user_data) so it
    # can be updated without recreating the instance.
    #
    # The stock Ubuntu AMI has NO aws CLI, so install it first -- without this the very
    # first line fails with "aws: command not found" and cloud-init aborts the whole
    # user-data run (observed on first boot).
    # AWS CLI v2 -- there is NO awscli apt package on Ubuntu 24.04 (the stock AMI has
    # none), so the official installer is the only reliable route. Without this the very
    # first S3 fetch dies with "aws: command not found" and cloud-init aborts the whole
    # user-data run.
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y || true
    apt-get install -y unzip curl || true
    if ! command -v aws >/dev/null 2>&1; then
      curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
      cd /tmp && unzip -qo awscliv2.zip && ./aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip
    fi
    aws s3 cp s3://ejc3-dev-scripts/user-data/nextjs.sh /tmp/user_data.sh --region us-west-1
    chmod +x /tmp/user_data.sh && /tmp/user_data.sh
  BOOTSTRAP
  )

  lifecycle {
    ignore_changes = [
      ami,
      user_data,
      user_data_base64,
    ]
  }

  tags = {
    Name    = "nextjs-dev"
    Purpose = "Next.js dev servers behind a Cloudflare Tunnel"
    DevEBS  = "true"
  }
}

locals {
  # Root volume of the Next.js box, for AWS Backup. It holds everything that is NOT
  # reproducible from terraform: both kids' projects and git history, their separate
  # GitHub logins, and the Claude and Codex credentials. Losing it means redoing every
  # login by hand, so it needs the same protection as the other dev volumes.
  #
  # Resolved from the instance rather than hardcoded like the metal boxes' volumes: a spot
  # replacement mints a new volume id, and a hardcoded ARN would keep backing up the old
  # detached one while the live disk silently went unprotected.
  nextjs_root_volume_arn = var.enable_nextjs_dev ? "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:volume/${aws_instance.nextjs_dev[0].root_block_device[0].volume_id}" : ""
}

resource "aws_eip" "nextjs_dev" {
  count  = var.enable_nextjs_dev ? 1 : 0
  domain = "vpc"
  tags   = { Name = "nextjs-dev" }
}

resource "aws_eip_association" "nextjs_dev" {
  count         = var.enable_nextjs_dev ? 1 : 0
  instance_id   = aws_instance.nextjs_dev[0].id
  allocation_id = aws_eip.nextjs_dev[0].id
}

output "nextjs_dev_public_ip" {
  description = "Public IP of the Next.js dev box (management only; web traffic goes via the tunnel)"
  value       = var.enable_nextjs_dev ? aws_eip.nextjs_dev[0].public_ip : null
}

output "nextjs_dev_ssh_command" {
  description = "SSH into the Next.js dev box"
  value       = var.enable_nextjs_dev ? "ssh -i ~/.ssh/fcvm-ec2 ubuntu@${aws_eip.nextjs_dev[0].public_ip}" : null
}
