# The staging copy of the imagine web app runs on Cloudflare Workers (imagine-stage, see
# workers-stage.tf) and plays against the SAME game backend as production (its IMAGINE_WS_URL
# is the production socket). imagine-scale puts that backend to sleep after five minutes
# without a socket, and only a wake invocation brings it back. Production wakes it through
# the imagine-waker role, assumed with a Vercel OIDC token; a Worker has no such token, so
# the stage gets the narrowest credential AWS offers it: a user whose only permission is to
# invoke that one function. The key lives in an administration-only container that the
# operator copies into the Worker's secret container (workers-stage/imagine); Terraform
# never reads the Worker's container. Rotate with
# `terraform apply -replace=aws_iam_access_key.imagine_stage_waker`, then reload the Worker.
resource "aws_iam_user" "imagine_stage_waker" {
  name = "imagine-stage-waker"
  tags = { Name = "imagine-stage-waker", Project = "imagine" }
}

resource "aws_iam_user_policy" "imagine_stage_waker" {
  name = "invoke-imagine-scale"
  user = aws_iam_user.imagine_stage_waker.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.imagine_scale.arn
    }]
  })
}

resource "aws_iam_access_key" "imagine_stage_waker" {
  user = aws_iam_user.imagine_stage_waker.name
}

resource "aws_secretsmanager_secret" "imagine_stage_waker" {
  name                    = "imagine-stage/waker"
  description             = "Access key of imagine-stage-waker, the staging Worker's credential to invoke imagine-scale (copy into workers-stage/imagine)"
  recovery_window_in_days = 0
  tags                    = { Name = "imagine-stage/waker", Project = "imagine", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "imagine_stage_waker" {
  secret_id = aws_secretsmanager_secret.imagine_stage_waker.id
  secret_string = jsonencode({
    IMAGINE_AWS_ACCESS_KEY_ID     = aws_iam_access_key.imagine_stage_waker.id
    IMAGINE_AWS_SECRET_ACCESS_KEY = aws_iam_access_key.imagine_stage_waker.secret
  })
}
