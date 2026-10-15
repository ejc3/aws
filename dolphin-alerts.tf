# dolphin-alerts.tf: the owner's email on the Dolphin platform's alert topic.
#
# dolphin-labs-hq/dolphin-aws (ecs.tf) owns the topic `dolphin-alerts` in the Dolphin account: every alarm on its ECS
# cluster and services notifies it. The address is read here from people/addresses (people.tf), so it is never written
# into either repository. Like cost_alerts_email, an email subscription stays PendingConfirmation until the owner clicks
# the link AWS sends.

# Looked up by name through the Dolphin provider: no account id is written here (scripts/test-dolphin-account.py keeps
# references to it out of the management account's files).
data "aws_sns_topic" "dolphin_alerts" {
  provider = aws.dolphin
  name     = "dolphin-alerts"
}

resource "aws_sns_topic_subscription" "dolphin_alerts_owner" {
  provider  = aws.dolphin
  topic_arn = data.aws_sns_topic.dolphin_alerts.arn
  protocol  = "email"
  endpoint  = local.browser_manager_owner
}
