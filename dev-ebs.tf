# dev-ebs.tf
#
# Every development box can create and use its own temporary EBS volumes without
# inheriting another box's broader IAM role. The guardrails are intentionally simple:
#   - us-west-1 and us-west-2 only
#   - encrypted gp3 only
#   - the create call must tag the volume DevEBS=true
#   - attach/detach only between DevEBS volumes and DevEBS instances
#   - delete only DevEBS volumes
#
# The jumpboxes already have AdministratorAccess. ARM/x86 share dev-server-role,
# nextjs keeps its narrow dedicated role, Mac keeps its own role, and io/parallel
# share a new EBS-only role.

locals {
  dev_ebs_volume_arns = [
    "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:volume/*",
    "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:volume/*",
  ]

  dev_ebs_instance_arns = [
    "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*",
    "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:instance/*",
  ]

  dev_ebs_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DescribeEBS"
        Effect = "Allow"
        Action = [
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeInstances",
          "ec2:DescribeVolumes",
          "ec2:DescribeVolumeStatus",
          "ec2:DescribeVolumesModifications",
          "ec2:DescribeTags",
        ]
        Resource = "*"
      },
      {
        Sid      = "CreateTaggedEncryptedGp3"
        Effect   = "Allow"
        Action   = "ec2:CreateVolume"
        Resource = local.dev_ebs_volume_arns
        Condition = {
          StringEquals = {
            "aws:RequestedRegion"   = ["us-west-1", "us-west-2"]
            "ec2:VolumeType"        = "gp3"
            "aws:RequestTag/DevEBS" = "true"
          }
          Bool = {
            "ec2:Encrypted" = "true"
          }
        }
      },
      {
        # CreateVolume with tag-specifications requires a separate CreateTags grant.
        Sid      = "TagDuringCreate"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = local.dev_ebs_volume_arns
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = "CreateVolume"
          }
        }
      },
      {
        Sid      = "ManageTagsOnDevVolumes"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = local.dev_ebs_volume_arns
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/DevEBS" = "true"
          }
        }
      },
      {
        # AttachVolume/DetachVolume authorise both resources independently: this
        # statement covers the volume side, and the next covers the instance side.
        Sid      = "AttachDetachDevVolumes"
        Effect   = "Allow"
        Action   = ["ec2:AttachVolume", "ec2:DetachVolume"]
        Resource = local.dev_ebs_volume_arns
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/DevEBS" = "true"
          }
        }
      },
      {
        Sid      = "AttachDetachToDevInstances"
        Effect   = "Allow"
        Action   = ["ec2:AttachVolume", "ec2:DetachVolume"]
        Resource = local.dev_ebs_instance_arns
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/DevEBS" = "true"
          }
        }
      },
      {
        Sid      = "DeleteDevVolumes"
        Effect   = "Allow"
        Action   = "ec2:DeleteVolume"
        Resource = local.dev_ebs_volume_arns
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/DevEBS" = "true"
          }
        }
      },
    ]
  })
}

# ARM and x86 metal boxes.
resource "aws_iam_role_policy" "dev_server_ebs" {
  name   = "dev-ebs-volume-access"
  role   = aws_iam_role.dev_server.id
  policy = local.dev_ebs_policy
}

# Kids' Next.js box: add only EBS lifecycle access, not the metal boxes' broader role.
resource "aws_iam_role_policy" "nextjs_dev_ebs" {
  name   = "dev-ebs-volume-access"
  role   = aws_iam_role.nextjs_dev.id
  policy = local.dev_ebs_policy
}

# Optional Mac dev box retains its separate Secrets Manager/SSM role too.
resource "aws_iam_role_policy" "mac_dev_ebs" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  name     = "dev-ebs-volume-access"
  role     = aws_iam_role.mac_instance[0].id
  policy   = local.dev_ebs_policy
}

# io-box and parallel-box need no AWS access beyond temporary EBS volume lifecycle, plus Claude Haiku 5.5
# on Bedrock since 2026-10-10 (bedrock-haiku.tf).
resource "aws_iam_role" "dev_ebs_only" {
  name = "dev-ebs-only-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = { Name = "dev-ebs-only-role" }
}

# Same reason as nextjs-dev: no host should be reachable only over ssh.
resource "aws_iam_role_policy_attachment" "dev_ebs_only_ssm" {
  role       = aws_iam_role.dev_ebs_only.name
  policy_arn = aws_iam_policy.ssm_managed_instance.arn

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role_policy" "dev_ebs_only" {
  name   = "dev-ebs-volume-access"
  role   = aws_iam_role.dev_ebs_only.id
  policy = local.dev_ebs_policy
}

resource "aws_iam_instance_profile" "dev_ebs_only" {
  name = "dev-ebs-only-profile"
  role = aws_iam_role.dev_ebs_only.name
}
