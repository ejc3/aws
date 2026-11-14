# A short Cloudflare Browser Run credential for an explicitly approved benchmark.
# Ordinary applies leave only the empty administrator-only secret container.
# The token can create billable Browser Run sessions, so it is usable only in
# a fixed, at-most-one-hour UTC window and from the primary jumpbox's IPv4.
# Do not run the benchmark's checkout on the jumpbox: an administrator can use
# Cloudflare's direct REST endpoint here and export only the resulting timings.

variable "browser_run_window" {
  description = "Explicit UTC credential window for an approved Browser Run benchmark; null keeps the token disabled."
  type = object({
    starts_at  = string
    expires_at = string
  })
  default = null

  validation {
    condition = var.browser_run_window == null ? true : try(
      can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.browser_run_window.starts_at)) &&
      can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.browser_run_window.expires_at)) &&
      timecmp(var.browser_run_window.starts_at, var.browser_run_window.expires_at) < 0 &&
      timecmp(var.browser_run_window.expires_at, timeadd(var.browser_run_window.starts_at, "1h")) <= 0,
      false
    )
    error_message = "Use UTC timestamps ending in Z for a positive Browser Run window of at most one hour."
  }
}

locals {
  # Key by both boundaries so extending or scheduling a window never
  # reactivates the previous campaign's bearer.
  browser_run_active = var.browser_run_window == null ? false : timecmp(plantimestamp(), var.browser_run_window.expires_at) < 0
  browser_run_leases = local.browser_run_active ? {
    "${var.browser_run_window.starts_at}/${var.browser_run_window.expires_at}" = var.browser_run_window
  } : {}
}

data "cloudflare_account_api_token_permission_groups_list" "browser_rendering_write" {
  for_each   = local.browser_run_leases
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "Browser Run Write"
  scope      = "com.cloudflare.api.account"
}

resource "aws_secretsmanager_secret" "cloudflare_browser_run" {
  name                    = "cloudflare-browser-run"
  description             = "Administrator-only Browser Run benchmark credential, empty unless explicitly enabled."
  recovery_window_in_days = 7
  tags                    = { Name = "cloudflare-browser-run", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "cloudflare_browser_run" {
  secret_arn          = aws_secretsmanager_secret.cloudflare_browser_run.arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]
      Resource  = aws_secretsmanager_secret.cloudflare_browser_run.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "cloudflare_account_token" "browser_run" {
  for_each   = local.browser_run_leases
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "browser-run-bench-terraform"
  not_before = each.value.starts_at
  expires_on = each.value.expires_at
  condition = {
    request_ip = { in = [for ip in aws_eip.jumpbox : "${ip.public_ip}/32"] }
  }
  policies = [{
    effect = "allow"
    permission_groups = [
      for g in data.cloudflare_account_api_token_permission_groups_list.browser_rendering_write[each.key].result : { id = g.id }
      if g.name == "Browser Run Write"
    ]
    resources = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]

  lifecycle {
    precondition {
      condition     = length(aws_eip.jumpbox) == 1
      error_message = "Browser Run requires the primary jumpbox Elastic IPv4 address."
    }
    precondition {
      condition     = length([for g in data.cloudflare_account_api_token_permission_groups_list.browser_rendering_write[each.key].result : g.id if g.name == "Browser Run Write"]) == 1
      error_message = "Expected exactly one Browser Run Write account permission group."
    }
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_browser_run" {
  for_each  = local.browser_run_leases
  secret_id = aws_secretsmanager_secret.cloudflare_browser_run.id
  secret_string = jsonencode({
    account_id = var.cloudflare_account_id
    api_token  = cloudflare_account_token.browser_run[each.key].value
    expires_at = each.value.expires_at
  })
  depends_on = [aws_secretsmanager_secret_policy.cloudflare_browser_run]
}
