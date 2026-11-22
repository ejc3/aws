# family-apple-signing.tf
#
# Family's Apple Development signing identity (constellation apps/apple, the Family app), kept in
# Secrets Manager so a device build can be signed on any of the owner's Macs (the Mac mini with the
# USB-paired iPhone first) without a certificate living only in one keychain.
#
# One JSON secret: P12_BASE64 (the identity: certificate and private key, as PKCS#12), P12_PASSWORD
# (the .p12's own passphrase), CERTIFICATE_ID and EXPIRES (the certificate's App Store Connect id and
# expiry, for renewal). The value is set from the owner's Next.js container, which generates the private
# key in memory, has the owner upload its signing request once at developer.apple.com (the shared
# App Store Connect key is at Apple's per-key limit for development certificates), and puts the
# result here. Administration can also manage the identity.
resource "aws_secretsmanager_secret" "family_apple_development" {
  name                    = "family/apple-development"
  description             = "Family's Apple Development signing identity (JSON: P12_BASE64, P12_PASSWORD, CERTIFICATE_ID, EXPIRES). Set by the owner's Next.js container; see family-apple-signing.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "family/apple-development", Project = "constellation" }
}

locals {
  family_apple_owner_session = "${aws_iam_role.nextjs_dev.unique_id}:ct-ejc3"
}

resource "aws_secretsmanager_secret_policy" "family_apple_development" {
  secret_arn = aws_secretsmanager_secret.family_apple_development.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "OwnerContainerReadAndSet"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.nextjs_dev.arn }
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
        Resource  = aws_secretsmanager_secret.family_apple_development.arn
        Condition = { StringEquals = { "aws:userid" = local.family_apple_owner_session } }
      },
      {
        Sid       = "OnlyAdministrationAndOwnerCanAccessIdentity"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret", "secretsmanager:UpdateSecretVersionStage",
        ]
        Resource = aws_secretsmanager_secret.family_apple_development.arn
        # The broker assigns ct-ejc3. On rollback to the shared host, only administration is exempt.
        Condition = merge(
          { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } },
          local.nextjs_ct_serving ? { StringNotEquals = { "aws:userid" = local.family_apple_owner_session } } : {},
        )
      },
    ]
  })
}
