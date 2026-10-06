# workers-stage.tf
#
# The Cloudflare side of the sites that also deploy to Workers (see workers-deploy.tf and
# AGENTS.md, "Sites also deploying to Cloudflare Workers"). Each site's own GitHub Actions
# deploys the application with Wrangler, which creates the Worker `<site>-stage`; this file
# owns what the application deploy must not: the Access gate and the Worker's URL switches,
# so an apply can never replace an application bundle and a deploy can never open a URL.
# It is the same split as colton-games-stage (cloudflare.tf, #16): Terraform for the
# envelope, Wrangler for the code.
#
# TO ADD A SITE, in this order, because a Worker's Access destination is its immutable id and
# that exists only after the first deploy:
#   1. Merge the site's workflow with workers_dev and preview_urls false in its wrangler.jsonc
#      and let the first deploy create the Worker (no public URL).
#   2. Add its name and id (GET /accounts/<id>/workers/workers, or `wrangler deployments`) to
#      local.workers_stage below with urls = false. Apply. The Worker is adopted, the Access
#      application gains it, nothing becomes reachable.
#   3. Set urls = true and, in the repo, workers_dev and preview_urls true. Apply. The URL
#      switches turn on only after the Access application already covers the Worker.
# Never reverse 2 and 3, and never turn a URL on for a Worker that is not in this map.

locals {
  workers_stage = {
    "nest-step-stage"   = { id = "d31376c75b8247f7b0a8e8a7e0fc7dba", urls = false }
    "remote-claw-stage" = { id = "54a603228ad24345b518a9c10cfb3e26", urls = false }
  }
  workers_stage_names = sort(keys(local.workers_stage))
}

# One Worker-native Access application for every staging Worker: it follows each request routed to
# a covered Worker, including its workers.dev hostname and its version previews. It reuses the
# family allowlist and the non-interactive service-token policy of the other cc-games staging
# surfaces. It targets the immutable Worker id, not the Worker resource, so the graph has no cycle.
resource "cloudflare_zero_trust_access_application" "workers_stage" {
  account_id       = var.cloudflare_account_id
  name             = "Site staging Workers"
  type             = "self_hosted"
  session_duration = "24h"

  # URLs must be off for every covered Worker before this protection can be removed.
  lifecycle {
    prevent_destroy = true
  }

  destinations = [
    for name in local.workers_stage_names : {
      type      = "worker"
      worker_id = local.workers_stage[name].id
    }
  ]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.cc_games_allowed.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.cc_games_service.id
      precedence = 2
    },
  ]
}

# Adopt each Worker the first deploy created. Wrangler keeps owning code, bindings and versions.
import {
  for_each = local.workers_stage
  to       = cloudflare_worker.stage[each.key]
  id       = "${var.cloudflare_account_id}/${each.value.id}"
}

resource "cloudflare_worker" "stage" {
  for_each   = local.workers_stage
  account_id = var.cloudflare_account_id
  name       = each.key

  subdomain = {
    enabled          = each.value.urls
    previews_enabled = each.value.urls
  }

  # Both protections exist before a later one-line change can enable a URL surface.
  depends_on = [
    cloudflare_workers_subdomain.cc_games,
    cloudflare_zero_trust_access_application.workers_stage,
  ]

  lifecycle {
    prevent_destroy = true

    # Wrangler owns observability alongside the deployed bundle; the provider would default it off.
    ignore_changes = [observability]
  }
}

output "workers_stage_urls" {
  description = "Where each staging Worker answers once its URLs are on (Access protects every one)"
  value = {
    for name, w in local.workers_stage : name => w.urls ? "https://${name}.cc-games.workers.dev" : "no public URL"
  }
}
