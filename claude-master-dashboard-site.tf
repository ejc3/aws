# ---------------------------------------------------------------------------------
# The claude-master dashboard site (ejc3/claude-master-dashboard): a Cloudflare Worker that
# reads the ClaudeMaster CloudWatch namespace (claude-master-dashboard.tf is the console
# dashboard over the same metrics).
#
# The Worker itself -- its code, versions and deployments -- is owned by Wrangler in that
# repository's Deploy workflow. This file owns only what stands around it:
#
#   1. Cloudflare Access in front of every URL of the Worker, reusing this account's Google and
#      one-time PIN identity providers (no new OAuth client). The policy admits the owner only:
#      the dashboard names subscription accounts and the people using them, which is not for
#      the family-wide cc-games allowlist. The app checks the Access assertion again.
#   2. A read-only AWS key for the Worker, which cannot assume an AWS role (Workers have no
#      OIDC identity AWS accepts). It may only read CloudWatch metrics in this region.
#
# After the first apply, set the Worker's secrets from the outputs (ejc3/claude-master-dashboard
# docs/deploy.md); its Deploy workflow needs a Workers-scripts token minted as AGENTS.md
# "Minting tokens" describes, stored as that repository's CLOUDFLARE_API_TOKEN secret.
# ---------------------------------------------------------------------------------

locals {
  claude_master_dashboard_worker = "claude-master-dashboard"
  # wrangler.jsonc turns on workers.dev for the Worker; preview URLs stay off.
  claude_master_dashboard_host = "${local.claude_master_dashboard_worker}.${cloudflare_workers_subdomain.cc_games.subdomain}.workers.dev"
}

resource "cloudflare_zero_trust_access_policy" "claude_master_dashboard_owner" {
  account_id       = var.cloudflare_account_id
  name             = "claude-master dashboard: owner"
  decision         = "allow"
  session_duration = "24h"

  include = [{ email = { email = nonsensitive(local.people.owner) } }]
}

resource "cloudflare_zero_trust_access_application" "claude_master_dashboard" {
  account_id       = var.cloudflare_account_id
  name             = "claude-master dashboard"
  type             = "self_hosted"
  session_duration = "24h"

  destinations = [{
    type = "public"
    uri  = local.claude_master_dashboard_host
  }]

  # The same login buttons as the cc-games sites, and no others.
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )

  # Declared here, not only on the policy: without the attachment an apply would leave the
  # hostname with no Access check at all.
  policies = [{
    id         = cloudflare_zero_trust_access_policy.claude_master_dashboard_owner.id
    precedence = 1
  }]
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
  # to one namespace; the dashboard queries only ClaudeMaster. The region condition keeps the
  # key to the region the metrics live in.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "ReadMetrics"
      Effect    = "Allow"
      Action    = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
      Resource  = "*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
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

output "claude_master_dashboard" {
  description = "What the claude-master dashboard Worker's secrets need (docs/deploy.md in ejc3/claude-master-dashboard)"
  value = {
    url                   = "https://${local.claude_master_dashboard_host}/claude-master"
    cf_access_team_domain = trimsuffix(local.cf_access_callback, "/cdn-cgi/access/callback")
    cf_access_aud         = cloudflare_zero_trust_access_application.claude_master_dashboard.aud
    aws_reader_secret     = aws_secretsmanager_secret.claude_master_dashboard_reader.name
  }
}
