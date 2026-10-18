# yourfantasymovie.com: the hostname of the Fantasy Films site (dolphin-labs-hq/dolphin-films, films/web).
#
# Registered through Cloudflare Registrar on 2026-10-05 (see AGENTS.md, "Cloudflare: domains, tokens"), so its zone is in this
# account already.
#
# SERVED BY THE WORKER dolphin-films-prod, and only by it. The records are proxied and two Workers routes send
# yourfantasymovie.com/* and www.yourfantasymovie.com/* to dolphin-films-prod (films/web/wrangler.jsonc env.production, deployed
# by the repository's films-cloudflare-stage.yml; its cf-worker.mjs redirects www to the apex). Production moved off Vercel on
# 2026-10-15 and the dolphin-labs Vercel team, which held the dolphin-films project, is being deleted (2026-10-18,
# dolphin-vercel-retired.tf), so there is no Vercel rollback any more: no switch, and nothing here may point the zone back at
# Vercel.
#
# THE RECORD CONTENTS ARE INERT. A proxied record is only there so the routes have a hostname to match; a route answers every
# request, so nothing is sent to the content. The values are the ones the records already hold (what Vercel recommended for
# this domain), kept so that this change leaves the serving records exactly as they are. Do not remove a route while the
# content is still these: with the team gone, a request that reached them would land on Vercel's edge for a domain nobody
# there owns. Replacing them with an originless placeholder (an in-place content change) is a separate step.

variable "yourfantasymovie_zone_id" {
  description = "Cloudflare zone id for yourfantasymovie.com (registered through Cloudflare Registrar 2026-10-05)"
  type        = string
  default     = "3580bb80ae9321c474048a88bfeec525"
}

locals {
  yourfantasymovie_apex_ips = ["216.150.1.1", "216.150.16.1"]
  yourfantasymovie_worker   = "dolphin-films-prod"
}

resource "cloudflare_dns_record" "yourfantasymovie_apex" {
  for_each = toset(local.yourfantasymovie_apex_ips)
  zone_id  = var.yourfantasymovie_zone_id
  name     = "yourfantasymovie.com"
  type     = "A"
  content  = each.value
  proxied  = true
  ttl      = 1
  comment  = "proxied; served by Worker ${local.yourfantasymovie_worker}"
}

resource "cloudflare_dns_record" "yourfantasymovie_www" {
  zone_id = var.yourfantasymovie_zone_id
  name    = "www"
  type    = "CNAME"
  content = "ecf035147d87bc65.vercel-dns-016.com"
  proxied = true
  ttl     = 1
  comment = "proxied; Worker ${local.yourfantasymovie_worker} redirects (308) to the apex"
}

resource "cloudflare_workers_route" "yourfantasymovie" {
  for_each = toset(["yourfantasymovie.com/*", "www.yourfantasymovie.com/*"])
  zone_id  = var.yourfantasymovie_zone_id
  pattern  = each.value
  script   = local.yourfantasymovie_worker
}
