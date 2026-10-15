# gamesworthwatching-zone.tf
#
# gamesworthwatching.com, registered through Cloudflare Registrar on 2026-09-05
# ($10.46/yr, auto-renew on, WHOIS redaction, transfer lock on), and pointed at Vercel.
#
# WHY DNS RECORDS AND NOT NAMESERVER DELEGATION. Cloudflare Registrar will not let a domain
# it registers use anyone else's nameservers -- the Domain Registration Agreement says the
# registrant "MAY NOT CHANGE THE NAMESERVERS ON THE REGISTRAR SERVICES, AND ... MUST TRANSFER
# TO A THIRD PARTY REGISTRAR IF IT WISHES TO CHANGE NAMESERVERS." So "delegate to Vercel"
# here means Cloudflare stays authoritative and these records point at Vercel's edge, which
# is a fully supported Vercel setup. If you ever genuinely need Vercel as the NAMESERVER,
# the domain has to move to a different registrar first.
#
# PRODUCTION IS ON CLOUDFLARE (2026-10-15; the dolphin-labs Vercel team is being retired). With
# local.gww_on_cloudflare the records are proxied and two Workers routes send gamesworthwatching.com/*
# and www.gamesworthwatching.com/* to the Worker dolphin-labs-prod (web/wrangler.jsonc env.production
# in dolphin-labs-hq/dolphin-labs, deployed by its cloudflare-stage.yml; its cf-worker.mjs redirects www
# to the apex, as Vercel did, and runs the every-minute cron vercel.json had). The record contents stay
# Vercel's: a route answers every request, so nothing reaches them, and setting the switch to false is
# the rollback while the Vercel project exists. DNS-only is what Vercel needs: an orange-clouded record
# terminates TLS at Cloudflare, and Vercel's certificate issuance then fails. Either half alone is
# harmless: a route on a DNS-only record does nothing, a proxied record without the route reaches Vercel.

variable "gww_zone_id" {
  description = "Zone id for gamesworthwatching.com (registered through Cloudflare Registrar, so the zone was created automatically)"
  type        = string
  default     = "8548a478c6a0572e46d61d24ab2ca762"
}

# Vercel's long-standing shared endpoints. They still work and are what Vercel documents for
# an apex on external DNS.
#
# Vercel now ALSO hands out per-domain targets (e.g. xyz.vercel-dns-016.com) once a domain is
# attached to a project, and prefers those. Get them with `vercel domains inspect
# gamesworthwatching.com` after adding the domain to the project, then override these two
# variables rather than editing the resources.
variable "gww_vercel_a" {
  description = "Vercel apex A target. Override with the per-domain value from `vercel domains inspect` if Vercel issues one."
  type        = string
  default     = "76.76.21.21"
}

variable "gww_vercel_cname" {
  description = "Vercel www CNAME target. Override with the per-domain value from `vercel domains inspect` if Vercel issues one."
  type        = string
  default     = "cname.vercel-dns.com"
}

locals {
  gww_on_cloudflare = true
  gww_worker        = "dolphin-labs-prod"
}

resource "cloudflare_dns_record" "gww_apex" {
  zone_id = var.gww_zone_id
  name    = "gamesworthwatching.com"
  type    = "A"
  content = var.gww_vercel_a
  proxied = local.gww_on_cloudflare
  ttl     = local.gww_on_cloudflare ? 1 : 300
  comment = local.gww_on_cloudflare ? "proxied; served by Worker ${local.gww_worker}" : "vercel apex"
}

resource "cloudflare_dns_record" "gww_www" {
  zone_id = var.gww_zone_id
  name    = "www"
  type    = "CNAME"
  content = var.gww_vercel_cname
  proxied = local.gww_on_cloudflare
  ttl     = local.gww_on_cloudflare ? 1 : 300
  comment = local.gww_on_cloudflare ? "proxied; Worker ${local.gww_worker} redirects (308) to the apex" : "vercel www"
}

resource "cloudflare_workers_route" "gww" {
  for_each = local.gww_on_cloudflare ? toset(["gamesworthwatching.com/*", "www.gamesworthwatching.com/*"]) : toset([])
  zone_id  = var.gww_zone_id
  pattern  = each.value
  script   = local.gww_worker
}

output "gamesworthwatching" {
  description = "Domain status and what is left to do on the Vercel side"
  value       = local.gww_on_cloudflare ? "gamesworthwatching.com -> Cloudflare Worker ${local.gww_worker} (proxied records, Workers routes)" : "gamesworthwatching.com -> Vercel (A ${var.gww_vercel_a}, www CNAME ${var.gww_vercel_cname}, unproxied)"
}
