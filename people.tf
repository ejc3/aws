# people.tf: where people's email addresses live, so they are not in git.
#
# Some configuration is a person's address and nothing else: who the Cloudflare Access policies let through to the dev sites, who
# owns the browser manager, the root address of the staging AWS account. They used to be literals here. They are one SecureString
# parameter instead, `/infra/people`, which this file creates EMPTY (a placeholder) and the administration set fills out of band:
#
#   aws ssm put-parameter --region us-west-1 --name /infra/people --type SecureString --overwrite --value file:///dev/stdin
#   (JSON on stdin: {"owner": "...", "family": ["...", "..."], "staging_account": "..."})
#
# Terraform ignores the value after creation, so it never overwrites it and the placeholder stays out of every plan. Every
# machine that plans (both jumpboxes, the Stop hook) has the administration role and reads the real value; a dev box cannot plan
# and cannot read it. The value is in Terraform state like the other secrets Terraform reads. Never write an address into a
# file, a comment, a test, a commit message or a pull request: use an example.com one.
resource "aws_ssm_parameter" "people" {
  name        = "/infra/people"
  description = "People's email addresses used by configuration (JSON: owner, family, staging_account). Value set out of band; see people.tf."
  type        = "SecureString"
  tier        = "Standard"
  value       = jsonencode({ placeholder = true })

  lifecycle {
    ignore_changes = [value]
  }
}
