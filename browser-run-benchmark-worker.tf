# The Ohio benchmark worker reads a separate Browser Run credential directly.
# The existing ARM token and ordinary/member pbox roles have no new access.
locals {
  browser_run_worker_grant = local.benchmark_worker_enabled ? {
    role         = aws_iam_role.benchmark_worker[0].arn
    vpc          = local.benchmark_worker_vpc_id
    private_ipv4 = local.benchmark_worker_private_ipv4
    public_ipv4  = local.benchmark_worker_public_ipv4
    public_ipv6  = local.benchmark_worker_ipv6_cidr
  } : null
  browser_run_worker_ips = local.browser_run_worker_grant == null ? [] : [
    format("%s/32", local.browser_run_worker_grant.public_ipv4),
    local.browser_run_worker_grant.public_ipv6,
  ]
  browser_run_worker_secret_arn = try(aws_secretsmanager_secret.cloudflare_browser_run_worker[0].arn, null)
  browser_run_worker_policy_json = local.browser_run_worker_grant == null ? jsonencode({
    Version = "2012-10-17", Statement = [],
    }) : jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "OnlyAdministrationAndBenchmarkWorker"
        Effect    = "Deny"
        Principal = "*"
        Action    = local.browser_run_read_actions
        Resource  = local.browser_run_worker_secret_arn
        Condition = {
          ArnNotLike = {
            "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [local.browser_run_worker_grant.role])
          }
        }
      },
      {
        Sid       = "ReadFromBenchmarkWorker"
        Effect    = "Allow"
        Principal = { AWS = local.browser_run_worker_grant.role }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = local.browser_run_worker_secret_arn
        Condition = {
          StringEquals = { "aws:Ec2InstanceSourceVpc" = local.browser_run_worker_grant.vpc }
          IpAddress    = { "aws:Ec2InstanceSourcePrivateIPv4" = format("%s/32", local.browser_run_worker_grant.private_ipv4) }
        }
      },
      {
        Sid       = "DenyBenchmarkRoleWithoutWorkerVpc"
        Effect    = "Deny"
        Principal = "*"
        Action    = local.browser_run_read_actions
        Resource  = local.browser_run_worker_secret_arn
        Condition = {
          ArnEquals               = { "aws:PrincipalArn" = local.browser_run_worker_grant.role }
          StringNotEqualsIfExists = { "aws:Ec2InstanceSourceVpc" = local.browser_run_worker_grant.vpc }
        }
      },
      {
        Sid       = "DenyBenchmarkRoleWithoutWorkerPrivateAddress"
        Effect    = "Deny"
        Principal = "*"
        Action    = local.browser_run_read_actions
        Resource  = local.browser_run_worker_secret_arn
        Condition = {
          ArnEquals            = { "aws:PrincipalArn" = local.browser_run_worker_grant.role }
          NotIpAddressIfExists = { "aws:Ec2InstanceSourcePrivateIPv4" = format("%s/32", local.browser_run_worker_grant.private_ipv4) }
        }
      },
    ]
  })
}

resource "aws_secretsmanager_secret" "cloudflare_browser_run_worker" {
  count                   = local.benchmark_worker_enabled ? 1 : 0
  name                    = "cloudflare-browser-run-worker"
  description             = "Browser Run Write credential for the dedicated Ohio FCVM benchmark worker."
  recovery_window_in_days = 7
  tags                    = { Name = "cloudflare-browser-run-worker", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "cloudflare_browser_run_worker" {
  count               = local.benchmark_worker_enabled ? 1 : 0
  secret_arn          = aws_secretsmanager_secret.cloudflare_browser_run_worker[count.index].arn
  block_public_policy = true
  policy              = local.browser_run_worker_policy_json
}

resource "cloudflare_account_token" "browser_run_worker" {
  count      = local.benchmark_worker_enabled ? 1 : 0
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "browser-run-worker-terraform"
  condition = {
    request_ip = { in = local.browser_run_worker_ips }
  }
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
    precondition {
      condition = (
        can(cidrnetmask(local.browser_run_worker_ips[0])) &&
        try(strcontains(local.browser_run_worker_ips[1], ":") && endswith(local.browser_run_worker_ips[1], "/128"), false)
      )
      error_message = "Browser Run needs the dedicated worker's Elastic IPv4 /32 and host IPv6 /128."
    }
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_browser_run_worker" {
  count     = local.benchmark_worker_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.cloudflare_browser_run_worker[count.index].id
  secret_string = jsonencode({
    account_id = var.cloudflare_account_id
    api_token  = cloudflare_account_token.browser_run_worker[count.index].value
  })
  depends_on = [aws_secretsmanager_secret_policy.cloudflare_browser_run_worker]
}
