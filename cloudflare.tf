# cloudflare.tf
#
# The Cloudflare half of the Next.js dev setup: the tunnel that reaches the box, the
# wildcard DNS that points at it, and the Access application that puts a Google login in
# front of every hostname.
#
# These were originally created by hand through the API while bootstrapping; they are
# imported here so the whole thing is reproducible from `terraform apply` rather than
# living only in someone's shell history.
#
# CREDENTIALS: the API token lives in Secrets Manager (cloudflare-tunnel-token) and must be
# scoped to this account with only what the resources below need -- Tunnel Write, Access Apps
# and Policies Write, Access Organizations/Identity Providers Write, Workers Scripts
# Read/Write, DNS Write, and Zone Read/Write.
# It is never written to disk or into the repo.
#
# The IdP permission is the non-obvious one: without it `terraform import` on the identity
# provider fails with a bare "auth.forbidden" that reads like a malformed import ID rather
# than a missing scope. If you re-mint this token, include it or the login method below
# becomes unmanageable.

data "aws_secretsmanager_secret_version" "cloudflare_token" {
  secret_id = "cloudflare-tunnel-token"
}

data "aws_secretsmanager_secret_version" "cloudflare_tunnel_creds" {
  secret_id = "cloudflare-tunnel-credentials"
}

provider "cloudflare" {
  api_token = data.aws_secretsmanager_secret_version.cloudflare_token.secret_string
}

# Workers Builds rejects account-owned tokens. Its control plane therefore uses a
# dedicated user-owned token whose payload is populated once, outside Terraform, after
# this Terraform-managed container exists. The ordinary account token remains the
# credential for every pre-existing Cloudflare resource in this stack.
resource "aws_secretsmanager_secret" "cloudflare_workers_builds_control_token" {
  name                    = "cloudflare-workers-builds-control-token"
  description             = "User-owned Cloudflare token for Terraform-managed Workers Builds configuration"
  recovery_window_in_days = 30
  tags                    = { Name = "cloudflare-workers-builds-control-token", Managed = "terraform" }

  lifecycle {
    prevent_destroy = true
  }
}

ephemeral "aws_secretsmanager_secret_version" "cloudflare_workers_builds_control_token" {
  count     = local.colton_games_workers_builds_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.cloudflare_workers_builds_control_token.id
}

provider "cloudflare" {
  alias = "workers_builds"

  # While the checked-in rollout gate is off, use the already-valid default credential
  # so bootstrap plans do not require a not-yet-populated secret version. No resource
  # talks to a Workers Builds or user-token endpoint until the gate is enabled.
  api_token = local.colton_games_workers_builds_enabled ? ephemeral.aws_secretsmanager_secret_version.cloudflare_workers_builds_control_token[0].secret_string : data.aws_secretsmanager_secret_version.cloudflare_token.secret_string
}

variable "cloudflare_account_id" {
  description = "Cloudflare account (Ej.campbell@gmail.com's Account)"
  type        = string
  default     = "12ea67fb7ced068de03f35c22688e436"
}

variable "cc_games_zone_id" {
  description = "Zone id for cc-games.dev (registered through Cloudflare Registrar, so the zone was created automatically)"
  type        = string
  default     = "5a7a8d961d72744d2e7fd155dcdeb42b"
}

variable "dev_allowed_emails" {
  description = "Google accounts allowed through Cloudflare Access to every *.cc-games.dev host. Adding someone is a one-line change here."
  type        = list(string)
  default = [
    "thecoltonc2014@gmail.com",
    "theconnorc2014@gmail.com",
    "ej.campbell@gmail.com",
  ]
}

# ---------------------------------------------------------------------------------
# The tunnel. cloudflared on the dev box dials OUT to this, which is why the box needs
# no inbound web ports at all.
# ---------------------------------------------------------------------------------
resource "cloudflare_zero_trust_tunnel_cloudflared" "cc_games_dev" {
  account_id = var.cloudflare_account_id
  name       = "cc-games-dev"
  config_src = "local" # ingress rules are managed on the box by ndev-register

  # The secret is held in Secrets Manager and consumed by the box; terraform must not
  # rotate it on every plan, which would break the running tunnel.
  tunnel_secret = jsondecode(data.aws_secretsmanager_secret_version.cloudflare_tunnel_creds.secret_string)["TunnelSecret"]

  lifecycle {
    ignore_changes = [tunnel_secret]
  }
}

# ---------------------------------------------------------------------------------
# Wildcard DNS. One record covers every project: ndev invents hostnames freely
# (colton.cc-games.dev, tetris.cc-games.dev) with no DNS change per project.
# ---------------------------------------------------------------------------------
resource "cloudflare_dns_record" "cc_games_wildcard" {
  zone_id = var.cc_games_zone_id
  name    = "*"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.cc_games_dev.id}.cfargotunnel.com"
  proxied = true # must be proxied -- cfargotunnel only resolves inside Cloudflare
  ttl     = 1    # 1 = automatic, required when proxied
  comment = "cc-games-dev tunnel"
}

# ---------------------------------------------------------------------------------
# Access: every *.cc-games.dev hostname requires a Google login from the allowlist.
# This is the only thing standing between the dev servers and the open internet, since
# the tunnel terminates inside Cloudflare.
# ---------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------
# Login method. Without this the only identity provider on a fresh Zero Trust account is
# type "cloudflare", which means "must be a member of the Cloudflare account" -- everyone
# else got "Cloudflare sign-in is restricted to members of the account" and had no way in.
# One-time PIN emails a code to whatever address is entered, and the policy above decides
# whether that address is allowed, so plain Gmail works with no Google OAuth app to set up.
# ---------------------------------------------------------------------------------
resource "cloudflare_zero_trust_access_identity_provider" "onetimepin" {
  account_id = var.cloudflare_account_id
  name       = "One-time PIN"
  type       = "onetimepin"
  config     = {}
}

# ---------------------------------------------------------------------------------
# "Sign in with Google" -- the primary login method. One click for anyone already signed
# in to Google in their browser, which is the whole point for the kids' accounts.
#
# The OAuth client itself lives in Google Cloud Console and CANNOT be created from
# terraform -- it needs an interactive browser session as the Google account. So this
# resource consumes credentials that were made by hand once; the secret is the only
# manual artifact in the whole cc-games setup.
#
#   redirect URI configured on the Google client:
#     https://ejc3.cloudflareaccess.com/cdn-cgi/access/callback
#
#   That is the org AUTH DOMAIN, which is NOT the org display name
#   ("white-base-038e.cloudflareaccess.com"). Getting this wrong is invisible until a
#   user actually tries to log in, then fails with redirect_uri_mismatch.
#
#   To check the Google side WITHOUT a browser or anyone's password -- ask Google to
#   authorize and read the error it hands back. A registered URI redirects on toward a
#   sign-in page; an unregistered one comes back with authError:
#
#     CID=$(aws secretsmanager get-secret-value --secret-id cloudflare-google-idp \
#            --region us-west-1 --query SecretString --output text | jq -r .client_id)
#     curl -s -o /dev/null -w '%{redirect_url}\n' \
#       "https://accounts.google.com/o/oauth2/v2/auth?client_id=$CID&response_type=code&scope=openid%20email&redirect_uri=https%3A%2F%2Fejc3.cloudflareaccess.com%2Fcdn-cgi%2Faccess%2Fcallback"
#
#   The authError payload is base64 and decodes to the real reason. "invalid_client"
#   means the client id is wrong; "redirect_uri_mismatch" means the id is fine and only
#   the URI is unregistered. Google also takes 5 minutes to a few hours to propagate
#   console changes, so a fresh edit can read as mismatched for a while.
#
#   The consent screen is "External" with var.dev_allowed_emails as test users. Google
#   only demands app verification once an app serves users beyond that list, so a named
#   handful stays unverified and works -- with a one-time "unverified app" interstitial.
#
# ONE-TIME PIN IS DELIBERATELY LEFT IN PLACE alongside this. Both providers are offered
# on the login page, so a broken or expired OAuth client degrades to "type your email,
# get a code" rather than locking everyone out of every dev server at once.
#
# Access still authorizes on the email allowlist in the policy below, identically for
# either provider -- an IdP only proves the address, it never grants access by itself.
# So adding a login method does not widen who can get in.
# ---------------------------------------------------------------------------------
variable "enable_google_login" {
  description = "Offer 'Sign in with Google' alongside one-time PIN. Needs the cloudflare-google-idp secret (client_id + client_secret)."
  type        = bool
  default     = true
}

data "aws_secretsmanager_secret_version" "google_idp" {
  count     = var.enable_google_login ? 1 : 0
  secret_id = "cloudflare-google-idp"
}

resource "cloudflare_zero_trust_access_identity_provider" "google" {
  count      = var.enable_google_login ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Google"
  type       = "google"

  config = {
    client_id     = jsondecode(data.aws_secretsmanager_secret_version.google_idp[0].secret_string)["client_id"]
    client_secret = jsondecode(data.aws_secretsmanager_secret_version.google_idp[0].secret_string)["client_secret"]
  }
}

# ---------------------------------------------------------------------------------
# The OAuth client cannot be managed as code -- Google publishes no API for "Web
# application" clients or their redirect URIs, so no terraform provider can wrap it.
# (google_iap_client looks like the answer and is not: the IAP OAuth Admin API was shut
# down in March 2026, and even before that the resource had no redirect-URI field.)
#
# What CAN be codified is noticing when the manual half is wrong. Without this, a missing
# redirect URI is invisible to terraform and only surfaces when a person clicks
# "Sign in with Google" and gets redirect_uri_mismatch.
# ---------------------------------------------------------------------------------
locals {
  # One source of truth for the callback: the org AUTH DOMAIN, not the display name.
  cf_access_callback = "https://ejc3.cloudflareaccess.com/cdn-cgi/access/callback"
}

data "external" "google_redirect_uri" {
  count   = var.enable_google_login ? 1 : 0
  program = ["${path.module}/scripts/check-google-redirect.sh"]

  query = {
    client_id    = jsondecode(data.aws_secretsmanager_secret_version.google_idp[0].secret_string)["client_id"]
    redirect_uri = local.cf_access_callback
  }
}

check "google_oauth_redirect_uri_registered" {
  assert {
    condition = !var.enable_google_login || contains(
      ["ok", "unreachable"], # unreachable = network blip, not a real regression
      data.external.google_redirect_uri[0].result.status
    )
    error_message = format(
      "Google is not accepting %s (status: %s). Add it under Authorized redirect URIs at https://console.cloud.google.com/auth/clients?project=669386542811 and Save; propagation takes 5min-a few hours. 'Sign in with Google' stays broken until then -- one-time PIN still works, so nobody is locked out.",
      local.cf_access_callback,
      try(data.external.google_redirect_uri[0].result.status, "unknown")
    )
  }
}

resource "cloudflare_zero_trust_access_application" "cc_games_dev" {
  account_id       = var.cloudflare_account_id
  name             = "cc-games dev servers"
  domain           = "*.cc-games.dev"
  type             = "self_hosted"
  session_duration = "24h"

  # Pinned to the providers this app actually uses. Adding the GitHub idp for dolphin-labs
  # otherwise makes a GitHub button appear on the twins' login page too -- it would be
  # refused by the email policy, but they should not be offered a door that never opens.
  # Splat rather than [0]: google is count-gated, and indexing it would fail the plan
  # whenever Google login is turned off instead of just leaving one-time PIN.
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )

  # The policy attachment MUST be declared here. Leaving it out makes terraform drop the
  # link on the next apply -- which would leave every *.cc-games.dev hostname reachable
  # with no Google login at all. Caught by reading the plan before applying.
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
}

# ---------------------------------------------------------------------------------
# The family board's wall screens (family.cc-games.dev/tv and /ink).
#
# Two locks, one outside the app and one inside it:
#
#   1. Cloudflare (this file). /tv, /ink and /api/device/* are reachable only by
#        - a device enrolled in this account's WARP client by someone on the allowlist
#          (the e-ink tablet; a TV that can run the Cloudflare One app), which Access
#          signs in through WARP so nobody types a password on a screen, or
#        - the home screen proxy: a small reverse proxy on the house network that adds
#          the service token below to every request, for screens that cannot run WARP.
#          It only works for these paths, because only this application accepts it.
#   2. The app. Behind Cloudflare, a screen still has to be paired: it shows a code and a
#      signed-in person types it into /pair (which stays behind the Google login on the
#      wildcard application). See docs/KIOSK.md and docs/DESIGN.md in ejc3/family.
#
# /_next/* (the site's compiled JavaScript, CSS and fonts; nothing private) stays open in
# an application of its own. It is shared with the phone pages, and the phones are neither
# enrolled in WARP nor behind the proxy.
# ---------------------------------------------------------------------------------

# Device enrollment permissions: who may enroll a device into this account's WARP client.
# Cloudflare allows one application of type "warp" per account. If one was already made in
# the dashboard, import it rather than letting this create a second:
#   terraform import cloudflare_zero_trust_access_application.warp_enrollment <account_id>/<app_id>
resource "cloudflare_zero_trust_access_policy" "warp_enrollment" {
  account_id       = var.cloudflare_account_id
  name             = "may enroll devices in WARP"
  decision         = "allow"
  session_duration = "24h"

  include = [
    for email in var.dev_allowed_emails : { email = { email = email } }
  ]
}

resource "cloudflare_zero_trust_access_application" "warp_enrollment" {
  account_id       = var.cloudflare_account_id
  name             = "Warp Login App"
  type             = "warp"
  session_duration = "24h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.warp_enrollment.id
      precedence = 1
    },
  ]
}

# "This request comes from a device running this account's WARP client."
resource "cloudflare_zero_trust_device_posture_rule" "warp_enrolled" {
  account_id  = var.cloudflare_account_id
  name        = "Runs this account's WARP client"
  description = "Used by the family wall screens. Passes only on devices enrolled in this Zero Trust account."
  type        = "warp"
  match = [
    { platform = "android" },
    { platform = "ios" },
    { platform = "mac" },
    { platform = "windows" },
    { platform = "linux" },
    { platform = "chromeos" },
  ]
}

resource "cloudflare_zero_trust_access_policy" "family_wall_screens_warp" {
  account_id       = var.cloudflare_account_id
  name             = "family wall screens: enrolled devices"
  decision         = "allow"
  session_duration = "24h"

  include = [
    for email in var.dev_allowed_emails : { email = { email = email } }
  ]
  require = [{
    device_posture = { integration_uid = cloudflare_zero_trust_device_posture_rule.warp_enrolled.id }
  }]
}

# The home screen proxy's credential. It lives on the proxy in the house; nothing on the
# dev box reads it. Same shape as the other service tokens in this file: finite lifetime,
# its own non_identity policy, the secret in Secrets Manager and nowhere else.
resource "cloudflare_zero_trust_access_service_token" "family_wall_proxy" {
  account_id = var.cloudflare_account_id
  name       = "family-wall-screen-proxy"
  duration   = "8760h" # 1 year
  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "family_wall_proxy" {
  account_id       = var.cloudflare_account_id
  name             = "family wall screens: home screen proxy"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = { token_id = cloudflare_zero_trust_access_service_token.family_wall_proxy.id }
  }]
}

resource "aws_secretsmanager_secret" "family_wall_proxy" {
  name        = "family-wall-screen-proxy-access"
  description = "Cloudflare Access service token for the home proxy in front of the family board's wall screens (/tv, /ink, /api/device/* only)"
  tags        = { Name = "family-wall-screen-proxy-access", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "family_wall_proxy" {
  secret_id = aws_secretsmanager_secret.family_wall_proxy.id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.family_wall_proxy.client_id
    client_secret = cloudflare_zero_trust_access_service_token.family_wall_proxy.client_secret
    base_url      = "https://family.cc-games.dev"
  })
}

resource "cloudflare_zero_trust_access_application" "family_wall_screens" {
  account_id       = var.cloudflare_account_id
  name             = "family board wall screens"
  type             = "self_hosted"
  domain           = "family.cc-games.dev/tv"
  session_duration = "24h"

  destinations = [
    { type = "public", uri = "family.cc-games.dev/tv" },
    { type = "public", uri = "family.cc-games.dev/ink" },
    { type = "public", uri = "family.cc-games.dev/api/device/*" },
  ]

  # An enrolled device is signed in through WARP: no login page on the screen.
  allow_authenticate_via_warp = true
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  # This application shares its hostname with the wildcard one; scope its cookie to its
  # own paths so the two sign-ins do not overwrite each other.
  path_cookie_attribute = true

  # As with cc_games_dev: the attachment MUST be declared here, or an apply drops it and
  # the paths fall back to the wildcard application.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.family_wall_screens_warp.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.family_wall_proxy.id
      precedence = 2
    },
  ]
}

# Was family_wall_screens_bypass, attached to all four paths; now it covers /_next only.
moved {
  from = cloudflare_zero_trust_access_policy.family_wall_screens_bypass
  to   = cloudflare_zero_trust_access_policy.family_wall_assets_bypass
}

resource "cloudflare_zero_trust_access_policy" "family_wall_assets_bypass" {
  account_id = var.cloudflare_account_id
  name       = "family board compiled assets"
  decision   = "bypass"

  include = [{
    everyone = {}
  }]
}

resource "cloudflare_zero_trust_access_application" "family_wall_assets" {
  account_id = var.cloudflare_account_id
  name       = "family board compiled assets"
  type       = "self_hosted"
  domain     = "family.cc-games.dev/_next/*"

  destinations = [
    { type = "public", uri = "family.cc-games.dev/_next/*" },
  ]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.family_wall_assets_bypass.id
      precedence = 1
    },
  ]
}

# The app verifies Access's signed token against this list of audiences
# (FAMILY_ACCESS_AUDS in the site's .env.local).
output "family_wall_screens_aud" {
  description = "Access audience tag of the family wall-screen application; add it to FAMILY_ACCESS_AUDS on the dev box"
  value       = cloudflare_zero_trust_access_application.family_wall_screens.aud
}

# ---------------------------------------------------------------------------------
# Service token: non-interactive access, for anything that cannot sit through a Google
# login -- scripts, health checks, or an agent that wants to fetch the running site.
#
# It is a client id + secret pair sent as CF-Access-Client-Id / CF-Access-Client-Secret
# headers. Cloudflare validates them at the edge and skips the IdP entirely.
#
# SECURITY: this is a bearer credential that bypasses the Gmail allowlist completely.
# Anyone holding it reaches every *.cc-games.dev host. It is therefore:
#   - kept in its own policy with decision = "non_identity", so it is auditable
#     separately from the humans rather than hidden inside the allow rule
#   - written to Secrets Manager, never to disk or the repo
#   - given a finite lifetime so a leak expires on its own
# Revoke by deleting the resource; `terraform apply` invalidates it immediately.
resource "cloudflare_zero_trust_access_service_token" "cc_games_automation" {
  account_id = var.cloudflare_account_id
  name       = "cc-games-automation"

  # Cloudflare only returns client_secret at creation. terraform keeps it in state, and
  # the copy in Secrets Manager below is what anything else should read.
  duration = "8760h" # 1 year
}

resource "aws_secretsmanager_secret" "cc_games_service_token" {
  name        = "cc-games-access-service-token"
  description = "Cloudflare Access service token for non-interactive access to *.cc-games.dev"
  tags        = { Name = "cc-games-access-service-token", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "cc_games_service_token" {
  secret_id = aws_secretsmanager_secret.cc_games_service_token.id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.cc_games_automation.client_id
    client_secret = cloudflare_zero_trust_access_service_token.cc_games_automation.client_secret
  })
}

# Separate policy, NOT folded into the human allowlist. decision = "non_identity" is what
# tells Access to accept a service token instead of requiring a logged-in user.
resource "cloudflare_zero_trust_access_policy" "cc_games_service" {
  account_id       = var.cloudflare_account_id
  name             = "automation service token"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.cc_games_automation.id
    }
  }]
}

resource "cloudflare_zero_trust_access_policy" "cc_games_allowed" {
  account_id       = var.cloudflare_account_id
  name             = "allowed people"
  decision         = "allow"
  session_duration = "24h"

  include = [
    for email in var.dev_allowed_emails : { email = { email = email } }
  ]
}

output "cc_games_tunnel_id" {
  description = "Cloudflare tunnel the dev box connects out through"
  value       = cloudflare_zero_trust_tunnel_cloudflared.cc_games_dev.id
}

output "cc_games_urls" {
  description = "How to publish a project"
  value       = "run `ndev` in a Next.js project -> https://<name>.cc-games.dev (Google login required)"
}

# ---------------------------------------------------------------------------------
# Colton Games staging and pull-request previews.
#
# Application code, versions, bindings, and deployments remain owned by Wrangler in
# CoderColton/colton-games. Terraform owns only the Worker envelope and its routing/auth
# boundary so a plan can never replace the OpenNext bundle.
#
# Keep workers.dev and previews disabled while this application is first created and
# verified; otherwise the first Terraform apply would expose both URL surfaces.
# ---------------------------------------------------------------------------------
locals {
  colton_games_worker_name            = "colton-games-stage"
  colton_games_worker_id              = "72edf31f83e240448fce38bef56104e3"
  colton_games_github_owner_id        = "250920182"
  colton_games_github_repository_id   = "1120877379"
  colton_games_workers_builds_enabled = false
  colton_games_worker_urls_enabled    = false

  # The repository's Wrangler configuration enables workers.dev and preview URLs. Do not
  # create an automatic trigger until Terraform has enabled those surfaces behind Access.
  colton_games_workers_build_triggers_enabled = (
    local.colton_games_workers_builds_enabled && local.colton_games_worker_urls_enabled
  )
}

# The account-wide label is independent of each Worker's URL switches. Owning it here
# lets the Access boundary exist before this Worker's workers.dev and preview URLs are
# enabled. Deleting it would break every workers.dev hostname in the account.
resource "cloudflare_workers_subdomain" "cc_games" {
  account_id = var.cloudflare_account_id
  subdomain  = "cc-games"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_worker" "colton_games_stage" {
  account_id = var.cloudflare_account_id
  name       = local.colton_games_worker_name

  subdomain = {
    enabled          = local.colton_games_worker_urls_enabled
    previews_enabled = local.colton_games_worker_urls_enabled
  }

  # The destination uses the immutable Worker tag below, rather than this resource ID,
  # to avoid an Access <-> Worker graph cycle. This ordering guarantees both protections
  # exist before a later one-line rollout enables either public URL surface.
  depends_on = [
    cloudflare_workers_subdomain.cc_games,
    cloudflare_zero_trust_access_application.colton_games_stage,
  ]

  # The Worker already exists and carries an OpenNext deployment. It must be adopted by
  # the import block below; replacement or destruction would sever the staging service.
  lifecycle {
    prevent_destroy = true

    # Wrangler owns application observability alongside the deployed bundle. The provider
    # defaults this nested object to disabled when it is omitted, which must not overwrite
    # the live Worker setting during import or fight later application deployments.
    ignore_changes = [observability]
  }
}

resource "cloudflare_zero_trust_access_application" "colton_games_stage" {
  account_id       = var.cloudflare_account_id
  name             = "Colton Games staging and previews"
  type             = "self_hosted"
  session_duration = "24h"

  # URLs must be disabled before this protection boundary can be removed.
  lifecycle {
    prevent_destroy = true
  }

  # Worker-native protection follows every production and preview request for this
  # Worker. Unlike a hostname application, it intentionally has no domain.
  destinations = [{
    type      = "worker"
    worker_id = local.colton_games_worker_id
  }]

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
}

import {
  to = cloudflare_worker.colton_games_stage
  id = "${var.cloudflare_account_id}/${local.colton_games_worker_id}"
}

# ---------------------------------------------------------------------------------
# Workers Builds. Cloudflare exposes these account endpoints only to a user-owned
# control-plane token. That token creates a narrower user-owned deployment token; the
# latter is registered with Builds and is the only credential available to build jobs.
# ---------------------------------------------------------------------------------
data "cloudflare_api_token_permission_groups_list" "workers_scripts_write" {
  provider = cloudflare.workers_builds
  count    = local.colton_games_workers_builds_enabled ? 1 : 0
  name     = "Workers%20Scripts%20Write"
}

resource "cloudflare_api_token" "colton_games_build_deploy" {
  provider = cloudflare.workers_builds
  count    = local.colton_games_workers_builds_enabled ? 1 : 0
  name     = "colton-games-workers-builds-deploy"

  policies = [{
    effect = "allow"
    permission_groups = [{
      id = one(data.cloudflare_api_token_permission_groups_list.workers_scripts_write[0].result).id
    }]
    resources = jsonencode({
      "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*"
    })
  }]

  # Cloudflare returns value only at creation. Do not create it until S3 can recover a
  # prior state version, and never let an unrelated plan revoke the live build token.
  depends_on = [aws_s3_bucket_versioning.terraform_state]

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_workers_build_token" "colton_games" {
  provider = cloudflare.workers_builds
  count    = local.colton_games_workers_builds_enabled ? 1 : 0

  account_id          = var.cloudflare_account_id
  build_token_name    = "colton-games-workers-builds-deploy"
  build_token_secret  = cloudflare_api_token.colton_games_build_deploy[0].value
  cloudflare_token_id = cloudflare_api_token.colton_games_build_deploy[0].id

  lifecycle {
    prevent_destroy = true
  }
}

# Cloudflare has no read or list endpoint for repository connections, so the provider
# deliberately preserves the last confirmed state and does not support import. Authorize
# the GitHub App for only this repository before enabling the rollout gate.
resource "cloudflare_workers_build_repository_connection" "colton_games" {
  provider = cloudflare.workers_builds
  count    = local.colton_games_workers_builds_enabled ? 1 : 0

  account_id            = var.cloudflare_account_id
  provider_type         = "github"
  provider_account_id   = local.colton_games_github_owner_id
  provider_account_name = "CoderColton"
  repo_id               = local.colton_games_github_repository_id
  repo_name             = "colton-games"

  depends_on = [aws_s3_bucket_versioning.terraform_state]

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_workers_build_trigger" "colton_games_staging" {
  provider = cloudflare.workers_builds
  count    = local.colton_games_workers_build_triggers_enabled ? 1 : 0

  account_id                 = var.cloudflare_account_id
  external_script_id         = local.colton_games_worker_id
  repository_connection_uuid = cloudflare_workers_build_repository_connection.colton_games[0].id
  build_token_uuid           = cloudflare_workers_build_token.colton_games[0].id
  trigger_name               = "Staging from main"
  build_command              = "npm run cf:build"
  deploy_command             = "npm run cf:deploy:built"
  root_directory             = "/"
  branch_includes            = ["main"]
  branch_excludes            = []
  path_includes              = ["*"]
  path_excludes              = []
  build_caching_enabled      = true

  depends_on = [cloudflare_worker.colton_games_stage]

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_workers_build_trigger" "colton_games_preview" {
  provider = cloudflare.workers_builds
  count    = local.colton_games_workers_build_triggers_enabled ? 1 : 0

  account_id                 = var.cloudflare_account_id
  external_script_id         = local.colton_games_worker_id
  repository_connection_uuid = cloudflare_workers_build_repository_connection.colton_games[0].id
  build_token_uuid           = cloudflare_workers_build_token.colton_games[0].id
  trigger_name               = "Pull request previews"
  build_command              = "npm run cf:build"
  deploy_command             = "npm run cf:upload:built"
  root_directory             = "/"
  branch_includes            = ["*"]
  branch_excludes            = ["main"]
  path_includes              = ["*"]
  path_excludes              = []
  build_caching_enabled      = true

  depends_on = [cloudflare_worker.colton_games_stage]

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_workers_build_trigger_environment_variables" "colton_games_staging" {
  provider = cloudflare.workers_builds
  count    = local.colton_games_workers_build_triggers_enabled ? 1 : 0

  account_id   = var.cloudflare_account_id
  trigger_uuid = cloudflare_workers_build_trigger.colton_games_staging[0].id
  variables = {
    CLOUDFLARE_ACCOUNT_ID = {
      value     = var.cloudflare_account_id
      is_secret = false
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_workers_build_trigger_environment_variables" "colton_games_preview" {
  provider = cloudflare.workers_builds
  count    = local.colton_games_workers_build_triggers_enabled ? 1 : 0

  account_id   = var.cloudflare_account_id
  trigger_uuid = cloudflare_workers_build_trigger.colton_games_preview[0].id
  variables = {
    CLOUDFLARE_ACCOUNT_ID = {
      value     = var.cloudflare_account_id
      is_secret = false
    }
    WRANGLER_CI_GENERATE_PREVIEW_ALIAS = {
      value     = "true"
      is_secret = false
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
