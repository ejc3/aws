# nextjs-ct.tf
#
# The replacement for nextjs-dev: the same shared box, with every account (ejc3 included) in a
# container of its own. Owner, 2026-10-18: "put all the user state in a container per person and
# have the natural sshd for each user end up in their account just like now", built as a NEW box
# ("get this new box fully perfect and then we will just stop everything and move the IP").
#
# WHY: every account on nextjs-dev has passwordless sudo, so anything placed for one account (a
# login, an SSH key to the owner's MacBook) is readable by all of them. On this box the host is an
# admin and routing layer nobody works in; each person gets a systemd-nspawn container with their
# home bind-mounted, root inside it and nowhere else, their own network namespace and their own
# AWS session. The container layer itself is nextjs-ct-user-data.tf.
#
# THIS FILE ONLY BUILDS THE BOX. It runs next to nextjs-dev and serves nothing until the cutover:
# no Elastic IP, no tunnel connector (cloudflared stays stopped here until the cutover, or the
# live tunnels would split traffic between the two boxes), a Name tag no consumer keys on. The
# cutover (Elastic IP, Name tag, alarms, backups, SSM targets) is a separate change that waits for
# the owner's go; see the pull request that adds the container layer.
#
# It uses nextjs-dev's own instance role on purpose. Every Secrets Manager resource policy that
# admits "nextjs-dev-role" by ARN then admits this box too, and the per-container sessions are
# sessions of that same role narrowed by a session policy (below), so a resource policy never has
# to learn a second principal.

variable "enable_nextjs_ct" {
  description = "Build the containerized replacement for nextjs-dev (serves nothing until the cutover)."
  type        = bool
  default     = true
}

variable "nextjs_ct_volume_size" {
  # nextjs-dev's root held 347 GB on 2026-10-18 (its homes, 400 GB volume at 90%). This box holds
  # the same homes plus one container root per person (a few GB each), with room to grow.
  description = "Root volume GB for the containerized nextjs box: the homes and every container root."
  type        = number
  default     = 600
}

resource "aws_instance" "nextjs_ct" {
  count = var.enable_nextjs_ct ? 1 : 0

  ami                    = var.firecracker_ami # Ubuntu 24.04 arm64, the same image as nextjs-dev
  instance_type          = var.nextjs_instance_type
  key_name               = var.firecracker_key_name
  subnet_id              = aws_subnet.subnet_a.id
  vpc_security_group_ids = [aws_security_group.nextjs_dev.id]
  iam_instance_profile   = aws_iam_instance_profile.nextjs_dev.name

  # subnet_a does not map public addresses, and this box has no Elastic IP until the cutover moves
  # nextjs-dev's to it. Without a public address it could not reach S3, GitHub or apt at all.
  associate_public_ip_address = true

  # IMDSv2 only, and one hop: the containers never talk to the real metadata service. Their
  # traffic to 169.254.169.254 is redirected to the host's per-container credential server
  # (nextjs-ct-imds), which hands each container its own narrowed session.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_size           = var.nextjs_ct_volume_size
    volume_type           = "gp3"
    delete_on_termination = false
    encrypted             = true
  }

  # Thin bootstrap, as nextjs-dev's: the real script is published to S3 so it can change without
  # replacing the instance. setup-sync on the box applies a newly published one.
  user_data = base64encode(<<-BOOTSTRAP
    #!/bin/bash
    export DEBIAN_FRONTEND=noninteractive
    for i in $(seq 1 30); do
      if ! command -v aws >/dev/null 2>&1; then
        apt-get -o DPkg::Lock::Timeout=600 update -y && apt-get -o DPkg::Lock::Timeout=600 install -y unzip curl \
          && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip \
          && (cd /tmp && unzip -qo awscliv2.zip && ./aws/install) ; rm -rf /tmp/aws /tmp/awscliv2.zip
      fi
      command -v aws >/dev/null 2>&1 && break
      sleep 10
    done
    # Retried until it succeeds: the first boot can race the network and the role's new S3 grant, and
    # without this script nothing installs setup-sync, the only thing that could recover later.
    # The ETag that was downloaded is handed to the script, which records it once it is healthy, so
    # setup-sync's first tick does not run the same script again.
    for i in $(seq 1 120); do
      if META=$(aws s3api get-object --bucket ejc3-dev-scripts --key user-data/nextjs-ct.sh /tmp/user_data.sh --region us-west-1 --output json); then
        break
      fi
      META=""
      sleep $(( i < 12 ? i * 5 : 60 ))
    done
    [ -n "$META" ] || { echo "nextjs-ct bootstrap: could not fetch the setup script" >&2; exit 1; }
    export NEXTJS_CT_ETAG=$(printf '%s' "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ETag"])')
    chmod +x /tmp/user_data.sh && /tmp/user_data.sh
  BOOTSTRAP
  )

  # The first boot fetches the script and the role must already be able to read it.
  depends_on = [aws_s3_object.nextjs_ct_user_data, aws_iam_role_policy.nextjs_dev]

  lifecycle {
    ignore_changes = [ami, user_data, user_data_base64]
  }

  # Takes nextjs-dev's name at the cutover, so everything that finds the box by name follows it: the
  # auto-reboot Lambda, claw-master's certificate renewal, the hop aliases on the metal boxes.
  tags = {
    Name    = local.nextjs_ct_serving ? "nextjs-dev" : "nextjs-ct"
    Purpose = "Containerized replacement for nextjs-dev (one container per person)"
  }
}

# ---------------------------------------------------------------------------------
# The cutover switch: local.nextjs_ct_serving (nextjs-ct-user-data.tf). Everything keyed to "the nextjs
# box" reads local.nextjs_live_instance, so flipping that one value moves the Elastic IP, the Name tag,
# the memory and disk alarms, the backup selection and the agents-md target together, starts this
# box's tunnels and agents, and stops (never terminates) nextjs-dev. Flipping it back is the rollback.
# ---------------------------------------------------------------------------------
locals {
  nextjs_live_instance = local.nextjs_ct_serving ? one(aws_instance.nextjs_ct) : one(aws_instance.nextjs_dev)
}

# Applies the switch on this box at once and checks it took (tunnels up when serving, down when not), so
# the address moves and nextjs-dev stops or starts only afterwards: never dead URLs while setup-sync's
# timer catches up, and never both boxes on one tunnel during a rollback.
resource "terraform_data" "nextjs_ct_converge" {
  count = var.enable_nextjs_ct ? 1 : 0

  triggers_replace = {
    serving  = local.nextjs_ct_serving
    instance = aws_instance.nextjs_ct[0].id
    helper   = filesha256("${path.module}/scripts/ssm-nextjs-ct-converge.sh")
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/ssm-nextjs-ct-converge.sh ${aws_instance.nextjs_ct[0].id} ${var.aws_region} ${local.nextjs_ct_serving}"
  }

  depends_on = [aws_s3_object.nextjs_ct_user_data]
}

resource "aws_ec2_instance_state" "nextjs_dev" {
  count       = var.enable_nextjs_dev && var.enable_nextjs_ct ? 1 : 0
  instance_id = aws_instance.nextjs_dev[0].id
  state       = local.nextjs_ct_serving ? "stopped" : "running"

  depends_on = [terraform_data.nextjs_ct_converge]
}

output "nextjs_ct_public_ip" {
  description = "Public IP of the containerized nextjs box (before the cutover; it then takes nextjs-dev's Elastic IP)"
  value       = var.enable_nextjs_ct ? aws_instance.nextjs_ct[0].public_ip : null
}

# ---------------------------------------------------------------------------------
# Per-container AWS sessions
#
# The host's credential server assumes nextjs-dev-role AGAIN for each container, with a session
# policy that removes what that person must not have (nextjs-ct-user-data.tf). A session's rights
# are the role's policies intersected with its session policy, so a container can never hold more
# than the box does today, and every grant added to nextjs-dev-role later reaches the containers
# without a second list to keep in step. aws:PrincipalArn of such a session is still the role ARN,
# so the Secrets Manager resource policies that admit nextjs-dev-role keep working unchanged.
#
# The role must name itself in its trust policy for this (an IAM role is never implicitly trusted
# by itself), and its identity policy allows the call. Both are scoped to this one role ARN.
# ---------------------------------------------------------------------------------
locals {
  nextjs_dev_role_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/nextjs-dev-role"
}

resource "aws_iam_role_policy" "nextjs_ct_self_assume" {
  name = "assume-self-for-container-sessions"
  role = aws_iam_role.nextjs_dev.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AssumeSelfWithASessionPolicyPerContainer"
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = local.nextjs_dev_role_arn
    }]
  })
}
