# browser-run.tf
#
# A Cloudflare API token for Browser Run (Browser Rendering), readable from the metal dev boxes, so fcvm's render
# benchmark (ejc3/fcvm bench/chromium) can measure Cloudflare's hosted browser as one of its arms: the same quick
# actions (screenshot, content) and the same 14-page corpus that Cloudflare's published Kitesurf table priced.
#
#   cloudflare-browser-run   Secrets Manager (us-west-1), JSON {"account_id": ..., "api_token": ...}. Readers:
#                            administration and dev-server-role (the metal boxes). Not nextjs-dev-role, whose
#                            accounts all have sudo, and not dev-ebs-only, which the pboxes run as: a client on a pbox
#                            gets the token through its environment from a metal box, never on a command line.
#
# One permission, Browser Rendering Write, on this account and nothing else: no Workers scripts, no zones, no DNS, no
# Access. Browser Run bills per browser hour on Workers Paid, so the token can spend money but cannot change anything
# that serves a site. Not IP-pinned: the metal boxes egress from several IPv4 and IPv6 addresses.
#
#   eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id cloudflare-browser-run \
#     --query SecretString --output text | jq -r '"export CLOUDFLARE_ACCOUNT_ID=\(.account_id|@sh)
#     export CLOUDFLARE_API_TOKEN=\(.api_token|@sh)"')"
#
# The permission group is looked up by its exact name through the token-minting provider, so the plan fails, rather than
# minting a token with a guessed id, if Cloudflare names it differently (the dev boxes cannot list permission groups to
# pin the id as cloudflare-observability.tf does). Same shape as workers-deploy.tf otherwise: a typed
# cloudflare_account_token, its value written to the container by Terraform (in state; dev boxes cannot read state).
# ROTATE with `terraform apply -replace=cloudflare_account_token.browser_run`.

data "cloudflare_account_api_token_permission_groups_list" "browser_rendering_write" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "Browser Rendering Write"
  scope      = "com.cloudflare.api.account"
}

locals {
  browser_run_permission_group_ids = [
    for g in data.cloudflare_account_api_token_permission_groups_list.browser_rendering_write.result : g.id
    if g.name == "Browser Rendering Write"
  ]
}

resource "aws_secretsmanager_secret" "cloudflare_browser_run" {
  name                    = "cloudflare-browser-run"
  description             = "Cloudflare Browser Run token (Browser Rendering Write only, account ${var.cloudflare_account_id}) and the account id, for fcvm's render benchmark. Written by Terraform from cloudflare_account_token.browser_run; see browser-run.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "cloudflare-browser-run", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "cloudflare_browser_run" {
  secret_arn = aws_secretsmanager_secret.cloudflare_browser_run.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "MetalBoxesRead"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.dev_server.arn }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = aws_secretsmanager_secret.cloudflare_browser_run.arn
      },
      {
        Sid       = "OnlyAdministrationAndMetalBoxes"
        Effect    = "Deny"
        Principal = "*"
        Action    = "secretsmanager:GetSecretValue"
        Resource  = aws_secretsmanager_secret.cloudflare_browser_run.arn
        Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn]) } }
      },
    ]
  })
}

resource "cloudflare_account_token" "browser_run" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "browser-run-bench-terraform"
  policies = [{
    effect            = "allow"
    permission_groups = [for id in local.browser_run_permission_group_ids : { id = id }]
    resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]

  lifecycle {
    precondition {
      condition     = length(local.browser_run_permission_group_ids) == 1
      error_message = "Expected exactly one account permission group named \"Browser Rendering Write\"."
    }
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_browser_run" {
  secret_id = aws_secretsmanager_secret.cloudflare_browser_run.id
  secret_string = jsonencode({
    account_id = var.cloudflare_account_id
    api_token  = cloudflare_account_token.browser_run.value
  })
}
