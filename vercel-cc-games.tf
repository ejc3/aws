# Colton Games production on Vercel, with cc-games.app as its primary domain.
#
# The project lives in the Vercel team `coltons-projects-7f9a4e8b`. This stack manages its
# DOMAINS only. The project's settings and env vars stay in Vercel for now: several env values
# are database credentials, and importing them would copy those into Terraform state.
#
# Rollout is staged so production never redirects to a domain that is not serving yet:
#   stage 2 (this file): cc-games.app becomes a project domain, ccgames.app redirects to it,
#                        the existing colton-games.com pair is imported AS IT IS.
#   stage 3 (done):      colton-games.com and www.colton-games.com redirect to cc-games.app.
# Only production is redirected. The dev URLs under cc-games.dev are a separate Cloudflare
# tunnel and are not touched.
#
# DNS stays at Cloudflare (both .app domains are registered there, and Cloudflare Registrar
# does not allow other nameservers). The apex records must NOT be proxied: an orange cloud
# ends TLS at Cloudflare and Vercel's certificate issuance then fails; see
# gamesworthwatching-zone.tf.

variable "vercel_team_id" {
  description = "Vercel team that owns the colton-games project (Colton's projects)"
  type        = string
  default     = "team_luQ5Id7cevXQ5kBbxzV7Jk2B"
}

variable "cc_games_app_zone_id" {
  description = "Cloudflare zone id for cc-games.app (registered through Cloudflare Registrar 2026-09-25)"
  type        = string
  default     = "38302d3b9d8d603a055d42f7a4a86ec9"
}

variable "ccgames_app_zone_id" {
  description = "Cloudflare zone id for ccgames.app (registered through Cloudflare Registrar 2026-09-25)"
  type        = string
  default     = "17193010dcc47fad09ea2469f87537a7"
}

ephemeral "aws_secretsmanager_secret_version" "vercel_api_token" {
  secret_id = aws_secretsmanager_secret.vercel_api_token.id
}

# `team` is deliberately NOT set on the provider. The token is scoped to the team's projects,
# and the provider's own team lookup is refused with team_unauthorized. Each resource passes
# team_id instead, which needs no team-level read.
provider "vercel" {
  api_token = ephemeral.aws_secretsmanager_secret_version.vercel_api_token.secret_string
}

data "vercel_project" "colton_games" {
  name    = "colton-games"
  team_id = var.vercel_team_id
}

locals {
  colton_games_vercel_project_id = "prj_dYI19Kk20cSBR2JirdbGNz5J91KF"
}

resource "vercel_project_domain" "cc_games_app" {
  team_id    = var.vercel_team_id
  project_id = data.vercel_project.colton_games.id
  domain     = "cc-games.app"
}

resource "vercel_project_domain" "ccgames_app" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "ccgames.app"
  redirect             = vercel_project_domain.cc_games_app.domain
  redirect_status_code = 308
}

# The old production domains now redirect to cc-games.app (308). cc-games.app was confirmed
# serving over HTTPS before this change, so production never redirects to a domain that is
# not live. colton-games.vercel.app is Vercel's own hostname and is left alone. (Both
# domains were imported into state in stage 2.)
resource "vercel_project_domain" "colton_games_com" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "colton-games.com"
  redirect             = vercel_project_domain.cc_games_app.domain
  redirect_status_code = 308
}

resource "vercel_project_domain" "colton_games_com_www" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "www.colton-games.com"
  redirect             = vercel_project_domain.cc_games_app.domain
  redirect_status_code = 308
}

resource "cloudflare_dns_record" "cc_games_app_apex" {
  zone_id = var.cc_games_app_zone_id
  name    = "cc-games.app"
  type    = "A"
  content = "76.76.21.21"
  proxied = false # see the header: proxying breaks Vercel cert issuance
  ttl     = 300
  comment = "vercel apex; Colton Games production"
}

resource "cloudflare_dns_record" "ccgames_app_apex" {
  zone_id = var.ccgames_app_zone_id
  name    = "ccgames.app"
  type    = "A"
  content = "76.76.21.21"
  proxied = false
  ttl     = 300
  comment = "vercel apex; redirects to cc-games.app"
}
