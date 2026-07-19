# mac-dev-secrets.tf
#
# VNC/account password for the temporary Mac, kept in SSM Parameter Store (SecureString)
# rather than in user_data. Follows the same pattern already used for
# /dev-servers/runner-ssh-key.
#
# Why not user_data: user_data is retrievable by anything holding
# ec2:DescribeInstanceAttribute. In this account that is only the SSO admin, the jumpbox
# role and TerraformGithubActionRole -- all yours -- but a credential still does not
# belong somewhere it can be read back in plaintext. The instance fetches it at boot with
# its own instance profile, so the secret never appears in the instance configuration.

resource "random_password" "mac_vnc" {
  count  = var.enable_mac_dev ? 1 : 0
  length = 20
  # Keep it shell/dscl-safe: no quotes, backslashes or $ that would need escaping.
  override_special = "-_.@#%+="
}

resource "aws_ssm_parameter" "mac_vnc" {
  count       = var.enable_mac_dev ? 1 : 0
  provider    = aws.mac
  name        = "/mac-dev/vnc-password"
  description = "ec2-user account/VNC password for the temporary Mac dev box"
  type        = "SecureString"
  value       = random_password.mac_vnc[0].result
  tags        = { Name = "mac-dev-vnc", Temporary = "true" }
}

# Instance profile so the Mac can read its own password at boot.
resource "aws_iam_role" "mac_instance" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  name     = "mac-dev-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "mac_instance" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  name     = "read-vnc-password"
  role     = aws_iam_role.mac_instance[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.mac_vnc[0].arn
      },
      {
        # SecureString is encrypted with the account's default SSM KMS key.
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.us-west-2.amazonaws.com" }
        }
      }
    ]
  })
}

resource "aws_iam_instance_profile" "mac_instance" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  name     = "mac-dev-instance"
  role     = aws_iam_role.mac_instance[0].name
}

output "mac_dev_vnc_password_param" {
  description = "SSM parameter holding the Mac's ec2-user/VNC password"
  value       = var.enable_mac_dev ? aws_ssm_parameter.mac_vnc[0].name : null
}
