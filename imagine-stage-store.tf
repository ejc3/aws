# The pictures of imagine's non-production deployments: the Cloudflare staging copy
# (imagine-stage, workers-stage.tf) and Vercel Preview deployments. Production's are under
# docs/images/ in imagine-docs (imagine.tf, "Pictures"); nothing here can reach that bucket,
# and nothing in production can reach this one.
#
# The web app keeps a picture's bytes under images/<file id>/<hash> of the store it is given
# (IMAGINE_STORE). This bucket holds only that: no document is stored here, because the
# staging copy plays against the production backend (imagine-stage-waker.tf), so what it
# shows of a document is production's and only its pictures are its own.
#
# Who reaches it:
#   Vercel Preview      role imagine-store-preview, through Vercel OIDC (imagine.tf)
#   imagine-stage       user imagine-stage-store: a Worker has no OIDC token, so it gets a key
#                       whose only permission is images/* here. The key lives in an
#                       administration-only container that the operator copies into the
#                       Worker's secret container (workers-stage/imagine) with the store's
#                       name and region; Terraform never reads the Worker's container.
#                       Rotate with `terraform apply -replace=aws_iam_access_key.imagine_stage_store`,
#                       then reload the Worker (scripts/workers-stage-secrets.sh imagine).

resource "aws_s3_bucket" "imagine_stage" {
  bucket = "imagine-stage-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "imagine-stage", Project = "imagine" }
}

resource "aws_s3_bucket_public_access_block" "imagine_stage" {
  bucket                  = aws_s3_bucket.imagine_stage.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "imagine_stage" {
  bucket = aws_s3_bucket.imagine_stage.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "imagine_stage" {
  bucket = aws_s3_bucket.imagine_stage.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Staging pictures are disposable: nothing deletes a picture yet (ejc3/imagine, "Pictures"),
# so this bucket would only grow. Each is kept 30 days.
resource "aws_s3_bucket_lifecycle_configuration" "imagine_stage" {
  bucket = aws_s3_bucket.imagine_stage.id
  rule {
    id     = "staging-pictures-expire"
    status = "Enabled"
    filter {
      prefix = "images/"
    }
    expiration {
      days = 30
    }
  }
}

resource "aws_iam_user" "imagine_stage_store" {
  name = "imagine-stage-store"
  tags = { Name = "imagine-stage-store", Project = "imagine" }
}

resource "aws_iam_user_policy" "imagine_stage_store" {
  name = "pictures-staging"
  user = aws_iam_user.imagine_stage_store.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.imagine_stage.arn}/images/*"
      },
      {
        # As at the task role (imagine.tf): without it S3 answers 403, not 404, for a
        # picture that is not there.
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.imagine_stage.arn
      },
    ]
  })
}

resource "aws_iam_access_key" "imagine_stage_store" {
  user = aws_iam_user.imagine_stage_store.name
}

resource "aws_secretsmanager_secret" "imagine_stage_store" {
  name                    = "imagine-stage/store"
  description             = "The staging Worker's picture store: bucket, region and the access key of imagine-stage-store (copy into workers-stage/imagine)"
  recovery_window_in_days = 0
  tags                    = { Name = "imagine-stage/store", Project = "imagine", Managed = "terraform" }
}

# Administration only, as workers-stage/<site> is: a dev box has no reason to hold it.
resource "aws_secretsmanager_secret_policy" "imagine_stage_store" {
  secret_arn = aws_secretsmanager_secret.imagine_stage_store.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.imagine_stage_store.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "imagine_stage_store" {
  secret_id = aws_secretsmanager_secret.imagine_stage_store.id
  secret_string = jsonencode({
    IMAGINE_STORE                   = "s3:${aws_s3_bucket.imagine_stage.bucket}/"
    IMAGINE_AWS_REGION              = var.aws_region
    IMAGINE_STORE_ACCESS_KEY_ID     = aws_iam_access_key.imagine_stage_store.id
    IMAGINE_STORE_SECRET_ACCESS_KEY = aws_iam_access_key.imagine_stage_store.secret
  })
}
