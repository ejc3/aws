# ---------------------------------------------------------------------------------
# The claude-master dashboard site (ejc3/claude-master-dashboard): a Cloudflare Worker that
# reads the ClawMaster CloudWatch namespace, ClaudeMaster before the rename (claude-master-dashboard.tf is the console
# dashboard over the same metrics).
#
# The same split as workers-stage.tf: the repository's Deploy workflow (Wrangler, with the shared
# token from workers-deploy.tf) owns the Worker's code and deployments; this file owns what the
# deploy must not:
#
#   1. Cloudflare Access on the Worker itself (a worker destination, so workers.dev and preview
#      URLs alike), reusing this account's Google and one-time PIN identity providers (no new
#      OAuth client). Its policy admits the owner and the people the owner named for it (the
#      `claude_master_dashboard` list in people/addresses: Colton and Connor, 2026-10-10), never
#      the family-wide cc-games allowlist as such: the dashboard names subscription accounts and
#      the people using them. The app checks the Access assertion again, against its own
#      DASHBOARD_ALLOWED_EMAILS, which must name the same people.
#   2. The Worker's URL switches, which turn on only after Access covers it.
#   3. A read-only AWS key for the Worker, which cannot assume an AWS role (Workers have no
#      OIDC identity AWS accepts). It may only read CloudWatch metrics in this region.
#
# ORDER, because a Worker's Access destination is its immutable id and that exists only after the
# first deploy (as in workers-stage.tf):
#   1. The repository deploys with workers_dev and preview_urls false: the Worker exists with no
#      public URL. Nothing below is created while the id is empty.
#   2. Set id (GET /accounts/<id>/workers/workers, or `wrangler deployments`) with urls = false
#      and apply: Access adopts the Worker; nothing becomes reachable.
#   3. Set urls = true here and workers_dev true in the repository's wrangler.jsonc; apply.
# Never reverse 2 and 3. After step 2, set the Worker's secrets from output
# "claude_master_dashboard" (docs/deploy.md in that repository).
# ---------------------------------------------------------------------------------

locals {
  claude_master_dashboard_worker = { name = "claude-master-dashboard", id = "a3016d88fb32412aa5fff7e271eb17a3", urls = true }
  # Zero or one entry: empty until the first deploy has created the Worker and its id is set.
  claude_master_dashboard_workers = local.claude_master_dashboard_worker.id == "" ? {} : {
    (local.claude_master_dashboard_worker.name) = local.claude_master_dashboard_worker
  }
}

resource "cloudflare_zero_trust_access_policy" "claude_master_dashboard_owner" {
  account_id       = var.cloudflare_account_id
  name             = "claude-master dashboard: owner"
  decision         = "allow"
  session_duration = "24h"

  include = concat(
    [{ email = { email = nonsensitive(local.people.owner) } }],
    [for e in try(nonsensitive(local.people.claude_master_dashboard), []) : { email = { email = e } }],
  )
}

resource "cloudflare_zero_trust_access_application" "claude_master_dashboard" {
  for_each         = local.claude_master_dashboard_workers
  account_id       = var.cloudflare_account_id
  name             = "claude-master dashboard"
  type             = "self_hosted"
  session_duration = "24h"

  # A replacement would get a new audience tag, and the Worker would refuse everyone until its
  # CF_ACCESS_AUD secret is set again; URLs must be off before this protection can go.
  lifecycle {
    prevent_destroy = true
  }

  destinations = [{
    type      = "worker"
    worker_id = each.value.id
  }]

  # The same login buttons as the cc-games sites, and no others.
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )

  # Declared here, not only on the policy: without the attachment an apply would leave the
  # Worker with no Access check at all.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.claude_master_dashboard_owner.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.claude_master_dashboard_agents.id
      precedence = 2
    },
  ]
}

# Terraform owns only the Worker's two URL switches, as for the staging Workers.
import {
  for_each = local.claude_master_dashboard_workers
  to       = cloudflare_workers_script_subdomain.claude_master_dashboard[each.key]
  id       = "${var.cloudflare_account_id}/${each.key}"
}

resource "cloudflare_workers_script_subdomain" "claude_master_dashboard" {
  for_each         = local.claude_master_dashboard_workers
  account_id       = var.cloudflare_account_id
  script_name      = each.key
  enabled          = each.value.urls
  previews_enabled = false

  depends_on = [
    cloudflare_workers_subdomain.cc_games,
    cloudflare_zero_trust_access_application.claude_master_dashboard,
  ]
}

# ---------------------------------------------------------------------------------
# Read-only CloudWatch access for the Worker.
# ---------------------------------------------------------------------------------
resource "aws_iam_user" "claude_master_dashboard" {
  name = "claude-master-dashboard-reader"
  tags = { Name = "claude-master-dashboard-reader", Managed = "terraform" }
}

resource "aws_iam_user_policy" "claude_master_dashboard" {
  name = "read-claude-master-metrics"
  user = aws_iam_user.claude_master_dashboard.name

  # CloudWatch has no resource-level permissions for these reads, so they cannot be narrowed
  # to one namespace; the dashboard queries only ClawMaster. The region condition keeps the
  # key to the region the metrics live in.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "ReadMetrics"
      Effect    = "Allow"
      Action    = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
      Resource  = "*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      }, {
      # The Alarms box reads only alarms named claude-master*; DescribeAlarms is scoped to those ARNs (#384).
      Sid      = "ReadClaudeMasterAlarms"
      Effect   = "Allow"
      Action   = "cloudwatch:DescribeAlarms"
      Resource = "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:claude-master*"
    }]
  })
}

resource "aws_iam_access_key" "claude_master_dashboard" {
  user = aws_iam_user.claude_master_dashboard.name
}

resource "aws_secretsmanager_secret" "claude_master_dashboard_reader" {
  name        = "claude-master-dashboard/aws-reader"
  description = "Read-only CloudWatch key for the claude-master dashboard Worker (set as its Worker secrets)"
  tags        = { Name = "claude-master-dashboard/aws-reader", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "claude_master_dashboard_reader" {
  secret_id = aws_secretsmanager_secret.claude_master_dashboard_reader.id
  secret_string = jsonencode({
    AWS_ACCESS_KEY_ID     = aws_iam_access_key.claude_master_dashboard.id
    AWS_SECRET_ACCESS_KEY = aws_iam_access_key.claude_master_dashboard.secret
    AWS_REGION            = var.aws_region
  })
}

# ---------------------------------------------------------------------------------
# Agents on the metal boxes (#374): a service token of their own, so an agent can read what the dashboard shows
# (`/api/claude-master/snapshot`) and check a deploy, as the owner does in a browser. The token is admitted by a
# non_identity policy after the owner's; the Worker admits exactly its client id (DASHBOARD_SERVICE_TOKEN_CLIENT_ID,
# matched against the assertion's common_name) and still checks every person against DASHBOARD_ALLOWED_EMAILS.
# Readers: dev-server-role only (the metal boxes), which already reads the ClawMaster metrics and the
# /claude-master/server log the dashboard is built from, so the page shows it nothing new. Not nextjs-dev-role:
# every account there has sudo. Not the cc-games automation token or the Macs' token: revoking this touches nothing else.
resource "cloudflare_zero_trust_access_service_token" "claude_master_dashboard_agents" {
  account_id = var.cloudflare_account_id
  name       = "claude-master-dashboard-agents"
  duration   = "8760h"

  lifecycle {
    # A replacement changes the client id the Worker admits: set DASHBOARD_SERVICE_TOKEN_CLIENT_ID again after it.
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "claude_master_dashboard_agents" {
  account_id       = var.cloudflare_account_id
  name             = "claude-master dashboard: metal-box agents"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.claude_master_dashboard_agents.id
    }
  }]
}

resource "aws_secretsmanager_secret" "claude_master_dashboard_agent_access" {
  name        = "claude-master-dashboard/agent-access"
  description = "Cloudflare Access service token for metal-box agents to read the claude-master dashboard (client_id, client_secret)"
  tags        = { Name = "claude-master-dashboard/agent-access", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "claude_master_dashboard_agent_access" {
  secret_id = aws_secretsmanager_secret.claude_master_dashboard_agent_access.id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.claude_master_dashboard_agents.client_id
    client_secret = cloudflare_zero_trust_access_service_token.claude_master_dashboard_agents.client_secret
  })
}

resource "aws_secretsmanager_secret_policy" "claude_master_dashboard_agent_access" {
  secret_arn = aws_secretsmanager_secret.claude_master_dashboard_agent_access.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndMetalBoxes"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.claude_master_dashboard_agent_access.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn]) } }
    }]
  })
}

resource "aws_iam_role_policy" "dev_server_claude_master_dashboard_access" {
  name = "claude-master-dashboard-agent-access"
  role = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.claude_master_dashboard_agent_access.arn
    }]
  })
}

output "claude_master_dashboard" {
  description = "What the claude-master dashboard Worker's secrets need (docs/deploy.md in ejc3/claude-master-dashboard)"
  value = {
    url                   = local.claude_master_dashboard_worker.urls ? "https://${local.claude_master_dashboard_worker.name}.${cloudflare_workers_subdomain.cc_games.subdomain}.workers.dev/claude-master" : "no public URL"
    cf_access_team_domain = trimsuffix(local.cf_access_callback, "/cdn-cgi/access/callback")
    cf_access_aud = try(
      cloudflare_zero_trust_access_application.claude_master_dashboard[local.claude_master_dashboard_worker.name].aud,
      "not yet: set the Worker id (step 2)",
    )
    aws_reader_secret = aws_secretsmanager_secret.claude_master_dashboard_reader.name
    # The Worker secret DASHBOARD_SERVICE_TOKEN_CLIENT_ID (not secret: the client secret is the credential).
    agent_service_token_client_id = cloudflare_zero_trust_access_service_token.claude_master_dashboard_agents.client_id
    agent_access_secret           = aws_secretsmanager_secret.claude_master_dashboard_agent_access.name
  }
}
