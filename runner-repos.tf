# runner-repos.tf
#
# Self-hosted runners for repos other than ejc3/fcvm. Today the runner system serves exactly
# one repo on bare metal; CoderColton/colton-games and dolphin-labs-hq/dolphin-labs are being
# added on ordinary x86 spot instances (their jobs need no KVM).
#
# STEP 1 (this file, for now): each repo's controller token. A runner registration token needs
# repo ADMIN, so each repo's owner mints a fine-grained token limited to that one repo with
# Administration RW (register, list, remove runners) and Webhooks RW (the workflow_job hook):
#   CoderColton/colton-games       minted by CoderColton (ejc3 has write, not admin there)
#   dolphin-labs-hq/dolphin-labs   minted by ejc3 as org admin
# Same pattern as /github-runner/pat: Terraform creates the parameter with a placeholder and
# never manages the value, so the token is not in state. Set it with:
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /github-runner/repo-pat/<owner>/<repo> --value file:///dev/stdin
# Only the runner Lambdas may read it (runner-autoscale.tf). The runner INSTANCE role is denied
# every parameter outside /github-runner/bootstrap/* (runner-vpc.tf), so a CI job cannot read it.
locals {
  runner_extra_repos = ["CoderColton/colton-games", "dolphin-labs-hq/dolphin-labs"]
}

resource "aws_ssm_parameter" "github_runner_repo_pat" {
  for_each = var.enable_github_runner ? toset(local.runner_extra_repos) : toset([])

  name  = "/github-runner/repo-pat/${each.value}"
  type  = "SecureString"
  value = "placeholder"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "github-runner-repo-pat" }
}
