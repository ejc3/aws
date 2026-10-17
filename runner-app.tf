# runner-app.tf
#
# Self-hosted runners for repos other than ejc3/fcvm (step 2; step 1 is runner-repos.tf, the
# per-repo controller tokens). ejc3/fcvm keeps its own metal controller in runner-autoscale.tf,
# untouched. These repos get ephemeral x86 SPOT VMs from a separate, much smaller controller:
#
#   GitHub workflow_job --> front (runner-webhook-front.tf, routes by repository.full_name)
#                              |-- ejc3/fcvm --> github-runner-webhook (metal, unchanged)
#                              '-- served repo --> github-app-runner (this file)
#   EventBridge, every 2 min --> github-app-runner reconcile
#
# The policy lives in runner-app/app_runner.py: one runner per queued job, no reuse, reap what
# never registered, sat idle or outlived any job. A job opts in with
#
#   runs-on: [self-hosted, <repo label>, <size>]      # e.g. [self-hosted, cc-games, xl]
#
# and nothing else; a job asking for any other label is never served here.
#
# SIZES. Diversified x86 compute pools, tried in order, each subnet in turn. x86 because every
# one of these pools scored 9/10 for spot placement in us-west-1 against 3/10 for arm64
# (2026-09-27), and the repos' toolchains (Playwright webkit, semgrep, SwiftShader) are known
# good there. c7a first: its vCPUs are whole cores, and this work is CPU-bound.
locals {
  runner_app_sizes = {
    xl = ["c7a.16xlarge", "c7i.16xlarge", "c8i.16xlarge", "c6a.16xlarge", "c6i.16xlarge"]
    l  = ["c7a.8xlarge", "c7i.8xlarge", "c8i.8xlarge", "c6a.8xlarge", "c6i.8xlarge"]
    s  = ["c7a.2xlarge", "c7i.2xlarge", "c8i.2xlarge", "c6a.2xlarge", "c6i.2xlarge"]
  }

  # vCPUs of one host of each size. Every type listed under a size has this many: these are all
  # 4 vCPUs per "xlarge", which the controller's precondition below checks by each type's name.
  # The one map the controller counts vCPUs by (each repo's `vcpus`, and each host's Vcpus tag).
  runner_app_size_vcpus = { xl = 64, l = 32, s = 8 }

  # One entry per served repo. `max` bounds that repo's concurrent hosts; it is separate from
  # fcvm's four per architecture, so these repos can never take fcvm's metal.
  #
  # `max_vcpus` (optional) bounds the vCPUs of a repo's concurrent hosts as well: a launch waits
  # when the vCPUs in use (live hosts, and launches not listed yet, each by its own size) plus
  # those of the size the job asks for would pass it. `max` stays as a host-count backstop. A repo
  # without `max_vcpus` is capped by hosts alone. No repo sets it now: the budgeted repos' vCPU caps come from their
  # group (burst_max_vcpus per repo, throttled_vcpus for the whole group), and are counted the same way.
  #
  # dolphin-films (a downstream of dolphin-labs, dolphin-films.tf) asks for the same `dolphin`
  # label as its upstream. Runners register per repo and hosts are counted by their Repo tag,
  # so sharing the label shares no runner, host or cap: its caps (throttled 2) are its own and it can never take one of
  # dolphin-labs' hosts. What the dolphin repos do share is one budget bucket (runner_budget_groups). `alarm` names its
  # per-repo alarm, which would otherwise collide with dolphin-labs' (both are named after the label). dolphin-maps
  # (another dolphin-labs-hq project) is the same shape: the `dolphin` label, its own caps, its own alarm; so is
  # dolphin-family.
  #
  # ejc3/imagine has the owner of ejc3/fcvm, so nothing here may rest on the owner: its runners
  # register to https://github.com/ejc3/imagine only (a repository-level runner is never given
  # another repository's job), serve only jobs whose every label is in its own set, and its
  # deliveries are routed here by repository.full_name while fcvm's still go to the metal webhook.
  #
  # `image` (optional) names a prebuilt image from runner-app-image.tf, built for that same repo. The repo's hosts then
  # boot the newest available build of it that this account's pipeline made and tagged, and Canonical's Ubuntu 24.04,
  # with a log line saying so, when there is none. A repo without `image` boots Canonical's image, as before.
  #
  # A repo in a budget group (runner_budget_groups, below) also sets `throttled_max`: its host cap while its group is
  # throttled, when the group's repos together are also held to the group's throttled_vcpus. While the group is in
  # burst it gets the group's burst ceilings instead. `max` (and `max_vcpus`, where set) stay a hard backstop in both modes, and setting `max = 0`
  # still stops a repo outright. For these repos `max` is the burst host ceiling, 64.
  runner_app_repos = {
    "CoderColton/colton-games"      = { label = "cc-games", max = 8, image = "colton-games-runner" }
    "dolphin-labs-hq/dolphin-labs"  = { label = "dolphin", max = 64, throttled_max = 8, image = "dolphin-labs-runner" }
    "dolphin-labs-hq/dolphin-films" = { label = "dolphin", max = 64, throttled_max = 2, image = "dolphin-films-runner", alarm = "dolphin-films" }
    "dolphin-labs-hq/dolphin-maps"  = { label = "dolphin", max = 64, throttled_max = 2, image = "dolphin-maps-runner", alarm = "dolphin-maps" }
    "ejc3/imagine"                  = { label = "imagine", max = 64, throttled_max = 8, image = "imagine-runner" }

    # dolphin-family: the same shape as dolphin-maps, behind its own token gate (below).
    "dolphin-labs-hq/dolphin-family" = { label = "dolphin", max = 64, throttled_max = 2, alarm = "dolphin-family" }
  }

  # BUDGET. Each group is a leaky bucket of dollars (runner-app/app_runner.py, "budget buckets"; GITHUB-RUNNERS.md,
  # "Budget buckets"). It refills at daily_usd a day, continuously (daily_usd / 86400 a second), and holds at most
  # capacity_days of it, so a quiet stretch banks room for a busy day. It drains by what the group's live hosts cost at
  # runner_cost. While the bucket is healthy the group's repos burst to burst_max hosts and burst_max_vcpus vCPUs each;
  # when it would clip, they fall back to their throttled caps: each repo its throttled_max hosts, and the group's repos
  # together throttled_vcpus vCPUs (below), so throttled spend is at most the refill and the bucket cannot drain further.
  #
  #   throttle  when the level is under reserve_fraction of capacity, or would reach zero within lookahead_hours at the
  #             current burn (counting the host about to be launched);
  #   resume    only once the level is back to resume_fraction of capacity and the projection is clear (hysteresis).
  #
  # Throttling never terminates a host; it only stops launches past the throttled caps, and the jobs wait in the queue as
  # at any cap. To change a budget, change daily_usd here (or a default below) and apply; the bucket keeps its level,
  # and only the refill rate and the capacity change.
  #
  # The dolphin repos share one bucket: they share the `dolphin` label and one owner. dolphin-family draws on it once its
  # token gate is on (a repo outside runner_app_config is not served and is not counted). colton-games is in no group.
  runner_budget_defaults = {
    capacity_days   = 2   # the bucket holds two days' budget ($100 at $50/day)
    burst_max       = 64  # hosts per repo in burst: the most hosts 512 vCPUs can be (64 `s`)
    burst_max_vcpus = 512 # vCPUs per repo in burst
    # Throttle when the bucket would empty within this long at the current burn. One repo at its full burst (512 vCPUs
    # of `s`, $14.85/h) loses $12.77/h net of the refill, so a 1-hour lookahead would never fire before the $15
    # reserve; 2 hours throttles it at about $25.50.
    lookahead_hours       = 2
    reserve_fraction      = 0.15 # throttle also when the level is under this fraction of capacity ($15)
    resume_fraction       = 0.30 # back to burst only at this fraction ($30), so the mode does not flap
    throttled_alarm_hours = 8    # alarm when a group was throttled in more than this many of the last 24 hours
  }
  runner_budget_groups = {
    imagine = { daily_usd = 50, repos = ["ejc3/imagine"] }
    dolphin = { daily_usd = 50, repos = ["dolphin-labs-hq/dolphin-labs", "dolphin-labs-hq/dolphin-films", "dolphin-labs-hq/dolphin-maps", "dolphin-labs-hq/dolphin-family"] }
  }
  # throttled_vcpus: the vCPUs the group's repos may hold together while throttled. The refill rate (daily_usd / 24 an
  # hour), less the overhead of every host the throttled caps allow (the sum of the group's throttled_max, each at
  # host_hour_usd), at vcpu_hour_usd, rounded down: at $50 a day, imagine 72 vCPUs (8 hosts) and dolphin 68 (14 hosts).
  # Held there, the group spends no more than it refills. A group holding nothing may still launch one host of any size
  # (app_runner.py ensure_runner), so a job larger than this cap never waits forever; one `xl` host alone ($1.74/h) is
  # still under the refill ($2.08/h).
  runner_budget = { for name, g in local.runner_budget_groups : name => merge(local.runner_budget_defaults, g, {
    throttled_vcpus = max(0, floor((g.daily_usd / 24 - sum(concat([0], [for r in g.repos : try(local.runner_app_repos[r].throttled_max, 0)])) * local.runner_cost.host_hour_usd) / local.runner_cost.vcpu_hour_usd))
  }) }
  # repo => its group's name.
  runner_app_budget_group = merge([for name, g in local.runner_budget_groups : { for repo in g.repos : repo => name }]...)

  # COST. Constants, never the pricing APIs at run time, each set slightly high on purpose: the bucket should throttle a
  # little early rather than late.
  runner_cost = {
    # Spot, us-west-1, every type in runner_app_sizes on 2026-10-17 (describe-spot-price-history, both AZs):
    # s (2xlarge) $0.175-0.205/h = $0.0218-0.0257 per vCPU-hour; l (8xlarge) $0.605-0.664/h = $0.0189-0.0208;
    # xl (16xlarge) $0.986-1.351/h = $0.0154-0.0211. 0.027 is above the dearest (c7a.2xlarge, $0.0257).
    vcpu_hour_usd = 0.027
    # Per host-hour, whatever its size: its public IPv4 address, $0.005/h (AWS's charge for every public IPv4 address
    # since 2024-02-01), plus its 80 GB gp3 root volume at $0.096/GB-month in us-west-1 (80 x 0.096 / 730 h = $0.0105/h).
    # $0.0155/h, rounded up.
    host_hour_usd = 0.016
  }

  # A repo whose token gate is false is left out of the controller's REPOS as well: with no token its every reconcile
  # reports "skipped: no controller token", which withholds the aggregate Repo=ALL metric for every repo and fires
  # github-app-runner-reconcile-silent.
  runner_app_config = {
    for repo, c in local.runner_app_repos : repo => {
      label      = c.label
      max        = c.max
      max_vcpus  = try(c.max_vcpus, null) # null: no vCPU cap, hosts alone
      image      = try(c.image, "")       # "": Canonical's image
      pat_secret = "github-runner/repo-pat/${repo}"
      sizes      = local.runner_app_sizes
      vcpus      = local.runner_app_size_vcpus
      # Every entry carries app_repo so the map stays one type; empty means "no app, use the PAT".
      app_repo = contains(local.runner_app_github_app_repos, repo) ? repo : ""

      # "": in no budget group, capped by max and max_vcpus alone (colton-games).
      budget_group  = lookup(local.runner_app_budget_group, repo, "")
      throttled_max = try(c.throttled_max, c.max)
      # The most this repo may hold in any mode, which its too-many alarms watch: its burst ceilings within its hard
      # backstop for a budgeted repo, else its caps.
      peak_max = contains(keys(local.runner_app_budget_group), repo) ? min(c.max, local.runner_budget[local.runner_app_budget_group[repo]].burst_max) : c.max
      peak_max_vcpus = (contains(keys(local.runner_app_budget_group), repo)
        ? (try(c.max_vcpus, null) == null ? local.runner_budget[local.runner_app_budget_group[repo]].burst_max_vcpus : min(c.max_vcpus, local.runner_budget[local.runner_app_budget_group[repo]].burst_max_vcpus))
      : try(c.max_vcpus, null))
    } if lookup(local.runner_app_token_gates, repo, true)
  }

  # Repos whose controller token comes from the runners GitHub App (github-app/runners, below) rather than a year-long PAT:
  # the app is installed on these. A repo outside an owner the app can be installed on (CoderColton's colton-games: the app
  # would need Colton's own account) keeps its PAT. The PAT stays the fallback until the app is proven in production.
  # ejc3/imagine takes the app's token once the app is installed on ejc3 (selected repositories: imagine only) and that
  # installation is in github-app/runners; until then app_token() finds no ejc3 installation and its PAT is used.
  # dolphin-family is not here: it uses its own controller token until the runners app's installation is widened to it.
  runner_app_github_app_repos = ["dolphin-labs-hq/dolphin-labs", "dolphin-labs-hq/dolphin-films", "dolphin-labs-hq/dolphin-maps", "ejc3/imagine"]
  # Only the repos actually served (runner_app_config is gated): a repo whose token gate is still false adds no hosts, so
  # counting its cap would loosen the combined alarm before it serves anything. Each repo counts the most hosts it may
  # hold in any mode (peak_max): a budgeted repo's burst ceiling of 64, which is exactly the most hosts its 512 vCPUs can
  # be (64 `s`), so no lower host count is safe.
  runner_app_max_total = sum([for c in values(local.runner_app_config) : c.peak_max])

  # The repos with a vCPU ceiling in some mode (every budgeted repo, and any with max_vcpus): each gets a vCPU alarm at
  # its peak (too_many_app_runner_vcpus_per_repo).
  runner_app_vcpu_capped = { for repo, c in local.runner_app_config : repo => c if c.peak_max_vcpus != null }

  # The groups with at least one served repo: each gets a bucket, its metrics and its alarms.
  runner_budget_served = { for name, g in local.runner_budget : name => g if length([for r in g.repos : r if contains(keys(local.runner_app_config), r)]) > 0 }

  # Canonical's own account. Its images, and this account's own prebuilt images tagged by runner-app-image.tf's
  # pipeline, are the only ones this controller may launch.
  runner_app_ami_owner = "099720109477"
}

# ============================================ network
# No inbound at all: a runner only dials out (GitHub, package mirrors). Nothing may SSH in,
# not even another runner; operators use SSM Session Manager.
resource "aws_security_group" "runner_app" {
  count       = var.enable_github_runner ? 1 : 0
  name        = "github-app-runner-sg"
  description = "Ephemeral app-repo runners: no inbound, outbound internet"
  vpc_id      = aws_vpc.runner[0].id

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "Internet access"
  }

  tags = { Name = "github-app-runner-sg" }
}

# ============================================ controller
data "archive_file" "runner_app" {
  type        = "zip"
  output_path = "${path.module}/.terraform/github-app-runner.zip"

  source {
    content  = file("${path.module}/runner-app/app_runner.py")
    filename = "app_runner.py"
  }
  source {
    content  = file("${path.module}/runner-app/bootstrap.sh")
    filename = "bootstrap.sh"
  }
}

resource "aws_iam_role" "runner_app_lambda" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# The job host's own role. Jobs here run other people's code (every writer on the served repos,
# and their dependencies), and anything on the host can use this role through IMDS. So it can do
# exactly one thing: read and delete ITS OWN bootstrap credential, the parameter the controller
# tagged with this instance's ARN. Not fcvm's github-runner-instance-role, which also carries SSM
# agent connectivity, account-wide DescribeNetworkInterfaces, the session-audit bucket write and a
# row in fcvm's registration table, and would pass on any grant later added for fcvm's runners.
# The host needs nothing else: IPv6 is assigned at launch, and there is no SSM agent use.
resource "aws_iam_role" "runner_app_instance" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner-instance-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = { Name = "github-app-runner-instance-role" }
}

resource "aws_iam_role_policy" "runner_app_instance" {
  count = var.enable_github_runner ? 1 : 0
  name  = "consume-own-bootstrap-credential-only"
  role  = aws_iam_role.runner_app_instance[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeOwnBootstrapCredential"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
        Condition = {
          StringEquals = { "ssm:resourceTag/InstanceArn" = "$${ec2:SourceInstanceARN}" }
          Null         = { "ec2:SourceInstanceARN" = "false" }
        }
      },
      {
        # Another host's still-booting credential stays unreadable even if a broad SSM Allow
        # is ever attached here by mistake.
        Sid      = "DenyOtherBootstrapCredentials"
        Effect   = "Deny"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory", "ssm:GetParametersByPath", "ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
        Condition = {
          StringNotEqualsIfExists = { "ssm:resourceTag/InstanceArn" = "$${ec2:SourceInstanceARN}" }
        }
      },
      {
        Sid         = "DenyEveryOtherParameterPayload"
        Effect      = "Deny"
        Action      = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory", "ssm:GetParametersByPath"]
        NotResource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
      },
      {
        Sid      = "DenyBulkAndHistoricalParameterPayloads"
        Effect   = "Deny"
        Action   = ["ssm:GetParameters", "ssm:GetParameterHistory", "ssm:GetParametersByPath"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "runner_app" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner-profile"
  role  = aws_iam_role.runner_app_instance[0].name
}

# Launch, tag and terminate only github-app-runner instances, only from Canonical's images or this
# account's own images tagged Purpose=github-app-runner-image (runner-app-image.tf), only into the
# runner subnets with github-app-runner-sg and github-app-runner-profile (whose
# role can consume its own bootstrap credential and nothing else). Broker a host's own
# bootstrap credential exactly as fcvm's controller does. Read only the served repos'
# controller tokens.
resource "aws_iam_role_policy" "runner_app_lambda" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner"
  role  = aws_iam_role.runner_app_lambda[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OwnLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/github-app-runner",
          "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/github-app-runner:*",
        ]
      },
      {
        Sid      = "ReadServedRepoTokens"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = concat([for repo in local.runner_extra_repos : aws_secretsmanager_secret.github_runner_repo_pat[repo].arn], [aws_secretsmanager_secret.github_app_runners[0].arn])
      },
      {
        Sid      = "ResolveUbuntuImage"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:us-west-1::parameter/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
      },
      {
        Sid      = "DescribeInstances"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "ec2:DescribeImages"]
        Resource = "*"
      },
      {
        Sid      = "LaunchCanonicalImagesOnly"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1::image/*"
        # Canonical's public images report ImageOwnerAlias "amazon", and IAM evaluates
        # ec2:Owner as that alias, not Canonical's account id; with the id alone every launch
        # was refused on the image. The controller resolves the AMI from Canonical's SSM
        # parameter, so this only bounds it to AWS-published images.
        Condition = {
          StringEquals = { "ec2:Owner" = [local.runner_app_ami_owner, "amazon"] }
        }
      },
      {
        # The owner widened the Canonical-only rule for this alone (GITHUB-RUNNERS.md, "Prebuilt images"): an image this
        # account owns AND that carries the tag only runner-app-image.tf's pipeline puts on at CreateImage. Not fcvm's
        # Purpose=github-runner images, and no image of any other account.
        Sid      = "LaunchPrebuiltRunnerImages"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1::image/*"
        Condition = {
          StringEquals = {
            "ec2:Owner"               = data.aws_caller_identity.current.account_id
            "aws:ResourceTag/Purpose" = local.runner_app_image_purpose
          }
        }
      },
      {
        Sid      = "LaunchIntoRunnerSubnetsWithAppGroup"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = concat(local.runner_launch_subnet_arns, [aws_security_group.runner_app[0].arn])
      },
      {
        Sid      = "LaunchTaggedAppInstance"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-app-runner", "ec2:MetadataHttpTokens" = "required" }
          ArnEquals    = { "ec2:InstanceProfile" = aws_iam_instance_profile.runner_app[0].arn }
        }
      },
      {
        Sid      = "LaunchTaggedAppENI"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:network-interface/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-app-runner" }
          ArnEquals    = { "ec2:Subnet" = local.runner_launch_subnet_arns }
        }
      },
      {
        # A spot launch through RunInstances also creates a spot-instances-request, which IAM
        # authorizes as its own resource; without this every launch is UnauthorizedOperation.
        # The instance statement above still decides WHAT may launch (tag, profile, IMDSv2).
        Sid      = "LaunchAppSpotRequest"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:spot-instances-request/*"
      },
      {
        Sid      = "LaunchEncryptedAppVolume"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:volume/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-app-runner" }
          Bool         = { "ec2:Encrypted" = "true" }
        }
      },
      {
        Sid      = "TagOnlyDuringAppLaunch"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = [for kind in ["instance", "volume", "network-interface"] : "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:${kind}/*"]
        Condition = {
          StringEquals                = { "ec2:CreateAction" = "RunInstances", "aws:RequestTag/Role" = "github-app-runner" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Name", "Role", "Repo", "Size", "Vcpus", "JobId", "InspectorEc2Exclusion"] }
        }
      },
      {
        Sid      = "DenyTagChangesOutsideLaunch"
        Effect   = "Deny"
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"]
        Resource = "*"
        Condition = {
          StringNotEqualsIfExists = { "ec2:CreateAction" = "RunInstances" }
        }
      },
      {
        Sid      = "TerminateAppRunnersOnly"
        Effect   = "Allow"
        Action   = "ec2:TerminateInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/Role" = "github-app-runner" }
        }
      },
      {
        Sid      = "PassRunnerRoleToEC2Only"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.runner_app_instance[0].arn
        Condition = {
          StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" }
        }
      },
      {
        Sid         = "DenyPassingOtherRoles"
        Effect      = "Deny"
        Action      = "iam:PassRole"
        NotResource = aws_iam_role.runner_app_instance[0].arn
      },
      {
        # The same instance-bound handoff fcvm uses (runner-bootstrap.tf): created only after
        # launch, tagged with that instance's ARN, which is the only one its role may read.
        Sid      = "BrokerInstanceBoundBootstrapCredential"
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:AddTagsToResource"]
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-runner", "aws:RequestTag/Fleet" = "github-app-runner" }
          StringLike   = { "aws:RequestTag/InstanceArn" = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/i-*" }
        }
      },
      {
        # One launch claim per job (app_runner.py claim()): conditional put, release on failure.
        Sid      = "LaunchClaims"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.runner_app_claims[0].arn
      },
      {
        # The budget buckets (app_runner.py update_bucket): a consistent read, then a write conditional on the version.
        Sid      = "BudgetBuckets"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.runner_app_budget[0].arn
      },
      {
        # Its own runner-count and vCPU metrics, which the alarms below read.
        Sid       = "PublishRunnerCounts"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "GitHubAppRunner" } }
      },
      {
        # Only this fleet's credentials: fcvm's share the path and the Role tag, not Fleet.
        Sid      = "DeleteBrokeredCredentialOnly"
        Effect   = "Allow"
        Action   = "ssm:DeleteParameter"
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/Role" = "github-runner", "aws:ResourceTag/Fleet" = "github-app-runner" }
        }
      },
    ]
  })
}

resource "aws_lambda_function" "runner_app" {
  count            = var.enable_github_runner ? 1 : 0
  filename         = data.archive_file.runner_app.output_path
  source_code_hash = data.archive_file.runner_app.output_base64sha256
  function_name    = "github-app-runner"
  role             = aws_iam_role.runner_app_lambda[0].arn
  handler          = "app_runner.handler"
  runtime          = "python3.12"
  timeout          = 120
  # The first reconcile used 104 of the default 128 MB (boto3 alone is most of it); a larger
  # listing would run out. 256 MB also doubles the CPU share, which shortens every round.
  memory_size = 256

  # One decision at a time, so two deliveries for the same job cannot both launch.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      REPOS             = jsonencode(local.runner_app_config)
      RUNNER_APP_SECRET = aws_secretsmanager_secret.github_app_runners[0].name
      CLAIMS_TABLE      = aws_dynamodb_table.runner_app_claims[0].name
      LAUNCH_SUBNETS    = jsonencode([for subnet in local.runner_launch_subnets : { subnet_id = subnet.id, availability_zone = subnet.availability_zone }])
      SECURITY_GROUP_ID = aws_security_group.runner_app[0].id
      INSTANCE_PROFILE  = aws_iam_instance_profile.runner_app[0].name
      RUNNER_ACCOUNT_ID = data.aws_caller_identity.current.account_id
      # A VM is registered in about two minutes; metal needed fifteen.
      BOOT_GRACE_MINUTES   = "10"
      IDLE_MINUTES         = "10"
      MAX_LIFETIME_MINUTES = "180"
      VOLUME_GB            = "80"

      # Every repo the runners app is meant to cover, token gates or not: what an installation may select.
      APP_INSTALLATION_REPOS = jsonencode(local.runner_app_github_app_repos)

      # The budget buckets (locals above): one item per group in BUDGET_TABLE.
      BUDGET_TABLE = aws_dynamodb_table.runner_app_budget[0].name
      BUDGET_GROUPS = jsonencode({ for name, g in local.runner_budget : name => {
        daily_usd       = g.daily_usd, capacity_days = g.capacity_days, burst_max = g.burst_max, burst_max_vcpus = g.burst_max_vcpus,
        lookahead_hours = g.lookahead_hours, reserve_fraction = g.reserve_fraction, resume_fraction = g.resume_fraction,
        throttled_vcpus = g.throttled_vcpus
      } })
      VCPU_HOUR_USD = tostring(local.runner_cost.vcpu_hour_usd)
      HOST_HOUR_USD = tostring(local.runner_cost.host_hour_usd)
    }
  }

  tags = { Name = "github-app-runner" }

  lifecycle {
    precondition {
      # The vCPU caps count a host by its size, so every type listed under a size must have that size's vCPUs. These
      # pools are all 4 vCPUs per "xlarge" (c6a/c6i/c7a/c7i/c8i): an N-xlarge type has 4N.
      condition = alltrue(flatten([
        for size, types in local.runner_app_sizes : [
          for t in types : try(4 * tonumber(regex("\\.([0-9]+)xlarge$", t)[0]), -1) == lookup(local.runner_app_size_vcpus, size, 0)
        ]
      ]))
      error_message = "Every instance type under a size in runner_app_sizes must have runner_app_size_vcpus[size] vCPUs."
    }
    precondition {
      # A repo may boot only an image built for it: the controller asks for the image's Repo tag to be the repo's own.
      condition     = alltrue([for repo, c in local.runner_app_config : c.image == "" || try(local.runner_app_images[c.image].repo, "") == repo])
      error_message = "A repo's `image` must name an entry of runner_app_images (runner-app-image.tf) whose repo is that repo."
    }
    precondition {
      # A repo draws on at most one bucket, and every repo a group names is one this file serves.
      condition     = length(local.runner_app_budget_group) == length(flatten([for g in values(local.runner_budget_groups) : g.repos])) && alltrue([for r in keys(local.runner_app_budget_group) : contains(keys(local.runner_app_repos), r)])
      error_message = "Every repo in runner_budget_groups must be in runner_app_repos, and in only one group."
    }
    precondition {
      # A budgeted repo's throttled caps sit inside its hard backstop, and the thresholds are ordered.
      condition = alltrue([for repo, c in local.runner_app_config : c.budget_group == "" || (c.throttled_max <= c.max && can(local.runner_app_repos[repo].throttled_max))]) && alltrue([
        for g in values(local.runner_budget) : g.daily_usd > 0 && g.capacity_days >= 1 && 0 < g.reserve_fraction && g.reserve_fraction < g.resume_fraction && g.resume_fraction <= 1 && g.lookahead_hours > 0
      ])
      error_message = "A budgeted repo needs throttled_max <= max; each group needs daily_usd > 0, capacity_days >= 1, lookahead_hours > 0 and 0 < reserve_fraction < resume_fraction <= 1."
    }
  }

  depends_on = [aws_iam_role_policy.runner_app_lambda]
}

# Deliveries arrive asynchronously from the front. No retry after an error: a retry after a
# launch could double it, and the two-minute reconcile covers anything dropped.
resource "aws_lambda_function_event_invoke_config" "runner_app" {
  count                        = var.enable_github_runner ? 1 : 0
  function_name                = aws_lambda_function.runner_app[0].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_cloudwatch_event_rule" "runner_app_reconcile" {
  count               = var.enable_github_runner ? 1 : 0
  name                = "github-app-runner-reconcile"
  description         = "Launch for uncovered queued jobs and reap idle or stuck app runners"
  schedule_expression = "rate(2 minutes)"
}

resource "aws_cloudwatch_event_target" "runner_app_reconcile" {
  count = var.enable_github_runner ? 1 : 0
  rule  = aws_cloudwatch_event_rule.runner_app_reconcile[0].name
  arn   = aws_lambda_function.runner_app[0].arn
  input = jsonencode({ reconcile = true })
}

resource "aws_lambda_permission" "runner_app_reconcile" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowEventBridgeReconcile"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_app[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.runner_app_reconcile[0].arn
}

# ============================================ alarms
# Separate from too-many-runners (which counts Role=github-runner, fcvm's metal only).
# One claim per job (app_runner.py claim()). Items expire by their own `expires_at`; TTL only
# garbage-collects them a day later.
resource "aws_dynamodb_table" "runner_app_claims" {
  count        = var.enable_github_runner ? 1 : 0
  name         = "github-app-runner-claims"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "repo"
  range_key    = "job"

  attribute {
    name = "repo"
    type = "S"
  }

  attribute {
    name = "job"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = { Name = "github-app-runner-claims" }
}

# One item per budget group (app_runner.py, "budget buckets"): {level_usd, updated_at, rate_usd_h, throttled, spend,
# version}. Nothing here is precious: a missing item starts full, so no point-in-time recovery.
resource "aws_dynamodb_table" "runner_app_budget" {
  count        = var.enable_github_runner ? 1 : 0
  name         = "github-app-runner-budget"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "group"

  attribute {
    name = "group"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = { Name = "github-app-runner-budget" }
}

# The controller publishes GitHubAppRunner/LiveRunners (Repo=ALL and per repo) and LiveVcpus (per
# repo) on every 2-minute reconcile. AWS/EC2 has no per-tag instance count, so this is the only
# reliable one. The combined cap is the sum of the most hosts each repo may hold in any mode
# (peak_max: a budgeted repo's burst ceiling, else its `max`), so above it the controller is over-launching whatever the
# sizes.
resource "aws_cloudwatch_metric_alarm" "too_many_app_runners" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "too-many-app-runners"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3 # 15 minutes
  threshold           = local.runner_app_max_total
  alarm_description   = "More app-repo runners than every served repo's cap allows, for 15+ minutes - the controller is over-launching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "LiveRunners"
  dimensions          = { Repo = "ALL" }
  statistic           = "Maximum"
  period              = 300
  treat_missing_data  = "notBreaching"
}

# No count at all for 15 minutes means the reconcile is not running, so nothing reaps hosts
# or enforces lifetimes. Missing data is the signal here.
resource "aws_cloudwatch_metric_alarm" "app_runner_reconcile_silent" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "github-app-runner-reconcile-silent"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 0
  alarm_description   = "github-app-runner published no runner count for 15 minutes: its reconcile (reaping, lifetimes) is not running"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "LiveRunners"
  dimensions          = { Repo = "ALL" }
  statistic           = "SampleCount"
  period              = 300
  treat_missing_data  = "breaching"
}

# Per repo: the combined alarm above fires only when EVERY repo is saturated together, so one
# repo over-launching on its own would go unseen. Each repo's LiveRunners above the most it may hold in any mode
# (peak_max: a budgeted repo's burst ceiling; a burst within it is the bucket's to allow, not an over-launch).
# Named after the repo's label, or its `alarm` where two repos share a label.
resource "aws_cloudwatch_metric_alarm" "too_many_app_runners_per_repo" {
  for_each            = var.enable_github_runner ? local.runner_app_config : {}
  alarm_name          = "too-many-app-runners-${try(local.runner_app_repos[each.key].alarm, each.value.label)}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3 # 15 minutes
  threshold           = each.value.peak_max
  alarm_description   = "More ${each.key} app runners than its cap of ${each.value.peak_max}, for 15+ minutes - the controller is over-launching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "LiveRunners"
  dimensions          = { Repo = each.key }
  statistic           = "Maximum"
  period              = 300
  treat_missing_data  = "notBreaching"
}

# Per repo with a vCPU ceiling: the host alarm above sees only the backstop, and a repo over-launching large hosts passes
# its vCPU ceiling long before its host count (9 `xl` hosts are 576 vCPUs). Each such repo's LiveVcpus above its peak
# (peak_max_vcpus: a budgeted repo's burst ceiling of 512, else its max_vcpus).
resource "aws_cloudwatch_metric_alarm" "too_many_app_runner_vcpus_per_repo" {
  for_each            = var.enable_github_runner ? local.runner_app_vcpu_capped : {}
  alarm_name          = "too-many-app-runner-vcpus-${try(local.runner_app_repos[each.key].alarm, each.value.label)}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3 # 15 minutes
  threshold           = each.value.peak_max_vcpus
  alarm_description   = "More ${each.key} app runner vCPUs than its cap of ${each.value.peak_max_vcpus}, for 15+ minutes - the controller is over-launching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "LiveVcpus"
  dimensions          = { Repo = each.key }
  statistic           = "Maximum"
  period              = 300
  treat_missing_data  = "notBreaching"
}

# ejc3/imagine's spend, measured apart from the bucket: LiveVcpus is the controller's own host count, not the bucket's
# arithmetic, so this is the backstop that catches the bucket draining too little. While bursting, the bucket lets a
# group spend at most its full capacity plus a day's refill in any day ($150: 232 vCPUs held all day at runner_cost),
# and the hosts still running when it throttles come on top (no host outlives 3 hours: 3 h of 512 vCPUs is 64 averaged
# over the day). Throttled, Imagine holds at most its group's 72 vCPUs. So a day in which every hour averages more than
# 232 + 64 = 296 vCPUs is more than the bucket can allow. Until the bucket this fired at 128 vCPUs in 6 of 24 hours,
# which bursting now reaches on an ordinary busy day; the bucket enforces the spend, and this is retuned to a backstop.
# Missing data is the reconcile-silent alarm's to report.
locals {
  imagine_runner_alarm_vcpus = (
    ceil(local.runner_budget.imagine.daily_usd * (1 + local.runner_budget.imagine.capacity_days) / 24 / local.runner_cost.vcpu_hour_usd)
    + ceil(local.runner_budget.imagine.burst_max_vcpus * 3 / 24)
  )
}

resource "aws_cloudwatch_metric_alarm" "imagine_runner_hours_daily" {
  count               = var.enable_github_runner && var.imagine_token_ready ? 1 : 0
  alarm_name          = "imagine-runner-hours-daily"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 24
  datapoints_to_alarm = 24
  threshold           = local.imagine_runner_alarm_vcpus
  alarm_description   = "ejc3/imagine kept more than ${local.imagine_runner_alarm_vcpus} app runner vCPUs up on average in every one of the last 24 hours: more than its budget bucket can allow in a day. The bucket's arithmetic or its cost constants (runner-app.tf runner_cost) are wrong."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "LiveVcpus"
  dimensions          = { Repo = "ejc3/imagine" }
  statistic           = "Average"
  period              = 3600
  treat_missing_data  = "notBreaching"
}

# Per budget group: throttled in more than throttled_alarm_hours of the last 24 (an hour counts when the group was
# throttled for at least half of it). Throttling is the bucket working, so a short spell is normal; a third of a day
# means the work no longer fits the budget, and the owner decides whether to raise daily_usd.
resource "aws_cloudwatch_metric_alarm" "runner_budget_throttled" {
  for_each            = var.enable_github_runner ? local.runner_budget_served : {}
  alarm_name          = "github-app-runner-budget-throttled-${each.key}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 24
  datapoints_to_alarm = each.value.throttled_alarm_hours + 1
  threshold           = 0.5
  alarm_description   = "The ${each.key} runner budget (${each.value.daily_usd} USD a day) was throttled in more than ${each.value.throttled_alarm_hours} of the last 24 hours: its repos ran at their throttled caps. Raise daily_usd in runner-app.tf if the work has grown."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "Throttled"
  dimensions          = { Group = each.key }
  statistic           = "Average"
  period              = 3600
  treat_missing_data  = "notBreaching"
}

# Per budget group: the last 7 days of the bucket's drain (SpendUsd7d) above 7 days of budget, a backstop to the
# bucket's arithmetic. The bucket bounds the long-run average to daily_usd, but a week that starts with a full bucket
# may spend its whole capacity on top ($350 + $100), and hosts launched in burst run on after a throttle (up to 3 hours)
# while the level is already at zero. So this can fire on legitimate use: it says a week cost more than its budget, and
# is informational.
resource "aws_cloudwatch_metric_alarm" "runner_budget_weekly" {
  for_each            = var.enable_github_runner ? local.runner_budget_served : {}
  alarm_name          = "github-app-runner-budget-weekly-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 7 * each.value.daily_usd
  alarm_description   = "The ${each.key} runners cost more than ${7 * each.value.daily_usd} USD over the last 7 days (the budget bucket's drain at runner-app.tf runner_cost)."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "SpendUsd7d"
  dimensions          = { Group = each.key }
  statistic           = "Maximum"
  period              = 3600
  treat_missing_data  = "notBreaching"
}

# A refused runners-app installation (app_runner.py check_installation_scope) only falls back to the repo's own PAT and logs
# "... refusing it": the installation itself is too broad and stays so until someone narrows it, so it alarms.
# The controller's log group, managed so the metric filter below never races Lambda's implicit creation of it (a fresh
# deployment has the function before it has ever logged). Adopted, not recreated; retention stays as it was (never expire).
resource "aws_cloudwatch_log_group" "runner_app" {
  count = var.enable_github_runner ? 1 : 0
  name  = "/aws/lambda/github-app-runner"
}

# Adopt the group only where it already exists and the runners are on: a fresh deployment creates it, and the one-flip
# teardown (enable_github_runner = false) leaves nothing for the import to target.
data "aws_cloudwatch_log_groups" "runner_app" {
  log_group_name_prefix = "/aws/lambda/github-app-runner"
}

import {
  # A map, so the index is each.value (a number): an import's `to` may not call functions.
  for_each = var.enable_github_runner && contains(data.aws_cloudwatch_log_groups.runner_app.log_group_names, "/aws/lambda/github-app-runner") ? { only = 0 } : {}
  to       = aws_cloudwatch_log_group.runner_app[each.value]
  id       = "/aws/lambda/github-app-runner"
}

resource "aws_cloudwatch_log_metric_filter" "runner_app_installation_refused" {
  count          = var.enable_github_runner ? 1 : 0
  name           = "github-app-runner-installation-refused"
  log_group_name = aws_cloudwatch_log_group.runner_app[0].name
  pattern        = "\"refusing it\""
  metric_transformation {
    name          = "InstallationRefused"
    namespace     = "GitHubAppRunner"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "runner_app_installation_refused" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "github-app-runner-installation-refused"
  alarm_description   = "The runners GitHub App's installation was refused as too broad (another account, all repositories, or a repository it does not serve); the controller is using the repos' own PATs. Narrow the installation (GITHUB-RUNNERS.md)."
  namespace           = "GitHubAppRunner"
  metric_name         = "InstallationRefused"
  statistic           = "Sum"
  period              = 600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  depends_on          = [aws_cloudwatch_log_metric_filter.runner_app_installation_refused]
}

resource "aws_cloudwatch_metric_alarm" "runner_app_errors" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "github-app-runner-errors"
  alarm_description   = "github-app-runner raised or timed out; app-repo jobs may be waiting for runners"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.runner_app[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# ============================================ GitHub side
# One workflow_job hook per repo, through the same API Gateway URL and with the same HMAC secret
# as fcvm's hook; the front tells them apart by repository.full_name. Each hook is created with
# that repo's own controller token (Webhooks RW, runner-repos.tf), read ephemerally so it never
# enters state. A provider cannot for_each, hence one alias per repo (one per token).
#
# Gated separately because the tokens are put into their secrets by hand: until then there is
# no secret version to read. A cold start creates the containers with this set false (README,
# Prerequisites), populates them, then runs the full apply with the default.
variable "enable_runner_app_webhooks" {
  description = "Create the workflow_job webhooks on the served repos (needs their controller tokens in Secrets Manager)"
  type        = bool
  default     = true
}

# A per-repo gate for dolphin-films, whose controller token has to be minted by the org owner (a fine-grained token cannot be
# created by API) and put by hand. Reading a secret that has no value fails EVERY plan of this repository, so until the value
# exists this stays false and the repo's webhook is neither planned nor its token read. Set the default to true in a commit
# once the token is stored, never with -var: a later plan without it would propose deleting the webhook.
variable "dolphin_films_token_ready" {
  description = "dolphin-labs-hq/dolphin-films' controller token has a value in Secrets Manager, so its webhook can be created"
  type        = bool
  default     = true
}

# The same gate for dolphin-maps: true once the org owner has minted and stored its token.
variable "dolphin_maps_token_ready" {
  description = "dolphin-labs-hq/dolphin-maps' controller token has a value in Secrets Manager, so its webhook can be created"
  type        = bool
  default     = true
}

# The same gate for dolphin-family: false until the org owner has minted and stored its token, then true in a commit.
variable "dolphin_family_token_ready" {
  description = "dolphin-labs-hq/dolphin-family's controller token has a value in Secrets Manager, so its webhook can be created"
  type        = bool
  default     = false
}

# The same gate for ejc3/imagine: true once its controller token is stored (runner-repos.tf). Its hook is made by the
# administration GitHub App, but the gate still keeps the token read and the controller entry out until the token exists.
variable "imagine_token_ready" {
  description = "ejc3/imagine's controller token has a value in Secrets Manager, so its webhook can be created"
  type        = bool
  default     = true
}

locals {
  runner_app_webhooks = var.enable_github_runner && var.enable_runner_app_webhooks

  # A repo with a hand-minted token: its webhook and token read need its gate as well as the global one. A repo not
  # listed here needs only the global gate.
  runner_app_token_gates = {
    "dolphin-labs-hq/dolphin-films" = var.dolphin_films_token_ready
    "dolphin-labs-hq/dolphin-maps"  = var.dolphin_maps_token_ready
    "ejc3/imagine"                  = var.imagine_token_ready

    "dolphin-labs-hq/dolphin-family" = var.dolphin_family_token_ready
  }
  dolphin_films_webhook = local.runner_app_webhooks && var.dolphin_films_token_ready
  dolphin_maps_webhook  = local.runner_app_webhooks && var.dolphin_maps_token_ready
  imagine_webhook       = local.runner_app_webhooks && var.imagine_token_ready

  dolphin_family_webhook = local.runner_app_webhooks && var.dolphin_family_token_ready

  runner_app_webhook_repos = [
    for r in local.runner_extra_repos : r
    if lookup(local.runner_app_token_gates, r, true)
  ]

  # Only the hooks made with a repo's own controller PAT read it. The dolphin-labs-hq and ejc3 hooks are made as the
  # administration GitHub App, so their PATs are never read here and can be retired without breaking a plan.
  runner_app_pat_hook_repos = [
    for r in local.runner_app_webhook_repos : r
    if !contains(["dolphin-labs-hq", "ejc3"], split("/", r)[0])
  ]
}

ephemeral "aws_secretsmanager_secret_version" "runner_repo_pat" {
  for_each  = local.runner_app_webhooks ? toset(local.runner_app_pat_hook_repos) : toset([])
  secret_id = aws_secretsmanager_secret.github_runner_repo_pat[each.value].id
}

provider "github" {
  alias = "colton_games"
  owner = "CoderColton"
  token = local.runner_app_webhooks ? ephemeral.aws_secretsmanager_secret_version.runner_repo_pat["CoderColton/colton-games"].secret_string : null
}

# The dolphin-labs-hq repos' hooks are managed as the administration agent's GitHub App (github-admin-tokens.tf: installed on
# the organization with repository_hooks write), not with the repos' controller PATs, so those PATs can be retired: the
# controller itself takes installation tokens from the runners app (app_runner.py). colton-games keeps its PAT: the app
# would have to be installed from Colton's own account. Read only while the hooks are managed.
ephemeral "aws_secretsmanager_secret_version" "github_app_admin" {
  count     = local.runner_app_webhooks ? 1 : 0
  secret_id = aws_secretsmanager_secret.github_app_admin.id
}

locals {
  github_app_admin = local.runner_app_webhooks ? jsondecode(ephemeral.aws_secretsmanager_secret_version.github_app_admin[0].secret_string) : null
}

provider "github" {
  alias = "dolphin_labs"
  owner = "dolphin-labs-hq"
  dynamic "app_auth" {
    for_each = local.runner_app_webhooks ? [1] : []
    content {
      id              = tostring(local.github_app_admin.app_id)
      installation_id = tostring(local.github_app_admin.installations["dolphin-labs-hq"])
      pem_file        = local.github_app_admin.private_key
    }
  }
}


resource "github_repository_webhook" "runner_app_colton_games" {
  count      = local.runner_app_webhooks ? 1 : 0
  provider   = github.colton_games
  repository = "colton-games"
  events     = ["workflow_job"]
  active     = true

  configuration {
    url          = "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = random_password.github_webhook[0].result
  }

  depends_on = [aws_lambda_function.runner_app, aws_lambda_function.runner_webhook_front]
}


resource "github_repository_webhook" "runner_app_dolphin_labs" {
  count      = local.runner_app_webhooks ? 1 : 0
  provider   = github.dolphin_labs
  repository = "dolphin-labs"
  events     = ["workflow_job"]
  active     = true

  configuration {
    url          = "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = random_password.github_webhook[0].result
  }

  depends_on = [aws_lambda_function.runner_app, aws_lambda_function.runner_webhook_front]
}

resource "github_repository_webhook" "runner_app_dolphin_films" {
  count      = local.dolphin_films_webhook ? 1 : 0
  provider   = github.dolphin_labs
  repository = "dolphin-films"
  events     = ["workflow_job"]
  active     = true

  configuration {
    url          = "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = random_password.github_webhook[0].result
  }

  depends_on = [aws_lambda_function.runner_app, aws_lambda_function.runner_webhook_front]
}


# ejc3/imagine's hook, made as the administration GitHub App's ejc3 installation through agents-md.tf's provider (the same
# app, installation and owner). Not the default provider: that one holds github-webhook-admin-pat, limited to ejc3/fcvm.
resource "github_repository_webhook" "runner_app_imagine" {
  count      = local.imagine_webhook ? 1 : 0
  provider   = github.agents_md
  repository = "imagine"
  events     = ["workflow_job"]
  active     = true

  configuration {
    url          = "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = random_password.github_webhook[0].result
  }

  depends_on = [aws_lambda_function.runner_app, aws_lambda_function.runner_webhook_front]
}

resource "github_repository_webhook" "runner_app_dolphin_maps" {
  count      = local.dolphin_maps_webhook ? 1 : 0
  provider   = github.dolphin_labs
  repository = "dolphin-maps"
  events     = ["workflow_job"]
  active     = true

  configuration {
    url          = "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = random_password.github_webhook[0].result
  }

  depends_on = [aws_lambda_function.runner_app, aws_lambda_function.runner_webhook_front]
}

resource "github_repository_webhook" "runner_app_dolphin_family" {
  count      = local.dolphin_family_webhook ? 1 : 0
  provider   = github.dolphin_labs
  repository = "dolphin-family"
  events     = ["workflow_job"]
  active     = true

  configuration {
    url          = "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = random_password.github_webhook[0].result
  }

  depends_on = [aws_lambda_function.runner_app, aws_lambda_function.runner_webhook_front]
}

# The runners GitHub App (ejc3-aws-runners): installation tokens for the controller, narrowed per call to one repository and
# to administration:write + actions:read, expiring within the hour (app_runner.py, app_token). JSON: app_id, slug,
# private_key, installations {owner: id}. Terraform owns only the container; the value is stored from GitHub's
# app-manifest conversion without being printed. Readers: the controller and the jumpboxes' role, nothing else. This
# replaces the controller's year-long per-repo PATs for the repos in local.runner_app_github_app_repos.
resource "aws_secretsmanager_secret" "github_app_runners" {
  count                   = var.enable_github_runner ? 1 : 0
  name                    = "github-app/runners"
  description             = "The runners GitHub App's credentials (JSON: app id, private key, installations) for the github-app-runner controller. See runner-app.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "github-app/runners", Project = "github-runners" }
}

resource "aws_secretsmanager_secret_policy" "github_app_runners" {
  count      = var.enable_github_runner ? 1 : 0
  secret_arn = aws_secretsmanager_secret.github_app_runners[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheControllerAndTheJumpboxRoleCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.github_app_runners[0].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat([aws_iam_role.runner_app_lambda[0].arn], local.github_admin_token_readers) } }
    }]
  })
}
