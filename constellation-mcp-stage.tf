# constellation-mcp-stage.tf
#
# Lattice's remote MCP server on staging (ejc3/constellation apps/mcp, docs/AUTH.md "MCP clients"): a Cloudflare Worker,
# constellation-mcp-stage, that MCP clients (claude.ai connectors, Claude Code) reach over Streamable HTTP at
#
#   https://lattice-mcp-stage.dolphin-labs.dev/mcp  -> the Worker -> https://lattice-stage.dolphin-labs.dev/lattice/v1/*
#
# The Worker is its own OAuth 2.1 authorization server (the MCP authorization spec) and signs people in through an Access
# for SaaS (OIDC) application. That application, not a self-hosted Access gate, is who gets in: an MCP client must reach
# the Worker's discovery, registration and token endpoints with no Access session, as Lattice's apps reach the bypassed
# /lattice/v1 paths with the API's own token (constellation-lattice-stage.tf). The sign-in is OWNER-ONLY, like the
# staging host; opening it is the owner's call.
#
# What lives where:
#   - here: the OIDC application, the Worker's OAuth KV namespace, its hostname, its runtime secrets (in AWS, as for the
#     other staging Workers, workers-stage-secrets.tf), and the cloudflare-deploy environment its repository deploys from
#     (workers-deploy.tf, local.workers_deploy_environments_ejc3).
#   - in ejc3/constellation: the code, deployed by Wrangler from main only through that environment.
#
# The Worker asks the API for a Lattice token for the person it signed in (POST /lattice/v1/token), presenting a shared
# secret. Terraform makes that secret and writes it to both sides: the Worker's container below, and the API's
# constellation/nonprod/lattice-service in the constellation account (its container is constellation's own, admin-secret
# module; only reads are restricted there). The staging task reads it at start, so it takes effect on its next deployment.
#
# ORDER, because a custom domain needs the Worker to exist and the Worker exists only after its first deploy:
#   1. Apply this file with constellation_mcp_stage_deployed = false: the OIDC application, the KV namespace, the
#      secrets and the deploy environment. Nothing is reachable.
#   2. constellation's deploy workflow (main) creates the Worker, with workers_dev and preview_urls false.
#   3. Set constellation_mcp_stage_deployed = true and apply: the hostname goes to the Worker. Then load its secrets with
#      `scripts/workers-stage-secrets.sh constellation-mcp` (Worker constellation-mcp-stage).

locals {
  constellation_mcp_stage_worker   = "constellation-mcp-stage" # ejc3/constellation apps/mcp/wrangler.jsonc, top level
  constellation_mcp_stage_hostname = "lattice-mcp-stage.dolphin-labs.dev"
  # Step 3 above: true since the Worker exists (first deploy 2026-10-23, ejc3/constellation deploy-mcp). Set it false
  # before deleting the Worker, so the hostname goes first.
  constellation_mcp_stage_deployed = true

  # Access for SaaS (OIDC) endpoints are per application, under the team domain and the application's client id.
  constellation_mcp_oidc_base = "${local.browser_manager_issuer}/cdn-cgi/access/sso/oidc/${cloudflare_zero_trust_access_application.constellation_mcp_stage_oidc.saas_app.client_id}"

  constellation_mcp_stage_pointer = {
    hostname        = local.constellation_mcp_stage_hostname
    worker          = local.constellation_mcp_stage_worker
    kv_namespace_id = cloudflare_workers_kv_namespace.constellation_mcp_stage_oauth.id
    secrets         = aws_secretsmanager_secret.constellation_mcp_stage.name
  }
}

# Its own policy, as lattice_stage_owner, so someone added elsewhere never gets in here by the way.
resource "cloudflare_zero_trust_access_policy" "constellation_mcp_stage_owner" {
  account_id       = var.cloudflare_account_id
  name             = "Lattice MCP staging: owner"
  decision         = "allow"
  session_duration = "12h"
  include          = [{ email = { email = local.browser_manager_owner } }]
}

resource "cloudflare_zero_trust_access_application" "constellation_mcp_stage_oidc" {
  account_id = var.cloudflare_account_id
  name       = "Lattice MCP staging sign-in (constellation)"
  type       = "saas"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.github[*].id,
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  app_launcher_visible = false

  saas_app = {
    auth_type = "oidc"
    # The Worker's /callback only (apps/mcp/src/worker.ts). It sends a PKCE verifier and its client secret.
    redirect_uris = ["https://${local.constellation_mcp_stage_hostname}/callback"]
    grant_types   = ["authorization_code_with_pkce"]
    scopes        = ["openid", "email", "profile"]
  }

  # The policy attachment MUST be declared here; see cc_games_dev in cloudflare.tf.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.constellation_mcp_stage_owner.id
      precedence = 1
    },
  ]
}

# The Worker's OAuth state (@cloudflare/workers-oauth-provider: clients, grants, tokens). Wrangler binds it by id
# (OAUTH_KV); the id is not a credential and reaches the deploy job as an environment variable below.
resource "cloudflare_workers_kv_namespace" "constellation_mcp_stage_oauth" {
  account_id = var.cloudflare_account_id
  title      = "constellation-mcp-stage-oauth"

  lifecycle {
    # The deployed Worker's binding names this id; a replacement would leave it bound to a deleted namespace.
    prevent_destroy = true
  }
}

# Stage-only: it only has to be unguessable.
resource "random_password" "constellation_lattice_service" {
  length  = 48
  special = false
}

# The Worker's runtime secrets (JSON: variable name -> value), loaded into it with scripts/workers-stage-secrets.sh, as
# for the other staging Workers. Unlike theirs, every value here comes from a resource in this file, so Terraform writes
# the version (in state, as the tunnel token is); administration only may read it.
resource "aws_secretsmanager_secret" "constellation_mcp_stage" {
  name                    = "workers-stage/constellation-mcp"
  description             = "Runtime secrets of the constellation-mcp-stage Worker (JSON: variable name -> value), written by constellation-mcp-stage.tf; load with scripts/workers-stage-secrets.sh constellation-mcp."
  recovery_window_in_days = 7
  tags                    = { Name = "workers-stage/constellation-mcp", Project = "workers-stage" }
}

resource "aws_secretsmanager_secret_policy" "constellation_mcp_stage" {
  secret_arn = aws_secretsmanager_secret.constellation_mcp_stage.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.constellation_mcp_stage.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "constellation_mcp_stage" {
  secret_id = aws_secretsmanager_secret.constellation_mcp_stage.id
  secret_string = jsonencode({
    ACCESS_CLIENT_ID         = cloudflare_zero_trust_access_application.constellation_mcp_stage_oidc.saas_app.client_id
    ACCESS_CLIENT_SECRET     = cloudflare_zero_trust_access_application.constellation_mcp_stage_oidc.saas_app.client_secret
    ACCESS_AUTHORIZATION_URL = "${local.constellation_mcp_oidc_base}/authorization"
    ACCESS_TOKEN_URL         = "${local.constellation_mcp_oidc_base}/token"
    ACCESS_JWKS_URL          = "${local.constellation_mcp_oidc_base}/jwks"
    ACCESS_ISSUER            = local.constellation_mcp_oidc_base
    ORBIT_API                = "https://${local.lattice_stage_hostname}"
    LATTICE_SERVICE_SECRET   = random_password.constellation_lattice_service.result
  })
}

# The API's side of the same secret is constellation/nonprod/lattice-service in the constellation account (constellation's
# infra/aws secrets.tf made the container; the staging task injects LATTICE_SERVICE_SECRET from it). Its resource policy
# lets only constellation's own roles read it, so Terraform here cannot manage a version of it: a version resource reads
# its value back on every refresh, and the first apply (2026-10-23) wrote the value and then failed every later plan with
# AccessDenied. The value written then (version terraform-Gtz4..., AWSCURRENT) stays; Terraform forgets it below. To
# rotate, replace random_password.constellation_lattice_service, apply, and put the new value there with a session in
# constellation-terraform (fcvm-arm) or OrganizationAccountAccessRole (jumpbox): PutSecretValue is not denied.
removed {
  from = aws_secretsmanager_secret_version.constellation_lattice_service
  lifecycle {
    destroy = false
  }
}

# What constellation's deploy job needs besides the token (workers-deploy.tf): the account and the KV namespace id. Neither
# is a credential; both live in the same main-only environment so the job reads everything from one place.
resource "github_actions_environment_variable" "constellation_mcp_stage" {
  provider = github.agents_md
  for_each = {
    CLOUDFLARE_ACCOUNT_ID = var.cloudflare_account_id
    MCP_STAGE_KV_ID       = cloudflare_workers_kv_namespace.constellation_mcp_stage_oauth.id
  }
  repository    = "constellation"
  environment   = github_repository_environment.workers_deploy_ejc3["constellation"].environment
  variable_name = each.key
  value         = each.value
}

# Step 3: the hostname, once the Worker exists. Wrangler never routes it (the deploy token has no DNS or routes).
resource "cloudflare_workers_custom_domain" "constellation_mcp_stage" {
  count      = local.constellation_mcp_stage_deployed ? 1 : 0
  account_id = var.cloudflare_account_id
  zone_id    = var.dolphin_zone_id
  hostname   = local.constellation_mcp_stage_hostname
  service    = local.constellation_mcp_stage_worker
  depends_on = [
    cloudflare_zero_trust_access_application.constellation_mcp_stage_oidc,
    aws_secretsmanager_secret_version.constellation_mcp_stage,
  ]
}

# MCP clients reach the Worker with no Access session: it is its own OAuth server, and people sign in through the
# owner-only OIDC application above. So its host must not sit behind the *.dolphin-labs.dev GitHub login: an exact-host
# bypass, as dolphin_maps_public (dolphin-labs-zone.tf), only while the hostname exists. Owner's go-ahead, 2026-10-23.
resource "cloudflare_zero_trust_access_policy" "constellation_mcp_stage_public" {
  count      = local.constellation_mcp_stage_deployed ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Lattice MCP staging (public; the Worker's own OAuth)"
  decision   = "bypass"

  include = [{
    everyone = {}
  }]
}

resource "cloudflare_zero_trust_access_application" "constellation_mcp_stage_public" {
  count      = local.constellation_mcp_stage_deployed ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Lattice MCP staging (public; the Worker's own OAuth)"
  type       = "self_hosted"
  domain     = local.constellation_mcp_stage_hostname

  destinations = [
    { type = "public", uri = local.constellation_mcp_stage_hostname },
  ]

  # Declared here, as on every application: an apply drops an attachment that is not.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.constellation_mcp_stage_public[0].id
      precedence = 1
    },
  ]
}
