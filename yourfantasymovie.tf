# yourfantasymovie.com: the hostname of the Fantasy Films site (dolphin-labs-hq/dolphin-films, films/web).
#
# Registered through Cloudflare Registrar on 2026-10-05 (see AGENTS.md, "Cloudflare: domains, tokens"), so its zone is in this
# account already. The Vercel project (`dolphin-films`, team `dolphin-labs`, root directory films/web, Git-connected to the repo)
# and its two domains were created with the Vercel CLI as the owner, like imagine's: the Terraform Vercel provider here reaches
# the colton-games team only (vercel.tf). Only the DNS is Terraform's.
#
# PRODUCTION IS ON CLOUDFLARE (2026-10-15; the dolphin-labs Vercel team is being retired). With
# local.yourfantasymovie_on_cloudflare the records are proxied and two Workers routes send yourfantasymovie.com/* and
# www.yourfantasymovie.com/* to the Worker dolphin-films-prod (films/web/wrangler.jsonc env.production, deployed by the
# repository's films-cloudflare-stage.yml; its cf-worker.mjs redirects www to the apex, as Vercel did). The record contents
# stay Vercel's (GET /v6/domains/yourfantasymovie.com/config, "recommendedIPv4" and "recommendedCNAME", rank 1): a route
# answers every request, so nothing reaches them, and setting the switch to false is the rollback while the Vercel project
# exists. DNS-only is what Vercel needs (proxying breaks its certificate issuance, vercel-cc-games.tf). Either half alone is
# harmless: a route on a DNS-only record does nothing, a proxied record without the route reaches Vercel.

variable "yourfantasymovie_zone_id" {
  description = "Cloudflare zone id for yourfantasymovie.com (registered through Cloudflare Registrar 2026-10-05)"
  type        = string
  default     = "3580bb80ae9321c474048a88bfeec525"
}

locals {
  yourfantasymovie_apex_ips      = ["216.150.1.1", "216.150.16.1"]
  yourfantasymovie_on_cloudflare = true
  yourfantasymovie_worker        = "dolphin-films-prod"
}

resource "cloudflare_dns_record" "yourfantasymovie_apex" {
  for_each = toset(local.yourfantasymovie_apex_ips)
  zone_id  = var.yourfantasymovie_zone_id
  name     = "yourfantasymovie.com"
  type     = "A"
  content  = each.value
  proxied  = local.yourfantasymovie_on_cloudflare
  ttl      = local.yourfantasymovie_on_cloudflare ? 1 : 300
  comment  = local.yourfantasymovie_on_cloudflare ? "proxied; served by Worker ${local.yourfantasymovie_worker}" : "vercel apex; Fantasy Films (dolphin-films, films/web)"
}

resource "cloudflare_dns_record" "yourfantasymovie_www" {
  zone_id = var.yourfantasymovie_zone_id
  name    = "www"
  type    = "CNAME"
  content = "ecf035147d87bc65.vercel-dns-016.com"
  proxied = local.yourfantasymovie_on_cloudflare
  ttl     = local.yourfantasymovie_on_cloudflare ? 1 : 300
  comment = local.yourfantasymovie_on_cloudflare ? "proxied; Worker ${local.yourfantasymovie_worker} redirects (308) to the apex" : "vercel www; redirects (308) to yourfantasymovie.com"
}

resource "cloudflare_workers_route" "yourfantasymovie" {
  for_each = local.yourfantasymovie_on_cloudflare ? toset(["yourfantasymovie.com/*", "www.yourfantasymovie.com/*"]) : toset([])
  zone_id  = var.yourfantasymovie_zone_id
  pattern  = each.value
  script   = local.yourfantasymovie_worker
}
