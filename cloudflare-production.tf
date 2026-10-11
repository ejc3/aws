# cloudflare-production.tf
#
# Colton Games PRODUCTION on Cloudflare Workers (owner, 2026-10-11): moving it off Vercel so that Cloudflare's abuse
# protection (WAF, rate limiting, bot rules) covers it. Everything is prepared here; the move itself is ONE line,
# local.colton_games_production_on_cloudflare, and moving back is the same line. Until then production is served by Vercel
# exactly as before (vercel-cc-games.tf) and nothing here changes what a visitor gets.
#
#   the Worker      colton-games-prod, deployed by the games repo's Cloudflare workflow from main (wrangler.jsonc
#                   env.production), the same build as colton-games-stage, with production's secrets
#                   (workers-prod-secrets.tf, loaded by scripts/workers-prod-secrets.sh). Wrangler owns its code.
#   before cutover  it answers only at its workers.dev address, which a HOSTNAME Access application covers (family
#                   allowlist and the automation service token). Not a Worker-native one as the staging Workers use: a
#                   `worker` destination protects every route and Custom Domain of the Worker too, so it would put
#                   Access in front of production at the cutover.
#   zone protection cc-games.org (Free plan): Always Use HTTPS, TLS 1.2 minimum, Full (strict) to the origin, HSTS as Vercel
#                   sends it, Block AI bots, WAF custom rules and the plan's one rate-limiting rule. All of it acts only
#                   on PROXIED traffic, so while every record is DNS-only (today) it does nothing. Bot Fight Mode stays
#                   OFF: on Free it cannot be skipped for anything and would challenge the multiplayer engines'
#                   callbacks, API clients and service-token automation.
#   the cutover     local.colton_games_production_on_cloudflare = true: the cc-games.org apex record becomes proxied
#                   (same content, Vercel's address) and a Workers route sends cc-games.org/* to colton-games-prod. Both
#                   orders are safe: a route on a DNS-only record does nothing, and a proxied record without the route
#                   reaches Vercel through Cloudflare with a valid certificate (Full strict). false undoes both.
#                   www.cc-games.org and the old domains stay on Vercel's 308 redirects to cc-games.org, unchanged.
#                   AGENTS.md ("Colton Games production on Cloudflare") has the runbook.
#
# Why a route on the existing record and not a Workers Custom Domain: a Custom Domain cannot be attached while the
# hostname has its own DNS record, so the switch would be "delete Vercel's record, then attach", two steps with an outage
# between them, and the same again to roll back. Flipping `proxied` is one in-place change per direction.
#
# CREDENTIAL: these resources need Zone Settings, Zone WAF, Bot Management and Workers Routes write on cc-games.org. The
# provider token (cloudflare-tunnel-token) carries them as a policy scoped to that one zone only (added 2026-10-11; see
# AGENTS.md).

locals {
  # THE CUTOVER SWITCH. false: Vercel serves cc-games.org (DNS-only record). true: Cloudflare serves it from the Worker.
  colton_games_production_on_cloudflare = true

  colton_games_prod_worker_name = "colton-games-prod"
  colton_games_prod_workers_dev = "${local.colton_games_prod_worker_name}.cc-games.workers.dev"
}

# -------------------------------------------------------------------------------------
# Before the cutover: the workers.dev address, behind Access
# -------------------------------------------------------------------------------------

# Exists before the Worker's first deploy, so its workers.dev address is never reachable without a sign-in.
resource "cloudflare_zero_trust_access_application" "colton_games_prod_workers_dev" {
  account_id       = var.cloudflare_account_id
  name             = "Colton Games production Worker (workers.dev)"
  type             = "self_hosted"
  domain           = local.colton_games_prod_workers_dev
  session_duration = "24h"

  destinations = [
    { type = "public", uri = local.colton_games_prod_workers_dev },
  ]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.cc_games_allowed.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.cc_games_service.id
      precedence = 2
    },
  ]

  # The workers.dev address must be turned off (wrangler.jsonc env.production) before this protection can go.
  lifecycle {
    prevent_destroy = true
  }
}

# The Worker's two URL switches, adopted once its first deploy created it: workers.dev on (behind the application above),
# version previews off. Only the switches, never the whole Worker (workers-stage.tf explains why); wrangler.jsonc
# env.production sets the same values on every deploy.
import {
  to = cloudflare_workers_script_subdomain.colton_games_prod
  id = "${var.cloudflare_account_id}/${local.colton_games_prod_worker_name}"
}

resource "cloudflare_workers_script_subdomain" "colton_games_prod" {
  account_id       = var.cloudflare_account_id
  script_name      = local.colton_games_prod_worker_name
  enabled          = true
  previews_enabled = false

  depends_on = [
    cloudflare_workers_subdomain.cc_games,
    cloudflare_zero_trust_access_application.colton_games_prod_workers_dev,
  ]
}

# -------------------------------------------------------------------------------------
# The multiplayer launcher's identity on the Worker
# -------------------------------------------------------------------------------------

# Vercel production invokes games-mp-launch-production through role games-mp-launcher (Vercel OIDC). A Worker has no
# Vercel OIDC token, so this user holds the role's one permission: invoke that one function, unqualified ARN. The launch
# function still does all admission (its own ceiling, input validation). Its key goes only into the production Worker's
# managed secrets (workers-prod-secrets.tf).
#   Rotate: terraform apply -replace=aws_iam_access_key.games_mp_launcher_worker, then scripts/workers-prod-secrets.sh colton-games.
resource "aws_iam_user" "games_mp_launcher_worker" {
  name = "games-mp-launcher-worker"
  tags = { Name = "games-mp-launcher-worker", Project = "games-multiplayer" }
}

resource "aws_iam_user_policy" "games_mp_launcher_worker" {
  name = "invoke-games-mp-launch"
  user = aws_iam_user.games_mp_launcher_worker.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeProductionLaunchFunction"
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.games_mp_launch["production"].arn
    }]
  })
}

resource "aws_iam_access_key" "games_mp_launcher_worker" {
  user = aws_iam_user.games_mp_launcher_worker.name
}

# -------------------------------------------------------------------------------------
# cc-games.org zone protection (Free plan). Inert while the records are DNS-only.
# -------------------------------------------------------------------------------------

locals {
  cc_games_org_zone_settings = {
    always_use_https = "on"
    min_tls_version  = "1.2"
    # Full (strict): Cloudflare checks the origin's certificate. Needed for the moment in the cutover when the record is
    # proxied but the route is not yet in place (Vercel answers with its own valid certificate); Flexible would loop.
    ssl = "strict"
  }
}

resource "cloudflare_zone_setting" "cc_games_org" {
  for_each   = local.cc_games_org_zone_settings
  zone_id    = var.cc_games_org_zone_id
  setting_id = each.key
  value      = each.value
}

# HSTS exactly as Vercel sends it today (max-age=63072000, no includeSubDomains, no preload), so browsers that have it
# keep it after the cutover and nothing new is promised for the subdomains.
resource "cloudflare_zone_setting" "cc_games_org_hsts" {
  zone_id    = var.cc_games_org_zone_id
  setting_id = "security_header"
  value = {
    strict_transport_security = {
      enabled            = true
      max_age            = 63072000
      include_subdomains = false
      preload            = false
      nosniff            = false
    }
  }
}

# Block AI bots (a Free-plan feature). Bot Fight Mode deliberately off: see the header.
resource "cloudflare_bot_management" "cc_games_org" {
  zone_id            = var.cc_games_org_zone_id
  ai_bots_protection = "block"
  fight_mode         = false
}

locals {
  # Callers that are not browsers and must never meet a challenge: the multiplayer engines (Fargate, a new address every
  # match) call back /api/mp/engine/<id>/... with their per-match secret.
  cc_games_org_engine_callbacks = "starts_with(http.request.uri.path, \"/api/mp/engine/\")"
  # The API routes a person's browser writes to, minus the lobby: /api/mp/me is polled about once a second per queued
  # player, and a school network puts a whole class behind one address.
  cc_games_org_rate_limited = "(starts_with(http.request.uri.path, \"/api/\") and not starts_with(http.request.uri.path, \"/api/mp/\"))"
}

# WAF custom rules (Free: 5). Order matters: the engines' callbacks skip the legacy browser checks first.
resource "cloudflare_ruleset" "cc_games_org_waf" {
  zone_id     = var.cc_games_org_zone_id
  name        = "cc-games.org custom rules"
  description = "Colton Games production (cloudflare-production.tf)"
  kind        = "zone"
  phase       = "http_request_firewall_custom"

  rules = [
    {
      ref         = "engine_callbacks_skip_browser_checks"
      description = "Multiplayer engine callbacks: not browsers, authenticated by their per-match secret"
      expression  = local.cc_games_org_engine_callbacks
      action      = "skip"
      action_parameters = {
        ruleset  = "current"
        products = ["bic", "securityLevel"]
      }
      logging = { enabled = true }
      enabled = true
    },
    {
      ref         = "block_scanner_paths"
      description = "Paths only vulnerability scanners ask for: the site has no PHP, WordPress, CGI or dotfiles"
      expression  = "(ends_with(http.request.uri.path, \".php\") or starts_with(http.request.uri.path, \"/wp-\") or starts_with(http.request.uri.path, \"/.env\") or starts_with(http.request.uri.path, \"/.git\") or starts_with(http.request.uri.path, \"/cgi-bin\"))"
      action      = "block"
      enabled     = true
    },
    {
      ref         = "block_unused_methods"
      description = "Methods the site never answers"
      expression  = "not http.request.method in {\"GET\" \"HEAD\" \"POST\" \"PUT\" \"PATCH\" \"DELETE\" \"OPTIONS\"}"
      action      = "block"
      enabled     = true
    },
    {
      ref         = "block_api_without_user_agent"
      description = "API calls with no User-Agent (browsers and the engines always send one)"
      expression  = "(${local.cc_games_org_rate_limited} and http.user_agent eq \"\")"
      action      = "block"
      enabled     = true
    },
  ]
}

# The Free plan's one rate-limiting rule: per address (and data centre), 10 s window, 10 s block. 100 requests in 10 s to
# the write-capable API routes is far above a person (the busiest periodic call, playtime, is one every 30 s per tab) and
# above a classroom behind one address, and far below a script hammering sign-in, uploads or feedback. The lobby and the
# engines' callbacks are outside it (local.cc_games_org_rate_limited).
resource "cloudflare_ruleset" "cc_games_org_rate_limit" {
  zone_id     = var.cc_games_org_zone_id
  name        = "cc-games.org rate limiting"
  description = "Colton Games production (cloudflare-production.tf)"
  kind        = "zone"
  phase       = "http_ratelimit"

  rules = [
    {
      ref         = "api_writes_per_address"
      description = "API routes, lobby excluded: at most 100 requests per address in 10 s"
      expression  = local.cc_games_org_rate_limited
      action      = "block"
      ratelimit = {
        characteristics     = ["ip.src", "cf.colo.id"]
        period              = 10
        requests_per_period = 100
        mitigation_timeout  = 10
      }
      enabled = true
    },
  ]
}

# -------------------------------------------------------------------------------------
# The cutover
# -------------------------------------------------------------------------------------

# cc-games.org/* -> colton-games-prod. Takes effect only while the apex record is proxied (vercel-cc-games.tf reads the
# same switch). The record keeps Vercel's address, so with the switch off nothing reaches the Worker.
resource "cloudflare_workers_route" "colton_games_prod" {
  count   = local.colton_games_production_on_cloudflare ? 1 : 0
  zone_id = var.cc_games_org_zone_id
  pattern = "cc-games.org/*"
  script  = local.colton_games_prod_worker_name

  # The zone's protections exist before production traffic can reach the Worker.
  depends_on = [
    cloudflare_zone_setting.cc_games_org,
    cloudflare_ruleset.cc_games_org_waf,
    cloudflare_ruleset.cc_games_org_rate_limit,
  ]
}

output "colton_games_production" {
  description = "Where Colton Games production is served from, and the production Worker's pre-cutover address (behind Access)"
  value = {
    served_by           = local.colton_games_production_on_cloudflare ? "cloudflare (${local.colton_games_prod_worker_name})" : "vercel"
    worker              = local.colton_games_prod_worker_name
    workers_dev         = "https://${local.colton_games_prod_workers_dev}"
    secrets_terraform   = aws_secretsmanager_secret.workers_prod["colton-games/terraform"].name
    secrets_out_of_band = aws_secretsmanager_secret.workers_prod["colton-games"].name
  }
}
