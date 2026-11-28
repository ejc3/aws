# The app's sending domain (optional): an Amazon SES domain identity in the app's own account, with
# Easy DKIM and a custom MAIL FROM, and its DNS in Cloudflare, as dolphin-labs-email-send.tf does for
# the work address. The app's repository grants its task role ses:SendEmail on this identity, From
# the one address in the pointer, and nothing else (ejc3/constellation infra/modules/app `email`).
# Family: its sign-in codes (ejc3/constellation docs/FAMILY.md, "Family's own sign-in").
#
# ACTIVATION after the apply, once (no Terraform resource does it): SES production access for the
# app's account in its region (a new account is in the SES sandbox and sends only to verified
# addresses): aws sesv2 put-account-details --region <region> --production-access-enabled
# --mail-type TRANSACTIONAL --website-url <site> --use-case-description "..." in a session in the
# app account's OrganizationAccountAccessRole. AWS reviews it.

locals {
  email_on        = var.email_sender != null
  email_mail_from = local.email_on ? "bounce.${var.email_sender.domain}" : null
  email_pointer = local.email_on ? {
    identity_arn = aws_sesv2_email_identity.sender[0].arn
    from         = "${var.email_sender.from_local_part}@${var.email_sender.domain}"
  } : null
}

resource "aws_sesv2_email_identity" "sender" {
  provider       = aws.app
  count          = local.email_on ? 1 : 0
  email_identity = var.email_sender.domain
  tags           = { Name = var.email_sender.domain, App = var.app }
}

resource "aws_sesv2_email_identity_mail_from_attributes" "sender" {
  provider               = aws.app
  count                  = local.email_on ? 1 : 0
  email_identity         = aws_sesv2_email_identity.sender[0].email_identity
  mail_from_domain       = local.email_mail_from
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

# Easy DKIM always issues three tokens; count, not for_each, because they are known only after the identity exists.
resource "cloudflare_dns_record" "sender_dkim" {
  count   = local.email_on ? 3 : 0
  zone_id = var.email_sender.zone_id
  name    = "${aws_sesv2_email_identity.sender[0].dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.email_sender.domain}"
  type    = "CNAME"
  content = "${aws_sesv2_email_identity.sender[0].dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"
  proxied = false
  ttl     = 1
  comment = "SES Easy DKIM for ${var.app}'s sending domain; modules/app-account/email.tf"
}

resource "cloudflare_dns_record" "sender_mail_from_mx" {
  count    = local.email_on ? 1 : 0
  zone_id  = var.email_sender.zone_id
  name     = local.email_mail_from
  type     = "MX"
  content  = "feedback-smtp.${var.region}.amazonses.com"
  priority = 10
  proxied  = false
  ttl      = 1
  comment  = "SES custom MAIL FROM (bounces) for ${var.app}; modules/app-account/email.tf"
}

resource "cloudflare_dns_record" "sender_mail_from_spf" {
  count   = local.email_on ? 1 : 0
  zone_id = var.email_sender.zone_id
  name    = local.email_mail_from
  type    = "TXT"
  content = "\"v=spf1 include:amazonses.com ~all\""
  proxied = false
  ttl     = 1
  comment = "SPF of ${var.app}'s SES MAIL FROM domain; modules/app-account/email.tf"
}
