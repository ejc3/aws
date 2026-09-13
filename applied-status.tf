# applied-status.tf
#
# A non-secret answer to "is main applied?" for the dev boxes.
#
# Dev boxes deliberately cannot read Terraform state or its lock table: state holds
# credentials (the dev-hop private key, provider tokens) and these boxes run agents with
# permission prompts disabled. Instead the jumpbox publishes what a full plan found, every
# time the repo's Stop hook plans (.claude/hooks/verify-consistent.sh ->
# scripts/publish-applied-status.sh), and only from a clean origin/main checkout:
#
#   aws ssm get-parameter --region us-west-1 --name /aws-infra/applied-status \
#     --query Parameter.Value --output text
#
#   {"main":"<sha>","subject":"...","plan":"clean|pending|error","pending_count":N,
#    "pending":["<address> <action>",...],"checked_at":"<UTC>"}
#
# A merged PR is applied when its merge commit is an ancestor of "main" and "plan" is
# "clean". Terraform owns the parameter; the jumpbox writes its value, so value is ignored.

resource "aws_ssm_parameter" "applied_status" {
  name        = "/aws-infra/applied-status"
  description = "Non-secret result of the jumpbox's last full plan of origin/main (see applied-status.tf)"
  type        = "String"
  tier        = "Standard"
  value       = jsonencode({ plan = "unpublished" })

  lifecycle {
    ignore_changes = [value]
  }
}

locals {
  # Read-only views that let a dev box confirm a deploy without state or secret access.
  # Each returns metadata only. Deliberately excluded because their responses can carry
  # credentials: Terraform state and its lock table, Lambda configuration (environment
  # variables), log reads, launch-template or instance user data, and every other SSM
  # parameter or secret.
  dev_box_deploy_status_statements = concat([
    {
      Sid      = "ReadAppliedStatus"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = [aws_ssm_parameter.applied_status.arn]
    },
    {
      # Alarm definitions and states, e.g. fleet-backup-copies-missing. No resource scope.
      Sid      = "ReadAlarmStates"
      Effect   = "Allow"
      Action   = ["cloudwatch:DescribeAlarms"]
      Resource = ["*"]
    },
    {
      # Backup and copy job metadata: resource, state, timestamps. No resource scope.
      Sid      = "ReadBackupJobStatus"
      Effect   = "Allow"
      Action   = ["backup:ListBackupJobs", "backup:ListCopyJobs"]
      Resource = ["*"]
    },
    ], var.enable_github_runner ? [
    {
      # Table schema and item count only; no GetItem, Query or Scan.
      Sid      = "DescribeRunnerRegistrationTable"
      Effect   = "Allow"
      Action   = ["dynamodb:DescribeTable"]
      Resource = [aws_dynamodb_table.runner_registration[0].arn]
    },
    {
      # Schedule only. ListTargetsByRule is left out: target input can carry data.
      Sid      = "DescribeRunnerCleanupSchedule"
      Effect   = "Allow"
      Action   = ["events:DescribeRule"]
      Resource = [aws_cloudwatch_event_rule.runner_cleanup[0].arn]
    },
  ] : [])
}
