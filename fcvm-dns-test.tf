# fcvm-dns-test.tf
#
# A Cloudflare token for measuring DNS propagation in the fcvm.dev zone from the dev boxes (owner, 2026-10-24: "Get
# yourself pr please. Can give you tightly scoped key"). An agent creates a short-lived test record, times how long
# Cloudflare's authoritative servers and the public resolvers take to answer it, and deletes it. That measures the fleet's
# DNS design, a unique name per launch (fcvm-dev.tf, ejc3/fcvm#1136).
#
#   fcvm-dns-test-token   Secrets Manager (us-west-1), the raw token string. Readers: administration and dev-server-role
#                         (the grant is a statement in assume-constellation-terraform, constellation-account.tf, because
#                         dev-server-role holds 20 of 20 managed policies and its inline policies are at IAM's size cap).
#
#   export CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 \
#     --secret-id fcvm-dns-test-token --query SecretString --output text)
#
# What the token can do: DNS Read and DNS Write on the fcvm.dev zone, nothing else (no other zone, no account
# permission). Cloudflare scopes a token to a zone, not to a name, so it could edit or delete ANY record in fcvm.dev,
# including the coordinator's. The limit on names is a convention the box keeps:
#   - touch only names matching dnstest-*.fcvm.dev, and delete each one when its measurement ends;
#   - never touch the fleet coordinator's names (slot-*, slot-*-v4, ec2-*, _acme-challenge*), stage.fcvm.dev,
#     fleet-api-stage.fcvm.dev, the apex, or any other record.
#
# Client IPs: the token works only from the dev fleet's two subnets over IPv6 (where the dev boxes reach
# api.cloudflare.com) and from the metal boxes' Elastic IPs over IPv4. Every address comes from Terraform: the subnets'
# IPv6 blocks (main.tf) and the EIPs (firecracker-dev.tf, x86-dev.tf). A box elsewhere (a pbox in another VPC or region)
# gets a 403 from the API even if it can read the secret.
#
# Expiry: local.fcvm_dns_test_token_expires_on, 90 days after it was set. Cloudflare refuses an expired token. To renew,
# move the date forward by 90 days and apply; Terraform writes the token's value into the secret either way.
#
# Same shape as cloudflare-observability.tf: a typed cloudflare_account_token minted through the token_minter provider
# alias, its value written to the container by Terraform (so it is in state; dev boxes cannot read state). ROTATE with
# `terraform apply -replace=cloudflare_account_token.fcvm_dns_test`.
locals {
  fcvm_dns_test_token_expires_on = "2027-01-22T00:00:00Z"
  dns_read                       = "82e64a83756745bbbb1c9c2701bf816b" # Cloudflare's "DNS Read" permission group (zone scope)

  fcvm_dns_test_client_cidrs = concat(
    [for s in local.dev_fleet_subnets : s.ipv6_cidr_block],
    [for e in aws_eip.firecracker_dev : "${e.public_ip}/32"],
    [for e in aws_eip.x86_dev : "${e.public_ip}/32"],
  )
}

resource "aws_secretsmanager_secret" "fcvm_dns_test_token" {
  name                    = "fcvm-dns-test-token"
  description             = "Cloudflare token: DNS Read and Write on fcvm.dev only, dev-fleet client IPs, 90-day expiry. Test records under dnstest-*.fcvm.dev only. Written by Terraform from cloudflare_account_token.fcvm_dns_test; see fcvm-dns-test.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "fcvm-dns-test-token", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "fcvm_dns_test_token" {
  secret_arn = aws_secretsmanager_secret.fcvm_dns_test_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheDevServerRoleCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.fcvm_dns_test_token.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn]) } }
    }]
  })
}

resource "cloudflare_account_token" "fcvm_dns_test" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "fcvm-dns-test-fcvm-dev"
  expires_on = local.fcvm_dns_test_token_expires_on
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.dns_read }, { id = local.dns_write }]
    resources         = jsonencode({ "com.cloudflare.api.account.zone.${var.fcvm_dev_zone_id}" = "*" })
  }]
  condition = {
    request_ip = { in = local.fcvm_dns_test_client_cidrs }
  }
}

resource "aws_secretsmanager_secret_version" "fcvm_dns_test_token" {
  secret_id     = aws_secretsmanager_secret.fcvm_dns_test_token.id
  secret_string = cloudflare_account_token.fcvm_dns_test.value
}
