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
        local.fcvm_fleet_access_role_arn,
      ] } }
    }]
  })
}
