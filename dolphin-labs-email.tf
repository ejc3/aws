# The privacy alias on dolphin-labs.dev (local.dolphin_privacy_alias): the contact address Dolphin Maps's privacy policy
# publishes (GDPR Art. 13(1)(a)), forwarded by Cloudflare Email Routing to the owner's own inbox, so the
# owner's personal address is never on the site. The destination is the `dolphin_privacy_forward`
# key of people/addresses (people.tf), set out of band by the owner; never a literal here.
#
# ACTIVATION: (1) put dolphin_privacy_forward (one address) into people/addresses; until then
# nothing below is created. (2) Cloudflare emails that address a verification link when it is
# first added; forwarding starts once it is clicked.
locals {
  # nonsensitive() as for dolphin_steve (dolphin-identity-center.tf): people/addresses arrives marked sensitive, and a
  # count derived from a sensitive value is refused by Terraform.
  dolphin_privacy_forward = nonsensitive(try(tostring(local.people.dolphin_privacy_forward), ""))
  dolphin_privacy_enabled = local.dolphin_enabled && local.dolphin_privacy_forward != ""
  # Assembled, so no address literal sits in the repository (scripts/test-people.py scans every tracked file).
  dolphin_privacy_alias = "privacy@${local.dolphin_labs_domain}"
  dolphin_labs_domain   = "dolphin-labs.dev"
}

resource "cloudflare_email_routing_dns" "dolphin" {
  count   = local.dolphin_email_routing_enabled ? 1 : 0
  zone_id = var.dolphin_zone_id
}

resource "cloudflare_email_routing_address" "owner" {
  count      = local.dolphin_privacy_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  email      = local.dolphin_privacy_forward
}

resource "cloudflare_email_routing_rule" "dolphin_privacy" {
  count    = local.dolphin_privacy_enabled ? 1 : 0
  zone_id  = var.dolphin_zone_id
  name     = "Dolphin Maps privacy contact"
  enabled  = true
  priority = 10
  matchers = [{ type = "literal", field = "to", value = local.dolphin_privacy_alias }]
  actions  = [{ type = "forward", value = [local.dolphin_privacy_forward] }]

  depends_on = [cloudflare_email_routing_dns.dolphin, cloudflare_email_routing_address.owner]
}

# Work addresses on dolphin-labs.dev (owner, 2026-10-18: "a dolphin labs work email that forwards to my gmail ... one
# ej@ and steve@"). ej@ forwards to the owner's inbox, the destination above that is already verified. steve@ forwards
# to Steve's address, the dolphin_steve key of people/addresses (also his Identity Center user name,
# dolphin-identity-center.tf). Cloudflare will not forward to a destination nobody has verified, so his destination is
# created first (Cloudflare emails him a link) and his rule only appears once the account lists that address as
# verified: the first plan after he clicks adds it. Addresses are assembled from parts, never literals
# (scripts/test-people.py).
data "cloudflare_email_routing_addresses" "verified" {
  count      = local.dolphin_enabled ? 1 : 0
  account_id = var.cloudflare_account_id
  verified   = true
  max_items  = 100
}

locals {
  dolphin_email_routing_enabled = local.dolphin_enabled && (local.dolphin_privacy_forward != "" || local.dolphin_steve_email != "")
  dolphin_verified_destinations = local.dolphin_enabled ? [for a in data.cloudflare_email_routing_addresses.verified[0].result : lower(a.email)] : []
  dolphin_work_aliases = {
    for k, v in {
      ej    = local.dolphin_privacy_enabled ? local.dolphin_privacy_forward : ""
      steve = local.dolphin_enabled && contains(local.dolphin_verified_destinations, lower(local.dolphin_steve_email)) ? local.dolphin_steve_email : ""
    } : k => v if v != ""
  }
}

resource "cloudflare_email_routing_address" "steve" {
  count      = local.dolphin_enabled && local.dolphin_steve_email != "" ? 1 : 0
  account_id = var.cloudflare_account_id
  email      = local.dolphin_steve_email
}

resource "cloudflare_email_routing_rule" "dolphin_work" {
  for_each = local.dolphin_work_aliases
  zone_id  = var.dolphin_zone_id
  name     = "Dolphin Labs work address: ${each.key}"
  enabled  = true
  priority = 20
  matchers = [{ type = "literal", field = "to", value = "${each.key}@${local.dolphin_labs_domain}" }]
  actions  = [{ type = "forward", value = [each.value] }]

  depends_on = [cloudflare_email_routing_dns.dolphin, cloudflare_email_routing_address.owner, cloudflare_email_routing_address.steve]
}
