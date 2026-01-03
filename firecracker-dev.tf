# Firecracker Development Instance
# ARM64 metal instance for Firecracker/KVM testing
# Cost: ~$1.36/hour for c6g.metal (stop when not in use!)
#
# IMPORTANT: NV2 nested virtualization requires a custom kernel with DSB patches.
# See CLAUDE.md for kernel rebuild instructions after adding new patches to fcvm.

variable "enable_firecracker_instance" {
  description = "Enable standalone Firecracker development instance"
  type        = bool
  default     = true # Enabled - instance is imported
}

variable "firecracker_instance_type" {
  description = "Instance type for Firecracker dev"
  type        = string
  default     = "c7g.metal" # ARM64 Graviton3 metal for nested virt
}

variable "firecracker_volume_size" {
  description = "Root volume size in GB"
  type        = number
  default     = 300
}

variable "firecracker_key_name" {
  description = "SSH key pair name"
  type        = string
  default     = "fcvm-ec2"
}

# AMI for ARM64 Ubuntu - hardcoded to match imported instance
variable "firecracker_ami" {
  description = "AMI ID for Firecracker instance"
  type        = string
  default     = "ami-0094253710d975cfa" # Ubuntu 24.04 ARM64 (2025-12-12)
}

# Security group for Firecracker dev instance
resource "aws_security_group" "firecracker_dev" {
  count       = var.enable_firecracker_instance ? 1 : 0
  name_prefix = "${var.project_name}-firecracker-dev-sg-"
  description = "Security group for Firecracker development instance"
  vpc_id      = local.vpc_id

  lifecycle {
    create_before_destroy = true
  }

  # SSH access
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "SSH access"
  }

  # Eternal Terminal (persistent SSH sessions)
  ingress {
    from_port   = 2022
    to_port     = 2022
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Eternal Terminal"
  }

  # All outbound traffic for package installs, etc.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-firecracker-dev-sg"
  }
}

# Firecracker dev instance
resource "aws_instance" "firecracker_dev" {
  count         = var.enable_firecracker_instance ? 1 : 0
  ami           = var.firecracker_ami
  instance_type = var.firecracker_instance_type
  key_name      = var.firecracker_key_name

  # Network configuration
  subnet_id                   = aws_subnet.subnet_a.id
  vpc_security_group_ids      = [aws_security_group.firecracker_dev[0].id]
  associate_public_ip_address = true

  # IAM role for SSM access
  iam_instance_profile = aws_iam_instance_profile.jumpbox_admin[0].name

  # Spot instance - ~70% cheaper than on-demand
  instance_market_options {
    market_type = "spot"
    spot_options {
      instance_interruption_behavior = "stop"
      spot_instance_type             = "persistent"
    }
  }

  # Root volume - PERSISTENT (survives instance termination)
  root_block_device {
    volume_size           = var.firecracker_volume_size
    volume_type           = "gp3"
    delete_on_termination = false  # Keep EBS when instance terminates
    iops                  = 3000
    throughput            = 125
    tags = {
      Name   = "firecracker-dev-root"
      Backup = "daily"
    }
  }

  # User data - captures current instance setup for reproducibility
  user_data = base64encode(<<-EOF
    #!/bin/bash
    set -euxo pipefail

    # ============================================
    # System packages
    # ============================================
    apt-get update
    apt-get upgrade -y

    apt-get install -y \
      zsh \
      curl \
      wget \
      git \
      jq \
      build-essential \
      software-properties-common \
      podman \
      uidmap \
      slirp4netns \
      fuse-overlayfs \
      containernetworking-plugins \
      nftables \
      iproute2 \
      dnsmasq \
      cmake \
      ninja-build \
      pkg-config \
      autoconf \
      libtool \
      fuse3 \
      libfuse3-dev \
      protobuf-compiler \
      libprotobuf-dev \
      libsodium-dev \
      libcurl4-openssl-dev \
      libutempter-dev \
      unzip \
      zip \
      flex \
      bison \
      libssl-dev \
      libelf-dev \
      bc \
      dwarves

    # ============================================
    # GitHub CLI
    # ============================================
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
    apt-get update
    apt-get install -y gh

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
    # Firecracker (ARM64)
    # ============================================
    FIRECRACKER_VERSION="v1.13.1"
    ARCH="aarch64"
    wget -q -O /tmp/firecracker.tgz \
      "https://github.com/firecracker-microvm/firecracker/releases/download/$${FIRECRACKER_VERSION}/firecracker-$${FIRECRACKER_VERSION}-$${ARCH}.tgz"
    tar -xzf /tmp/firecracker.tgz -C /tmp/
    mv /tmp/release-$${FIRECRACKER_VERSION}-$${ARCH}/firecracker-$${FIRECRACKER_VERSION}-$${ARCH} /usr/local/bin/firecracker
    chmod +x /usr/local/bin/firecracker
    rm -rf /tmp/firecracker.tgz /tmp/release-$${FIRECRACKER_VERSION}-$${ARCH}
    firecracker --version

    # ============================================
    # Podman rootless config
    # ============================================
    echo "ubuntu:100000:65536" >> /etc/subuid
    echo "ubuntu:100000:65536" >> /etc/subgid
    sysctl -w kernel.unprivileged_userns_clone=1
    echo "kernel.unprivileged_userns_clone=1" >> /etc/sysctl.conf

    # ============================================
    # Shell setup for ubuntu user
    # ============================================
    sudo -u ubuntu bash << 'SHELL_SETUP'
    set -e
    mkdir -p ~/.local/bin ~/.config ~/.zsh

    # Starship prompt
    curl -sS https://starship.rs/install.sh | sh -s -- -y -b ~/.local/bin

    # Starship config
    cat > ~/.config/starship.toml << 'STARSHIP'
    # Minimal single-line prompt
    format = "$directory$git_branch$git_status$character"
    add_newline = false

    [directory]
    truncation_length = 3

    [git_branch]
    format = "[$branch]($style) "
    ignore_branches = ["main", "master"]

    [git_status]
    format = '[$all_status$ahead_behind]($style) '

    [character]
    success_symbol = "[❯](green)"
    error_symbol = "[❯](red)"
    STARSHIP

    # fzf
    git clone --depth 1 https://github.com/junegunn/fzf.git ~/.fzf
    ~/.fzf/install --all --no-bash --no-fish --key-bindings --completion --update-rc

    # Atuin (shell history)
    curl --proto '=https' --tlsv1.2 -sSf https://setup.atuin.sh | bash

    # zsh plugins
    git clone https://github.com/zsh-users/zsh-autosuggestions ~/.zsh/zsh-autosuggestions
    git clone https://github.com/zsh-users/zsh-syntax-highlighting ~/.zsh/zsh-syntax-highlighting

    # .zshrc
    cat > ~/.zshrc << 'ZSHRC'
    # Modern shell config

    # PATH for local tools
    export PATH="$HOME/.local/bin:$HOME/.atuin/bin:$HOME/.cargo/bin:$HOME/bin:$PATH"

    # History settings
    HISTFILE=~/.zsh_history
    HISTSIZE=100000
    SAVEHIST=100000
    setopt SHARE_HISTORY
    setopt HIST_IGNORE_DUPS
    setopt HIST_IGNORE_SPACE

    # Completion
    autoload -Uz compinit && compinit

    # Starship prompt
    eval "$(starship init zsh)"

    # fzf
    [ -f ~/.fzf.zsh ] && source ~/.fzf.zsh

    # Atuin (Ctrl-R history)
    command -v atuin >/dev/null && eval "$(atuin init zsh)"
    . "$HOME/.atuin/bin/env"

    # zsh-autosuggestions
    [ -f ~/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh ] && source ~/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh

    # zsh-syntax-highlighting (must be last)
    [ -f ~/.zsh/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh ] && source ~/.zsh/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh

    # Useful aliases
    alias ll="ls -la"
    alias gs="git status"
    alias gd="git diff"
    ZSHRC
    SHELL_SETUP

    # Change default shell to zsh
    chsh -s /usr/bin/zsh ubuntu

    # ============================================
    # Shell history setup
    # ============================================
    sudo -u ubuntu bash << 'HISTORY_SETUP'
    touch ~/.zsh_history
    # Import bash history into atuin
    ~/.atuin/bin/atuin import auto || true
    HISTORY_SETUP

    # ============================================
    # Claude Code
    # ============================================
    sudo -u ubuntu bash -c 'npm install -g @anthropic-ai/claude-code'

    echo "Firecracker dev instance ready!" | tee /tmp/firecracker-status
  EOF
  )

  # Monitoring
  monitoring = false

  tags = {
    Name = "fcvm-metal-arm"
  }

  # Lifecycle - prevent recreation for imported instance
  lifecycle {
    ignore_changes = [
      ami,
      user_data,
      user_data_base64,
      metadata_options,
      iam_instance_profile,
      root_block_device[0].encrypted,
      root_block_device[0].kms_key_id,
    ]
  }
}

# ============================================
# Auto-stop after 3 days idle
# ============================================

resource "aws_cloudwatch_metric_alarm" "firecracker_dev_idle" {
  count               = var.enable_firecracker_instance ? 1 : 0
  alarm_name          = "firecracker-dev-idle-3d"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 72 # 72 x 1hr = 72 hours = 3 days
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 3600 # 1 hour (required for multi-day spans)
  statistic           = "Average"
  threshold           = 5 # Less than 5% CPU = idle
  alarm_description   = "Stop Firecracker dev instance after 3 days idle"

  dimensions = {
    InstanceId = aws_instance.firecracker_dev[0].id
  }

  alarm_actions = ["arn:aws:automate:us-west-1:ec2:stop"]

  tags = {
    Name = "firecracker-dev-idle-3d"
  }
}

# Output the instance ID and connection command
output "firecracker_dev_instance_id" {
  description = "Instance ID of Firecracker dev instance"
  value       = var.enable_firecracker_instance ? aws_instance.firecracker_dev[0].id : null
}

output "firecracker_dev_public_ip" {
  description = "Public IP of Firecracker dev instance"
  value       = var.enable_firecracker_instance ? aws_instance.firecracker_dev[0].public_ip : null
}

output "firecracker_dev_ssh_command" {
  description = "Command to connect to Firecracker dev instance via SSH"
  value       = var.enable_firecracker_instance ? "ssh -i ~/.ssh/${var.firecracker_key_name} ubuntu@${aws_instance.firecracker_dev[0].public_ip}" : null
}
