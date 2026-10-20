# agent-local-md.tf
#
# Each distributed account's MACHINE-LOCAL agent instructions, ~/.claude/local.md, which its ~/.claude/CLAUDE.md
# (agents-md.tf) imports with `@~/.claude/local.md`. They hold what must not go into a public file (ejc3/agents-md and this
# repository are both public): fcvm-arm's describes the agent browser and the owner's sessions in it. So the text is NOT
# in git. Each one is an administration-only secret, seeded from the live file by an administrator; Terraform reads it,
# puts it in the dev-scripts bucket next to the account's AGENTS.md, and an SSM association installs it as the account,
# as agents-md.tf does for AGENTS.md:
#
#   agent-local-md/fcvm-metal-arm/ubuntu  ->  fcvm-metal-arm, ubuntu, ~/.claude/local.md
#   agent-local-md/nextjs-dev/ejc3        ->  nextjs-dev, ejc3, ~/.claude/local.md
#
# so a rebuilt box gets its file back, and an edit made on the box is put back daily. Change one by putting a new value
# in its secret (from a 0600 file, never a command line) and applying.
#
# COLD BOOTSTRAP. A data source cannot read a secret that has no value yet, so each target has a checked-in gate in
# local.agent_local_md_ready: apply with it false (the container is created), put the value, set it true, apply.
#
# WHO READS WHAT. The secret: administration only. The bucket object: administration and that box's role only (a bucket
# policy narrows dev-server-role's whole-bucket read, which fcvm-x86 shares, to fcvm-arm's own instance, and keeps each
# box off the other's file). On the containerized nextjs box no container can read the bucket at all (nextjs-ct's
# HostOnlyData denial).

locals {
  # Flip one to true only after its secret has a value (see COLD BOOTSTRAP above).
  agent_local_md_ready = {
    "fcvm-metal-arm/ubuntu" = false
    "nextjs-dev/ejc3"       = false
  }

  # The role that may read each box's object and, when the role is shared, the one instance of it that may. A shared
  # role with no such instance (the box disabled) gets an ARN no instance has, so the denial stays and fails closed.
  agent_local_md_readers = {
    "fcvm-metal-arm/ubuntu" = { role = aws_iam_role.dev_server.arn, shared = true, instances = aws_instance.firecracker_dev[*].arn }
    "nextjs-dev/ejc3"       = { role = aws_iam_role.nextjs_dev.arn, shared = false, instances = [] }
  }
  agent_local_md_no_instance = "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/none"

  agent_local_md_live = { for k, v in local.agents_md_targets : k => v if local.agent_local_md_ready[k] }
}

resource "aws_secretsmanager_secret" "agent_local_md" {
  for_each                = local.agents_md_targets
  name                    = "agent-local-md/${each.key}"
  description             = "~/.claude/local.md for ${each.key}: machine-local agent instructions, kept out of git (agent-local-md.tf). Value set out of band."
  recovery_window_in_days = 7
  tags                    = { Name = "agent-local-md/${each.key}", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_policy" "agent_local_md" {
  for_each   = local.agents_md_targets
  secret_arn = aws_secretsmanager_secret.agent_local_md[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.agent_local_md[each.key].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

data "aws_secretsmanager_secret_version" "agent_local_md" {
  for_each  = local.agent_local_md_live
  secret_id = aws_secretsmanager_secret.agent_local_md[each.key].id
}

resource "aws_s3_object" "agent_local_md" {
  for_each     = local.agent_local_md_live
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "agents-md/${each.key}/local.md"
  content      = data.aws_secretsmanager_secret_version.agent_local_md[each.key].secret_string
  content_type = "text/markdown"
}

# Explicit denials on the local.md objects only; nothing else in the bucket is affected.
resource "aws_s3_bucket_policy" "dev_scripts" {
  bucket = aws_s3_bucket.dev_scripts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [for k, r in local.agent_local_md_readers : {
        Sid       = "LocalMdOnlyItsBox${replace(title(replace(k, "/[^a-z0-9]+/", " ")), " ", "")}"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.dev_scripts.arn}/agents-md/${k}/local.md"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [r.role]) } }
      }],
      [for k, r in local.agent_local_md_readers : {
        Sid       = "LocalMdOnlyItsInstance${replace(title(replace(k, "/[^a-z0-9]+/", " ")), " ", "")}"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.dev_scripts.arn}/agents-md/${k}/local.md"
        Condition = {
          ArnEquals       = { "aws:PrincipalArn" = r.role }
          StringNotEquals = { "ec2:SourceInstanceARN" = length(r.instances) > 0 ? r.instances : [local.agent_local_md_no_instance] }
        }
      } if r.shared],
    )
  })
}

# Runs when the content changes (its sha256 is in the commands) and daily, exactly as agents-md.tf's association: the
# download is checked against that sha256, and the file is written by the account itself, through a symlink if it has
# one (inside its home only), with a rename. Restarts nothing: a running session reads it at its next start.
resource "aws_ssm_association" "agent_local_md" {
  for_each = { for k, v in local.agent_local_md_live : k => v if length(v.instance_ids) > 0 }

  name                = "AWS-RunShellScript"
  association_name    = "agent-local-md-${replace(each.key, "/", "-")}"
  schedule_expression = "rate(1 day)"
  max_concurrency     = "1"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = each.value.instance_ids
  }

  parameters = {
    executionTimeout = "120"
    commands         = <<-EOT
      set -eu
      user='${each.value.user}'
      sum='${nonsensitive(sha256(data.aws_secretsmanager_secret_version.agent_local_md[each.key].secret_string))}'
      src='s3://${aws_s3_bucket.dev_scripts.id}/${aws_s3_object.agent_local_md[each.key].key}'
      export PATH="$PATH:/usr/local/bin:/snap/bin"
      tmp=$(mktemp)
      trap 'rm -f "$tmp"' EXIT
      aws s3 cp "$src" "$tmp" --region ${var.aws_region} --only-show-errors
      echo "$sum  $tmp" | sha256sum -c --quiet
      chown "$user" "$tmp"
      chmod 0600 "$tmp"
      home=$(getent passwd "$user" | cut -d: -f6)
      [ -n "$home" ] && [ -d "$home" ]
      link="$home/.claude/local.md"
      if [ -L "$link" ]; then target=$(readlink -f "$link"); else target="$link"; fi
      case "$target" in "$home"/*) ;; *) echo "agent-local-md: $target is outside $home; not writing" >&2; exit 1 ;; esac
      runuser -u "$user" -- sh -c 'set -eu; mkdir -p "$(dirname "$1")"; cp "$2" "$1.agent-local-md.tmp"; chmod 600 "$1.agent-local-md.tmp"; mv "$1.agent-local-md.tmp" "$1"' sh "$target" "$tmp"
      echo "agent-local-md: installed ${each.key} ($sum) at $target"
      ${local.agents_md_codex_copy}
    EOT
  }
}

output "agent_local_md_secrets" {
  description = "The administration-only secrets holding each distributed account's ~/.claude/local.md (names only)"
  value       = { for k, s in aws_secretsmanager_secret.agent_local_md : k => s.name }
}
