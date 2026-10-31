# The build satellite has no personal/device credentials or secret-fetch bootstrap.
# Its only AWS capability is registering its existing SSM agent and opening its own
# Session Manager channels. It cannot read parameters, secrets, state, or assume roles.
resource "aws_iam_role" "mac_instance" {
  provider = aws.west2
  name     = "macpbox-instance"
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
  provider = aws.west2
  name     = "ssm-agent-channels"
  role     = aws_iam_role.mac_instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel"
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_instance_profile" "mac_instance" {
  provider = aws.west2
  name     = "macpbox-instance"
  role     = aws_iam_role.mac_instance.name
}
