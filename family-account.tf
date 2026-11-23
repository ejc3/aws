# family-account.tf
#
# Family's own AWS account (ejc3/constellation docs/APP-ACCOUNTS.md: one account per app, Family
# first), from modules/app-account: the account and its OU, guardrails (us-west-1 only, no leaving
# the organization, no IAM users or keys), its state bucket and apply role `family-terraform`, a
# budget, the /infra/family pointer, and its staging host. What runs in the account is
# ejc3/constellation's (infra/apps/family, from infra/modules/app), applied there with that role.
#
# First apply, as for every account: the provider alias below needs the account id, so apply the
# account first (-target=module.family_account.aws_organizations_account.this), then everything.

provider "aws" {
  alias  = "family"
  region = var.aws_region
  assume_role {
    role_arn = "arn:aws:iam::${module.family_account.account_id}:role/OrganizationAccountAccessRole"
  }
}

module "family_account" {
  source = "./modules/app-account"
  providers = {
    aws     = aws
    aws.app = aws.family
  }
  app     = "family"
  ou_name = "Family"
  email   = local.family_account_email
  # From the organization resource, not the data source: the guardrail SCP is then ordered after
  # the organization's policy types are enabled (security-organization.tf), as Astryx's depends_on does.
  organization_root_id = aws_organizations_organization.security.roots[0].id
  region               = var.aws_region
  global_actions       = local.dolphin_region_scp_global_actions
  appliers = {
    dev_server_role_arn  = aws_iam_role.dev_server.arn
    applier_instance_arn = local.constellation_applier_instance_arn
    nextjs_dev_role_arn  = aws_iam_role.nextjs_dev.arn
  }
  # The task and Postgres: about $43 a month with the task on (constellation docs/APP-ACCOUNTS.md).
  monthly_budget_usd = 60
  budget_email       = local.browser_manager_owner
  stage = {
    cloudflare_account_id = var.cloudflare_account_id
    zone_id               = var.dolphin_zone_id
    hostname              = "family-sync-stage.dolphin-labs.dev"
    team_name             = "ejc3"
    access_issuer         = local.browser_manager_issuer
    idp_ids = concat(
      cloudflare_zero_trust_access_identity_provider.github[*].id,
      cloudflare_zero_trust_access_identity_provider.google[*].id,
      [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
    )
    # The API (API_APPS=family, :8081): its sign-in behind Access; /family/v1/* and /family/native/*
    # with the app's own token; /healthz. zero-cache (:4848) takes everything else. The API's
    # /family/zero/* (zero-cache's calls, on the task's localhost) is never routed.
    api_port         = 8081
    zero_port        = 4848
    login_path       = "/family/native/login"
    api_path_regex   = "^/(family/(v1|native)/|healthz$)"
    login_policy_ids = [cloudflare_zero_trust_access_policy.cc_games_allowed.id]
  }
}

output "family_terraform" {
  description = "What ejc3/constellation's infra/apps/family backend and provider name (no credentials); also in /infra/family"
  value       = module.family_account.pointer
}
