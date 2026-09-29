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
