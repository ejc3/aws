# dolphin-identity-center.tf
#
# Who signs in to the Dolphin account, through IAM Identity Center (owned by this, the management account; its portal is
# output dolphin_sign_in_url):
#
#   the owner   existing permission set AdministratorAccess, on the Dolphin account
#   Steve       new permission set DolphinReadOnly, on the Dolphin account only: AWS ReadOnlyAccess, 8-hour sessions, and
#               an explicit deny on every read that returns a secret value (below)
#
# Steve's user (user name = his address) and his assignment exist only while the people/addresses secret holds his address
# under the key dolphin_steve (people.tf). A user created through the API gets no invitation: he sets his password with
# "Forgot password" at the portal.
#
# The AdministratorAccess permission set predates Terraform here and is only read, not managed.

data "aws_ssoadmin_instances" "this" {}

locals {
  sso_instance_arn  = tolist(data.aws_ssoadmin_instances.this.arns)[0]
  identity_store_id = tolist(data.aws_ssoadmin_instances.this.identity_store_ids)[0]

  # nonsensitive() for the reason given in people.tf: an unchanged address must plan no change.
  dolphin_steve_email = nonsensitive(try(tostring(local.people.dolphin_steve), ""))
}

data "aws_ssoadmin_permission_set" "administrator" {
  instance_arn = local.sso_instance_arn
  name         = "AdministratorAccess"
}

data "aws_identitystore_user" "owner" {
  identity_store_id = local.identity_store_id
  alternate_identifier {
    unique_attribute {
      attribute_path  = "UserName"
      attribute_value = "ejc3"
    }
  }
}

resource "aws_ssoadmin_account_assignment" "dolphin_owner_admin" {
  instance_arn       = local.sso_instance_arn
  permission_set_arn = data.aws_ssoadmin_permission_set.administrator.arn
  principal_type     = "USER"
  principal_id       = data.aws_identitystore_user.owner.user_id
  target_type        = "AWS_ACCOUNT"
  target_id          = aws_organizations_account.dolphin.id
}

# ------------------------------------------------------------------------------------------------ Steve: read-only
resource "aws_ssoadmin_permission_set" "dolphin_read_only" {
  instance_arn     = local.sso_instance_arn
  name             = "DolphinReadOnly"
  description      = "Read-only on the Dolphin account; no secret values"
  session_duration = "PT8H"
  tags             = { Managed = "terraform" }
}

resource "aws_ssoadmin_managed_policy_attachment" "dolphin_read_only" {
  instance_arn       = local.sso_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.dolphin_read_only.arn
  managed_policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# ReadOnlyAccess already leaves out secretsmanager:GetSecretValue. It does include other reads that hand back a secret,
# so those are denied here: ssm:Get* (SecureString parameters decrypt), s3:Get* on the state (Terraform state holds
# secret values; the bucket's own policy refuses it too), and the configuration reads that return environment variables
# or user data in plain text (Lambda, ECS task definitions, CodeBuild projects, EC2 user data and launch templates),
# which a kms:Decrypt deny does not stop.
resource "aws_ssoadmin_permission_set_inline_policy" "dolphin_read_only" {
  instance_arn       = local.sso_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.dolphin_read_only.arn
  inline_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "NoSecretValues"
        Effect = "Deny"
        Action = [
          "secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue",
          "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory",
          "kms:Decrypt",
          "lambda:GetFunction", "lambda:GetFunctionConfiguration",
          "ecs:DescribeTaskDefinition",
          "codebuild:BatchGetProjects",
          "ec2:DescribeInstanceAttribute", "ec2:DescribeLaunchTemplateVersions", "ec2:GetLaunchTemplateData",
        ]
        Resource = "*"
      },
      {
        Sid      = "NoTerraformState"
        Effect   = "Deny"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "arn:aws:s3:::${local.dolphin_state_bucket}/*"
      },
    ]
  })
}

resource "aws_identitystore_user" "dolphin_steve" {
  count             = local.dolphin_steve_email == "" ? 0 : 1
  identity_store_id = local.identity_store_id
  user_name         = local.dolphin_steve_email
  display_name      = "Steve Krutzler"
  name {
    given_name  = "Steve"
    family_name = "Krutzler"
  }
  emails {
    value   = local.dolphin_steve_email
    primary = true
  }

  lifecycle {
    precondition {
      condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", local.dolphin_steve_email))
      error_message = "people/addresses: dolphin_steve must be one email address."
    }
  }
}

resource "aws_ssoadmin_account_assignment" "dolphin_steve_read_only" {
  count              = local.dolphin_steve_email == "" ? 0 : 1
  instance_arn       = local.sso_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.dolphin_read_only.arn
  principal_type     = "USER"
  principal_id       = aws_identitystore_user.dolphin_steve[0].user_id
  target_type        = "AWS_ACCOUNT"
  target_id          = aws_organizations_account.dolphin.id
}

output "dolphin_sign_in_url" {
  description = "IAM Identity Center portal: the owner (AdministratorAccess) and, once dolphin_steve is set, Steve (DolphinReadOnly)"
  value       = "https://${local.identity_store_id}.awsapps.com/start"
}
