# fcvm-fleet-prod-drills.tf
#
# The production fleet's Spot reclaim drill (ejc3/aws#942): an AWS FIS experiment that sends one running prod GPU host
# (Purpose=fcvm-fleet-host, Fleet=prod, us-east-1) the real two-minute Spot interruption notice, so the coordinator's
# replace-at-notice path and the second awake host are exercised end to end. fcvm-arm may start, read and stop this
# experiment (fcvm-fleet-account.tf, fcvm_fleet_terraform_observe) and nothing else in FIS; the experiment's own role
# may interrupt only prod hosts. Behind var.fcvm_fleet_prod_enabled like the stack it drills.

resource "aws_iam_role" "fcvm_fleet_prod_spot_drill" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-prod-spot-drill"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "fis.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.fcvm_fleet_prod_tags
}

resource "aws_iam_role_policy" "fcvm_fleet_prod_spot_drill" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "interrupt-prod-hosts-only"
  role     = aws_iam_role.fcvm_fleet_prod_spot_drill[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InterruptOnlyProdHosts"
        Effect   = "Allow"
        Action   = "ec2:SendSpotInstanceInterruptions"
        Resource = "arn:aws:ec2:us-east-1:${local.fcvm_fleet_account_id}:instance/*"
        Condition = { StringEquals = {
          "ec2:ResourceTag/Fleet"   = "prod"
          "ec2:ResourceTag/Purpose" = "fcvm-fleet-host"
        } }
      },
      { Sid = "FindThem", Effect = "Allow", Action = "ec2:DescribeInstances", Resource = "*" },
    ]
  })
}

resource "aws_fis_experiment_template" "fcvm_fleet_prod_spot_drill" {
  count       = local.fcvm_fleet_prod
  provider    = aws.fcvm_fleet_east1
  description = "fcvm prod: one running GPU host gets the two-minute Spot interruption notice"
  role_arn    = aws_iam_role.fcvm_fleet_prod_spot_drill[0].arn

  stop_condition {
    source = "none"
  }

  target {
    name           = "one-prod-host"
    resource_type  = "aws:ec2:spot-instance"
    selection_mode = "COUNT(1)"
    resource_tag {
      key   = "Fleet"
      value = "prod"
    }
    resource_tag {
      key   = "Purpose"
      value = "fcvm-fleet-host"
    }
    filter {
      path   = "State.Name"
      values = ["running"]
    }
  }

  action {
    name      = "interrupt"
    action_id = "aws:ec2:send-spot-instance-interruptions"
    parameter {
      key   = "durationBeforeInterruption"
      value = "PT2M"
    }
    target {
      key   = "SpotInstances"
      value = "one-prod-host"
    }
  }

  tags = merge(local.fcvm_fleet_prod_tags, { Name = "fcvm-fleet-prod-spot-drill" })
}
