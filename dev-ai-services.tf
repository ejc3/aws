# dev-ai-services.tf
#
# The ElevenLabs API key (voice and sound for the games), for the dev boxes and the games.
# Terraform owns only the secret's container and who may read it; the value is set out of band
# (it came from the owner) and never enters git:
#   aws secretsmanager put-secret-value --secret-id games/elevenlabs-api-key \
#     --secret-string file://<a 0600 file>
# Readers: the administration set, dev-server-role (the metal boxes) and nextjs-dev-role (where
# the games are built). Agents fetch it when they need it; nothing writes it to disk on the
# boxes.

resource "aws_secretsmanager_secret" "elevenlabs_api_key" {
  name                    = "games/elevenlabs-api-key"
  description             = "ElevenLabs API key (voice and sound for the games). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "games/elevenlabs-api-key", Project = "games" }
}

locals {
  elevenlabs_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]
}

# Nobody but the administration set and the two dev roles may read it, whatever an identity
# policy elsewhere says.
resource "aws_secretsmanager_secret_policy" "elevenlabs_api_key" {
  secret_arn = aws_secretsmanager_secret.elevenlabs_api_key.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheDevBoxesCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.elevenlabs_api_key.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.elevenlabs_readers) } }
    }]
  })
}

data "aws_iam_policy_document" "elevenlabs_read" {
  statement {
    sid       = "ReadTheElevenLabsKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.elevenlabs_api_key.arn]
  }
}

resource "aws_iam_policy" "elevenlabs_read" {
  name        = "dev-elevenlabs-read"
  description = "Dev boxes: read the ElevenLabs API key (games/elevenlabs-api-key) and nothing else"
  policy      = data.aws_iam_policy_document.elevenlabs_read.json
}

resource "aws_iam_role_policy_attachment" "elevenlabs_read" {
  for_each   = { dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }
  role       = each.value
  policy_arn = aws_iam_policy.elevenlabs_read.arn
}

# The deployed games: ELEVENLABS_API_KEY for the colton-games project, server-side (sensitive:
# Vercel never shows it again, and it reaches no browser unless code prefixes it NEXT_PUBLIC_,
# which it must not). Production and Preview; a preview is any branch of the games repo, whose
# writers are the owner's family. The value passes through Terraform state, like the other
# games secrets (games-multiplayer-bringup.tf): state already holds credentials and is guarded
# for that. (value_wo would keep it out, but needs Terraform 1.11; this repo pins 1.10.3.)
data "aws_secretsmanager_secret_version" "elevenlabs_api_key" {
  secret_id = aws_secretsmanager_secret.elevenlabs_api_key.id
}

resource "vercel_project_environment_variable" "elevenlabs_api_key" {
  team_id    = var.vercel_team_id
  project_id = local.colton_games_vercel_project_id
  key        = "ELEVENLABS_API_KEY"
  value      = data.aws_secretsmanager_secret_version.elevenlabs_api_key.secret_string
  target     = ["production", "preview"]
  sensitive  = true
  comment    = "ElevenLabs; managed by ejc3/aws dev-ai-services.tf from Secrets Manager games/elevenlabs-api-key"
}

# AWS Price List API for the dev boxes (metal and nextjs-dev, the "dolphin" box): public list
# prices only, so agents can price an instance, a volume or a model call themselves. Nothing
# here reads the account's own spend (no Cost Explorer, no billing): that stays with admins.
# The Price List API has no resource-level permissions, hence "*".
data "aws_iam_policy_document" "dev_pricing_read" {
  statement {
    sid = "ReadPublicPriceLists"
    actions = [
      "pricing:DescribeServices",
      "pricing:GetAttributeValues",
      "pricing:GetProducts",
      "pricing:ListPriceLists",
      "pricing:GetPriceListFileUrl",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "dev_pricing_read" {
  name        = "dev-pricing-read"
  description = "Dev boxes: read AWS public list prices (Price List API); no account spend"
  policy      = data.aws_iam_policy_document.dev_pricing_read.json
}

resource "aws_iam_role_policy_attachment" "dev_pricing_read" {
  for_each   = { dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }
  role       = each.value
  policy_arn = aws_iam_policy.dev_pricing_read.arn
}

# Browserbase (hosted headless browsers, for fetching pages that block plain requests). One JSON
# secret holds BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID, so an agent can export both:
#   eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id browserbase/credentials \
#     --query SecretString --output text | jq -r 'to_entries[] | "export \(.key)=\(.value|@sh)"')"
# Terraform owns the container and who may read it; the value is set out of band and never enters
# git (put-secret-value with a 0600 file). Readers: the administration set, dev-server-role (the
# metal boxes), nextjs-dev-role (the dolphin box), the dolphin-labs news pass's Lambda role, the dolphin-maps-loop
# function (dolphin-maps-loop.tf) and the search-fetch-usage poller (search-fetch-usage.tf). Not dev-ebs-only-role: it is the I/O box's role too (io-box.tf). Every account on
# nextjs-dev has full sudo, so
# the grant is box-wide, as with the ElevenLabs key.
resource "aws_secretsmanager_secret" "browserbase" {
  name                    = "browserbase/credentials"
  description             = "Browserbase API key and project id (JSON: BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "browserbase/credentials", Project = "dev" }
}

# The dolphin-maps loops read it (dolphin-maps-loop.tf); their role grants itself the read, so it is a reader here but
# takes no attachment below. dolphin-labs' news pass runs in the Dolphin account with its own copy (dolphin-aws news.tf).
locals {
  browserbase_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn, aws_iam_role.dolphin_maps_loop.arn, aws_iam_role.search_fetch_usage.arn]
}

resource "aws_secretsmanager_secret_policy" "browserbase" {
  secret_arn = aws_secretsmanager_secret.browserbase.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheDevBoxesCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.browserbase.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.browserbase_readers) } }
    }]
  })
}

data "aws_iam_policy_document" "browserbase_read" {
  statement {
    sid       = "ReadTheBrowserbaseCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.browserbase.arn]
  }
}

resource "aws_iam_policy" "browserbase_read" {
  name        = "dev-browserbase-read"
  description = "Dev boxes: read the Browserbase credentials (browserbase/credentials) and nothing else"
  policy      = data.aws_iam_policy_document.browserbase_read.json
}

resource "aws_iam_role_policy_attachment" "browserbase_read" {
  for_each   = { dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }
  role       = each.value
  policy_arn = aws_iam_policy.browserbase_read.arn
}

# Search and fetch API keys, alternatives to Browserbase for the scraping jobs and to the Bedrock models: DeepSeek
# (api.deepseek.com: chat, reasoning, and its fetch and search tools), Exa (exa.ai: web search, page contents and
# crawling) and ScrapingDog (scrapingdog.com: scraping API with rendering and proxies). One JSON secret per provider,
# `<provider>/api-key`, holds `<PROVIDER>_API_KEY`, so an agent exports it:
#   eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id deepseek/api-key \
#     --query SecretString --output text | jq -r 'to_entries[] | "export \(.key)=\(.value|@sh)"')"
# Terraform owns the containers and who may read them; values are set out of band (put-secret-value from a 0600 file)
# and never enter git. Readers are Browserbase's: the administration set, dev-server-role (the metal boxes),
# nextjs-dev-role (the dolphin box), and the dolphin-maps loops, whose role grants itself the read and gets
# `<PROVIDER>_SECRET_ID` (dolphin-maps-loop.tf). dolphin-labs' news pass has its own copies in the Dolphin account.
# Not dev-ebs-only-role (the I/O box's role too). Every account on nextjs-dev has full sudo, so the grant is box-wide,
# as with Browserbase.
locals {
  search_fetch_api_keys = {
    deepseek    = { title = "DeepSeek", env = "DEEPSEEK" }
    exa         = { title = "Exa", env = "EXA" }
    scrapingdog = { title = "ScrapingDog", env = "SCRAPINGDOG" }
  }
  search_fetch_api_key_readers = local.browserbase_readers
}

resource "aws_secretsmanager_secret" "search_fetch_api_key" {
  for_each                = local.search_fetch_api_keys
  name                    = "${each.key}/api-key"
  description             = "${each.value.title} API key (JSON: ${each.value.env}_API_KEY). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "${each.key}/api-key", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "search_fetch_api_key" {
  for_each   = local.search_fetch_api_keys
  secret_arn = aws_secretsmanager_secret.search_fetch_api_key[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationTheDevBoxesAndTheJobsCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.search_fetch_api_key[each.key].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.search_fetch_api_key_readers) } }
    }]
  })
}

data "aws_iam_policy_document" "search_fetch_api_keys_read" {
  statement {
    sid       = "ReadTheSearchFetchApiKeys"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for s in aws_secretsmanager_secret.search_fetch_api_key : s.arn]
  }
}

resource "aws_iam_policy" "search_fetch_api_keys_read" {
  name        = "dev-search-fetch-api-keys-read"
  description = "Dev boxes: read the search and fetch API keys (deepseek, exa, scrapingdog /api-key) and nothing else"
  policy      = data.aws_iam_policy_document.search_fetch_api_keys_read.json
}

resource "aws_iam_role_policy_attachment" "search_fetch_api_keys_read" {
  for_each   = { dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }
  role       = each.value
  policy_arn = aws_iam_policy.search_fetch_api_keys_read.arn
}

# App Store Connect API key for Games Worth Watching's Apple apps (dolphin-labs, apps/native): the
# key that creates the app record and uploads builds to TestFlight for internal testers. One JSON
# secret holds ISSUER_ID, KEY_ID, PRIVATE_KEY (the .p8 text) and TEAM_ID (the developer team that
# signs the builds). The owner creates the key in App
# Store Connect (Users and Access, Integrations, role Admin: cloud signing creates
# the distribution certificate) and sets the value out of band:
#   read -rs ISSUER; read -rs KEY_ID; read -rs TEAM; jq -n --arg i "$ISSUER" --arg k "$KEY_ID" --arg t "$TEAM" \
#     --rawfile p "AuthKey_${KEY_ID}.p8" '{ISSUER_ID: $i, KEY_ID: $k, TEAM_ID: $t, PRIVATE_KEY: $p}' | aws secretsmanager put-secret-value --region us-west-1 \
#     --secret-id games-worth-watching/app-store-connect --secret-string file:///dev/stdin && rm "AuthKey_${KEY_ID}.p8"
# (with set -o pipefail; the .p8 is the only copy, so it is removed only after the upload succeeds)
# Readers: the administration set, nextjs-dev-role (the dolphin box, which archives on the Mac
# build tunnel and uploads), and dolphin-labs-testflight (CI's uploads; dolphin-labs-testflight.tf). Every account on nextjs-dev has full sudo, so the grant is box-wide, as
# with browserbase/credentials. fcvm-arm's role (dev_server) reads it too: dolphin-labs-hq/dolphin-maps'
# iPhone app is built and uploaded from fcvm-arm (archived on the MacBook over `ssh mac`), and its
# TestFlight feedback and build status are read through the same API. The grant is pinned to
# fcvm-arm's instance with ec2:SourceInstanceARN, so fcvm-x86, which shares the role, cannot read it.
resource "aws_secretsmanager_secret" "app_store_connect" {
  name                    = "games-worth-watching/app-store-connect"
  description             = "App Store Connect API key for Games Worth Watching (JSON: ISSUER_ID, KEY_ID, TEAM_ID, PRIVATE_KEY). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "games-worth-watching/app-store-connect", Project = "dolphin-labs" }
}

resource "aws_secretsmanager_secret_policy" "app_store_connect" {
  secret_arn = aws_secretsmanager_secret.app_store_connect.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationTheDolphinBoxAndTestFlightCICanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.app_store_connect.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.nextjs_dev.arn, aws_iam_role.dolphin_labs_testflight.arn, aws_iam_role.dev_server.arn]) } }
    }]
  })
}

data "aws_iam_policy_document" "app_store_connect_read" {
  statement {
    sid       = "ReadTheAppStoreConnectKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app_store_connect.arn]
  }
}

resource "aws_iam_policy" "app_store_connect_read" {
  name        = "dev-app-store-connect-read"
  description = "Dolphin box: read the Games Worth Watching App Store Connect key (games-worth-watching/app-store-connect) and nothing else"
  policy      = data.aws_iam_policy_document.app_store_connect_read.json
}

resource "aws_iam_role_policy_attachment" "app_store_connect_read" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.app_store_connect_read.arn
}

# fcvm-arm only (dev_server is shared with fcvm-x86): the read is allowed from fcvm-arm's instance.
resource "aws_iam_role_policy" "dev_server_app_store_connect" {
  count = var.enable_firecracker_instance ? 1 : 0
  name  = "read-app-store-connect-from-fcvm-arm"
  role  = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.app_store_connect.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn } }
    }]
  })
}

# Anthropic API key, the final paid backup for claw-master (`--backup-api-key env:CLAW_MASTER_BACKUP_API_KEY`; the
# binary still reads CLAUDE_MASTER_BACKUP_API_KEY, its name before the rename), used only after every subscription
# profile is out of quota. It lived in ~/claude_api.txt on fcvm;
# the value is set out of band (put-secret-value from a 0600 file) and never enters git. Readers: the
# administration set, dev-server-role (the metal boxes), the shared claw-master server's role and nextjs-dev-role
# for the owner's nextjs-ct container (nextjs-dev-fcvm-parity.tf; every member container is denied it).
#   export CLAW_MASTER_BACKUP_API_KEY=$(aws secretsmanager get-secret-value --region us-west-1 \
#     --secret-id claude-master/backup-api-key --query SecretString --output text)
resource "aws_secretsmanager_secret" "claw_master_backup_key" {
  name                    = "claude-master/backup-api-key"
  description             = "Anthropic API key, final paid backup for claude-master (raw key string). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "claude-master/backup-api-key", Project = "dev" }
}

locals {
  claw_master_backup_key_readers = concat([aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn], aws_iam_role.claw_master_server[*].arn)
}

resource "aws_secretsmanager_secret_policy" "claw_master_backup_key" {
  secret_arn = aws_secretsmanager_secret.claw_master_backup_key.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheMetalBoxesCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.claw_master_backup_key.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.claw_master_backup_key_readers) } }
    }]
  })
}

data "aws_iam_policy_document" "claw_master_backup_key_read" {
  statement {
    sid       = "ReadTheClaudeMasterBackupKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.claw_master_backup_key.arn]
  }
}

resource "aws_iam_policy" "claw_master_backup_key_read" {
  name        = "dev-claude-master-backup-key-read"
  description = "Metal dev boxes: read the claude-master backup API key (claude-master/backup-api-key) and nothing else"
  policy      = data.aws_iam_policy_document.claw_master_backup_key_read.json
}

resource "aws_iam_role_policy_attachment" "claw_master_backup_key_read" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.claw_master_backup_key_read.arn
}

# The shared claw-master server is the one box that actually uses the backup key.
resource "aws_iam_role_policy_attachment" "claw_master_backup_key_read_server" {
  count      = var.enable_claw_master_server ? 1 : 0
  role       = aws_iam_role.claw_master_server[0].name
  policy_arn = aws_iam_policy.claw_master_backup_key_read.arn
}

# Turso platform API token (create and delete databases and groups, mint database tokens), for the metal dev boxes.
# The value is set out of band (put-secret-value from stdin) and never enters git or Terraform state. Readers: the
# administration set, dev-server-role (the metal boxes) and nextjs-dev-role for the owner's nextjs-ct container
# (nextjs-dev-fcvm-parity.tf; every member container is denied it: this token can create and delete databases).
#   export TURSO_API_TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 \
#     --secret-id turso/api-token --query SecretString --output text)
resource "aws_secretsmanager_secret" "turso_api_token" {
  name                    = "turso/api-token"
  description             = "Turso platform API token (raw string). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "turso/api-token", Project = "dev" }
}

locals {
  turso_api_token_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]
}

resource "aws_secretsmanager_secret_policy" "turso_api_token" {
  secret_arn = aws_secretsmanager_secret.turso_api_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheMetalBoxesCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.turso_api_token.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.turso_api_token_readers) } }
    }]
  })
}

data "aws_iam_policy_document" "turso_api_token_read" {
  statement {
    sid       = "ReadTheTursoApiToken"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.turso_api_token.arn]
  }
}

resource "aws_iam_policy" "turso_api_token_read" {
  name        = "dev-turso-api-token-read"
  description = "Metal dev boxes: read the Turso API token (turso/api-token) and nothing else"
  policy      = data.aws_iam_policy_document.turso_api_token_read.json
}

resource "aws_iam_role_policy_attachment" "turso_api_token_read" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.turso_api_token_read.arn
}

# abliteration.ai (owner, 2026-10-23), for opencode on the owner's boxes: opencode's built-in `abliteration-ai` provider
# (models.dev; https://api.abliteration.ai/v1, OpenAI-compatible) reads its key from ABLIT_KEY, and the opencode() function
# in the managed ~/.zshrc (dev-user-data.tf) reads it from here at each launch, into opencode's environment only. Two
# secrets, both raw strings set out of band (put-secret-value from a 0600 file), never in git or state:
# - abliteration/admin-api-key: the organization's Admin API key (projects, project keys, spend limits). Administration
#   only. It made the inference key: project `opencode-dev-boxes`, key `opencode` (permission model.invoke).
# - abliteration/api-key: that inference key. Readers: administration, dev-server-role (both metal boxes; granted in
#   assume-constellation-terraform, constellation-account.tf, since dev-server-role holds 20 of 20 managed policies) and
#   nextjs-dev-role for the owner's nextjs-ct container (nextjs-dev-fcvm-parity.tf; every member container is denied it).
# Rotate: mint a new project key with the admin key, put it here, revoke the old one.
resource "aws_secretsmanager_secret" "abliteration_admin_api_key" {
  name                    = "abliteration/admin-api-key"
  description             = "abliteration.ai organization Admin API key (raw string). Administration only; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "abliteration/admin-api-key", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "abliteration_admin_api_key" {
  secret_arn = aws_secretsmanager_secret.abliteration_admin_api_key.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.abliteration_admin_api_key.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "aws_secretsmanager_secret" "abliteration_api_key" {
  name                    = "abliteration/api-key"
  description             = "abliteration.ai project API key for opencode (raw string; ABLIT_KEY). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "abliteration/api-key", Project = "dev" }
}

locals {
  abliteration_api_key_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]
}

resource "aws_secretsmanager_secret_policy" "abliteration_api_key" {
  secret_arn = aws_secretsmanager_secret.abliteration_api_key.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheOwnersBoxesCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.abliteration_api_key.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.abliteration_api_key_readers) } }
    }]
  })
}

# DeepSeek on Amazon Bedrock for nextjs-dev-role (every nextjs-ct container), the same two models dev-server-role has
# (BedrockRuntimeInvoke in dev-instance-common.tf), for opencode (dev-user-data.tf's opencode_user_setup, run in every
# container). Owner, 2026-10-21: "get deepseek working on the nextjs box ... fcvm has it". The account is already
# subscribed (fcvm's first use), so there is no Marketplace grant. A managed policy: nextjs-dev-role's inline policies are
# near IAM's 10,240-character total. Box-wide, as every grant on that role is: every container may invoke them.
resource "aws_iam_policy" "nextjs_dev_bedrock_deepseek" {
  name        = "nextjs-dev-bedrock-deepseek"
  description = "nextjs-dev-role: invoke DeepSeek v3.2 and R1 on Bedrock (opencode), nothing else (dev-ai-services.tf)"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "InvokeDeepSeekForOpencode"
      Effect = "Allow"
      Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
      Resource = [
        "arn:aws:bedrock:*::foundation-model/deepseek.v3.2",
        "arn:aws:bedrock:*::foundation-model/deepseek.r1-v1:0",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.deepseek.*",
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "nextjs_dev_bedrock_deepseek" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.nextjs_dev_bedrock_deepseek.arn
}
