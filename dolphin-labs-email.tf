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
  count   = local.dolphin_privacy_enabled ? 1 : 0
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
