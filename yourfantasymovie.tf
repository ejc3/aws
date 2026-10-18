# yourfantasymovie.com: the hostname of the Fantasy Films site (dolphin-labs-hq/dolphin-films, films/web).
#
# Registered through Cloudflare Registrar on 2026-10-05 (see AGENTS.md, "Cloudflare: domains, tokens"), so its zone is in this
# account already.
#
# SERVED BY THE WORKER dolphin-films-prod, and only by it. The records are proxied and two Workers routes send
# yourfantasymovie.com/* and www.yourfantasymovie.com/* to dolphin-films-prod (films/web/wrangler.jsonc env.production, deployed
# by the repository's films-cloudflare-stage.yml; its cf-worker.mjs redirects www to the apex). Production moved off Vercel on
# 2026-10-15 and the dolphin-labs Vercel team, which held the dolphin-films project, is being deleted (2026-10-18,
# ejc3/aws #503), so there is no Vercel rollback: no switch, and nothing here points at Vercel.
#
# NO ORIGIN, ON PURPOSE. A proxied record exists only so the routes have a hostname to match; a route answers every request,
# so the content is never contacted. The apex is the standard Workers-only placeholder, A 192.0.2.1 (TEST-NET-1, routes
# nowhere), and www is a proxied CNAME to the apex, so neither names anything outside this zone. www stays a CNAME because the
# provider replaces a record whose type changes and Cloudflare refuses an A record beside a CNAME of the same name: changing
# the type would leave www without a record between the delete and the create. The zone has no AAAA records.

variable "yourfantasymovie_zone_id" {
  description = "Cloudflare zone id for yourfantasymovie.com (registered through Cloudflare Registrar 2026-10-05)"
  type        = string
  default     = "3580bb80ae9321c474048a88bfeec525"
}

locals {
  yourfantasymovie_worker = "dolphin-films-prod"
}

resource "cloudflare_dns_record" "yourfantasymovie_apex" {
  zone_id = var.yourfantasymovie_zone_id
  name    = "yourfantasymovie.com"
  type    = "A"
  content = "192.0.2.1"
  proxied = true
  ttl     = 1
  comment = "proxied; Worker ${local.yourfantasymovie_worker} route serves all; origin unreachable on purpose"
}

# The apex used to be two A records (Vercel's two addresses, for_each by address). One becomes the placeholder in place; the
# other, cloudflare_dns_record.yourfantasymovie_apex["216.150.16.1"], is deleted (two identical placeholder records are
# refused). The apex keeps a proxied record throughout.
moved {
  from = cloudflare_dns_record.yourfantasymovie_apex["216.150.1.1"]
  to   = cloudflare_dns_record.yourfantasymovie_apex
}

resource "cloudflare_dns_record" "yourfantasymovie_www" {
  zone_id = var.yourfantasymovie_zone_id
  name    = "www"
  type    = "CNAME"
  content = "yourfantasymovie.com"
  proxied = true
  ttl     = 1
  comment = "proxied; Worker ${local.yourfantasymovie_worker} route serves all (308 to apex); no origin"
}

resource "cloudflare_workers_route" "yourfantasymovie" {
  for_each = toset(["yourfantasymovie.com/*", "www.yourfantasymovie.com/*"])
  zone_id  = var.yourfantasymovie_zone_id
  pattern  = each.value
  script   = local.yourfantasymovie_worker
}
