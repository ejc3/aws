# nextjs-dev-fcvm-parity.tf
#
# The owner's nextjs-ct container (ejc3) can do what fcvm-metal-arm can. Owner, 2026-10-22: "make sure ejc3 on nextjs
# has same ability as fcvm". The owner's container session is the whole of nextjs-dev-role (nextjs-ct-user-data.tf:
# only the host-only denials narrow it), so the grants go on that role, in ONE managed policy: its inline policies are
# near IAM's 10,240-character total and it has 19 of its 20 managed slots after this one.
#
# Every member container is denied all of it again (nextjs_ct_deny_member, nextjs-ct-user-data.tf). Members are root
# in their own container only; their AWS credentials come from nextjs-ct-imds with their session policy, so a grant on
# the role is no longer box-wide the way it was on the shared nextjs-dev box, where every account had sudo on the host.
#
# What fcvm has and this does not carry over, on purpose:
#   - the GitHub runners (SSM SendCommand to them, start/stop/terminate, their SSH key, github-pat-ejc3): fleet
#     operation, not development; the container has its own GitHub login;
#   - the browser-manager tunnel's connector token and the Windows PC's claw-master token: each runs a connector on
#     one named host (two connectors on one token split traffic), and the PC's is pinned to fcvm-arm's instance;
#   - the IPv6 prefix on fcvm's own network interfaces, and the dev-scripts bucket (the host-only denials keep the
#     containers out of it).

locals {
  # Secrets fcvm reads that the owner's container reads too. Each secret's own resource policy lists nextjs-dev-role as
  # a reader as well (dev-ai-services.tf, dolphin-films.tf, dolphin-relay.tf, dolphin-maps-loop.tf,
  # claw-master-dashboard-site.tf).
  nextjs_dev_parity_secrets = concat(
    [
      aws_secretsmanager_secret.turso_api_token.arn,
      aws_secretsmanager_secret.claw_master_backup_key.arn,
      aws_secretsmanager_secret.dolphin_relay_signup.arn,
      aws_secretsmanager_secret.dolphin_maps_pems.arn,
      aws_secretsmanager_secret.claw_master_dashboard_agent_access.arn,
      aws_secretsmanager_secret.abliteration_api_key.arn,
    ],
    [for k, s in aws_secretsmanager_secret.dolphin_films : s.arn if startswith(k, "nonprod/")],
  )

  # What a member container may still invoke on Bedrock: Haiku 5.5 (bedrock-haiku.tf) and opencode's DeepSeek
  # (dev-ai-services.tf). Everything else on Bedrock is denied to members by NotResource.
  nextjs_ct_member_bedrock = [
    "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/${local.bedrock_haiku_profile}",
    "arn:aws:bedrock:*::foundation-model/${local.bedrock_haiku_model}",
    "arn:aws:bedrock:*::foundation-model/deepseek.v3.2",
    "arn:aws:bedrock:*::foundation-model/deepseek.r1-v1:0",
    "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.deepseek.*",
  ]
}

resource "aws_iam_policy" "nextjs_dev_fcvm_parity" {
  name        = "nextjs-dev-fcvm-parity"
  description = "nextjs-dev-role: what dev-server-role (fcvm) has, for the owner's container; denied to members (nextjs-dev-fcvm-parity.tf)"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # dev-server-role's BedrockRuntimeInvoke: every Claude model, through the account's inference profiles.
        Sid      = "BedrockClaude"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
        Resource = ["arn:aws:bedrock:*::foundation-model/anthropic.*", "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"]
      },
      {
        # The Mantle endpoint Claude Code uses on the metal boxes (dev-server-role's BedrockMantleInference).
        Sid      = "BedrockMantle"
        Effect   = "Allow"
        Action   = ["bedrock-mantle:CreateInference", "bedrock-mantle:CountTokens", "bedrock-mantle:Get*", "bedrock-mantle:List*"]
        Resource = "arn:aws:bedrock-mantle:*:${data.aws_caller_identity.current.account_id}:project/*"
      },
      {
        # First-use model subscription, as dev-server-role's BedrockMarketplace (see the reason there).
        Sid      = "BedrockMarketplace"
        Effect   = "Allow"
        Action   = ["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"]
        Resource = "*"
      },
      {
        Sid      = "ReadTheMetalBoxSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.nextjs_dev_parity_secrets
      },
      {
        # dolphin-maps' PeMS login starts on a dev box, which stores it and rotates it (dolphin-maps-loop.tf).
        Sid      = "PutThePemsLogin"
        Effect   = "Allow"
        Action   = ["secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
        Resource = aws_secretsmanager_secret.dolphin_maps_pems.arn
      },
      {
        # claw-master's log group (claw-master-server.tf's dev_server_claw_master_logs_read).
        Sid    = "ReadTheClawMasterLogs"
        Effect = "Allow"
        Action = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams", "logs:StartQuery", "logs:StopQuery"]
        Resource = [
          aws_cloudwatch_log_group.claw_master_server.arn,
          "${trimsuffix(aws_cloudwatch_log_group.claw_master_server.arn, ":*")}:*",
        ]
      },
      {
        # Logs Insights results are fetched by query id; the action has no resource to scope to.
        Sid      = "ReadInsightsResults"
        Effect   = "Allow"
        Action   = "logs:GetQueryResults"
        Resource = "*"
      },
      {
        # The dolphin-maps loop's journal, to replay a pass (dolphin-maps-loop.tf).
        Sid      = "ReadDolphinMapsJournals"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.dolphin_maps_loop.arn}/journal/*"
      },
      {
        Sid       = "ListDolphinMapsJournals"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = aws_s3_bucket.dolphin_maps_loop.arn
        Condition = { StringLike = { "s3:prefix" = ["journal/*"] } }
      },
      {
        # Apply ejc3/constellation (constellation-account.tf): its apply role trusts nextjs-dev-role too.
        Sid      = "ConstellationApply"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = aws_iam_role.constellation_terraform.arn
      },
      {
        Sid      = "ConstellationPointer"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.constellation.arn
      },
      {
        # Apply ejc3/astryx-infra (astryx-account.tf): its apply role trusts nextjs-dev-role too.
        Sid      = "AstryxApply"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = aws_iam_role.astryx_terraform.arn
      },
      {
        Sid      = "AstryxPointer"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.astryx.arn
      },
      {
        # EC2 service quotas and their request history (dev-instance-common.tf's dev-service-quotas-read).
        Sid      = "ReadEc2Quotas"
        Effect   = "Allow"
        Action   = ["servicequotas:GetServiceQuota", "servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota"]
        Resource = "arn:aws:servicequotas:*:${data.aws_caller_identity.current.account_id}:ec2/*"
      },
      {
        Sid      = "ListQuotas"
        Effect   = "Allow"
        Action   = ["servicequotas:ListServiceQuotas", "servicequotas:GetAWSDefaultServiceQuota"]
        Resource = "*"
      },
      {
        # dev-server-role's EC2ReadOnly: the fleet, and the spot market before a launch.
        Sid    = "Ec2Read"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeImages", "ec2:DescribeSecurityGroups",
          "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeTags",
          "ec2:DescribeKeyPairs", "ec2:DescribeNetworkInterfaces", "ec2:DescribeNatGateways", "ec2:DescribeAddresses",
          "ec2:DescribeRouteTables", "ec2:DescribeSpotPriceHistory", "ec2:GetSpotPlacementScores",
          "ec2:DescribeSpotInstanceRequests", "ec2:DescribeInstanceTypes", "ec2:DescribeInstanceTypeOfferings",
          "ec2:DescribeAvailabilityZones",
        ]
        Resource = "*"
      },
      {
        Sid      = "SendEmail"
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "*"
      },
      {
        Sid    = "CodeArtifact"
        Effect = "Allow"
        Action = [
          "codeartifact:GetAuthorizationToken", "codeartifact:GetRepositoryEndpoint", "codeartifact:ReadFromRepository",
          "codeartifact:PublishPackageVersion", "codeartifact:PutPackageMetadata", "codeartifact:DescribePackageVersion",
          "codeartifact:ListPackageVersions", "codeartifact:ListPackages", "codeartifact:DeletePackageVersions",
        ]
        Resource = "*"
      },
      {
        Sid       = "CodeArtifactToken"
        Effect    = "Allow"
        Action    = "sts:GetServiceBearerToken"
        Resource  = "*"
        Condition = { StringEquals = { "sts:AWSServiceName" = "codeartifact.amazonaws.com" } }
      },
    ]
  })
  tags = { Name = "nextjs-dev-fcvm-parity", Managed = "terraform" }
}

# Attached only while nextjs-ct serves (nextjs-ct.tf's one switch). The rollback starts the retired nextjs-dev, where
# every account has sudo and so holds the role's own credentials, with no session policy to narrow them: there the
# role goes back to what it had before this file.
resource "aws_iam_role_policy_attachment" "nextjs_dev_fcvm_parity" {
  count      = local.nextjs_ct_serving ? 1 : 0
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.nextjs_dev_fcvm_parity.arn
}
