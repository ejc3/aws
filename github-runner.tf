# GitHub Actions Self-Hosted Runner
# ARM64 spot instance for CI/CD
# Cost: ~$0.50-0.70/hour for c7g.metal spot (vs $2.88 on-demand)

variable "enable_github_runner" {
  description = "Enable GitHub Actions runner spot instance"
  type        = bool
  default     = true
}

variable "github_runner_instance_type" {
  description = "Instance type for GitHub runner"
  type        = string
  default     = "c7g.metal"  # Same as dev instance for consistency
}

variable "github_runner_spot_price" {
  description = "Maximum spot price (leave empty for on-demand price cap)"
  type        = string
  default     = ""  # No cap - pay market price
}

# Spot instance request for GitHub runner
resource "aws_spot_instance_request" "github_runner" {
  count                = var.enable_github_runner ? 1 : 0
  ami                  = var.firecracker_ami
  instance_type        = var.github_runner_instance_type
  key_name             = var.firecracker_key_name
  spot_price           = var.github_runner_spot_price != "" ? var.github_runner_spot_price : null
  wait_for_fulfillment = true
  spot_type            = "persistent"

  # Network configuration
  subnet_id                   = aws_subnet.subnet_a.id
  vpc_security_group_ids      = [aws_security_group.firecracker_dev[0].id]
  associate_public_ip_address = true

  # IAM role with admin access (same as jumpbox for flexibility)
  iam_instance_profile = aws_iam_instance_profile.jumpbox_admin[0].name

  # Root volume
  root_block_device {
    volume_size           = 100  # Smaller than dev instance
    volume_type           = "gp3"
    delete_on_termination = true
    iops                  = 3000
    throughput            = 125
  }

  # User data for GitHub runner setup
  user_data = base64encode(<<-EOF
    #!/bin/bash
    set -euxo pipefail

    # ============================================
    # System packages
    # ============================================
    apt-get update
    apt-get upgrade -y

    apt-get install -y \
      curl \
      wget \
      git \
      jq \
      build-essential \
      podman \
      uidmap \
      slirp4netns \
      fuse-overlayfs \
      containernetworking-plugins

    # ============================================
    # Node.js 22.x
    # ============================================
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs

    # ============================================
    # Rust via rustup
    # ============================================
    sudo -u ubuntu bash -c 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y'

    # ============================================
    # GitHub Actions Runner
    # ============================================
    mkdir -p /opt/actions-runner
    cd /opt/actions-runner

    # Download latest runner
    RUNNER_VERSION=$(curl -s https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name' | sed 's/v//')
    curl -o actions-runner-linux-arm64.tar.gz -L "https://github.com/actions/runner/releases/download/v$${RUNNER_VERSION}/actions-runner-linux-arm64-$${RUNNER_VERSION}.tar.gz"
    tar xzf actions-runner-linux-arm64.tar.gz
    rm actions-runner-linux-arm64.tar.gz

    # Set ownership
    chown -R ubuntu:ubuntu /opt/actions-runner

    # Install dependencies
    ./bin/installdependencies.sh

    # ============================================
    # Podman rootless config
    # ============================================
    echo "ubuntu:100000:65536" >> /etc/subuid
    echo "ubuntu:100000:65536" >> /etc/subgid

    echo "GitHub runner ready for configuration!" | tee /tmp/runner-status
    echo "To configure, run: cd /opt/actions-runner && ./config.sh --url https://github.com/OWNER/REPO --token TOKEN"
  EOF
  )

  tags = {
    Name = "github-runner"
  }

  # Lifecycle
  lifecycle {
    ignore_changes = [
      user_data,
      user_data_base64,
    ]
  }
}

# Tag the spot instance (spot requests don't propagate tags automatically)
resource "aws_ec2_tag" "github_runner_name" {
  count       = var.enable_github_runner ? 1 : 0
  resource_id = aws_spot_instance_request.github_runner[0].spot_instance_id
  key         = "Name"
  value       = "github-runner"

  depends_on = [aws_spot_instance_request.github_runner]
}

# Outputs
output "github_runner_instance_id" {
  description = "Instance ID of GitHub runner"
  value       = var.enable_github_runner ? aws_spot_instance_request.github_runner[0].spot_instance_id : null
}

output "github_runner_public_ip" {
  description = "Public IP of GitHub runner"
  value       = var.enable_github_runner ? aws_spot_instance_request.github_runner[0].public_ip : null
}

output "github_runner_spot_request_id" {
  description = "Spot request ID"
  value       = var.enable_github_runner ? aws_spot_instance_request.github_runner[0].id : null
}

output "github_runner_ssh_command" {
  description = "SSH command for GitHub runner"
  value       = var.enable_github_runner ? "ssh -i ~/.ssh/${var.firecracker_key_name} ubuntu@${aws_spot_instance_request.github_runner[0].public_ip}" : null
}
