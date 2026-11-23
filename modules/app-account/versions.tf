terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # aws: the management account (the organization, the pointer). aws.app: inside the app's own
      # account, through its OrganizationAccountAccessRole (the caller's provider alias).
      configuration_aliases = [aws.app]
    }
    cloudflare = { source = "ejc3/cloudflare" }
  }
}
