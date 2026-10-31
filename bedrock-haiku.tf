# bedrock-haiku.tf
#
# Claude Haiku 5.5 on Amazon Bedrock, and no other model, for:
#
#   - every box's instance role that had no Bedrock. The owner, 2026-10-10: "I am fine if this box has
#     haiku access. All boxes can be." That is nextjs-dev-role (the kids' box, no Bedrock until then:
#     nextjs-dev.tf), dev-ebs-only-role (the I/O box and the parallel boxes), wbox-role, each member wbox's role
#     (wbox-<person>-role, member-wbox.tf), claude-master-server-role and, when the Mac is on, mac-dev-instance. The grant is Haiku 5.5, not every
#     Anthropic model. The metal boxes already have every anthropic.* model through dev-server-role
#     (BedrockRuntimeInvoke in dev-instance-common.tf); this file leaves them alone. Not here: the GPU boxes
#     run with no instance profile at all (gpu-box.tf), the jumpboxes are administrators, and runners and
#     the AMI builder are CI machines, not boxes.
#   - the fcvm fleet coordinator's task role, in the fleet account, for the viewer word check. Its grant is in
#     fcvm-fleet.tf (CheckViewerWordsWithClaudeHaiku55) with that account's id, using the model locals below.
#
# The model, from AWS's model card (docs.aws.amazon.com/bedrock/latest/userguide/
# model-card-anthropic-claude-haiku-5-5.html, checked 2026-10-10; launched on Bedrock 2026-10-07):
#   - Model id anthropic.claude-haiku-5-5, AWS Marketplace product prod-6cyn7tgqazjhu.
#   - bedrock-runtime only. It has no in-Region endpoint: it is invoked through the US geographic
#     inference profile us.anthropic.claude-haiku-5-5, which from us-west-2 routes to us-east-1, us-east-2
#     and us-west-2. A routed request needs the profile ARN AND the foundation-model ARN in the region it
#     lands in, so the foundation-model ARN carries a region wildcard (as BedrockRuntimeInvoke's does): AWS
#     may add destinations, and the model id still limits it to Haiku 5.5.
#   - Not on bedrock-mantle outside GovCloud, so there is no bedrock-mantle grant. The Anthropic Messages
#     body goes through InvokeModel instead (the Anthropic SDK's Bedrock client does exactly that), and
#     Converse/ConverseStream are authorized by the same two actions.
#   - us-west-2, as opencode's DeepSeek (dev-user-data.tf): callers set the region explicitly.

locals {
  # GitHub's immutable subject for main of dolphin-labs-hq/dolphin-labs (owner and repository ids), for the role
  # its TestFlight workflow assumes (dolphin-labs-testflight.tf).
  dolphin_labs_main_sub = "repo:dolphin-labs-hq@316189183/dolphin-labs@1330391842:ref:refs/heads/main"

  bedrock_haiku_region  = "us-west-2"
  bedrock_haiku_model   = "anthropic.claude-haiku-5-5"
  bedrock_haiku_profile = "us.${local.bedrock_haiku_model}"

  bedrock_haiku_statements = [
    {
      Sid    = "InvokeClaudeHaiku55"
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      Resource = [
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/${local.bedrock_haiku_profile}",
        "arn:aws:bedrock:*::foundation-model/${local.bedrock_haiku_model}",
      ]
    },
    {
      # The first invocation of a Marketplace model in the account subscribes the account, with the
      # caller's permissions; after that no caller needs these. Subscribe is limited to Haiku 5.5's
      # product with aws-marketplace:ProductId, the key AWS documents for exactly this. ViewSubscriptions
      # takes no product condition. No Unsubscribe.
      Sid       = "SubscribeClaudeHaiku55OnFirstUse"
      Effect    = "Allow"
      Action    = "aws-marketplace:Subscribe"
      Resource  = "*"
      Condition = { "ForAnyValue:StringEquals" = { "aws-marketplace:ProductId" = ["prod-6cyn7tgqazjhu"] } }
    },
    {
      Sid      = "ViewMarketplaceSubscriptions"
      Effect   = "Allow"
      Action   = "aws-marketplace:ViewSubscriptions"
      Resource = "*"
    },
  ]
}

# One inline policy per role, so each role's own least-privilege policy keeps its shape. A new box role
# joins by adding it here.
locals {
  bedrock_haiku_box_roles = merge(
    {
      nextjs-dev   = aws_iam_role.nextjs_dev.id
      dev-ebs-only = aws_iam_role.dev_ebs_only.id
      wbox         = aws_iam_role.wbox.id
    },
    { for p, r in aws_iam_role.member_wbox : "member-wbox-${p}" => r.id },
    var.enable_claw_master_server ? { claude-master-server = aws_iam_role.claw_master_server[0].id } : {},
  )
}

resource "aws_iam_role_policy" "box_bedrock_haiku" {
  for_each = local.bedrock_haiku_box_roles
  name     = "invoke-claude-haiku-5-5"
  role     = each.value
  policy   = jsonencode({ Version = "2012-10-17", Statement = local.bedrock_haiku_statements })
}

