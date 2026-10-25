# dolphin-labs-email-send.tf
#
# Sending AS the owner's Dolphin Labs work address (the ej alias, dolphin-labs-email.tf), which until now only received:
# the owner adds it to Gmail as "Send mail as" with Amazon SES's SMTP endpoint, and mail from it passes SPF, DKIM and
# DMARC for dolphin-labs.dev. Inbound routing is untouched: Cloudflare Email Routing keeps the apex MX, its SPF TXT and
# its cf2024-1 DKIM key.
#
#   SES domain identity    dolphin-labs.dev with Easy DKIM (RSA 2048): three CNAMEs <token>._domainkey -> amazonses.com.
#                          DKIM d=dolphin-labs.dev is what aligns for DMARC.
#   Custom MAIL FROM       bounce.dolphin-labs.dev: MX to SES's feedback endpoint and its own SPF TXT, so the envelope
#                          sender is in the domain (SPF aligns too) and the apex SPF never needs an SES include. A record
#                          of its own means the * previews wildcard (dolphin-labs-zone.tf) no longer answers for it.
#   DMARC                  _dmarc TXT, p=none, aggregate reports to the ej alias. Tighten to quarantine once the reports
#                          show only Cloudflare-forwarded and SES mail.
#   Sender                 IAM user dolphin-labs-ej-smtp: ses:SendRawEmail (what SMTP calls) on this identity only, with
#                          From the ej alias only. Its SMTP password is derived from its key for this region; both go only
#                          into dolphin-labs/ej-smtp, administration only, never an output. They are in Terraform state, as
#                          the moderation keys are.
#
# Everything AWS is in the DOLPHIN account (provider aws.dolphin, us-west-1, a region its SCP allows), billed and audited
# there, as dolphin-maps-moderation.tf. Created only while the ej alias forwards (local.dolphin_privacy_enabled), since
# Gmail's confirmation mail for "Send mail as" arrives through that forward.
#
# ACTIVATION after the apply, once (no Terraform resource or API in this repository does it):
#   (1) SES production access for the Dolphin account in us-west-1 (a new account is in the sandbox and may send only to
#       verified addresses): aws sesv2 put-account-details --region us-west-1 --production-access-enabled
#       --mail-type TRANSACTIONAL --website-url https://dolphin-labs.dev --use-case-description "..." in a session in the
#       Dolphin account's OrganizationAccountAccessRole. AWS reviews it, usually within a day.
#   (2) Gmail, Settings, Accounts and Import, "Send mail as", Add another email address: the ej alias, SMTP server
#       email-smtp.us-west-1.amazonaws.com port 587 with TLS, user name and password from dolphin-labs/ej-smtp.
#   Rotate: terraform apply -replace=aws_iam_access_key.dolphin_labs_ej_smtp[0], then the new password into Gmail.
locals {
  dolphin_ej_send_enabled = local.dolphin_privacy_enabled
  dolphin_ej_send_region  = "us-west-1"
  # Assembled, so no address literal sits in the repository (scripts/test-people.py).
  dolphin_ej_address         = "ej@${local.dolphin_labs_domain}"
  dolphin_ej_mail_from       = "bounce.${local.dolphin_labs_domain}"
  dolphin_ej_smtp_host       = "email-smtp.${local.dolphin_ej_send_region}.amazonaws.com"
  dolphin_ej_smtp_secret     = "dolphin-labs/ej-smtp"
  dolphin_ej_smtp_user       = "dolphin-labs-ej-smtp"
  dolphin_ej_send_hour_alarm = 100 # one person's correspondence; far more in an hour is a leaked password or a loop

  dolphin_ej_smtp_admins = [
    "arn:aws:iam::${data.aws_caller_identity.dolphin.account_id}:role/OrganizationAccountAccessRole",
    local.dolphin_terraform_role_arn,
    local.dolphin_sso_admin_role_arns,
  ]
}

resource "aws_sesv2_email_identity" "dolphin_labs" {
  provider       = aws.dolphin
  count          = local.dolphin_ej_send_enabled ? 1 : 0
  email_identity = local.dolphin_labs_domain
  tags           = { Name = local.dolphin_labs_domain, Project = "dolphin-labs" }

  lifecycle {
    # The SMTP password is derived for the provider's region, and the Dolphin account's SCP denies every other region.
    precondition {
      condition     = var.aws_region == local.dolphin_ej_send_region && contains(local.dolphin_security_regions, local.dolphin_ej_send_region)
      error_message = "SES sending must be in the aws.dolphin provider's region, one the Dolphin account's SCP allows (dolphin-labs-email-send.tf)."
    }
  }
}

resource "aws_sesv2_email_identity_mail_from_attributes" "dolphin_labs" {
  provider               = aws.dolphin
  count                  = local.dolphin_ej_send_enabled ? 1 : 0
  email_identity         = aws_sesv2_email_identity.dolphin_labs[0].email_identity
  mail_from_domain       = local.dolphin_ej_mail_from
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

# Easy DKIM always issues three tokens; count, not for_each, because they are known only after the identity exists.
resource "cloudflare_dns_record" "dolphin_ses_dkim" {
  count   = local.dolphin_ej_send_enabled ? 3 : 0
  zone_id = var.dolphin_zone_id
  name    = "${aws_sesv2_email_identity.dolphin_labs[0].dkim_signing_attributes[0].tokens[count.index]}._domainkey.${local.dolphin_labs_domain}"
  type    = "CNAME"
  content = "${aws_sesv2_email_identity.dolphin_labs[0].dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"
  proxied = false
  ttl     = 1
  comment = "SES Easy DKIM for sending as the work address; dolphin-labs-email-send.tf"
}

resource "cloudflare_dns_record" "dolphin_ses_mail_from_mx" {
  count    = local.dolphin_ej_send_enabled ? 1 : 0
  zone_id  = var.dolphin_zone_id
  name     = local.dolphin_ej_mail_from
  type     = "MX"
  content  = "feedback-smtp.${local.dolphin_ej_send_region}.amazonses.com"
  priority = 10
  proxied  = false
  ttl      = 1
  comment  = "SES custom MAIL FROM (bounces); dolphin-labs-email-send.tf"
}

resource "cloudflare_dns_record" "dolphin_ses_mail_from_spf" {
  count   = local.dolphin_ej_send_enabled ? 1 : 0
  zone_id = var.dolphin_zone_id
  name    = local.dolphin_ej_mail_from
  type    = "TXT"
  content = "\"v=spf1 include:amazonses.com ~all\""
  proxied = false
  ttl     = 1
  comment = "SPF of the SES MAIL FROM domain; the apex keeps Email Routing's own; dolphin-labs-email-send.tf"
}

resource "cloudflare_dns_record" "dolphin_dmarc" {
  count   = local.dolphin_ej_send_enabled ? 1 : 0
  zone_id = var.dolphin_zone_id
  name    = "_dmarc.${local.dolphin_labs_domain}"
  type    = "TXT"
  content = "\"v=DMARC1; p=none; rua=mailto:${local.dolphin_ej_address}\""
  proxied = false
  ttl     = 1
  comment = "DMARC, monitoring only; dolphin-labs-email-send.tf"
}

data "aws_iam_policy_document" "dolphin_ej_smtp" {
  count = local.dolphin_ej_send_enabled ? 1 : 0

  statement {
    sid       = "SendAsTheWorkAddressOnly"
    actions   = ["ses:SendRawEmail"]
    resources = [aws_sesv2_email_identity.dolphin_labs[0].arn]
    condition {
      test     = "StringEquals"
      variable = "ses:FromAddress"
      values   = [local.dolphin_ej_address]
    }
  }
}

resource "aws_iam_user" "dolphin_labs_ej_smtp" {
  provider = aws.dolphin
  count    = local.dolphin_ej_send_enabled ? 1 : 0
  name     = local.dolphin_ej_smtp_user
  tags     = { Name = local.dolphin_ej_smtp_user, Project = "dolphin-labs" }
}

resource "aws_iam_user_policy" "dolphin_labs_ej_smtp" {
  provider = aws.dolphin
  count    = local.dolphin_ej_send_enabled ? 1 : 0
  name     = "send-as-work-address"
  user     = aws_iam_user.dolphin_labs_ej_smtp[0].name
  policy   = data.aws_iam_policy_document.dolphin_ej_smtp[0].json
}

resource "aws_iam_access_key" "dolphin_labs_ej_smtp" {
  provider = aws.dolphin
  count    = local.dolphin_ej_send_enabled ? 1 : 0
  user     = aws_iam_user.dolphin_labs_ej_smtp[0].name
}

resource "aws_secretsmanager_secret" "dolphin_labs_ej_smtp" {
  provider                = aws.dolphin
  count                   = local.dolphin_ej_send_enabled ? 1 : 0
  name                    = local.dolphin_ej_smtp_secret
  description             = "SES SMTP credentials for sending as the Dolphin Labs work address from Gmail (JSON: SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, FROM). Written by ejc3/aws dolphin-labs-email-send.tf."
  recovery_window_in_days = 7
  tags                    = { Name = local.dolphin_ej_smtp_secret, Project = "dolphin-labs" }
}

resource "aws_secretsmanager_secret_policy" "dolphin_labs_ej_smtp" {
  provider            = aws.dolphin
  count               = local.dolphin_ej_send_enabled ? 1 : 0
  secret_arn          = aws_secretsmanager_secret.dolphin_labs_ej_smtp[0].arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]
      Resource  = "*"
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.dolphin_ej_smtp_admins } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "dolphin_labs_ej_smtp" {
  provider  = aws.dolphin
  count     = local.dolphin_ej_send_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.dolphin_labs_ej_smtp[0].id
  secret_string = jsonencode({
    SMTP_HOST     = local.dolphin_ej_smtp_host
    SMTP_PORT     = "587"
    SMTP_USERNAME = aws_iam_access_key.dolphin_labs_ej_smtp[0].id
    SMTP_PASSWORD = aws_iam_access_key.dolphin_labs_ej_smtp[0].ses_smtp_password_v4
    FROM          = local.dolphin_ej_address
  })
}

# A leaked password can send only as the ej alias, but it can still send a lot. SES's Send metric is per account and
# region, and nothing else in the Dolphin account sends.
resource "aws_cloudwatch_metric_alarm" "dolphin_ej_send_volume" {
  provider            = aws.dolphin
  count               = local.dolphin_ej_send_enabled ? 1 : 0
  alarm_name          = "dolphin-labs-ej-send-volume"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = local.dolphin_ej_send_hour_alarm
  alarm_description   = "More than ${local.dolphin_ej_send_hour_alarm} SES sends in an hour from the Dolphin Labs work address: a leaked SMTP password or a loop. Rotate per ejc3/aws dolphin-labs-email-send.tf."
  alarm_actions       = [data.aws_sns_topic.dolphin_alerts.arn]
  treat_missing_data  = "notBreaching"
  namespace           = "AWS/SES"
  metric_name         = "Send"
  statistic           = "Sum"
  period              = 3600
}

output "dolphin_labs_email_send" {
  description = "Sending as the Dolphin Labs work address: the SMTP endpoint, the MAIL FROM domain, and the Dolphin-account secret holding the SMTP user name and password (never the values)."
  value = local.dolphin_ej_send_enabled ? {
    smtp_host        = local.dolphin_ej_smtp_host
    smtp_port        = 587
    mail_from_domain = local.dolphin_ej_mail_from
    secret           = aws_secretsmanager_secret.dolphin_labs_ej_smtp[0].name
  } : null
}
