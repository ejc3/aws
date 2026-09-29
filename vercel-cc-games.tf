# Colton Games production on Vercel, with cc-games.org as its canonical domain.
#
# The project lives in the Vercel team `coltons-projects-7f9a4e8b`. This file manages its
# DOMAINS. The project's settings and env vars stay in Vercel: several env values are database
# credentials, and importing them would copy those into Terraform state. The one exception is
# the multiplayer set (MP_*, CRON_SECRET, AWS_ROLE_ARN/AWS_REGION, Preview's Skyhook pair and
# the automation bypass), which Terraform creates and owns: games-multiplayer-bringup.tf.
#
# Rollout is staged so production never redirects to a domain that is not serving yet:
#   stage 2 (this file): cc-games.app becomes a project domain, ccgames.app redirects to it,
#                        the existing colton-games.com pair is imported AS IT IS.
#   stage 3 (done):      colton-games.com and www.colton-games.com redirect to cc-games.app.
#   stage 4 (2026-09-29): cc-games.net is canonical; every other name redirects to it.
#   stage 5 (2026-09-29): cc-games.org is canonical; every other name, .net included, redirects.
# Only production is redirected. The dev URLs under cc-games.dev are a separate Cloudflare
# tunnel and are not touched.
#
# cc-games.org (registered 2026-09-29) is canonical: cc-games.app is blocked on a school network,
# so every other name -- cc-games.app and cc-games.net included -- 308-redirects straight to
# cc-games.org, and nothing redirects to .app. Multiplayer: play.cc-games.org
# (games-multiplayer.tf) is production's first entry and MP_API is https://cc-games.org; MP_API
# moves BEFORE the old canonical name redirects (an engine's callbacks lose their Authorization
# header across a redirect; docs/games-multiplayer.md). The play.* entries all keep serving.
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

variable "cc_games_net_zone_id" {
  description = "Cloudflare zone id for cc-games.net (registered through Cloudflare Registrar 2026-09-28)"
  type        = string
  default     = "7ecab81e3d2db28b0aeabb2f8e04c1e6"
}

variable "cc_games_org_zone_id" {
  description = "Cloudflare zone id for cc-games.org (registered through Cloudflare Registrar 2026-09-29)"
  type        = string
  default     = "7efede9790c3944c5d8b3a1fb3747fdf"
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

# Redirects to cc-games.net only after every other name points at cc-games.net directly, so no
# name ever chains through a domain that is itself a redirect.
resource "vercel_project_domain" "cc_games_app" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "cc-games.app"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308

  depends_on = [
    vercel_project_domain.ccgames_app,
    vercel_project_domain.colton_games_com,
    vercel_project_domain.colton_games_com_www,
    vercel_project_domain.www_cc_games_app,
    vercel_project_domain.www_ccgames_app,
  ]
}

# Canonical until 2026-09-29; now it redirects to cc-games.org, after every name that pointed at
# it points at cc-games.org directly (no redirect chains).
resource "vercel_project_domain" "cc_games_net" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "cc-games.net"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308

  depends_on = [
    vercel_project_domain.cc_games_app,
    vercel_project_domain.ccgames_app,
    vercel_project_domain.colton_games_com,
    vercel_project_domain.colton_games_com_www,
    vercel_project_domain.www_cc_games_app,
    vercel_project_domain.www_ccgames_app,
    vercel_project_domain.www_cc_games_net,
  ]
}

# The canonical production name (see the header): it serves, it never redirects.
resource "vercel_project_domain" "cc_games_org" {
  team_id    = var.vercel_team_id
  project_id = data.vercel_project.colton_games.id
  domain     = "cc-games.org"
}

resource "vercel_project_domain" "www_cc_games_org" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "www.cc-games.org"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308
}

resource "vercel_project_domain" "www_cc_games_net" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "www.cc-games.net"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308
}

resource "vercel_project_domain" "ccgames_app" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "ccgames.app"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308
}

# The old production domains redirect to cc-games.org (308; they pointed at cc-games.app, then
# cc-games.net, until 2026-09-29). colton-games.vercel.app is Vercel's own hostname and is left alone. (Both
# domains were imported into state in stage 2.)
resource "vercel_project_domain" "colton_games_com" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "colton-games.com"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308
}

resource "vercel_project_domain" "colton_games_com_www" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "www.colton-games.com"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308
}

# www variants. Every hostname a person might type ends up on https://cc-games.org: the
# apex and www of both spellings redirect to it, and http:// is upgraded by Vercel.
resource "vercel_project_domain" "www_cc_games_app" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "www.cc-games.app"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308
}

resource "vercel_project_domain" "www_ccgames_app" {
  team_id              = var.vercel_team_id
  project_id           = data.vercel_project.colton_games.id
  domain               = "www.ccgames.app"
  redirect             = vercel_project_domain.cc_games_org.domain
  redirect_status_code = 308
}

resource "cloudflare_dns_record" "cc_games_app_apex" {
  zone_id = var.cc_games_app_zone_id
  name    = "cc-games.app"
  type    = "A"
  content = "76.76.21.21"
  proxied = false # see the header: proxying breaks Vercel cert issuance
  ttl     = 300
  comment = "vercel apex; redirects to cc-games.org"
}

resource "cloudflare_dns_record" "cc_games_net_apex" {
  zone_id = var.cc_games_net_zone_id
  name    = "cc-games.net"
  type    = "A"
  content = "76.76.21.21"
  proxied = false # see the header: proxying breaks Vercel cert issuance
  ttl     = 300
  comment = "vercel apex; redirects to cc-games.org"
}

resource "cloudflare_dns_record" "cc_games_org_apex" {
  zone_id = var.cc_games_org_zone_id
  name    = "cc-games.org"
  type    = "A"
  content = "76.76.21.21"
  proxied = false # see the header: proxying breaks Vercel cert issuance
  ttl     = 300
  comment = "vercel apex; Colton Games production (canonical)"
}

resource "cloudflare_dns_record" "cc_games_org_www" {
  zone_id = var.cc_games_org_zone_id
  name    = "www"
  type    = "CNAME"
  content = "cname.vercel-dns.com"
  proxied = false
  ttl     = 300
  comment = "vercel www; redirects to cc-games.org"
}

resource "cloudflare_dns_record" "cc_games_net_www" {
  zone_id = var.cc_games_net_zone_id
  name    = "www"
  type    = "CNAME"
  content = "cname.vercel-dns.com"
  proxied = false
  ttl     = 300
  comment = "vercel www; redirects to cc-games.org"
}

resource "cloudflare_dns_record" "ccgames_app_apex" {
  zone_id = var.ccgames_app_zone_id
  name    = "ccgames.app"
  type    = "A"
  content = "76.76.21.21"
  proxied = false
  ttl     = 300
  comment = "vercel apex; redirects to cc-games.org"
}

resource "cloudflare_dns_record" "cc_games_app_www" {
  zone_id = var.cc_games_app_zone_id
  name    = "www"
  type    = "CNAME"
  content = "cname.vercel-dns.com"
  proxied = false
  ttl     = 300
  comment = "vercel www; redirects to cc-games.org"
}

resource "cloudflare_dns_record" "ccgames_app_www" {
  zone_id = var.ccgames_app_zone_id
  name    = "www"
  type    = "CNAME"
  content = "cname.vercel-dns.com"
  proxied = false
  ttl     = 300
  comment = "vercel www; redirects to cc-games.org"
}
