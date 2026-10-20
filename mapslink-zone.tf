# mapslink-zone.tf
#
# mapslink.app, registered through Cloudflare Registrar on 2026-10-20 (USD 8.20 the first year, 14.20 to renew, auto-renew
# off, WHOIS redaction). A short address to type into the Tesla's browser: every request, any host in the zone and any
# path, gets a 302 to the Dolphin Maps car screen (https://maps.dolphin-labs.dev/car, public through its bypass Access
# application in dolphin-labs-zone.tf), which shows a QR code to pair with the phone. 302, not 301/308, while the car page
# is new, so the target can change without browsers caching the old one (ejc3/aws #535).
#
# NO ORIGIN, ON PURPOSE, as gamesworthwatching-zone.tf: the records exist only so Cloudflare answers for the names, and the
# redirect rule answers every request at the edge before any origin is contacted. The apex is the Workers-only placeholder
# A 192.0.2.1 (TEST-NET-1) and www a proxied CNAME to the apex. No Access application: the car's browser has no identity.
# .app is on the HSTS preload list, so browsers only ever ask over HTTPS; Always Use HTTPS covers anything else.
#
# CREDENTIAL: Zone Settings write and Dynamic URL Redirects write on this zone, which the provider token
# (cloudflare-tunnel-token) carries for gamesworthwatching.com and this zone (one PUT of its own definition, 2026-10-20).

variable "mapslink_zone_id" {
  description = "Zone id for mapslink.app (registered through Cloudflare Registrar, so the zone was created automatically)"
  type        = string
  default     = "cde9e6667530d0945b6efb971738215b"
}

locals {
  mapslink_target = "https://maps.dolphin-labs.dev/car"
}

resource "cloudflare_dns_record" "mapslink_apex" {
  zone_id = var.mapslink_zone_id
  name    = "mapslink.app"
  type    = "A"
  content = "192.0.2.1"
  proxied = true
  ttl     = 1
  comment = "proxied; redirect rule answers all (302 to the Dolphin Maps car page); no origin"
}

resource "cloudflare_dns_record" "mapslink_www" {
  zone_id = var.mapslink_zone_id
  name    = "www"
  type    = "CNAME"
  content = "mapslink.app"
  proxied = true
  ttl     = 1
  comment = "proxied; redirect rule answers all (302 to the Dolphin Maps car page); no origin"
}

resource "cloudflare_zone_setting" "mapslink_always_use_https" {
  zone_id    = var.mapslink_zone_id
  setting_id = "always_use_https"
  value      = "on"
}

resource "cloudflare_ruleset" "mapslink_redirects" {
  zone_id     = var.mapslink_zone_id
  name        = "mapslink.app redirects"
  description = "Every request to the Dolphin Maps car page (mapslink-zone.tf)"
  kind        = "zone"
  phase       = "http_request_dynamic_redirect"

  rules = [
    {
      ref         = "everything_to_car"
      description = "mapslink.app and www.mapslink.app, any path, 302 to the Dolphin Maps car page"
      expression  = "(http.host in {\"mapslink.app\" \"www.mapslink.app\"})"
      action      = "redirect"
      action_parameters = {
        from_value = {
          status_code           = 302
          target_url            = { value = local.mapslink_target }
          preserve_query_string = false
        }
      }
      enabled = true
    },
  ]
}

output "mapslink" {
  description = "Where mapslink.app goes"
  value       = "mapslink.app and www.mapslink.app -> 302 ${local.mapslink_target}"
}
