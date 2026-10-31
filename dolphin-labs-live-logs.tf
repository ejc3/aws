# dolphin-labs-live-logs.tf
#
# The R2 bucket for dolphin-labs-hq/dolphin-labs' live capture (docs/ENGINE.md "Live capture" there): one Durable
# Object a league in the dolphin-labs Workers polls every provider about each game in play and keeps each provider's
# raw answers in its own storage. Once a day is over, the object writes that day's logs here
# (live/<league>/<ET date>/<provider>.jsonl) and deletes them from its storage; without the bucket they stay in Durable
# Object storage, which grows about 12 MB a live game-hour (measured 2026-10-30), some 29 GB in a month of NBA, NHL,
# NFL and college games.
#
# The Workers bind it as LIVE_LOGS (web/wrangler.jsonc, top level and env.production), in a change that follows this
# apply: a binding to a bucket that does not exist fails the deploy. The deploy token (workers-deploy.tf: Workers
# Scripts Write) is expected to suffice for the binding, as it does for constellation-mcp-stage's binding to a KV
# namespace this file's neighbours create; the first deploy with the binding is the check.
#
# Nothing is public: no custom domain, no r2.dev URL. The objects are raw provider answers, which the repository keeps
# out of git.
resource "cloudflare_r2_bucket" "dolphin_labs_live_logs" {
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-live-logs"

  lifecycle {
    # The Workers' binding names this bucket; a replacement would leave it bound to a deleted one.
    prevent_destroy = true
  }
}
