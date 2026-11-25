# lights-signin.tf
#
# The Lights iPhone app's sign-in (ejc3/constellation apps/lights-web/worker/session.ts, docs/LIGHTS.md "Sign-in on the
# phone"), as Costume Party's and Lattice's (constellation-lattice-stage.tf): the owner, 2026-11-25, "For the costume
# app, we gate with cloudflare".
#
#   /native/login        stays behind the lights-stage hostname application (constellation-stage-smoke.tf). The Worker
#                        checks Access's JWT against this team and that application's audience, then mints a session
#                        token of its own, HMAC-signed with LIGHTS_TOKEN_KEY.
#   /api/v1/*            skips Access: a more specific path application with a bypass policy. The Worker's own token is
#                        the gate there (it refuses every request without a valid one; sign-in off means no token is
#                        valid). Nothing else on the host is bypassed.
#
# LIGHTS_TOKEN_KEY, ACCESS_TEAM_DOMAIN and ACCESS_AUD go into workers-stage/lights/terraform (lights-vision.tf), which
# scripts/workers-stage-secrets.sh loads onto the Worker. The key is in Terraform state, as the other Workers' keys are.
#   Rotate (signs everyone out): terraform apply -replace='random_bytes.lights_token_key', then
#   scripts/workers-stage-secrets.sh lights, after a deploy has finished.

# ORDER, as workers-stage.tf's URL switches: (1) apply with lights_phone_bypass = false: the Worker's
# settings exist, nothing skips Access; run scripts/workers-stage-secrets.sh lights after a deploy, and
# check the lights-stage /api/health reports accessCheck: verified and phoneSignIn: on. (2) Only then
# set lights_phone_bypass = true and apply. Before its Access settings arrive, the Worker can only check
# that an Access token header is present on its other routes, which a bypassed request could forge.
locals {
  lights_phone_bypass       = false
  lights_stage_bearer_paths = ["${local.lights_stage_hostname}/api/v1/*"]
}

resource "random_bytes" "lights_token_key" {
  length = 32
}

resource "cloudflare_zero_trust_access_policy" "lights_stage_bearer_paths" {
  count      = local.lights_phone_bypass ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Lights staging: the phone's bearer-token paths (the Worker is the gate)"
  decision   = "bypass"
  include    = [{ everyone = {} }]
}

# A more specific path wins over the hostname's application, so only /api/v1/* skips Access.
resource "cloudflare_zero_trust_access_application" "lights_stage_bearer_paths" {
  count        = local.lights_phone_bypass ? 1 : 0
  account_id   = var.cloudflare_account_id
  name         = "Lights staging: the phone's bearer-token paths"
  type         = "self_hosted"
  domain       = local.lights_stage_bearer_paths[0]
  destinations = [for uri in local.lights_stage_bearer_paths : { type = "public", uri = uri }]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.lights_stage_bearer_paths[0].id
      precedence = 1
    },
  ]
}
