# lights-vision.tf
#
# Lets the Lights staging Worker (lights-stage, ejc3/constellation apps/lights-web) read a photo of a house with Claude
# Haiku 5.5: the buying wizard's photo reader (constellation docs/LIGHTS.md, "Buying hardware in the app", step 2). The
# person's photo travels in the request; nothing is stored. A Worker cannot assume an AWS role, hence a key, as
# colton-games and dolphin-maps do for moderation.
#
# Exactly one grant, the shared Haiku 5.5 statements of bedrock-haiku.tf (InvokeModel and its stream on the US inference
# profile and the foundation model, and the first-use Marketplace subscribe for that one product), so this user can
# reach no other model. In this account, where Haiku 5.5 is already subscribed and the box roles call it.
#
#   lights-stage   IAM user lights-vision-worker-stage; its key goes only into workers-stage/lights/terraform, which
#                  scripts/workers-stage-secrets.sh loads into the Worker. Lights has no hand-set staging secrets yet,
#                  so the loader reads this container alone for it.
#
# The container holds LIGHTS_AWS_ACCESS_KEY_ID, LIGHTS_AWS_SECRET_ACCESS_KEY, LIGHTS_BEDROCK_REGION and
# LIGHTS_BEDROCK_MODEL. The key is in Terraform state, as the moderation Workers' keys are.
#   Rotate: terraform apply -replace='aws_iam_access_key.lights_vision_worker', then scripts/workers-stage-secrets.sh
#   lights, after a deploy has finished.
#
# Spend: one photo of at most 1600 px is about 1,600 input tokens and 2,000 output tokens, about $0.001 at Haiku 5.5's
# list price. The Worker is behind the staging Access application (workers-stage.tf), so only the family reaches it.
# No alarm of its own: Bedrock's metrics are per model, and the boxes call Haiku 5.5 in this account too.
locals {
  lights_vision_worker = {
    user      = "lights-vision-worker-stage"
    worker    = "lights-stage"
    container = "workers-stage/lights/terraform"
  }
}

resource "aws_iam_user" "lights_vision_worker" {
  name = local.lights_vision_worker.user
  tags = { Name = local.lights_vision_worker.user, Project = "constellation-lights" }
}

resource "aws_iam_user_policy" "lights_vision_worker" {
  name   = "invoke-claude-haiku-5-5"
  user   = aws_iam_user.lights_vision_worker.name
  policy = jsonencode({ Version = "2012-10-17", Statement = local.bedrock_haiku_statements })
}

resource "aws_iam_access_key" "lights_vision_worker" {
  user = aws_iam_user.lights_vision_worker.name
}

resource "aws_secretsmanager_secret" "lights_vision_worker" {
  name                    = local.lights_vision_worker.container
  description             = "Runtime secrets of the ${local.lights_vision_worker.worker} Worker that Terraform makes (JSON: the photo reader's Bedrock key, region and model). Written by ejc3/aws lights-vision.tf; loaded by scripts/workers-stage-secrets.sh lights."
  recovery_window_in_days = 7
  tags                    = { Name = local.lights_vision_worker.container, Project = "constellation-lights" }
}

resource "aws_secretsmanager_secret_policy" "lights_vision_worker" {
  secret_arn          = aws_secretsmanager_secret.lights_vision_worker.arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]
      Resource  = "*"
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "lights_vision_worker" {
  secret_id = aws_secretsmanager_secret.lights_vision_worker.id
  secret_string = jsonencode({
    LIGHTS_AWS_ACCESS_KEY_ID     = aws_iam_access_key.lights_vision_worker.id
    LIGHTS_AWS_SECRET_ACCESS_KEY = aws_iam_access_key.lights_vision_worker.secret
    LIGHTS_BEDROCK_REGION        = local.bedrock_haiku_region
    LIGHTS_BEDROCK_MODEL         = local.bedrock_haiku_profile
  })

  lifecycle {
    # A staging key only in a staging container.
    precondition {
      condition     = startswith(local.lights_vision_worker.container, "workers-stage/") && endswith(local.lights_vision_worker.worker, "-stage")
      error_message = "The Lights photo reader's key must go only to a staging container (lights-vision.tf)."
    }
  }
}

output "lights_vision" {
  description = "How the Lights staging Worker reads house photos: the Bedrock region and model, and the container Terraform writes its key into."
  value = {
    region    = local.bedrock_haiku_region
    model     = local.bedrock_haiku_profile
    container = aws_secretsmanager_secret.lights_vision_worker.name
  }
}
