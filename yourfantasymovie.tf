# yourfantasymovie.com: the hostname of the Fantasy Films site (dolphin-labs-hq/dolphin-films, films/web).
#
# Registered through Cloudflare Registrar on 2026-10-05 (see AGENTS.md, "Cloudflare: domains, tokens"), so its zone is in this
# account already. The Vercel project (`dolphin-films`, team `dolphin-labs`, root directory films/web, Git-connected to the repo)
# and its two domains were created with the Vercel CLI as the owner, like imagine's: the Terraform Vercel provider here reaches
# the colton-games team only (vercel.tf). Only the DNS is Terraform's.
#
# DNS only, never proxied: proxying breaks Vercel's certificate issuance (vercel-cc-games.tf). The values are the ones Vercel
# reports for this project (GET /v6/domains/yourfantasymovie.com/config, "recommendedIPv4" and "recommendedCNAME", rank 1).
# `www` is a Vercel redirect (308) to the apex.

variable "yourfantasymovie_zone_id" {
  description = "Cloudflare zone id for yourfantasymovie.com (registered through Cloudflare Registrar 2026-10-05)"
  type        = string
  default     = "3580bb80ae9321c474048a88bfeec525"
}

locals {
  yourfantasymovie_apex_ips = ["216.150.1.1", "216.150.16.1"]
}

resource "cloudflare_dns_record" "yourfantasymovie_apex" {
  for_each = toset(local.yourfantasymovie_apex_ips)
  zone_id  = var.yourfantasymovie_zone_id
  name     = "yourfantasymovie.com"
  type     = "A"
  content  = each.value
  proxied  = false # proxying breaks Vercel cert issuance
  ttl      = 300
  comment  = "vercel apex; Fantasy Films (dolphin-films, films/web)"
}

resource "cloudflare_dns_record" "yourfantasymovie_www" {
  zone_id = var.yourfantasymovie_zone_id
  name    = "www"
  type    = "CNAME"
  content = "ecf035147d87bc65.vercel-dns-016.com"
  proxied = false
  ttl     = 300
  comment = "vercel www; redirects (308) to yourfantasymovie.com"
}
