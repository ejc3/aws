# Preview hosting for the dolphin-labs project: *.dolphin-labs.dev, behind Cloudflare
# Access, gated by GitHub instead of Google.
#
# Same shape as cc-games.dev (see cloudflare.tf) -- a tunnel the dev box dials out to, one
# wildcard DNS record so new hostnames need no DNS change, and an Access application in
# front of everything. Two deliberate differences:
#
#   1. GITHUB, NOT GOOGLE. The people on this project are identified by GitHub account,
#      not Gmail. cc-games.dev keeps Google untouched; the two zones do not share an
#      identity provider, so changing one cannot lock anyone out of the other.
#   2. It is INERT until the domain exists. Everything below is gated on
#      enable_dolphin_zone, default false, because `dolphin-labs.dev` is not registered
#      yet and a zone id cannot be invented. Flipping the flag is the whole activation.
#
# ACTIVATION, in order -- neither step can be done from terraform:
#
#   1. Register dolphin-labs.dev through Cloudflare Registrar. Registering there creates
#      the zone automatically, which is why this takes a zone id rather than declaring a
#      cloudflare_zone: the zone already exists by the time terraform sees it.
#   2. Create a GitHub OAuth app (Settings -> Developer settings -> OAuth Apps) with the
#      callback URL printed by the cf_access_callback local in cloudflare.tf, and store
#      its client id/secret:
#
#        aws secretsmanager create-secret --name cloudflare-github-idp --region us-west-1 \
#          --secret-string '{"client_id":"...","client_secret":"..."}'
#
#   3. terraform apply -var enable_dolphin_zone=true -var dolphin_zone_id=<id>

# These defaulted to off/empty while dolphin-labs.dev was still unregistered. It is now
# registered and the zone is live, so they default ON: with the old defaults every plan run
# without -var flags proposed destroying the whole zone, which is a trap for whoever plans
# next (including CI drift checks). Set enable_dolphin_zone=false to take it down on purpose.
variable "enable_dolphin_zone" {
  description = "Stand up *.dolphin-labs.dev. Needs the domain registered, a zone id, and the cloudflare-github-idp secret."
  type        = bool
  default     = true
}

variable "dolphin_zone_id" {
  description = "Cloudflare zone id for dolphin-labs.dev (created automatically when registered through Cloudflare Registrar)."
  type        = string
  default     = "48eb9c7cae15cd0a58207a594297418c"
}

variable "dolphin_allowed_github_org" {
  description = "GitHub org whose members may reach every *.dolphin-labs.dev host. Membership IS the access list -- add someone to the org, they get in."
  type        = string
  default     = "dolphin-labs-hq"
}

locals {
  # Fail loudly at plan time rather than producing a half-built zone. Every resource below
  # is counted on this, so a missing zone id cannot silently create a tunnel with nothing
  # pointing at it.
  dolphin_enabled = var.enable_dolphin_zone && var.dolphin_zone_id != ""
}

# ---------------------------------------------------------------------------------
# GitHub identity provider. The OAuth app lives in GitHub and CANNOT be created from
# terraform -- it needs an interactive browser session as the account that owns it, the
# same constraint the Google provider has in cloudflare.tf.
# ---------------------------------------------------------------------------------
data "aws_secretsmanager_secret_version" "github_idp" {
  count     = local.dolphin_enabled ? 1 : 0
  secret_id = "cloudflare-github-idp"
}

resource "cloudflare_zero_trust_access_identity_provider" "github" {
  count      = local.dolphin_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "GitHub"
  type       = "github"
  config = {
    client_id     = jsondecode(data.aws_secretsmanager_secret_version.github_idp[0].secret_string)["client_id"]
    client_secret = jsondecode(data.aws_secretsmanager_secret_version.github_idp[0].secret_string)["client_secret"]
  }
}

# ---------------------------------------------------------------------------------
# The tunnel. cloudflared on the dev box dials OUT to this, so the box needs no inbound
# web ports -- identical to the cc-games tunnel, and deliberately a SEPARATE tunnel:
# sharing one would mean a config mistake on either project could take down both.
# ---------------------------------------------------------------------------------
resource "cloudflare_zero_trust_tunnel_cloudflared" "dolphin_labs" {
  count      = local.dolphin_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-dev"
  config_src = "local" # ingress rules are managed on the box, same as cc-games

  # Generated once and held in Secrets Manager. Terraform must not rotate it on every
  # plan; that would break the running tunnel.
  tunnel_secret = jsondecode(data.aws_secretsmanager_secret_version.dolphin_tunnel_creds[0].secret_string)["TunnelSecret"]

  lifecycle {
    ignore_changes = [tunnel_secret]
  }
}

data "aws_secretsmanager_secret_version" "dolphin_tunnel_creds" {
  count     = local.dolphin_enabled ? 1 : 0
  secret_id = "cloudflare-dolphin-tunnel-credentials"
}

# ---------------------------------------------------------------------------------
# Wildcard DNS. One record covers every preview: a project invents a hostname and it
# resolves immediately, with no DNS change per project.
# ---------------------------------------------------------------------------------
resource "cloudflare_dns_record" "dolphin_wildcard" {
  count   = local.dolphin_enabled ? 1 : 0
  zone_id = var.dolphin_zone_id
  name    = "*"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.dolphin_labs[0].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}

# ---------------------------------------------------------------------------------
# Access. Who may reach a preview at all.
# ---------------------------------------------------------------------------------
resource "cloudflare_zero_trust_access_policy" "dolphin_allowed" {
  count            = local.dolphin_enabled ? 1 : 0
  account_id       = var.cloudflare_account_id
  name             = "dolphin-labs allowed people"
  decision         = "allow"
  session_duration = "24h"

  # Matched on ORG MEMBERSHIP, which is what this selector actually does. An earlier version
  # passed each person's LOGIN as the org name -- that applies cleanly and then denies
  # everyone, because there is no org called "ejc3". Cloudflare's github_organization rule
  # matches an organization (optionally a team); individuals are matched by email, and a
  # GitHub email can be private or change.
  #
  # Org membership is also the better list: both people here are already in dolphin-labs-hq
  # because that is where the repo lives, so access follows the repo instead of drifting
  # from it.
  include = [
    {
      github_organization = {
        identity_provider_id = cloudflare_zero_trust_access_identity_provider.github[0].id
        name                 = var.dolphin_allowed_github_org
      }
    }
  ]
}

resource "cloudflare_zero_trust_access_application" "dolphin_labs" {
  count            = local.dolphin_enabled ? 1 : 0
  account_id       = var.cloudflare_account_id
  name             = "dolphin-labs previews"
  domain           = "*.dolphin-labs.dev"
  type             = "self_hosted"
  session_duration = "24h"

  # Every identity provider on the account is offered by default, so without this the
  # login page shows Google next to GitHub on a zone that is meant to be gated by GitHub
  # org membership. A Google sign-in would still be refused by the policy, but only after
  # the user has picked it, which reads as a broken login rather than a deliberate one.
  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.github[0].id]
  auto_redirect_to_identity = true

  # The policy attachment MUST be declared here. Leaving it out makes terraform drop the
  # link on the next apply, which would leave every *.dolphin-labs.dev hostname reachable
  # with no login at all. That exact mistake is documented on the cc-games application in
  # cloudflare.tf; it is repeated here because the failure is silent and total.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.dolphin_allowed[0].id
      precedence = 1
    },
  ]
}

# ---------------------------------------------------------------------------------
# Dolphin Maps PRODUCTION: maps.dolphin-labs.dev, served by the Worker dolphin-maps-prod (ejc3/aws #529). Not a preview:
# production left with the dolphin-labs Vercel team (#503), and the owner chose this host on 2026-10-19. The Worker is
# web/wrangler.jsonc env.production in dolphin-labs-hq/dolphin-maps (workers.dev and previews off, so this route is its
# only address); its secrets are workers-prod/dolphin-maps in the Dolphin account, loaded by
# scripts/workers-prod-secrets.sh dolphin-maps.
#
# The shape of gamesworthwatching-zone.tf: a proxied record exists only so the route has a hostname to match, and the
# route answers every request, so the content is never contacted. The record is the Workers-only placeholder, A
# 192.0.2.1 (TEST-NET-1, routes nowhere). It is a name of its own, so the zone's `*` wildcard (the previews tunnel)
# no longer answers for it: a wildcard only covers names that have no record.
#
# PUBLIC. The iPhone app and visitors reach it with no identity, so it has its own Access application for exactly
# this host with a bypass-everyone policy; Cloudflare applies the most specific application, so the wildcard
# application above still gates every other *.dolphin-labs.dev host and is not changed. The same pattern as
# family_wall_assets in cloudflare.tf. The bypass depends on the record and the route, so it can only exist once the
# host is the Worker's: without the record the name would fall to the wildcard and reach the previews tunnel, which
# must never be public. If the route cannot be created the host stays behind the GitHub login (fails closed).
#
# Not counted on local.dolphin_enabled: that switch takes the previews down, and production must not go with them.
#
# CREDENTIAL: the route needs Workers Routes write on this zone, which the provider token (cloudflare-tunnel-token)
# carries for cc-games.org, gamesworthwatching.com and yourfantasymovie.com only; add this zone to that policy first,
# the same one-PUT change. It already has DNS write and Zone Settings write here.
# ---------------------------------------------------------------------------------
locals {
  dolphin_maps_host   = "maps.dolphin-labs.dev"
  dolphin_maps_worker = "dolphin-maps-prod"
}

resource "cloudflare_dns_record" "dolphin_maps" {
  zone_id = var.dolphin_zone_id
  name    = local.dolphin_maps_host
  type    = "A"
  content = "192.0.2.1"
  proxied = true
  ttl     = 1
  comment = "proxied; Worker ${local.dolphin_maps_worker} route serves all; origin unreachable on purpose"
}

resource "cloudflare_workers_route" "dolphin_maps" {
  zone_id = var.dolphin_zone_id
  pattern = "${local.dolphin_maps_host}/*"
  script  = local.dolphin_maps_worker
}

resource "cloudflare_zero_trust_access_policy" "dolphin_maps_public" {
  account_id = var.cloudflare_account_id
  name       = "Dolphin Maps production (public)"
  decision   = "bypass"

  include = [{
    everyone = {}
  }]
}

resource "cloudflare_zero_trust_access_application" "dolphin_maps_public" {
  account_id = var.cloudflare_account_id
  name       = "Dolphin Maps production (public)"
  type       = "self_hosted"
  domain     = local.dolphin_maps_host

  destinations = [
    { type = "public", uri = local.dolphin_maps_host },
  ]

  # Declared here, as on every application: an apply drops an attachment that is not, and an application with no
  # policy refuses everyone, which would take production down.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.dolphin_maps_public.id
      precedence = 1
    },
  ]

  depends_on = [cloudflare_dns_record.dolphin_maps, cloudflare_workers_route.dolphin_maps]
}

# Plain http:// is upgraded at the edge for the whole zone (previews included: they are proxied and already https).
resource "cloudflare_zone_setting" "dolphin_always_use_https" {
  zone_id    = var.dolphin_zone_id
  setting_id = "always_use_https"
  value      = "on"
}

output "dolphin_labs_status" {
  description = "Whether *.dolphin-labs.dev is stood up, and what is missing if not"
  value = local.dolphin_enabled ? "active: https://<name>.dolphin-labs.dev (GitHub login required)" : join(" ", compact([
    "inactive --",
    var.enable_dolphin_zone ? "" : "set enable_dolphin_zone=true;",
    var.dolphin_zone_id == "" ? "register dolphin-labs.dev and set dolphin_zone_id;" : "",
    "needs secrets cloudflare-github-idp and cloudflare-dolphin-tunnel-credentials",
  ]))
}
