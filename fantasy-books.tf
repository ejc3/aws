# Private league books for Open Fantasy's staging view (ejc3/constellation, apps/fantasy-web).
# The staging Worker fantasy-stage binds this bucket as BOOKS in a later constellation change and
# serves books/<id>.json only behind the staging-Workers Access application (workers-stage.tf).
# The bucket has no public r2.dev URL. A book is built on fcvm-arm from the owner's test leagues
# (read-only provider captures that never enter a repository) and uploaded with the bucket-scoped
# credential in fantasy/books-upload. The protected Terraform state also contains the credential.
# See "Open Fantasy staging books" in README.md for token rotation.

locals {
  fantasy_books_bucket = "fantasy-stage-books"
  fantasy_books_credentials = {
    R2_ACCOUNT_ID        = var.cloudflare_account_id
    R2_ACCESS_KEY_ID     = cloudflare_account_token.fantasy_books_r2.id
    R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.fantasy_books_r2.value)
    R2_BUCKET            = cloudflare_r2_bucket.fantasy_books.name
  }
}

resource "cloudflare_r2_bucket" "fantasy_books" {
  account_id    = var.cloudflare_account_id
  name          = local.fantasy_books_bucket
  location      = "wnam"
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_managed_domain" "fantasy_books" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.fantasy_books.name
  enabled     = false
}

resource "cloudflare_account_token" "fantasy_books_r2" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "fantasy-stage-books-r2"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.r2_bucket_item_read }, { id = local.r2_bucket_item_write }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.fantasy_books.name}" = "*"
    })
  }]

  # The credential is returned once. Do not silently replace it after state loss.
  lifecycle {
    prevent_destroy = true
  }
}

# The uploader runs on fcvm-arm only: readable by the administration set and by dev-server-role's
# session on fcvm-arm's instance, not fcvm-x86's (which shares the role).
resource "aws_secretsmanager_secret" "fantasy_books_upload" {
  name                    = "fantasy/books-upload"
  description             = "Open Fantasy staging books upload credentials (JSON: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET): read and write on the fantasy-stage-books R2 bucket only. Written by Terraform; see fantasy-books.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "fantasy/books-upload", Project = "open-fantasy" }
}

resource "aws_secretsmanager_secret_version" "fantasy_books_upload" {
  secret_id     = aws_secretsmanager_secret.fantasy_books_upload.id
  secret_string = jsonencode(local.fantasy_books_credentials)
  depends_on    = [aws_secretsmanager_secret_policy.fantasy_books_upload]
}

resource "aws_secretsmanager_secret_policy" "fantasy_books_upload" {
  secret_arn = aws_secretsmanager_secret.fantasy_books_upload.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "FcvmArmReads"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.dev_server.arn }
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.fantasy_books_upload.arn
      }, {
      Sid       = "OnlyAdministrationAndTheDevServerRoleCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.fantasy_books_upload.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn]) } }
      }, {
      Sid       = "DenyOtherDevServerSessions"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.fantasy_books_upload.arn
      Condition = {
        ArnEquals               = { "aws:PrincipalArn" = aws_iam_role.dev_server.arn }
        StringNotEqualsIfExists = { "aws:userid" = try(local.browser_run_arm_userids[0], "no-fcvm-arm-instance") }
      }
    }]
  })
}
