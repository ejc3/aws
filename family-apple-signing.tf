# family-apple-signing.tf
#
# Family's Apple Development signing identity (constellation apps/apple, the Family app), kept in
# Secrets Manager so a device build can be signed on any of the owner's Macs (the Mac mini with the
# USB-paired iPhone first) without a certificate living only in one keychain.
#
# One JSON secret: P12_BASE64 (the identity: certificate and private key, as PKCS#12), P12_PASSWORD
# (the .p12's own passphrase), CERTIFICATE_ID and EXPIRES (the certificate's App Store Connect id and
# expiry, for renewal). The value is set by the dolphin box (nextjs-dev), which generates the private
# key in memory, has the owner upload its signing request once at developer.apple.com (the shared
# App Store Connect key is at Apple's per-key limit for development certificates), and puts the
# result here. Nothing else writes it; administration and the dolphin box read it.
resource "aws_secretsmanager_secret" "family_apple_development" {
  name                    = "family/apple-development"
  description             = "Family's Apple Development signing identity (JSON: P12_BASE64, P12_PASSWORD, CERTIFICATE_ID, EXPIRES). Set by the dolphin box; see family-apple-signing.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "family/apple-development", Project = "constellation" }
}

resource "aws_secretsmanager_secret_policy" "family_apple_development" {
  secret_arn = aws_secretsmanager_secret.family_apple_development.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheDolphinBoxCanReadOrWrite"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
      Resource  = aws_secretsmanager_secret.family_apple_development.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.nextjs_dev.arn]) } }
    }]
  })
}

data "aws_iam_policy_document" "family_apple_development" {
  statement {
    sid       = "ReadAndSetFamilysSigningIdentity"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
    resources = [aws_secretsmanager_secret.family_apple_development.arn]
  }
}

resource "aws_iam_policy" "family_apple_development" {
  name        = "dev-family-apple-development"
  description = "Dolphin box: read and set Family's Apple Development signing identity (family/apple-development) and nothing else"
  policy      = data.aws_iam_policy_document.family_apple_development.json
}

resource "aws_iam_role_policy_attachment" "family_apple_development" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.family_apple_development.arn
}
