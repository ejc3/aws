# default-security-groups.tf
#
# Every VPC's DEFAULT security group, emptied: no ingress and no egress (Security Hub EC2.2). Nothing here uses one.
# Every instance, launch template and launcher names its own group, and on 2026-10-19 none of these groups was attached
# to a network interface. A default group allows all traffic between its members and all egress, so anything launched
# without a group (a console click, a launch template that omits it) would get that silently; empty, it gets nothing
# and the mistake shows at once.
#
# aws_default_security_group adopts the group AWS made with the VPC (it cannot be created or deleted) and on apply
# REMOVES every rule it does not declare, so declaring none is the whole change. Removing the resource from Terraform
# leaves the group empty.
#
# One per VPC in each region where Security Hub runs its controls (us-west-1, us-west-2, us-east-1). A new VPC there gets
# its line here. The Ohio VPCs are not covered: Security Hub posture is off in us-east-2.
# scripts/test-security-hub-automation.py checks that no group here declares a rule.

# The AWS default VPCs this repository does not manage. us-west-2's is data.aws_vpc.west2_default (io-box.tf).
data "aws_vpc" "west1_default" {
  default = true
}

data "aws_vpc" "east1_default" {
  provider = aws.dr
  default  = true
}

# ============================================================ us-west-1
resource "aws_default_security_group" "main" {
  vpc_id = local.vpc_id # main.tf, the primary VPC
}

resource "aws_default_security_group" "west1_default" {
  vpc_id = data.aws_vpc.west1_default.id
}

resource "aws_default_security_group" "runner" {
  count  = var.enable_github_runner ? 1 : 0
  vpc_id = aws_vpc.runner[0].id # runner-vpc.tf
}

resource "aws_default_security_group" "pbox_fallback_us_west_1" {
  vpc_id = module.pbox_fallback_us_west_1.vpc_id # pbox-fallback.tf
}

# ============================================================ us-west-2
resource "aws_default_security_group" "west2_default" {
  provider = aws.west2
  vpc_id   = data.aws_vpc.west2_default.id
}

resource "aws_default_security_group" "pbox_fallback_us_west_2" {
  provider = aws.west2
  vpc_id   = module.pbox_fallback_us_west_2.vpc_id
}

# ============================================================ us-east-1
resource "aws_default_security_group" "east1_default" {
  provider = aws.dr
  vpc_id   = data.aws_vpc.east1_default.id
}

resource "aws_default_security_group" "pbox_fallback_us_east_1" {
  provider = aws.dr
  vpc_id   = module.pbox_fallback_us_east_1.vpc_id
}
