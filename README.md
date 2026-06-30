# AWS Infrastructure for fcvm

Terraform infrastructure for [ejc3/fcvm](https://github.com/ejc3/fcvm) CI/CD and development.

## Infrastructure

| Resource | Type | Purpose |
|----------|------|---------|
| **github-runner** | c7g.metal spot | GitHub Actions self-hosted runner (ARM64) |
| **firecracker-dev** | c7g.metal | Development instance with KVM |
| **jumpbox** | t4g.large | Persistent admin host |

## Auto-scaling Runners

GitHub Actions runners automatically scale based on demand:

- **Scale out**: Webhook triggers Lambda → launches spot instance when jobs queue
- **Scale in**: Cleanup Lambda stops idle runners (CPU < 5% for 30 mins)
- **Max runners**: 3 concurrent (configurable)
- **Cost**: ~$0.50-0.70/hr per c7g.metal spot

```
Job queued → Lambda launches spot → Runner picks up job → Idle 30m → Stopped
```

## Commands

```bash
# Infrastructure
make init        # Initialize Terraform
make plan        # Preview changes
make apply       # Deploy infrastructure
make output      # Show outputs

# Development instance
make dev-start   # Start firecracker-dev instance
make dev-stop    # Stop instance (preserves disk)
make dev-status  # Show instance status

# GitHub runners
make runners     # List all runner instances
```

## Cost

| Resource | Cost | Notes |
|----------|------|-------|
| github-runner | ~$0.50/hr | Spot, auto-stops after 30m idle |
| firecracker-dev | ~$2.88/hr | On-demand, auto-stops after 3 days idle |
| jumpbox | ~$0.07/hr | Always on |

**Idle runners cost $0** - they're stopped automatically.

## Files

```
main.tf              # VPC, subnet, routing
firecracker-dev.tf   # Dev instance + 3-day idle alarm
github-runner.tf     # Base runner + 30m idle alarm
runner-autoscale.tf  # Lambda webhook + cleanup
jumpbox.tf           # Admin host + backups
```

## Setup

1. Clone and initialize:
   ```bash
   git clone https://github.com/ejc3/aws-setup.git
   cd aws-setup
   make init
   make apply
   ```

2. GitHub webhook (already configured for ejc3/fcvm):
   - URL: `https://epteb3h8ia.execute-api.us-west-1.amazonaws.com/webhook`
   - Events: `workflow_job`

3. GitHub PAT stored in SSM:
   ```bash
   aws ssm put-parameter --name /github-runner/pat --value "ghp_xxx" --type SecureString --overwrite
   ```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        VPC (10.0.0.0/16)                    │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                  Subnet A (10.0.1.0/24)               │  │
│  │                                                       │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐ │  │
│  │  │ jumpbox  │  │ fc-dev   │  │ github-runner (1-3)  │ │  │
│  │  │ t4g.large│  │ c7g.metal│  │ c7g.metal spot       │ │  │
│  │  └──────────┘  └──────────┘  └──────────────────────┘ │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │   API Gateway     │
                    │   (webhook)       │
                    └─────────┬─────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
     ┌────────┴────────┐             ┌────────┴────────┐
     │ Lambda: webhook │             │ Lambda: cleanup │
     │ (scale out)     │             │ (scale in)      │
     └─────────────────┘             └─────────────────┘
```
