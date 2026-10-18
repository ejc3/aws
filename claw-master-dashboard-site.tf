# ---------------------------------------------------------------------------------
# The claw-master dashboard site (ejc3/claw-master-dashboard): a Cloudflare Worker that
# reads the ClawMaster CloudWatch namespace, ClaudeMaster before the rename (claw-master-dashboard.tf is the console
# dashboard over the same metrics).
#
# The same split as workers-stage.tf: the repository's Deploy workflow (Wrangler, with the shared
# token from workers-deploy.tf) owns the Worker's code and deployments; this file owns what the
# deploy must not:
#
#   1. Cloudflare Access on the Worker itself (a worker destination, so workers.dev and preview
#      URLs alike), reusing this account's Google and one-time PIN identity providers (no new
#      OAuth client). Its policy admits the owner and the people the owner named for it (the
#      `claude_master_dashboard` list in people/addresses: Colton and Connor, 2026-10-10), never
#      the family-wide cc-games allowlist as such: the dashboard names subscription accounts and
#      the people using them.
#   2. The app's own allowlist. The Worker checks the Access assertion again, against a list it
#      reads from the Workers KV namespace below (binding DASHBOARD_CONFIG, key allowed-emails).
#      Terraform writes that key from the same local as the Access policy, so people/addresses is
#      the one place to add or remove a person; nobody sets the Worker's list by hand. The second
#      check matters after a removal: Access refuses the person's next sign-in at once, but a
#      session it already issued lasts 24h, and the Worker refuses that session within about two
#      minutes (KV's cache plus the Worker's own).
#   3. The app's account aliases (key account-aliases in the same namespace), derived from
#      claude-master/account-labels, the names claw-master itself reports, so that secret is the
#      one place to name an account; nobody sets the Worker's aliases by hand.
#   4. The Worker's URL switches, which turn on only after Access covers it.
#   5. A read-only AWS key for the Worker, which cannot assume an AWS role (Workers have no
#      OIDC identity AWS accepts). It may only read CloudWatch metrics in this region.
#
# ORDER, because a Worker's Access destination is its immutable id and that exists only after the
# first deploy (as in workers-stage.tf):
#   1. The repository deploys with workers_dev and preview_urls false: the Worker exists with no
#      public URL. Nothing below is created while the id is empty.
#   2. Set id (GET /accounts/<id>/workers/workers, or `wrangler deployments`) with urls = false
#      and apply: Access adopts the Worker; nothing becomes reachable.
#   3. Set urls = true here and workers_dev true in the repository's wrangler.jsonc; apply.
# Never reverse 2 and 3. After step 2, set the Worker's secrets and its KV namespace variable from
# output "claw_master_dashboard" (docs/deploy.md in that repository).
# ---------------------------------------------------------------------------------

locals {
  # Each Worker gets its own Access application and URL switches, and follows the ORDER above on its own.
  # The rename (#415) retired claude-master-dashboard (id a3016d88fb32412aa5fff7e271eb17a3): taking it out
  # here destroys its URL switches first (a delete turns workers.dev and previews off; they depend on
  # the application), then its Access application. The dashboard repository then deletes the Worker.
  claw_master_dashboard_worker_list = [
    { name = "claw-master-dashboard", id = "df3cf048457e4063b4e60ce88d9388f0", urls = true, path = "/claw-master" },
  ]
  # A Worker is left out until the first deploy has created it and its id is set.
  claw_master_dashboard_workers = { for w in local.claw_master_dashboard_worker_list : w.name => w if w.id != "" }

  # Who the dashboard admits: the owner, then the people named for it. The ONE list: the Access policy reads it and
  # Terraform writes it to the Worker's KV allowlist below. Unmarked as people.tf explains, each part on its own
  # (nonsensitive() on the concatenation would leave the elements marked); the policy took exactly these values before.
  claw_master_dashboard_people = concat([nonsensitive(local.people.owner)], try(nonsensitive(local.people.claude_master_dashboard), []))
}

resource "cloudflare_zero_trust_access_policy" "claw_master_dashboard_owner" {
  account_id       = var.cloudflare_account_id
  name             = "claude-master dashboard: owner"
  decision         = "allow"
  session_duration = "24h"

  include = [for e in local.claw_master_dashboard_people : { email = { email = e } }]
}

# ---------------------------------------------------------------------------------
# The Worker's allowlist (2 above). Workers KV, because the pinned provider (ejc3/cloudflare 5.24.0) has no resource
# that sets one secret on a Worker without owning its code: v5 dropped cloudflare_workers_secret, and a secret_text
# binding exists only on cloudflare_workers_script / cloudflare_worker_version, which would take the code away from
# the repository's Wrangler deploys. A KV value is a separate object: Terraform writes it, a deploy never touches it.
#
# The Worker binds the namespace by id (output claw_master_dashboard, allowlist_kv_namespace_id). The id is not a
# credential; the deploy workflow takes it from the repository's production environment, as it takes the account id.
# Writing it needs Workers KV Storage Write on the provider token (AGENTS.md, "The claw-master dashboard site").
# The list is never empty (the owner is always on it), and the Worker admits nobody if the key is missing or empty.
# ---------------------------------------------------------------------------------
resource "cloudflare_workers_kv_namespace" "claw_master_dashboard_config" {
  account_id = var.cloudflare_account_id
  title      = "claw-master-dashboard-config"

  lifecycle {
    # The deployed Worker's binding names this id: a replacement would leave it bound to a deleted namespace, admitting
    # nobody, until the repository's variable and a new deploy caught up.
    prevent_destroy = true
  }
}

resource "cloudflare_workers_kv" "claw_master_dashboard_allowed_emails" {
  account_id   = var.cloudflare_account_id
  namespace_id = cloudflare_workers_kv_namespace.claw_master_dashboard_config.id
  key_name     = "allowed-emails"
  # Comma-separated, as the DASHBOARD_ALLOWED_EMAILS secret it replaces was. Marked sensitive here because the
  # provider's attribute is not: a plan shows that the list changed, never the addresses.
  value = sensitive(join(",", local.claw_master_dashboard_people))
}

# ---------------------------------------------------------------------------------
# The Worker's account aliases (3 above), from the names claw-master itself uses. Points exported before an account had
# a name carry its code, acct-<8 hex>; the dashboard folds each code into the person's name so they have one row. The
# names live in
# claude-master/account-labels (claw-master-server.tf), so Terraform derives the aliases from that secret and writes them
# to the same namespace, key account-aliases, one `acct-<8 hex>=NAME` per line (it replaces the hand-set
# DASHBOARD_ACCOUNT_ALIASES Worker secret). Adding a name is one edit of that secret and an apply.
#
# The derivation is claw-master's own (ejc3/CLIProxyAPI, internal/clawmaster/metrics.go):
#   - the code is AccountKeyFor: "acct-" + the first 4 bytes, in hex, of sha256(lower(trim(ACCOUNT_UUID)));
#   - the name is what the server reports for a labelled account, sanitizeDimension(label): each run of characters
#     outside [A-Za-z0-9._-] becomes "-", at most 48 characters;
#   - the lines are read by its loadAccountLabels rule (cmd/claw-master/main.go): blank lines and # comments are
#     skipped, the id is 8 to 64 hex digits and dashes, split at the first "=" (at the first space when there is no
#     "="), and a later line for the same account wins. A line that does not fit is skipped here (the server keeps its
#     old file on one, claw-master-server.tf).
# Everything derived from the secret stays marked sensitive: a plan shows that the aliases changed, never a name or code.
# ---------------------------------------------------------------------------------
data "aws_secretsmanager_secret_version" "claw_master_account_labels" {
  count     = var.enable_claw_master_server ? 1 : 0
  secret_id = aws_secretsmanager_secret.claw_master_account_labels[0].id
}

locals {
  claw_master_account_label_lines = [
    for l in split("\n", replace(try(data.aws_secretsmanager_secret_version.claw_master_account_labels[0].secret_string, ""), "\r", "")) : trimspace(l)
  ]
  # [id, name] for each line that is a label.
  claw_master_account_label_pairs = concat([], [
    for l in local.claw_master_account_label_lines : startswith(l, "#") ? [] : (
      strcontains(l, "=") ? regexall("^([0-9A-Fa-f-]{8,64})\\s*=\\s*(.*\\S)$", l) : regexall("^([0-9A-Fa-f-]{8,64})\\s+(.*\\S)$", l)
    )
  ]...)
  # account id => every name given to it, in order (the last one wins, as in claw-master).
  claw_master_account_label_names = { for p in local.claw_master_account_label_pairs : lower(p[0]) => p[1]... }
  claw_master_dashboard_account_aliases = sensitive(join("\n", concat(
    ["# acct-<8 hex>=NAME, written by Terraform from claude-master/account-labels (claw-master-dashboard-site.tf)"],
    sort([
      for id, names in local.claw_master_account_label_names :
      "acct-${substr(sha256(id), 0, 8)}=${substr(replace(names[length(names) - 1], "/[^A-Za-z0-9._-]+/", "-"), 0, 48)}"
    ]),
  )))
}

resource "cloudflare_workers_kv" "claw_master_dashboard_account_aliases" {
  account_id   = var.cloudflare_account_id
  namespace_id = cloudflare_workers_kv_namespace.claw_master_dashboard_config.id
  key_name     = "account-aliases"
  # The header line keeps the value non-empty when there are no labels; the Worker skips # lines.
  value = local.claw_master_dashboard_account_aliases
}

resource "cloudflare_zero_trust_access_application" "claw_master_dashboard" {
  for_each         = local.claw_master_dashboard_workers
  account_id       = var.cloudflare_account_id
  name             = replace(each.key, "-dashboard", " dashboard")
  type             = "self_hosted"
  session_duration = "24h"

  # A replacement would get a new audience tag, and the Worker would refuse everyone until its
  # CF_ACCESS_AUD secret is set again; URLs must be off before this protection can go.
  lifecycle {
    prevent_destroy = true
  }

  destinations = [{
    type      = "worker"
    worker_id = each.value.id
  }]

  # The same login buttons as the cc-games sites, and no others.
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )

  # Declared here, not only on the policy: without the attachment an apply would leave the
  # Worker with no Access check at all.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.claw_master_dashboard_owner.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.claw_master_dashboard_agents.id
      precedence = 2
    },
  ]
}

# Terraform owns only the Worker's two URL switches, as for the staging Workers.
import {
  for_each = local.claw_master_dashboard_workers
  to       = cloudflare_workers_script_subdomain.claw_master_dashboard[each.key]
  id       = "${var.cloudflare_account_id}/${each.key}"
}

resource "cloudflare_workers_script_subdomain" "claw_master_dashboard" {
  for_each         = local.claw_master_dashboard_workers
  account_id       = var.cloudflare_account_id
  script_name      = each.key
  enabled          = each.value.urls
  previews_enabled = false

  depends_on = [
    cloudflare_workers_subdomain.cc_games,
    cloudflare_zero_trust_access_application.claw_master_dashboard,
  ]
}

# ---------------------------------------------------------------------------------
# Read-only CloudWatch access for the Worker.
# ---------------------------------------------------------------------------------
resource "aws_iam_user" "claw_master_dashboard" {
  name = "claude-master-dashboard-reader"
  tags = { Name = "claude-master-dashboard-reader", Managed = "terraform" }
}

resource "aws_iam_user_policy" "claw_master_dashboard" {
  name = "read-claude-master-metrics"
  user = aws_iam_user.claw_master_dashboard.name

  # CloudWatch has no resource-level permissions for these reads, so they cannot be narrowed
  # to one namespace; the dashboard queries only ClawMaster. The region condition keeps the
  # key to the region the metrics live in.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "ReadMetrics"
      Effect    = "Allow"
      Action    = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
      Resource  = "*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      }, {
      # The Alarms box reads alarms named claude-master* (#384). It cannot be scoped to that prefix: AWS authorizes
      # DescribeAlarms against arn:...:alarm:* whatever AlarmNamePrefix asks for (checked 2026-10-11, the prefix-scoped
      # grant was refused with exactly that resource). So: every alarm in this account and region, read only.
      Sid      = "ReadAlarms"
      Effect   = "Allow"
      Action   = "cloudwatch:DescribeAlarms"
      Resource = "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:*"
    }]
  })
}

resource "aws_iam_access_key" "claw_master_dashboard" {
  user = aws_iam_user.claw_master_dashboard.name
}

resource "aws_secretsmanager_secret" "claw_master_dashboard_reader" {
  name        = "claude-master-dashboard/aws-reader"
  description = "Read-only CloudWatch key for the claude-master dashboard Worker (set as its Worker secrets)"
  tags        = { Name = "claude-master-dashboard/aws-reader", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "claw_master_dashboard_reader" {
  secret_id = aws_secretsmanager_secret.claw_master_dashboard_reader.id
  secret_string = jsonencode({
    AWS_ACCESS_KEY_ID     = aws_iam_access_key.claw_master_dashboard.id
    AWS_SECRET_ACCESS_KEY = aws_iam_access_key.claw_master_dashboard.secret
    AWS_REGION            = var.aws_region
  })
}

# ---------------------------------------------------------------------------------
# Agents on the metal boxes (#374): a service token of their own, so an agent can read what the dashboard shows
# (`/api/claude-master/snapshot`) and check a deploy, as the owner does in a browser. The token is admitted by a
# non_identity policy after the owner's; the Worker admits exactly its client id (DASHBOARD_SERVICE_TOKEN_CLIENT_ID,
# matched against the assertion's common_name) and still checks every person against its KV allowlist (above).
# Readers: dev-server-role only (the metal boxes), which already reads the ClawMaster metrics and the
# /claude-master/server log the dashboard is built from, so the page shows it nothing new. Not nextjs-dev-role:
# every account there has sudo. Not the cc-games automation token or the Macs' token: revoking this touches nothing else.
resource "cloudflare_zero_trust_access_service_token" "claw_master_dashboard_agents" {
  account_id = var.cloudflare_account_id
  name       = "claude-master-dashboard-agents"
  duration   = "8760h"

  lifecycle {
    # A replacement changes the client id the Worker admits: set DASHBOARD_SERVICE_TOKEN_CLIENT_ID again after it.
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "claw_master_dashboard_agents" {
  account_id       = var.cloudflare_account_id
  name             = "claude-master dashboard: metal-box agents"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.claw_master_dashboard_agents.id
    }
  }]
}

resource "aws_secretsmanager_secret" "claw_master_dashboard_agent_access" {
  name        = "claude-master-dashboard/agent-access"
  description = "Cloudflare Access service token for metal-box agents to read the claude-master dashboard (client_id, client_secret)"
  tags        = { Name = "claude-master-dashboard/agent-access", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "claw_master_dashboard_agent_access" {
  secret_id = aws_secretsmanager_secret.claw_master_dashboard_agent_access.id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.claw_master_dashboard_agents.client_id
    client_secret = cloudflare_zero_trust_access_service_token.claw_master_dashboard_agents.client_secret
  })
}

resource "aws_secretsmanager_secret_policy" "claw_master_dashboard_agent_access" {
  secret_arn = aws_secretsmanager_secret.claw_master_dashboard_agent_access.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndMetalBoxes"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.claw_master_dashboard_agent_access.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn]) } }
    }]
  })
}

resource "aws_iam_role_policy" "dev_server_claw_master_dashboard_access" {
  name = "claude-master-dashboard-agent-access"
  role = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.claw_master_dashboard_agent_access.arn
    }]
  })
}

output "claw_master_dashboard" {
  description = "What the claude-master dashboard Worker's secrets need (docs/deploy.md in ejc3/claw-master-dashboard)"
  value = {
    # Per Worker: its address, and its own application's audience (its CF_ACCESS_AUD secret).
    workers = { for w in local.claw_master_dashboard_worker_list : w.name => {
      url = w.urls ? "https://${w.name}.${cloudflare_workers_subdomain.cc_games.subdomain}.workers.dev${w.path}" : "no public URL"
      cf_access_aud = try(
        cloudflare_zero_trust_access_application.claw_master_dashboard[w.name].aud,
        "not yet: set the Worker id (step 2)",
      )
    } }
    cf_access_team_domain = trimsuffix(local.cf_access_callback, "/cdn-cgi/access/callback")
    aws_reader_secret     = aws_secretsmanager_secret.claw_master_dashboard_reader.name
    # The Worker secret DASHBOARD_SERVICE_TOKEN_CLIENT_ID (not secret: the client secret is the credential).
    agent_service_token_client_id = cloudflare_zero_trust_access_service_token.claw_master_dashboard_agents.client_id
    agent_access_secret           = aws_secretsmanager_secret.claw_master_dashboard_agent_access.name
    # The production environment's DASHBOARD_CONFIG_KV_ID variable in ejc3/claw-master-dashboard (not secret). The
    # list itself is never an output.
    allowlist_kv_namespace_id = cloudflare_workers_kv_namespace.claw_master_dashboard_config.id
  }
}
