# Bootstrap independently of the running coordinator, which requires an existing secret version.
locals {
  fcvm_fleet_stage_tls_name = "fcvm-fleet/stage/tls"
}

resource "aws_secretsmanager_secret" "fcvm_fleet_stage_tls" {
  provider                = aws.fcvm_fleet
  name                    = local.fcvm_fleet_stage_tls_name
  description             = "Wildcard TLS certificate for *.tiles-stage.fcvm.dev, the staging fleet's launch names (JSON: cert, key, scene_token). Bootstrapped before cutover and renewed by the staging coordinator."
  recovery_window_in_days = 7
  tags                    = { Name = local.fcvm_fleet_stage_tls_name, Project = "fcvm-fleet", Fleet = local.fcvm_fleet_stage.fleet }
}

# Whatever any other identity's IAM policy says, only staging's coordinator, hosts and tiles (and the role Terraform
# applies through) read or write it: no prod role holds a staging key.
resource "aws_secretsmanager_secret_policy" "fcvm_fleet_stage_tls" {
  provider   = aws.fcvm_fleet
  secret_arn = aws_secretsmanager_secret.fcvm_fleet_stage_tls.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheStagingStackAndTerraform"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
      Resource  = aws_secretsmanager_secret.fcvm_fleet_stage_tls.arn
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = [
        aws_iam_role.fcvm_fleet_coordinator_task.arn,
        aws_iam_role.fcvm_fleet_host.arn,
        aws_iam_role.fcvm_fleet_tile.arn,
        local.fcvm_lambda_tile_role_arns.stage,
        local.fcvm_fleet_access_role_arn,
      ] } }
    }]
  })
}

# All staging runtime consumers use this lookup's name or ARN. The independent bootstrap checks the actual PEM,
# its key, trust chain, expiry, and scene token before attaching the seed label. Renewal moves AWSCURRENT and
# preserves the original labeled version. Terraform reads only version metadata, never a certificate or key.
data "aws_secretsmanager_secret_versions" "fcvm_fleet_stage_tls" {
  provider  = aws.fcvm_fleet
  secret_id = aws_secretsmanager_secret.fcvm_fleet_stage_tls.arn

  lifecycle {
    postcondition {
      condition = alltrue([
        for label in ["AWSCURRENT", "FLEET_STAGE_TLS_BOOTSTRAPPED"] :
        anytrue([for version in self.versions : contains(version.version_stages, label)])
      ])
      error_message = "Bootstrap and verify the staging wildcard with scripts/fcvm-stage-certificate.py before switching the fleet."
    }
  }
}
