# dev-ai-services.tf
#
# The ElevenLabs API key (voice and sound for the games), for the dev boxes and the games.
# Terraform owns only the secret's container and who may read it; the value is set out of band
# (it came from the owner) and never enters git:
#   aws secretsmanager put-secret-value --secret-id games/elevenlabs-api-key \
#     --secret-string file://<a 0600 file>
# Readers: the administration set, dev-server-role (the metal boxes) and nextjs-dev-role (where
# the games are built). Agents fetch it when they need it; nothing writes it to disk on the
# boxes. (DeepSeek, by contrast, is on the metal boxes only: dev-instance-common.tf.)

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
# metal boxes) and nextjs-dev-role (the dolphin box). Every account on nextjs-dev has full sudo, so
# the grant is box-wide, as with the ElevenLabs key.
resource "aws_secretsmanager_secret" "browserbase" {
  name                    = "browserbase/credentials"
  description             = "Browserbase API key and project id (JSON: BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "browserbase/credentials", Project = "dev" }
}

locals {
  browserbase_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]
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

# Anthropic API key, the final paid backup for claude-master (`--backup-api-key env:CLAUDE_MASTER_BACKUP_API_KEY`),
# used only after every subscription profile is out of quota. It lived in ~/claude_api.txt on fcvm;
# the value is set out of band (put-secret-value from a 0600 file) and never enters git. Readers: the
# administration set, dev-server-role (the metal boxes) and the shared claude-master server's role.
# Not nextjs-dev-role: every account there has full sudo, so a grant would be box-wide, and this key
# is billed per token.
#   export CLAUDE_MASTER_BACKUP_API_KEY=$(aws secretsmanager get-secret-value --region us-west-1 \
#     --secret-id claude-master/backup-api-key --query SecretString --output text)
resource "aws_secretsmanager_secret" "claude_master_backup_key" {
  name                    = "claude-master/backup-api-key"
  description             = "Anthropic API key, final paid backup for claude-master (raw key string). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "claude-master/backup-api-key", Project = "dev" }
}

locals {
  claude_master_backup_key_readers = concat([aws_iam_role.dev_server.arn], aws_iam_role.claude_master_server[*].arn)
}

resource "aws_secretsmanager_secret_policy" "claude_master_backup_key" {
  secret_arn = aws_secretsmanager_secret.claude_master_backup_key.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheMetalBoxesCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.claude_master_backup_key.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.claude_master_backup_key_readers) } }
    }]
  })
}

data "aws_iam_policy_document" "claude_master_backup_key_read" {
  statement {
    sid       = "ReadTheClaudeMasterBackupKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.claude_master_backup_key.arn]
  }
}

resource "aws_iam_policy" "claude_master_backup_key_read" {
  name        = "dev-claude-master-backup-key-read"
  description = "Metal dev boxes: read the claude-master backup API key (claude-master/backup-api-key) and nothing else"
  policy      = data.aws_iam_policy_document.claude_master_backup_key_read.json
}

resource "aws_iam_role_policy_attachment" "claude_master_backup_key_read" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.claude_master_backup_key_read.arn
}

# The shared claude-master server is the one box that actually uses the backup key.
resource "aws_iam_role_policy_attachment" "claude_master_backup_key_read_server" {
  count      = var.enable_claude_master_server ? 1 : 0
  role       = aws_iam_role.claude_master_server[0].name
  policy_arn = aws_iam_policy.claude_master_backup_key_read.arn
}

# Turso platform API token (create and delete databases and groups, mint database tokens), for the metal dev boxes.
# The value is set out of band (put-secret-value from stdin) and never enters git or Terraform state. Readers: the
# administration set and dev-server-role (the metal boxes). Not nextjs-dev-role: every account there has full sudo, so a grant
# would be box-wide, and this token can create and delete databases.
#   export TURSO_API_TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 \
#     --secret-id turso/api-token --query SecretString --output text)
resource "aws_secretsmanager_secret" "turso_api_token" {
  name                    = "turso/api-token"
  description             = "Turso platform API token (raw string). Value set out of band; see dev-ai-services.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "turso/api-token", Project = "dev" }
}

locals {
  turso_api_token_readers = [aws_iam_role.dev_server.arn]
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
