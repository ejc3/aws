# cloudflare-observability.tf
#
# A long-lived, READ-ONLY Cloudflare token for logs and metrics, readable from the dev boxes, so an agent debugging a
# Worker or a site can read what Cloudflare recorded instead of asking a person to open the dashboard (owner,
# 2026-10-10: the claude-master dashboard's 500s were in Workers Logs and no dev box could see them).
#
#   cloudflare-observability-token   Secrets Manager (us-west-1), the raw token string. Readers: administration,
#                                    dev-server-role (the metal boxes) and nextjs-dev-role (every account there has
#                                    sudo, so the grant is box-wide, as with browserbase/credentials).
#
# Every analytics, logs and observability READ permission this account uses, account-wide and on every zone:
#   account: Account Analytics Read (GraphQL Analytics API: Workers requests, CPU, errors), Workers Observability Read
#            (Workers Logs and the telemetry query API), Workers Tail Read (live `wrangler tail`), Workers Scripts Read
#            (versions and deployments, to line logs up with a release), Logs Read (Logpush jobs), Allow Request
#            Tracer Read
#   zones:   Analytics Read, Logs Read, Zone Observability Read, Health Checks Read
# Deliberately left out: Access audit and SCIM logs (they carry people's email addresses), Security Center insights,
# and anything that can write. Not IP-pinned: the dev boxes egress over IPv6 and IPv4 from several addresses.
#
#   export CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 \
#     --secret-id cloudflare-observability-token --query SecretString --output text)
#
# Same shape as workers-deploy.tf: a typed cloudflare_account_token minted through the token_minter provider alias,
# its value written to the container by Terraform (so it is in state; dev boxes cannot read state). ROTATE with
# `terraform apply -replace=cloudflare_account_token.observability_read`.
resource "aws_secretsmanager_secret" "cloudflare_observability_token" {
  name                    = "cloudflare-observability-token"
  description             = "Read-only Cloudflare API token for logs and metrics (account ${var.cloudflare_account_id} and its zones). Written by Terraform from cloudflare_account_token.observability_read; see cloudflare-observability.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "cloudflare-observability-token", Project = "dev" }
}

locals {
  cloudflare_observability_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]

  # Permission group ids (GET /accounts/<id>/tokens/permission_groups), pinned by scripts/test-cloudflare-observability.py.
  cloudflare_observability_account_groups = {
    account_analytics_read     = "b89a480218d04ceb98b4fe57ca29dc1f"
    workers_observability_read = "66c1ed49f4ed46098b75696a6d4ee3c9"
    workers_tail_read          = "05880cd1bdc24d8bae0be2136972816b"
    workers_scripts_read       = "1a71c399035b4950a1bd1466bbe4f420"
    logs_read                  = "6a315a56f18441e59ed03352369ae956"
    request_tracer_read        = "f3604047d46144d2a3e9cf4ac99d7f16"
  }
  cloudflare_observability_zone_groups = {
    analytics_read          = "9c88f9c5bce24ce7af9a958ba9c504db"
    logs_read               = "c4a30cd58c5d42619c86a3c36c441e2d"
    zone_observability_read = "69ca0c60ffc24f5386ccb38885112c44"
    health_checks_read      = "fac65912d42144aa86b7dd33281bf79e"
  }
}

resource "aws_secretsmanager_secret_policy" "cloudflare_observability_token" {
  secret_arn = aws_secretsmanager_secret.cloudflare_observability_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheDevBoxesCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.cloudflare_observability_token.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.cloudflare_observability_readers) } }
    }]
  })
}

resource "cloudflare_account_token" "observability_read" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "observability-read-terraform"
  policies = [
    {
      effect            = "allow"
      permission_groups = [for id in values(local.cloudflare_observability_account_groups) : { id = id }]
      resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
    },
    {
      effect            = "allow"
      permission_groups = [for id in values(local.cloudflare_observability_zone_groups) : { id = id }]
      resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = { "com.cloudflare.api.account.zone.*" = "*" } })
    },
  ]
}

resource "aws_secretsmanager_secret_version" "cloudflare_observability_token" {
  secret_id     = aws_secretsmanager_secret.cloudflare_observability_token.id
  secret_string = cloudflare_account_token.observability_read.value
}

data "aws_iam_policy_document" "cloudflare_observability_read" {
  statement {
    sid       = "ReadTheCloudflareObservabilityToken"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.cloudflare_observability_token.arn]
  }
}

resource "aws_iam_policy" "cloudflare_observability_read" {
  name        = "dev-cloudflare-observability-read"
  description = "Dev boxes: read the read-only Cloudflare logs and metrics token (cloudflare-observability-token) and nothing else"
  policy      = data.aws_iam_policy_document.cloudflare_observability_read.json
}

resource "aws_iam_role_policy_attachment" "cloudflare_observability_read" {
  for_each   = { dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }
  role       = each.value
  policy_arn = aws_iam_policy.cloudflare_observability_read.arn
}
