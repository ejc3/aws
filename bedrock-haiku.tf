# bedrock-haiku.tf
#
# Claude Haiku 5.5 on Amazon Bedrock, and no other model, for two callers:
#
#   - nextjs-dev-role: every account on the kids' box. Until 2026-10-10 that box had no Bedrock at all
#     (nextjs-dev.tf). The owner, 2026-10-10: "I am fine if this box has haiku access. All boxes can be."
#     The grant is Haiku 5.5, not every Anthropic model. The metal boxes already have every anthropic.*
#     model through dev-server-role (BedrockRuntimeInvoke in dev-instance-common.tf); this file leaves them alone.
#   - dolphin-labs-news-bedrock: the hourly news pass of dolphin-labs-hq/dolphin-labs
#     (.github/workflows/refresh-news.yml on main), through GitHub OIDC. No AWS key is stored in GitHub.
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

# Its own inline policy, so nextjs-dev.tf's least-privilege policy keeps its shape.
resource "aws_iam_role_policy" "nextjs_dev_bedrock_haiku" {
  name   = "invoke-claude-haiku-5-5"
  role   = aws_iam_role.nextjs_dev.id
  policy = jsonencode({ Version = "2012-10-17", Statement = local.bedrock_haiku_statements })
}

# TRUST: workflow refresh-news.yml, on the main branch of dolphin-labs-hq/dolphin-labs, and nothing else.
#   - sub is GitHub's immutable form (owner and repository ids as well as names), as imagine-deploy's in
#     imagine.tf: GitHub's API (repos/dolphin-labs-hq/dolphin-labs/actions/oidc/customization/sub) reported
#     use_immutable_subject true with this prefix on 2026-10-10, so a trust on the plain name would never
#     match, and a repository that later takes the name cannot meet it. ref main admits the schedule and a
#     manual dispatch on main; a pull request (refs/pull/N/merge) or another branch cannot assume it.
#   - job_workflow_ref narrows main to the one workflow file: another workflow on main gets nothing. IAM
#     has accepted this GitHub claim as a trust condition since 2026-05-12 (IAM user guide, document history).
#   - The job declares no GitHub environment; adding one changes sub and needs this trust changed with it.
resource "aws_iam_role" "dolphin_labs_news_bedrock" {
  name        = "dolphin-labs-news-bedrock"
  description = "Assumed by dolphin-labs-hq/dolphin-labs refresh-news.yml (main) via GitHub OIDC: invoke Claude Haiku 5.5 on Bedrock"
  # A pass is capped at 10 minutes; one hour is the minimum and plenty.
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud"              = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub"              = "repo:dolphin-labs-hq@316189183/dolphin-labs@1330391842:ref:refs/heads/main"
          "token.actions.githubusercontent.com:job_workflow_ref" = "dolphin-labs-hq/dolphin-labs/.github/workflows/refresh-news.yml@refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "dolphin-labs-news-bedrock", Project = "dolphin-labs" }
}

resource "aws_iam_role_policy" "dolphin_labs_news_bedrock" {
  name   = "invoke-claude-haiku-5-5"
  role   = aws_iam_role.dolphin_labs_news_bedrock.id
  policy = jsonencode({ Version = "2012-10-17", Statement = local.bedrock_haiku_statements })
}

output "dolphin_labs_news_bedrock" {
  description = "Actions variables for dolphin-labs-hq/dolphin-labs's news workflow (refresh-news.yml)"
  value = {
    AWS_REGION           = local.bedrock_haiku_region
    AWS_BEDROCK_ROLE_ARN = aws_iam_role.dolphin_labs_news_bedrock.arn
    BEDROCK_MODEL_ID     = local.bedrock_haiku_profile
  }
}
