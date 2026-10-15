# dolphin-alerts.tf: the owner's email on the Dolphin platform's alert topic.
#
# dolphin-labs-hq/dolphin-aws (ecs.tf) owns the topic `dolphin-alerts` in the Dolphin account: every alarm on its ECS
# cluster and services notifies it. The address is read here from people/addresses (people.tf), so it is never written
# into either repository. Like cost_alerts_email, an email subscription stays PendingConfirmation until the owner clicks
# the link AWS sends.

resource "aws_sns_topic_subscription" "dolphin_alerts_owner" {
  provider  = aws.dolphin
  topic_arn = "arn:aws:sns:${var.aws_region}:${aws_organizations_account.dolphin.id}:dolphin-alerts"
  protocol  = "email"
  endpoint  = local.browser_manager_owner
}
