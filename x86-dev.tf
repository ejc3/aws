# x86 Development Instance
# Intel metal instance for x86-specific testing
# Cost: ~$0.77/hour spot for c5.metal

variable "enable_x86_dev_instance" {
  description = "Enable x86 development instance"
  type        = bool
  default     = true
}

variable "x86_dev_instance_type" {
  description = "Instance type for x86 dev"
  type        = string
  default     = "c5.metal" # 96 vCPU, 192GB RAM
}

variable "x86_dev_volume_size" {
  description = "Root volume size in GB"
  type        = number
  default     = 300
}

# AMI for x86 Ubuntu 24.04
data "aws_ami" "ubuntu_x86" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# Security group for x86 dev instance (reuse firecracker_dev SG pattern)
resource "aws_security_group" "x86_dev" {
  count       = var.enable_x86_dev_instance ? 1 : 0
  name_prefix = "${var.project_name}-x86-dev-sg-"
  description = "Security group for x86 development instance"
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

  # All outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-x86-dev-sg"
  }
}

# x86 dev instance
resource "aws_instance" "x86_dev" {
  count         = var.enable_x86_dev_instance ? 1 : 0
  ami           = data.aws_ami.ubuntu_x86.id
  instance_type = var.x86_dev_instance_type
  key_name      = var.firecracker_key_name

  # Network configuration - same VPC as ARM dev
  subnet_id                   = aws_subnet.subnet_a.id
  vpc_security_group_ids      = [aws_security_group.x86_dev[0].id]
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

  # Root volume
  root_block_device {
    volume_size           = var.x86_dev_volume_size
    volume_type           = "gp3"
    delete_on_termination = false
    iops                  = 3000
    throughput            = 125
    tags = {
      Name   = "x86-dev-root"
      Backup = "daily"
    }
  }

  # User data - same as ARM but with x86 Firecracker
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
      zip

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
    # Firecracker (x86_64)
    # ============================================
    FIRECRACKER_VERSION="v1.13.1"
    ARCH="x86_64"
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
    ~/.atuin/bin/atuin import auto || true
    HISTORY_SETUP

    # ============================================
    # Claude Code
    # ============================================
    npm install -g @anthropic-ai/claude-code

    echo "x86 dev instance ready!" | tee /tmp/x86-dev-status
  EOF
  )

  monitoring = false

  tags = {
    Name = "fcvm-metal-x86"
  }
}

# ============================================
# Auto-stop after 3 days idle
# ============================================

resource "aws_cloudwatch_metric_alarm" "x86_dev_idle" {
  count               = var.enable_x86_dev_instance ? 1 : 0
  alarm_name          = "x86-dev-idle-3d"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 72 # 72 x 1hr = 3 days
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 3600
  statistic           = "Average"
  threshold           = 5
  alarm_description   = "Stop x86 dev instance after 3 days idle"

  dimensions = {
    InstanceId = aws_instance.x86_dev[0].id
  }

  alarm_actions = ["arn:aws:automate:us-west-1:ec2:stop"]

  tags = {
    Name = "x86-dev-idle-3d"
  }
}

# Outputs
output "x86_dev_instance_id" {
  description = "Instance ID of x86 dev instance"
  value       = var.enable_x86_dev_instance ? aws_instance.x86_dev[0].id : null
}

output "x86_dev_public_ip" {
  description = "Public IP of x86 dev instance"
  value       = var.enable_x86_dev_instance ? aws_instance.x86_dev[0].public_ip : null
}

output "x86_dev_ssh_command" {
  description = "Command to connect to x86 dev instance via SSH"
  value       = var.enable_x86_dev_instance ? "ssh -i ~/.ssh/${var.firecracker_key_name} ubuntu@${aws_instance.x86_dev[0].public_ip}" : null
}
