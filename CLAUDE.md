# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Nested Virtualization (NV2) Kernel

**CRITICAL**: The fcvm project requires a custom kernel with DSB patches for nested virtualization.

The host kernel must have these patches from `fcvm/kernel/patches/`:
- `nv2-vsock-cache-sync.patch` - DSB in KVM nested exit path
- `nv2-vsock-rx-barrier.patch` - DSB in vsock RX path
- `mmfr4-override.patch` - ID register override for recursive nesting

**Rebuild host kernel after adding new patches:**
```bash
cd /home/ubuntu/fcvm
./kernel/build.sh  # Builds guest kernel with patches

# For HOST kernel (needs modules too):
KERNEL_VERSION=6.18.3 BUILD_DIR=/tmp/kernel-build-host ./kernel/build.sh
cd /tmp/kernel-build-host/linux-6.18.3
sudo make ARCH=arm64 modules_install
sudo cp arch/arm64/boot/Image /boot/vmlinuz-6.18.3-nested-dsb
sudo update-grub
sudo reboot
```

**Current kernel on instance:** Check with `uname -r` - should show `-nested-dsb` suffix if DSB patches are applied.

## Project Philosophy

**KISS - Keep It Simple, Stupid**

This project is opinionated and minimal:
- One way to do things (no options)
- Make-driven (no manual commands)
- Automatic dependencies (no setup steps)
- Zero configuration (sensible defaults)

## Key Principles

1. **Makefile Does Everything**: All setup, login, config creation is handled automatically via Makefile dependencies
2. **No Options**: Device code flow only, 0 ACU auto-pause only, us-west-1 only
3. **Sensible Defaults**: Everything pre-configured for maximum cost savings
4. **No Extra Docs**: README.md is the only user-facing documentation
5. **ALL AWS CHANGES VIA TERRAFORM**: Never use AWS CLI to create/modify/delete resources. Always update .tf files and run `make apply`

## Project Overview

AWS infrastructure management with:
- Terraform infrastructure-as-code
- Containerized dev environment (Podman/Docker)
- Device code authentication (paste URL in browser)
- Current configuration: Aurora Serverless v2 with 0 ACU auto-pause

## User Workflow

```bash
make init     # Auto-builds, auto-login, auto-config
make apply    # Deploy infrastructure
make connect  # Connect to database (if deployed)
```

Or just:
```bash
make apply  # Everything automatic - wake up and deploy
```

No manual build, no manual login, no manual infrastructure setup.

## Technical Details

### Automatic Dependencies

- `.container-built` - Tracks container build state, rebuilds when Dockerfile/aws-login.sh changes
- `.aws-login` - PHONY target that ALWAYS checks AWS auth, prompts for device code if expired
- `terraform.tfvars` - Auto-created from example on first use
- `~/.aws/config` - Auto-created with us-west-1 region

### Critical Design Decisions

**1. `.aws-login` must be PHONY**
- NOT a file-based dependency - it's a PHONY target
- Always runs the auth check, even if `.aws-login` file exists
- Handles session expiration automatically
- The `aws-login.sh` script does the actual credential check

**2. Container build is dependency-based**
- `.container-built` file tracks build state
- Depends on `Dockerfile` and `aws-login.sh`
- Rebuilds automatically when either changes
- Cached otherwise for speed

**3. Single command to deploy**
- `make apply` handles everything: build → login → deploy
- Extracts outputs from Terraform state
- No manual copy/paste of values
- Works after waking up with expired session

### Cost Optimization (Current Aurora Setup)

- `serverless_min_capacity = 0` - Enables automatic pause
- `seconds_until_auto_pause = 300` - 5 minutes idle before pause
- `serverless_max_capacity = 1` - Low ceiling for dev
- Result: ~$5-15/month instead of ~$45/month

### File Structure

```
.
├── Dockerfile              # Container with Terraform, AWS CLI, utilities
├── Makefile               # Everything is a make target
├── main.tf                # Infrastructure definition
├── variables.tf           # Config with sensible defaults
├── outputs.tf             # Output values
├── terraform.tfvars       # User values (auto-created, gitignored)
└── README.md             # Simple guide
```

## When Working on This Project

### Do:
- **ALWAYS use Makefile commands** - `make init`, `make apply`, `make destroy`, `make fmt`, etc.
- Keep the Makefile simple and automatic
- Use Makefile dependencies for prerequisites
- Make auth checks PHONY so they always run
- Maintain single opinionated way to do things
- Keep README.md concise
- Test "wake up" scenario (expired sessions)
- **Prefer Makefile targets over direct CLI commands** for reproducibility

### Don't:
- Add options or alternatives
- Create extra documentation files
- Add manual setup steps
- Make users think about configuration
- Cache auth state without re-checking (breaks session expiration)
- **Run raw commands when a Makefile target exists** - use `make` instead
- **NEVER add Claude Code attribution to git commits** - no "Generated with Claude Code" or "Co-Authored-By: Claude" in commit messages

### Common Pitfalls

**File-based auth tracking**: Don't do `touch .aws-login` without making it PHONY. Sessions expire but files don't.

**Container build caching**: Do track build state with `.container-built` file. Rebuilding every time is slow.

**Manual setup steps**: Don't make users copy/paste values. Extract from Terraform state automatically.

## Development Instances

Three "snowflake" dev instances with static Elastic IPs:

| Instance | Type | Purpose | Terraform |
|----------|------|---------|-----------|
| jumpbox | t4g.large | Remote management, admin AWS access | jumpbox.tf |
| fcvm-metal-arm | c7g.metal | Firecracker/KVM on ARM64 | firecracker-dev.tf |
| fcvm-metal-x86 | c5.metal | Firecracker/KVM on x86 | x86-dev.tf |

### SSH Access

```bash
# Get current IPs from terraform output
cd ~/src/aws && terraform output

# Or use the SSH commands directly
ssh -i ~/.ssh/fcvm-ec2 ubuntu@<jumpbox_public_ip>
ssh -i ~/.ssh/fcvm-ec2 ubuntu@<firecracker_dev_public_ip>
ssh -i ~/.ssh/fcvm-ec2 ubuntu@<x86_dev_public_ip>
```

### Shared Configuration

Common user_data scripts are in `dev-instance-common.tf`:
- `local.gh_auth_script` - GitHub CLI auth from Secrets Manager
- `local.claude_sync_script` - Claude Code Sync installation
- `local.gh_and_claude_sync_script` - Combined script

### GitHub PAT in Secrets Manager

GitHub authentication for private repos is stored in AWS Secrets Manager:
- **Secret name**: `github-pat-ejc3`
- **Region**: us-west-1
- **Used by**: claude-code-sync to clone private history repo

Instances fetch the token during user_data bootstrap:
```bash
GH_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id github-pat-ejc3 \
  --region us-west-1 \
  --query SecretString \
  --output text)
```

### Claude Code Sync

All dev instances have [claude-code-sync](https://github.com/ejc3/claude-code-sync) installed:
- Syncs Claude Code conversation history to GitHub
- Config: `~/.claude-code-sync-init.toml`
- Repo: `~/claude-history-sync`
- Remote: `https://github.com/ejc3/claude-code-history.git`

To sync manually:
```bash
claude-code-sync push   # Push local history to GitHub
claude-code-sync pull   # Pull history from GitHub
claude-code-sync        # Bidirectional sync (default)
```

### Elastic IPs

All dev instances have Elastic IPs for static addressing:
- IPs persist across stop/start cycles
- Defined in each instance's .tf file
- Cost: ~$3.60/month per unused EIP (free when attached to running instance)

## GitHub Actions Runners

- c7gd.metal spot instances, launched on-demand via webhook
- NVMe mounted as **btrfs at /mnt/fcvm-btrfs** (fcvm needs CoW reflinks)
- Check status: `make runners`

**Merging PRs:** Don't click "Update branch" before merging—it triggers redundant PR CI. Just merge directly.

## Common Tasks

**Add a new Terraform variable**:
1. Add to `variables.tf` with sensible default
2. Add to `terraform.tfvars.example`
3. Update README.md if user needs to change it

**Change authentication**:
Don't. Device code flow is the only way.

**Add alternative regions**:
Don't. us-west-1 is the choice.

**Add deployment options**:
Don't. 0 ACU auto-pause is the way.

## Philosophy in Action

User says: "I want options for..."
Answer: No. One opinionated way.

User says: "Can I use access keys instead of SSO?"
Answer: No. Device code flow only.

User says: "I want to configure..."
Answer: Makefile handles it automatically.

The goal is zero decisions, zero configuration, maximum simplicity.
