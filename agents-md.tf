# agents-md.tf
#
# The owner's user-level agent instructions live in the public repository ejc3/agents-md (owner, 2026-10-12: changes
# are proposed there, this jumpbox's agent merges them and distributes them). Terraform reads each automatically
# distributed file from that repository's main branch, puts it in the dev-scripts bucket, and an SSM association
# installs it on its machine as its account, replacing what is there:
#
#   hosts/fcvm-metal-arm/ubuntu/AGENTS.md  ->  fcvm-metal-arm, ubuntu, ~/.claude/CLAUDE.md
#   hosts/nextjs-dev/ejc3/AGENTS.md        ->  nextjs-dev, ejc3, ~/.claude/CLAUDE.md (a link to ~/.codex/AGENTS.md)
#
# and, for Codex, ~/.codex/AGENTS.override.md: the same text with ~/.claude/local.md inlined (agents_md_codex_copy).
#
# A merged change reaches its box at the next apply here (the PR watch applies after merging in ejc3/agents-md); the
# association also runs daily, so an edit made on the box is put back. The jumpbox's own file is in the repository but
# is not distributed from here: a jumpbox never takes its instructions from a path a dev box can propose to unattended.
#
# Direction: jumpbox -> bucket -> dev box. The dev boxes read their own object and write nothing back.

locals {
  agents_md_targets = {
    "fcvm-metal-arm/ubuntu" = { user = "ubuntu", instance_ids = aws_instance.firecracker_dev[*].id }
    "nextjs-dev/ejc3"       = { user = "ejc3", instance_ids = var.enable_nextjs_dev ? [local.nextjs_live_instance.id] : [] }
  }
  agents_md_github_app = jsondecode(ephemeral.aws_secretsmanager_secret_version.agents_md_github_app.secret_string)

  # Codex reads its instructions from its own home only (~/.codex/AGENTS.override.md if it exists, else
  # ~/.codex/AGENTS.md, the whole file) and does not follow Claude Code's `@~/.claude/local.md` import. On fcvm-arm
  # ~/.claude/CLAUDE.md is a file of its own, so Codex read none of the instructions; on nextjs-dev it read them but
  # not local.md (ejc3/agents-md#10). Both associations (this one and agent-local-md.tf's) therefore end by writing
  # Codex its own copy, AGENTS.override.md: CLAUDE.md with local.md inlined where CLAUDE.md imports it (the import
  # line dropped when there is no local.md), so Codex sees exactly what Claude Code sees. Written by the account,
  # inside its home, 0600 (it can hold local.md's private text), with a rename from a temporary file of its own: the
  # two associations run on their own schedules and can overlap.
  agents_md_codex_copy = <<-EOT
    claude="$home/.claude/CLAUDE.md"
    if [ -L "$claude" ]; then claude=$(readlink -f "$claude"); fi
    codex="$home/.codex/AGENTS.override.md"
    if [ -L "$codex" ]; then codex=$(readlink -f "$codex"); fi
    for p in "$claude" "$codex"; do case "$p" in "$home"/*) ;; *) echo "agents-md: $p is outside $home; not writing" >&2; exit 1 ;; esac; done
    runuser -u "$user" -- sh -c 'set -eu; [ -f "$1" ] || exit 0; mkdir -p "$(dirname "$3")"; umask 077; tmp=$(mktemp "$3.XXXXXX"); trap '"'"'rm -f "$tmp"'"'"' EXIT; while IFS= read -r line || [ -n "$line" ]; do if [ "$line" = "@~/.claude/local.md" ]; then if [ -f "$2" ]; then cat "$2"; [ -z "$(tail -c 1 "$2")" ] || echo; fi; else printf "%s\n" "$line"; fi; done < "$1" > "$tmp"; chmod 600 "$tmp"; mv "$tmp" "$3"' sh "$claude" "$home/.claude/local.md" "$codex"
    echo "agents-md: Codex copy at $codex"
  EOT
}

# The administration agent's GitHub App (github-admin-tokens.tf), installed on the ejc3 account, reads the
# repository. Read ephemerally: the key is never in state.
ephemeral "aws_secretsmanager_secret_version" "agents_md_github_app" {
  secret_id = aws_secretsmanager_secret.github_app_admin.id
}

provider "github" {
  alias = "agents_md"
  owner = "ejc3"
  app_auth {
    id              = tostring(local.agents_md_github_app.app_id)
    installation_id = tostring(local.agents_md_github_app.installations["ejc3"])
    pem_file        = local.agents_md_github_app.private_key
  }
}

data "github_repository_file" "agents_md" {
  provider   = github.agents_md
  for_each   = local.agents_md_targets
  repository = "agents-md"
  branch     = "main"
  file       = "hosts/${each.key}/AGENTS.md"
}

resource "aws_s3_object" "agents_md" {
  for_each     = local.agents_md_targets
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "agents-md/${each.key}/AGENTS.md"
  content      = data.github_repository_file.agents_md[each.key].content
  content_type = "text/markdown"
}

# nextjs-dev's role reads only its own setup script from this bucket; this adds its own instructions and nothing else.
# (dev-server-role, the metal boxes, already reads the whole bucket.)
resource "aws_iam_role_policy" "nextjs_dev_agents_md" {
  count = var.enable_nextjs_dev ? 1 : 0
  name  = "read-own-agents-md"
  role  = aws_iam_role.nextjs_dev.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadOwnAgentsMd"
      Effect   = "Allow"
      Action   = "s3:GetObject"
      Resource = "${aws_s3_bucket.dev_scripts.arn}/agents-md/nextjs-dev/*"
    }]
  })
}

# Runs when the content changes (its sha256 is in the commands) and daily. The download is checked against that sha256
# before anything is written, and the file is written by the account itself (runuser), through a symlink if the account
# has one, with a rename so a reader never sees half a file. Restarts nothing: a running session reads it next start.
resource "aws_ssm_association" "agents_md" {
  for_each = { for k, v in local.agents_md_targets : k => v if length(v.instance_ids) > 0 }

  name                = "AWS-RunShellScript"
  association_name    = "agents-md-${replace(each.key, "/", "-")}"
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
      sum='${sha256(data.github_repository_file.agents_md[each.key].content)}'
      src='s3://${aws_s3_bucket.dev_scripts.id}/${aws_s3_object.agents_md[each.key].key}'
      export PATH="$PATH:/usr/local/bin:/snap/bin"
      tmp=$(mktemp)
      trap 'rm -f "$tmp"' EXIT
      aws s3 cp "$src" "$tmp" --region ${var.aws_region} --only-show-errors
      echo "$sum  $tmp" | sha256sum -c --quiet
      chmod 0644 "$tmp"
      home=$(getent passwd "$user" | cut -d: -f6)
      [ -n "$home" ] && [ -d "$home" ]
      link="$home/.claude/CLAUDE.md"
      if [ -L "$link" ]; then target=$(readlink -f "$link"); else target="$link"; fi
      case "$target" in "$home"/*) ;; *) echo "agents-md: $target is outside $home; not writing" >&2; exit 1 ;; esac
      runuser -u "$user" -- sh -c 'set -eu; mkdir -p "$(dirname "$1")"; cp "$2" "$1.agents-md.tmp"; mv "$1.agents-md.tmp" "$1"' sh "$target" "$tmp"
      echo "agents-md: installed ${each.key} ($sum) at $target"
      ${local.agents_md_codex_copy}
    EOT
  }
}
