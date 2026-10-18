# gamesworthwatching-zone.tf
#
# gamesworthwatching.com, registered through Cloudflare Registrar on 2026-09-05 ($10.46/yr, auto-renew on, WHOIS
# redaction, transfer lock on). Cloudflare Registrar keeps the zone's nameservers on Cloudflare, so this zone is where
# its records live.
#
# SERVED BY THE WORKER dolphin-labs-prod, and only by it. The records are proxied and two Workers routes send
# gamesworthwatching.com/* and www.gamesworthwatching.com/* to dolphin-labs-prod (web/wrangler.jsonc env.production in
# dolphin-labs-hq/dolphin-labs, deployed by its cloudflare-stage.yml; its cf-worker.mjs redirects www to the apex and
# runs the every-minute cron as a Cron Trigger). Production moved off Vercel on 2026-10-15 and the dolphin-labs Vercel
# team is being deleted (2026-10-18, dolphin-vercel-retired.tf), so there is no Vercel rollback any more: no switch, and
# nothing here points at Vercel.
#
# NO ORIGIN, ON PURPOSE. A proxied record exists only so the routes have a hostname to match; a route answers every
# request, so the content is never contacted. The apex is the standard Workers-only placeholder, A 192.0.2.1 (TEST-NET-1,
# routes nowhere), and www is a proxied CNAME to the apex, so neither names anything outside this zone. www stays a CNAME
# because the provider replaces a record whose type changes and Cloudflare refuses an A record beside a CNAME of the same
# name: changing the type would leave www without a record between the delete and the create. The zone has no AAAA
# records for either name (kv and kv-stage are dolphin-redis.tf's tunnel records).

variable "gww_zone_id" {
  description = "Zone id for gamesworthwatching.com (registered through Cloudflare Registrar, so the zone was created automatically)"
  type        = string
  default     = "8548a478c6a0572e46d61d24ab2ca762"
}

locals {
  gww_worker = "dolphin-labs-prod"
}

resource "cloudflare_dns_record" "gww_apex" {
  zone_id = var.gww_zone_id
  name    = "gamesworthwatching.com"
  type    = "A"
  content = "192.0.2.1"
  proxied = true
  ttl     = 1
  comment = "proxied; Worker ${local.gww_worker} route serves all; origin unreachable on purpose"
}

resource "cloudflare_dns_record" "gww_www" {
  zone_id = var.gww_zone_id
  name    = "www"
  type    = "CNAME"
  content = "gamesworthwatching.com"
  proxied = true
  ttl     = 1
  comment = "proxied; Worker ${local.gww_worker} route serves all (308 to apex); no origin"
}

resource "cloudflare_workers_route" "gww" {
  for_each = toset(["gamesworthwatching.com/*", "www.gamesworthwatching.com/*"])
  zone_id  = var.gww_zone_id
  pattern  = each.value
  script   = local.gww_worker
}

output "gamesworthwatching" {
  description = "What serves gamesworthwatching.com"
  value       = "gamesworthwatching.com -> Cloudflare Worker ${local.gww_worker} (proxied records, Workers routes)"
}

# HTTPS AND THE APEX FOR EVERYTHING, ASSETS INCLUDED (dolphin-labs#583). The Worker redirects www and plain http to
# https://gamesworthwatching.com (cf-worker.mjs), but only for what reaches it: its static assets (/_next/static/...,
# /data/*.json, images) are served before the Worker runs, so http:// and www requests for them were answered in place.
# These run at the edge ahead of the Worker and its assets. Not `run_worker_first` in wrangler.jsonc: that sends every
# asset through the Worker, which costs an invocation per file and puts the Worker's cold start (measured 2026-10-16:
# p99 785 ms for its http redirect, against 143 ms for an asset) in front of them. Both rules act on proxied
# traffic, which is all of it: both records are always proxied.
#
# CREDENTIAL: Zone Settings write and Single Redirect (Dynamic URL Redirects) write on this zone, which the provider
# token carries for cc-games.org only (AGENTS.md); its policy needs this zone added first, the same one-PUT change.
resource "cloudflare_zone_setting" "gww_always_use_https" {
  zone_id    = var.gww_zone_id
  setting_id = "always_use_https"
  value      = "on"
}

resource "cloudflare_ruleset" "gww_redirects" {
  zone_id     = var.gww_zone_id
  name        = "gamesworthwatching.com redirects"
  description = "www to the apex before the Worker and its assets (gamesworthwatching-zone.tf)"
  kind        = "zone"
  phase       = "http_request_dynamic_redirect"

  rules = [
    {
      ref         = "www_to_apex"
      description = "www.gamesworthwatching.com to https://gamesworthwatching.com, path and query kept (308, as the Worker answers)"
      expression  = "(http.host eq \"www.gamesworthwatching.com\")"
      action      = "redirect"
      action_parameters = {
        from_value = {
          status_code           = 308
          target_url            = { expression = "concat(\"https://gamesworthwatching.com\", http.request.uri.path)" }
          preserve_query_string = true
        }
      }
      enabled = true
    },
  ]
}
