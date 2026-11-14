# browser-run.tf
#
# A standing Browser Run-only token for the fcvm render benchmark (hosted Chromium and Kitesurf Quick Actions).
# Read it on the primary fcvm-arm dev box for the benchmark process. Keep it out of arguments, logs and files.
# The secret resource policy allows administration and dev-server-role credentials issued to the primary ARM
# instance. The Cloudflare token also accepts requests only from that box's Elastic IPv4 and host IPv6.
# There is no per-run jumpbox step; the hosted benchmark runs on fcvm-arm. Browser usage is billable.
# The token is written by Terraform to Secrets Manager and is in state; dev boxes cannot read state.
# Rotate with terraform apply -replace='cloudflare_account_token.browser_run[0]'.
#
# Exactly one permission group: Browser Run Write on this Cloudflare account.
data "cloudflare_account_api_token_permission_groups_list" "browser_rendering_write" {
  count      = var.enable_firecracker_instance ? 1 : 0
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "Browser Run Write"
  scope      = "com.cloudflare.api.account"
}

# The host IPv6 is assigned after the ENI is created. Re-read the ENI after that step instead of
# using the empty snapshot from a newly created interface.
data "aws_network_interface" "browser_run_arm" {
  count = var.enable_firecracker_instance ? 1 : 0
  id    = aws_network_interface.firecracker_dev[count.index].id

  depends_on = [terraform_data.firecracker_dev_host_ipv6]
}

locals {
  browser_run_dev_role = aws_iam_role.dev_server.arn
  browser_run_arm_arns = aws_instance.firecracker_dev[*].arn
  browser_run_arm_ipv4 = [for eip in aws_eip.firecracker_dev_next : "${eip.public_ip}/32"]
  browser_run_arm_ipv6 = flatten([
    for eni in data.aws_network_interface.browser_run_arm : [for ip in eni.ipv6_addresses : "${ip}/128"]
  ])
  browser_run_read_actions = [
    "secretsmanager:GetSecretValue",
    "secretsmanager:BatchGetSecretValue",
  ]
  browser_run_groups = [
    for g in try(data.cloudflare_account_api_token_permission_groups_list.browser_rendering_write[0].result, []) : g.id
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
    Statement = concat(
      [{
        Sid       = "DenyOtherPrincipals"
        Effect    = "Deny"
        Principal = "*"
        Action    = local.browser_run_read_actions
        Resource  = aws_secretsmanager_secret.cloudflare_browser_run.arn
        Condition = {
          ArnNotLike = {
            "aws:PrincipalArn" = concat(
              local.games_mp_admin_principals,
              length(local.browser_run_arm_arns) == 1 ? [local.browser_run_dev_role] : [],
            )
          }
        }
      }],
      [for instance_arn in local.browser_run_arm_arns : {
        Sid       = "ReadFromPrimaryArmCredentials"
        Effect    = "Allow"
        Principal = { AWS = local.browser_run_dev_role }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = aws_secretsmanager_secret.cloudflare_browser_run.arn
        Condition = { ArnEquals = { "ec2:SourceInstanceARN" = instance_arn } }
      }],
      [for instance_arn in local.browser_run_arm_arns : {
        # An identity-policy Allow added later must not bypass the instance restriction.
        Sid       = "DenyDevRoleWithoutPrimaryArmCredentials"
        Effect    = "Deny"
        Principal = "*"
        Action    = local.browser_run_read_actions
        Resource  = aws_secretsmanager_secret.cloudflare_browser_run.arn
        Condition = {
          ArnEquals            = { "aws:PrincipalArn" = local.browser_run_dev_role }
          ArnNotEqualsIfExists = { "ec2:SourceInstanceARN" = instance_arn }
        }
      }],
    )
  })
}

resource "cloudflare_account_token" "browser_run" {
  count      = var.enable_firecracker_instance ? 1 : 0
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "browser-run-terraform"
  condition = {
    request_ip = { in = concat(local.browser_run_arm_ipv4, local.browser_run_arm_ipv6) }
  }
  policies = [{
    effect            = "allow"
    permission_groups = [for id in local.browser_run_groups : { id = id }]
    resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]

  lifecycle {
    precondition {
      condition     = length(local.browser_run_arm_ipv4) == 1 && length(local.browser_run_arm_ipv6) == 1
      error_message = "Browser Run needs exactly one primary ARM Elastic IPv4 and host IPv6 address."
    }
    precondition {
      condition     = length(local.browser_run_groups) == 1
      error_message = "Expected exactly one Browser Run Write account permission group."
    }
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_browser_run" {
  count     = var.enable_firecracker_instance ? 1 : 0
  secret_id = aws_secretsmanager_secret.cloudflare_browser_run.id
  secret_string = jsonencode({
    account_id = var.cloudflare_account_id
    api_token  = cloudflare_account_token.browser_run[0].value
  })

  # The access boundary exists before the first readable token value.
  depends_on = [aws_secretsmanager_secret_policy.cloudflare_browser_run]
}
