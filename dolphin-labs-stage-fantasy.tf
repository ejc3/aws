# dolphin-labs-stage-fantasy.tf
#
# The fantasy draft room in the dolphin-labs-stage Worker (dolphin-labs-hq/dolphin-labs, docs/fantasy/room.md) checks the
# Cloudflare Access token on its create, recover and list routes itself: RS256 against the team's certificates, the team's
# issuer, and an `aud` that contains FANTASY_ACCESS_AUD. Until that variable is set those routes answer 503
# access_not_configured (they fail closed). Production needs nothing: the room is off in env.production.
#
# The value is the AUD tag of the Access application that protects the Worker's hostname. That is the hostname-level
# application cloudflare_zero_trust_access_application.dolphin_labs_stage_check (dolphin-labs-stage-check.tf, domain
# dolphin-labs-stage.cc-games.workers.dev), NOT the shared Worker-native application workers_stage (workers-stage.tf):
# hostname-level Access takes precedence over the Worker-native gate, so a request to that host carries a token whose
# `aud` is the hostname application's. Version preview URLs keep the Worker-native gate, so a token there carries the
# shared workers_stage application's aud instead, and the room's protected routes fail closed on a preview (503
# access_not_configured or a refused token) by design: they answer only on the main hostname. Terraform writes it from
# that application's `aud`, so it follows the application if it is ever re-created, into a container of its own in the
# DOLPHIN account (provider aws.dolphin), workers-stage/dolphin-labs/terraform, beside the out-of-band
# workers-stage/dolphin-labs that dolphin-labs-hq/dolphin-aws owns (secrets.tf there). dolphin-aws must not manage this
# one. The same shape as workers-stage/dolphin-maps/terraform (dolphin-maps-moderation.tf): administration only, merged
# by scripts/workers-stage-secrets.sh, which refuses a name in both containers, so FANTASY_ACCESS_AUD lives only here.
#
# It is not a credential (an audience tag only names the application), but it is loaded as a Worker secret: a variable set
# in the dashboard would be dropped by the next `wrangler deploy`.
#
# Order: apply (from a jumpbox), then scripts/workers-stage-secrets.sh dolphin-labs after the site's own deploy has finished.
# Load again whenever the application is re-created (its aud changes).
locals {
  dolphin_labs_stage_fantasy_worker    = "dolphin-labs-stage"
  dolphin_labs_stage_fantasy_container = "workers-stage/dolphin-labs/terraform"
}

resource "aws_secretsmanager_secret" "dolphin_labs_stage_terraform" {
  provider                = aws.dolphin
  name                    = local.dolphin_labs_stage_fantasy_container
  description             = "Runtime secrets of the ${local.dolphin_labs_stage_fantasy_worker} Worker that Terraform makes (JSON: FANTASY_ACCESS_AUD, the Access audience of its hostname). Written by ejc3/aws dolphin-labs-stage-fantasy.tf; merged by the loader with the out-of-band container."
  recovery_window_in_days = 7
  tags                    = { Name = local.dolphin_labs_stage_fantasy_container, Project = "dolphin-labs" }
}

# Administration of the Dolphin account only: the same principals as dolphin-maps' Terraform-written containers.
resource "aws_secretsmanager_secret_policy" "dolphin_labs_stage_terraform" {
  provider            = aws.dolphin
  secret_arn          = aws_secretsmanager_secret.dolphin_labs_stage_terraform.arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]
      Resource  = "*"
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.dolphin_maps_moderation_admins } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "dolphin_labs_stage_terraform" {
  provider  = aws.dolphin
  secret_id = aws_secretsmanager_secret.dolphin_labs_stage_terraform.id
  secret_string = jsonencode({
    FANTASY_ACCESS_AUD = cloudflare_zero_trust_access_application.dolphin_labs_stage_check.aud
  })

  lifecycle {
    # The audience is only right while that application is the one on the Worker's own hostname.
    precondition {
      condition     = cloudflare_zero_trust_access_application.dolphin_labs_stage_check.domain == "${local.dolphin_labs_stage_fantasy_worker}.cc-games.workers.dev" && contains(keys(local.workers_stage), local.dolphin_labs_stage_fantasy_worker)
      error_message = "FANTASY_ACCESS_AUD must be the audience of the hostname-level Access application on dolphin-labs-stage.cc-games.workers.dev (dolphin-labs-stage-check.tf), and the Worker must be in local.workers_stage (workers-stage.tf)."
    }
    # The loader reads the container in us-west-1, and the Dolphin account's region SCP allows only its monitored regions.
    precondition {
      condition     = var.aws_region == "us-west-1" && contains(local.dolphin_security_regions, var.aws_region)
      error_message = "The container is made in the aws.dolphin provider's region, which must be us-west-1 (where scripts/workers-stage-secrets.sh reads it) and allowed by the Dolphin account's SCP (dolphin-region-scp.tf)."
    }
  }
}
