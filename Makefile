.PHONY: help shell init plan apply destroy output clean test connect fmt dev-start dev-stop dev-ssh dev-status runners .aws-login .check-aws-cli .check-podman ssh-jumpbox ssh-arm ssh-x86

# Container runtime (podman or docker)
CONTAINER_RUNTIME := $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null || echo /opt/homebrew/bin/podman)
PWD := $(shell pwd)
AWS_DIR := $(HOME)/.aws
ENV_FILE := $(PWD)/.env

ifneq (,$(wildcard $(ENV_FILE)))
include $(ENV_FILE)
export $(shell sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' $(ENV_FILE))
# Pass all env vars from .env to container via --env-file
CONTAINER_ENV_ARGS := --env-file $(ENV_FILE)
else
CONTAINER_ENV_ARGS :=
endif

# Default target
help:
	@echo "Available targets:"
	@echo "  make fmt         - Format Terraform files"
	@echo "  make init        - Initialize Terraform"
	@echo "  make plan        - Run Terraform plan"
	@echo "  make apply       - Deploy the infrastructure"
	@echo "  make connect     - Connect to MySQL database (IAM auth)"
	@echo "  make output      - Show Terraform outputs"
	@echo "  make destroy     - Destroy the infrastructure"
	@echo "  make shell       - Start an interactive shell"
	@echo "  make clean       - Clean everything"
	@echo "  make test        - Test container tools"
	@echo ""
	@echo "SSH to Dev Instances (direct via Elastic IP):"
	@echo "  make ssh-jumpbox - SSH to jumpbox"
	@echo "  make ssh-arm     - SSH to fcvm-metal-arm"
	@echo "  make ssh-x86     - SSH to fcvm-metal-x86"
	@echo ""
	@echo "Development Instance (legacy SSM):"
	@echo "  make dev-start   - Start dev instance (creates if needed)"
	@echo "  make dev-stop    - Stop dev instance (persists disk)"
	@echo "  make dev-ssh     - SSH into dev instance via SSM"
	@echo "  make dev-status  - Show dev instance status"
	@echo ""
	@echo "GitHub Runners:"
	@echo "  make runners     - List all GitHub runner instances"

# Ensure AWS CLI is installed via Homebrew (required for SSO)
.check-aws-cli:
	@if [ ! -f /opt/homebrew/bin/aws ]; then \
		echo "AWS CLI not found. Installing via Homebrew..."; \
		brew install awscli; \
		echo "AWS CLI installed successfully"; \
	fi

# Ensure Podman machine is running
.check-podman:
	@if command -v podman >/dev/null 2>&1; then \
		if ! podman machine inspect 2>/dev/null | grep -q '"State": "running"'; then \
			echo "Starting Podman machine..."; \
			podman machine start; \
		fi \
	fi

# Ensure AWS config exists
$(AWS_DIR)/config:
	@mkdir -p $(AWS_DIR)
	@echo "[default]" > $(AWS_DIR)/config
	@echo "region = us-west-1" >> $(AWS_DIR)/config
	@echo "output = json" >> $(AWS_DIR)/config

# Check if AWS credentials work, login if needed
.aws-login: .check-aws-cli $(AWS_DIR)/config .container-built
	@$(CONTAINER_RUNTIME) run -it --rm \
		-v $(AWS_DIR):/root/.aws \
		$(CONTAINER_ENV_ARGS) \
		--entrypoint /usr/local/bin/aws-login.sh \
		aws-dev

# Ensure terraform.tfvars exists (create if missing)
terraform.tfvars:
	@if [ ! -f terraform.tfvars ]; then \
		echo "# Terraform variables - edit as needed" > terraform.tfvars; \
		echo "# Password is auto-generated via random_password resource" >> terraform.tfvars; \
		echo "# IAM authentication enabled - use IAM tokens for connections" >> terraform.tfvars; \
		echo "Created terraform.tfvars"; \
	fi

# Build the container (internal target)
.container-built: .check-podman Dockerfile aws-login.sh
	$(CONTAINER_RUNTIME) build -t aws-dev .
	@touch .container-built

# Start interactive shell
shell: .aws-login
	$(CONTAINER_RUNTIME) run -it --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		--entrypoint /bin/bash \
		aws-dev

# Terraform commands (run in container)
fmt: .container-built
	$(CONTAINER_RUNTIME) run --rm \
		-v $(PWD):/workspace \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		fmt

init: .aws-login terraform.tfvars
	$(CONTAINER_RUNTIME) run --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		init

plan: .aws-login terraform.tfvars
	$(CONTAINER_RUNTIME) run --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		plan

apply: .aws-login terraform.tfvars
	$(CONTAINER_RUNTIME) run -it --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		apply -auto-approve

destroy: .aws-login
	$(CONTAINER_RUNTIME) run -it --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		destroy -auto-approve

output: .aws-login
	$(CONTAINER_RUNTIME) run --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		output

# Connect with IAM authentication
connect: .aws-login
	@echo "Connecting to MySQL database using IAM authentication..."
	@echo "NOTE: This requires IAM policies to be configured for the user"
	@$(CONTAINER_RUNTIME) run -it --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		--entrypoint sh \
		aws-dev -c ' \
			ENDPOINT=$$(terraform output -raw db_address 2>/dev/null) && \
			DB_NAME=$$(terraform output -raw db_name 2>/dev/null) && \
			DB_USER="iamuser" && \
			if [ -z "$$ENDPOINT" ]; then \
				echo "ERROR: Database not deployed. Run \"make apply\" first."; \
				exit 1; \
			fi && \
			echo "Getting IAM auth token for $$ENDPOINT..." && \
			TOKEN=$$(aws rds generate-db-auth-token \
				--hostname $$ENDPOINT \
				--port 3306 \
				--username $$DB_USER \
				--region us-west-1) && \
			echo "Connecting to $$ENDPOINT as $$DB_USER using IAM token..." && \
			echo "Note: Aurora may take 15-30 seconds to resume from paused state..." && \
			mariadb --connect-timeout=60 -h $$ENDPOINT -u $$DB_USER --password="$$TOKEN" --ssl-ca=/usr/local/share/amazon-rds-ca-cert.pem --ssl $$DB_NAME \
		'

# Test container tools
test:
	@echo "Testing container tools..."
	$(CONTAINER_RUNTIME) run --rm --entrypoint /bin/bash mysql-aws-dev -c \
		"terraform --version && echo '---' && aws --version && echo '---' && mysql --version"

# Clean everything
clean:
	rm -rf .terraform .terraform.lock.hcl terraform.tfstate terraform.tfstate.backup *.tfplan .aws-login .container-built

# Development instance management
dev-start: .aws-login
	@echo "Starting development instance..."
	@INSTANCE_ID=$$($(CONTAINER_RUNTIME) run --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		output -raw firecracker_dev_instance_id 2>/dev/null) && \
	if [ -z "$$INSTANCE_ID" ] || [ "$$INSTANCE_ID" = "null" ]; then \
		echo "Dev instance not deployed. Run: make apply" && exit 1; \
	fi && \
	STATE=$$(aws ec2 describe-instances --instance-ids $$INSTANCE_ID --query 'Reservations[0].Instances[0].State.Name' --output text --region $(shell grep 'aws_region' terraform.tfvars 2>/dev/null | cut -d'"' -f2 || echo us-west-1)) && \
	if [ "$$STATE" = "stopped" ]; then \
		echo "Starting instance $$INSTANCE_ID..." && \
		aws ec2 start-instances --instance-ids $$INSTANCE_ID --region $(shell grep 'aws_region' terraform.tfvars 2>/dev/null | cut -d'"' -f2 || echo us-west-1) && \
		echo "Waiting for instance to be running..." && \
		aws ec2 wait instance-running --instance-ids $$INSTANCE_ID --region $(shell grep 'aws_region' terraform.tfvars 2>/dev/null | cut -d'"' -f2 || echo us-west-1) && \
		echo "Instance started successfully"; \
	elif [ "$$STATE" = "running" ]; then \
		echo "Instance already running"; \
	else \
		echo "Instance is in state: $$STATE"; \
	fi

dev-stop: .check-aws-cli .container-built
	@echo "Stopping development instance..."
	@INSTANCE_ID=$$($(CONTAINER_RUNTIME) run --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		output -raw firecracker_dev_instance_id 2>/dev/null) && \
	if [ -z "$$INSTANCE_ID" ] || [ "$$INSTANCE_ID" = "null" ]; then \
		echo "Dev instance not deployed" && exit 1; \
	fi && \
	aws ec2 stop-instances --instance-ids $$INSTANCE_ID --region $(shell grep 'aws_region' terraform.tfvars 2>/dev/null | cut -d'"' -f2 || echo us-west-1) && \
	echo "Instance $$INSTANCE_ID stopped (disk persists)"

dev-ssh: .check-aws-cli .container-built
	@echo "Connecting to development instance..."
	@INSTANCE_ID=$$($(CONTAINER_RUNTIME) run --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		output -raw firecracker_dev_instance_id 2>/dev/null) && \
	if [ -z "$$INSTANCE_ID" ] || [ "$$INSTANCE_ID" = "null" ]; then \
		echo "Dev instance not deployed. Run: make apply" && exit 1; \
	fi && \
	echo "Starting SSM session to $$INSTANCE_ID..." && \
	aws ssm start-session --target $$INSTANCE_ID --region $(shell grep 'aws_region' terraform.tfvars 2>/dev/null | cut -d'"' -f2 || echo us-west-1)

dev-status: .check-aws-cli .container-built
	@INSTANCE_ID=$$($(CONTAINER_RUNTIME) run --rm \
		-v $(PWD):/workspace \
		-v $(AWS_DIR):/root/.aws:ro \
		$(CONTAINER_ENV_ARGS) \
		aws-dev \
		output -raw firecracker_dev_instance_id 2>/dev/null) && \
	if [ -z "$$INSTANCE_ID" ] || [ "$$INSTANCE_ID" = "null" ]; then \
		echo "Dev instance not deployed"; \
	else \
		REGION=$$(grep 'aws_region' terraform.tfvars 2>/dev/null | cut -d'"' -f2); \
		[ -z "$$REGION" ] && REGION=us-west-1; \
		echo "Instance ID: $$INSTANCE_ID" && \
		aws ec2 describe-instances --instance-ids $$INSTANCE_ID --region $$REGION \
			--query 'Reservations[0].Instances[0].[InstanceId,InstanceType,State.Name,PublicIpAddress]' \
			--output table; \
	fi

# GitHub runner management
runners:
	@echo "GitHub Runner Instances:"
	@aws ec2 describe-instances \
		--filters "Name=tag:Name,Values=github-runner,github-runner-autoscale" \
		--query 'Reservations[*].Instances[*].[Tags[?Key==`Name`].Value|[0],InstanceId,InstanceType,State.Name,PrivateIpAddress,LaunchTime]' \
		--output table --region us-west-1

# Direct SSH to dev instances (uses Elastic IPs from terraform output)
ssh-jumpbox:
	@IP=$$(terraform output -raw jumpbox_public_ip 2>/dev/null) && \
	if [ -z "$$IP" ] || [ "$$IP" = "null" ]; then \
		echo "Jumpbox not deployed or no Elastic IP. Run: make apply"; exit 1; \
	fi && \
	ssh -i ~/.ssh/fcvm-ec2 ubuntu@$$IP

ssh-arm:
	@IP=$$(terraform output -raw firecracker_dev_public_ip 2>/dev/null) && \
	if [ -z "$$IP" ] || [ "$$IP" = "null" ]; then \
		echo "ARM instance not deployed or no Elastic IP. Run: make apply"; exit 1; \
	fi && \
	ssh -i ~/.ssh/fcvm-ec2 ubuntu@$$IP

ssh-x86:
	@IP=$$(terraform output -raw x86_dev_public_ip 2>/dev/null) && \
	if [ -z "$$IP" ] || [ "$$IP" = "null" ]; then \
		echo "x86 instance not deployed or no Elastic IP. Run: make apply"; exit 1; \
	fi && \
	ssh -i ~/.ssh/fcvm-ec2 ubuntu@$$IP
