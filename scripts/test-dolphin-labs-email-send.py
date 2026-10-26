#!/usr/bin/env python3
"""dolphin-labs-email-send.tf: the owner can send as the ej work address through SES in the Dolphin account. The sender may
only call ses:SendRawEmail on the dolphin-labs.dev identity with From the ej alias; its SMTP credentials go only into an
administration-only secret, never an output; DKIM, the MAIL FROM subdomain and DMARC are added without touching the apex
records Cloudflare Email Routing owns."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-email-send.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name):
    m = re.search(r'^%s "%s" \{\n.*?^\}\n' % (re.escape(kind), re.escape(name)), code(TF) + "\n", re.M | re.S)
    assert m, (kind, name)
    return m.group()


class SenderTests(unittest.TestCase):
    def test_no_address_literal_in_the_file(self):
        self.assertNotRegex(TF, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
        self.assertIn('dolphin_ej_address         = "ej@${local.dolphin_labs_domain}"', TF)

    def test_one_action_on_this_identity_from_the_ej_alias_only(self):
        doc = block('data "aws_iam_policy_document"', "dolphin_ej_smtp")
        self.assertEqual(doc.count("statement {"), 1)
        self.assertIn('actions   = ["ses:SendRawEmail"]', doc)
        self.assertIn("resources = [aws_sesv2_email_identity.dolphin_labs[0].arn]", doc)
        self.assertIn('variable = "ses:FromAddress"', doc)
        self.assertIn("values   = [local.dolphin_ej_address]", doc)
        self.assertNotRegex(code(TF), r'"ses:\*"|"\*:\*"|resources\s+= \["\*"\]')
        self.assertEqual(code(TF).count('resource "aws_iam_user_policy"'), 1)
        for kind in ("aws_iam_role", "aws_iam_policy_attachment", "aws_iam_user_policy_attachment", "aws_iam_group"):
            self.assertNotIn('resource "%s"' % kind, TF)

    def test_smtp_credentials_go_only_into_the_admin_only_secret(self):
        ver = block('resource "aws_secretsmanager_secret_version"', "dolphin_labs_ej_smtp")
        self.assertIn("aws_iam_access_key.dolphin_labs_ej_smtp[0].id", ver)
        self.assertIn("aws_iam_access_key.dolphin_labs_ej_smtp[0].ses_smtp_password_v4", ver)
        rest = code(TF).replace(ver, "")
        self.assertNotRegex(rest, r"aws_iam_access_key\.dolphin_labs_ej_smtp\[0\]\.(id|secret|ses_smtp_password_v4)")
        out = block("output", "dolphin_labs_email_send")
        self.assertNotIn("aws_iam_access_key", out)
        pol = block('resource "aws_secretsmanager_secret_policy"', "dolphin_labs_ej_smtp")
        self.assertIn('Effect    = "Deny"', pol)
        self.assertIn('"secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"', pol)
        self.assertIn('ArnNotLike = { "aws:PrincipalArn" = local.dolphin_ej_smtp_admins }', pol)
        self.assertIn('dolphin_ej_smtp_secret     = "dolphin-labs/ej-smtp"', TF)


class DolphinAccountTests(unittest.TestCase):
    def test_every_aws_resource_is_in_the_dolphin_account(self):
        found = re.findall(r'^resource "(aws_\w+)" "(\w+)" \{\n(.*?)^\}', code(TF), re.M | re.S)
        self.assertGreaterEqual(len(found), 8)
        for kind, name, body in found:
            self.assertRegex(body, r"(?m)^  provider\s+= aws\.dolphin$", "%s.%s" % (kind, name))

    def test_nothing_names_the_dolphin_account_or_trusts_it(self):
        self.assertNotIn("aws_organizations_account.dolphin", TF)
        self.assertNotIn("local.dolphin_account_id", TF)
        self.assertNotRegex(TF, r"\b\d{12}\b")
        self.assertNotRegex(code(TF), r"sts:AssumeRole|Principal = \{")

    def test_region_is_the_providers_and_one_the_scp_allows(self):
        ident = block('resource "aws_sesv2_email_identity"', "dolphin_labs")
        self.assertIn("var.aws_region == local.dolphin_ej_send_region", ident)
        self.assertIn("contains(local.dolphin_security_regions, local.dolphin_ej_send_region)", ident)
        self.assertIn('dolphin_ej_send_region  = "us-west-1"', TF)


class DnsTests(unittest.TestCase):
    def test_apex_records_stay_email_routings(self):
        records = re.findall(r'^resource "cloudflare_dns_record" "\w+" \{\n(.*?)^\}', code(TF), re.M | re.S)
        names = [re.search(r"(?m)^  name\s+= (.+)$", r).group(1) for r in records]
        self.assertEqual(len(names), 4)
        for n in names:
            self.assertNotEqual(n.strip(), "local.dolphin_labs_domain")  # never a record on the apex
        self.assertNotIn("cloudflare_email_routing", code(TF))
        self.assertIn('dolphin_ej_mail_from       = "bounce.${local.dolphin_labs_domain}"', TF)

    def test_mail_from_points_at_ses_and_has_its_own_spf(self):
        mx = block('resource "cloudflare_dns_record"', "dolphin_ses_mail_from_mx")
        self.assertIn('content  = "feedback-smtp.${local.dolphin_ej_send_region}.amazonses.com"', mx)
        spf = block('resource "cloudflare_dns_record"', "dolphin_ses_mail_from_spf")
        self.assertIn("name    = local.dolphin_ej_mail_from", spf)
        self.assertIn('content = "\\"v=spf1 include:amazonses.com ~all\\""', spf)
        mf = block('resource "aws_sesv2_email_identity_mail_from_attributes"', "dolphin_labs")
        self.assertIn("mail_from_domain       = local.dolphin_ej_mail_from", mf)

    def test_three_dkim_cnames_unproxied(self):
        dkim = block('resource "cloudflare_dns_record"', "dolphin_ses_dkim")
        self.assertIn("count   = local.dolphin_ej_send_enabled ? 3 : 0", dkim)
        self.assertIn("._domainkey.${local.dolphin_labs_domain}", dkim)
        self.assertIn(".dkim.amazonses.com", dkim)
        self.assertIn("proxied = false", dkim)

    def test_dmarc_monitors_only(self):
        dmarc = block('resource "cloudflare_dns_record"', "dolphin_dmarc")
        self.assertIn('name    = "_dmarc.${local.dolphin_labs_domain}"', dmarc)
        self.assertIn("p=none; rua=mailto:${local.dolphin_ej_address}", dmarc)

    def test_everything_waits_for_the_ej_forward(self):
        self.assertIn("dolphin_ej_send_enabled = local.dolphin_privacy_enabled", TF)
        counted = re.findall(r'^(?:resource|data) "\w+" "\w+" \{\n  (?:provider\s+= aws\.dolphin\n  )?count\s+= (.+)$', code(TF), re.M)
        blocks = re.findall(r'^(?:resource|data) "', code(TF), re.M)
        self.assertEqual(len(counted), len(blocks))
        for c in counted:
            self.assertTrue(c.startswith("local.dolphin_ej_send_enabled ?"), c)


if __name__ == "__main__":
    unittest.main(buffer=True)
