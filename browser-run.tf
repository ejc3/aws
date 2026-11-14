# browser-run.tf
#
# A standing Cloudflare token for Browser Run (Browser Rendering), readable from the fcvm dev box, so the fcvm render
# benchmark (bench/chromium) can call Browser Run, including its hosted Kitesurf engine (?browser=kitesurf on Quick
# Actions and on the CDP endpoint), from the dev box or from the pboxes it drives, with no jumpbox step per run
# (owner, 2026-11-14: "What is this window thing? Kill the window. We shouldn't need back and forth with jumphost").
#
#   cloudflare-browser-run   Secrets Manager (us-west-1), JSON { account_id, api_token }. Readers: administration and
#                            dev-server-role (the metal boxes), through the secret's own resource policy, because
#                            dev-server-role's inline policies are at IAM's size limit (as workers-stage.tf).
#
# The token holds one permission, Browser Run Write, on this account, and nothing else: it cannot touch Workers, DNS,
# Access, tunnels or zones. It can create billable Browser Run sessions, which is its purpose. It is not IP-pinned: the
# dev box egresses through an HTTPS proxy and its pboxes change addresses. Not readable by dev-ebs-only-role, which the
# members' pboxes share; the dev box hands the token to its own pbox's environment.
#
#   eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id cloudflare-browser-run \
#     --query SecretString --output text | jq -r '"export CLOUDFLARE_ACCOUNT_ID=\(.account_id|@sh) CLOUDFLARE_API_TOKEN=\(.api_token|@sh)"')"
#
# Same shape as cloudflare-observability.tf: a typed cloudflare_account_token minted through the token_minter provider
# alias, its value written to the container by Terraform (so it is in state; dev boxes cannot read state). ROTATE with
# `terraform apply -replace=cloudflare_account_token.browser_run`.

# The permission group is looked up by its exact name and checked to be the only match, as #853 did, so the token can
# never carry a second group.
data "cloudflare_account_api_token_permission_groups_list" "browser_rendering_write" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "Browser Run Write"
  scope      = "com.cloudflare.api.account"
}

locals {
  browser_run_readers = [aws_iam_role.dev_server.arn]
  browser_run_groups = [
    for g in data.cloudflare_account_api_token_permission_groups_list.browser_rendering_write.result : g.id
    if g.name == "Browser Run Write"
  ]
}

resource "aws_secretsmanager_secret" "cloudflare_browser_run" {
  name                    = "cloudflare-browser-run"
  description             = "Cloudflare token with Browser Run Write only (account ${var.cloudflare_account_id}), for the fcvm render benchmark. Written by Terraform from cloudflare_account_token.browser_run; see browser-run.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "cloudflare-browser-run", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "cloudflare_browser_run" {
  secret_arn          = aws_secretsmanager_secret.cloudflare_browser_run.arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "MetalBoxesRead"
        Effect    = "Allow"
        Principal = { AWS = local.browser_run_readers }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = aws_secretsmanager_secret.cloudflare_browser_run.arn
      },
      {
        Sid       = "OnlyAdministrationAndMetalBoxes"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]
        Resource  = aws_secretsmanager_secret.cloudflare_browser_run.arn
        Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.browser_run_readers) } }
      },
    ]
  })
}

resource "cloudflare_account_token" "browser_run" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "browser-run-terraform"
  policies = [{
    effect            = "allow"
    permission_groups = [for id in local.browser_run_groups : { id = id }]
    resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]

  lifecycle {
    precondition {
      condition     = length(local.browser_run_groups) == 1
      error_message = "Expected exactly one Browser Run Write account permission group."
    }
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_browser_run" {
  secret_id = aws_secretsmanager_secret.cloudflare_browser_run.id
  secret_string = jsonencode({
    account_id = var.cloudflare_account_id
    api_token  = cloudflare_account_token.browser_run.value
  })

  # The access boundary exists before the first readable token value.
  depends_on = [aws_secretsmanager_secret_policy.cloudflare_browser_run]
}
