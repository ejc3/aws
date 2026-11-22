# fcvm-fleet-prod.tf
#
# The PRODUCTION fcvm fleet (fcvm.dev) beside staging in the same fcvm-fleet account, all behind
# var.fcvm_fleet_prod_enabled (fcvm-dev-prod.tf), which is false: with it off, this file and fcvm-fleet-prod-dns.tf and
# fcvm-fleet-prod-broadcast.tf create nothing. Owner decisions, 2026-11-18 and 2026-11-19 (ejc3/fcvm fleet production
# plan): the same account as staging, every prod resource scoped by the tag Fleet=prod; Turnstile gates every public
# viewer; $750 a day metered at the Spot price with forced sleep; two awake GPU hosts spread across availability zones;
# all modes public.
#
# The stack is local.fcvm_fleet_stacks.prod (fcvm-fleet.tf): Fleet=prod, names fcvm-fleet-prod-*, the certificate
# secret fcvm-fleet/prod/tls and the state object prod/state.json in the shared state bucket.
#
# - Hosts and tiles: module.fcvm_fleet_prod_east1, the staging region module's code with the prod prefix, so its own VPC
#   in us-east-1 (10.23.0.0/16), its own security groups and its own launch templates, which tag every instance and
#   spot request Fleet=prod. Only us-east-1 has fleet AMIs and the G spot quota. The images come from prod's own
#   release pointer, /fcvm-fleet/prod/ami-release; the staging publisher can write staging's templates only
#   (fcvm-fleet-ami-pipeline.tf), and promotion into prod is the next PR's (plan A4).
# - Roles: fcvm-fleet-prod-host and fcvm-fleet-prod-tile read only prod's certificate (and the host prod's agent token,
#   fcvm-dev-prod.tf); the coordinator's task role launches, starts, stops, retags, terminates and cancels only through
#   prod's templates and only what is tagged Fleet=prod (aws_iam_policy.fcvm_fleet_stack_scope["prod"]), and passes
#   only the prod host and tile roles. Staging's role is held to Fleet=stage by the same policy, so neither stack can
#   act on the other's instances.
# - The coordinator: its own ECS cluster and Fargate service in us-east-2 (staging's VPC there; its own security group),
#   one task, deployment minimum healthy 0 and maximum 100 as staging's. Its cloudflared container runs prod's tunnel
#   (fcvm-dev-prod.tf: fleet-api.fcvm.dev with /live and /poster.jpg public and /agent behind Access, fleet-ops.fcvm.dev
#   behind Access). Its image comes from /fcvm-fleet/prod/coordinator-image.
# - The certificate covers *.tiles.fcvm.dev only (--tls-scope subzone, ejc3/fcvm#1973): the coordinator answers its
#   DNS-01 record from its own subzone server (fcvm-fleet-prod-dns.tf), so it is given no Cloudflare token at all.
#
# Cost while on (prices from the staging files): the GPU hosts and tiles while awake, bounded by --daily-spend-usd; the
# 4 vCPU and 8 GB task, about $115 a month; the DNS load balancer and its two addresses (fcvm-fleet-prod-dns.tf); the
# IVS channel while live (fcvm-fleet-prod-broadcast.tf); 8 stopped pool tiles' gp3 root volumes, $20.48 a month.

locals {
  fcvm_fleet_prod_stack  = local.fcvm_fleet_stacks.prod
  fcvm_fleet_prod_region = "us-east-1"
  fcvm_fleet_prod_cidr   = "10.23.0.0/16"
  # The page's origin (ejc3/fcvm fleet/web wrangler.jsonc, env.production): the only browser origin /live accepts.
  fcvm_fleet_prod_site_origin = "https://fcvm.dev"
  # Owner, 2026-11-18: $750 a day, metered at the Spot price; the coordinator sleeps the fleet at the ceiling.
  fcvm_fleet_prod_daily_spend_usd = 750
  # Two awake GPU hosts in us-east-1, each launched by its own request into an availability zone no live host is in
  # (ejc3/fcvm#1973, src/cli/args.rs --awake-hosts and --host-spread az). A host given a spot notice is replaced at the
  # notice, so the replacement needs a third zone: the task definition requires three host zones.
  fcvm_fleet_prod_awake_hosts    = 2
  fcvm_fleet_prod_min_host_zones = 3

  # The prod region module, as a map so nothing below indexes an empty tuple while the stack is off.
  fcvm_fleet_prod_regional = { for m in module.fcvm_fleet_prod_east1 : local.fcvm_fleet_prod_region => m }

  # The prod release pointer's images (aws_ssm_parameter.fcvm_fleet_prod_ami_release): read from the resource, whose
  # state holds the live value after refresh, so the first apply needs no second pass.
  fcvm_fleet_prod_release   = [for p in aws_ssm_parameter.fcvm_fleet_prod_ami_release : jsondecode(nonsensitive(p.value))]
  fcvm_fleet_prod_host_amis = length(local.fcvm_fleet_prod_release) > 0 ? lookup(local.fcvm_fleet_prod_release[0].host_amis, local.fcvm_fleet_prod_region, {}) : {}
  fcvm_fleet_prod_tile_amis = length(local.fcvm_fleet_prod_release) > 0 ? lookup(local.fcvm_fleet_prod_release[0].tile_amis, local.fcvm_fleet_prod_region, {}) : {}

  # The host agent's arguments: staging's (fcvm-fleet.tf, fcvm_fleet_host_agent_args) with prod's agent endpoint, agent
  # token, certificate and frame zone. scripts/test-fcvm-fleet.py holds the rest equal to staging's.
  fcvm_fleet_prod_host_agent_args = var.fcvm_fleet_prod_enabled ? join(" ", concat([
    "--dial wss://${local.fcvm_dev_agent_prod_path}",
    "--access-secret ${aws_secretsmanager_secret.fcvm_fleet_prod_agent_access[0].name}",
    "--tls-secret ${aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].name}",
    "--secret-region ${local.fcvm_fleet_home_region}",
    "--host-from-imds",
    "--image localhost/fcvm-renderer:fleet",
    "--base-port 20100",
    "--publish-ip 0.0.0.0",
    "--ipv6-prefix-from-imds",
    "--frame-zone ${local.fcvm_fleet_prod_names_zone}",
    "--tls-publish-base ${local.fcvm_fleet_slot_tls_port_base}",
    "--public-ipv4-from-imds",
    "--spot-notice-url http://169.254.169.254",
    "--store-ready-file /var/lib/fcvm-fleet/store-ready",
    "--phones 2",
    ], local.fcvm_fleet_aggregated ? [
    "--aggregate-port ${local.fcvm_fleet_aggregate_port}",
    "--aggregate-tls-port ${local.fcvm_fleet_aggregate_tls_port}",
  ] : [])) : ""

  # What a launch may reference and create (the staging split, fcvm-fleet.tf), for prod's one region.
  fcvm_fleet_prod_tile_template_arns      = flatten([for m in values(local.fcvm_fleet_prod_regional) : m.tile_template_arns])
  fcvm_fleet_prod_host_template_arns      = flatten([for m in values(local.fcvm_fleet_prod_regional) : m.host_template_arns])
  fcvm_fleet_prod_tile_template_condition = length(local.fcvm_fleet_prod_tile_template_arns) > 0 ? local.fcvm_fleet_prod_tile_template_arns : ["arn:aws:ec2:*:*:launch-template/none"]
  fcvm_fleet_prod_host_template_condition = length(local.fcvm_fleet_prod_host_template_arns) > 0 ? local.fcvm_fleet_prod_host_template_arns : ["arn:aws:ec2:*:*:launch-template/none"]
  fcvm_fleet_prod_tile_referenced_arns = concat(
    var.fcvm_ami_pipeline_enabled ? [] : [for ami in values(local.fcvm_fleet_prod_tile_amis) : "arn:aws:ec2:${local.fcvm_fleet_prod_region}::image/${ami}"],
    flatten([for r, m in local.fcvm_fleet_prod_regional : [for s in m.subnet_ids : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:subnet/${s}"]]),
    [for r, m in local.fcvm_fleet_prod_regional : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:security-group/${m.security_group_id}"],
    local.fcvm_fleet_prod_tile_template_arns,
    ["arn:aws:ec2:us-east-1::image/ami-none"],
  )
  fcvm_fleet_prod_host_referenced_arns = concat(
    var.fcvm_ami_pipeline_enabled ? [] : [for ami in values(local.fcvm_fleet_prod_host_amis) : "arn:aws:ec2:${local.fcvm_fleet_prod_region}::image/${ami}" if ami != ""],
    flatten([for r, m in local.fcvm_fleet_prod_regional : [for s in m.host_subnet_ids : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:subnet/${s}"] if length(m.host_template_names) > 0]),
    [for r, m in local.fcvm_fleet_prod_regional : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:security-group/${m.security_group_id}" if length(m.host_template_names) > 0],
    local.fcvm_fleet_prod_host_template_arns,
    ["arn:aws:ec2:us-east-1::image/ami-none"],
  )
  fcvm_fleet_prod_created_arns      = [for kind in ["instance", "volume", "network-interface"] : "arn:aws:ec2:${local.fcvm_fleet_prod_region}:${local.fcvm_fleet_account_id}:${kind}/*"]
  fcvm_fleet_prod_spot_request_arns = ["arn:aws:ec2:${local.fcvm_fleet_prod_region}:${local.fcvm_fleet_account_id}:spot-instances-request/*"]
  fcvm_fleet_prod_fleet_arns        = ["arn:aws:ec2:${local.fcvm_fleet_prod_region}:${local.fcvm_fleet_account_id}:fleet/*"]
  fcvm_fleet_prod_instance_arns     = ["arn:aws:ec2:${local.fcvm_fleet_prod_region}:${local.fcvm_fleet_account_id}:instance/*"]
  fcvm_fleet_prod_volume_arns       = ["arn:aws:ec2:${local.fcvm_fleet_prod_region}:${local.fcvm_fleet_account_id}:volume/*"]

  fcvm_fleet_prod_host_templates = flatten([for r, m in local.fcvm_fleet_prod_regional : [for t in m.host_template_names : "${r}:${t}"]])
  fcvm_fleet_prod_host_subnets = flatten([for r, m in local.fcvm_fleet_prod_regional : [for s in m.host_subnet_ids : "${r}:${s}"]
  if length(m.host_template_names) > 0])

  # The prod coordinator's arguments: staging's (fcvm-fleet.tf, fcvm_fleet_coordinator_command) for the prod stack, with
  # these differences:
  # - No --access-viewer-address: the page at fcvm.dev opens wss://fleet-api.fcvm.dev/live itself, so CF-Connecting-IP
  #   is the viewer's address. Only https://fcvm.dev may open /live from a browser.
  # - Turnstile (FLEET_TURNSTILE_SECRET and FLEET_TURNSTILE_SITE_KEY, from fcvm-fleet/prod/turnstile through the
  #   execution role, fcvm-dev-prod.tf): a connection joins, takes a seat or wakes the fleet only after it passes.
  # - Access JWTs verified by the coordinator itself (as ejc3/aws#915 for staging): /live accepts the fleet-ops.fcvm.dev
  #   application's tokens (the owner and prod CI), /agent the prod agents' application's only.
  # - --daily-spend-usd 750, and two awake hosts spread across zones (above).
  # - The certificate and the launch names in tiles.fcvm.dev, with no Cloudflare token: --tls-scope subzone (ejc3/fcvm#1973,
  #   src/fleet/coord/cert.rs) orders *.tiles.fcvm.dev alone and answers its DNS-01 record from the coordinator's own
  #   subzone server, refuses --dns-token-secret, and names the EC2 tiles' health checks ec2-<n>.tiles.fcvm.dev. No
  #   --dns-zone-id, and no _acme-challenge record in fcvm.dev.
  # No --warm-hours: the owner has not given the windows.
  fcvm_fleet_prod_coordinator_command = var.fcvm_fleet_prod_enabled ? concat(
    ["--listen", "0.0.0.0:${local.fcvm_fleet_coordinator_port}", "--trust-cf-connecting-ip", "--allowed-origin", local.fcvm_fleet_prod_site_origin],
    ["--sleep-when-idle"],
    ["--fleet-name", local.fcvm_fleet_prod_stack.fleet],
    ["--ec2", "aws", "--ec2-tiles", "8", "--ec2-regions", local.fcvm_fleet_prod_region, "--ec2-template", "${local.fcvm_fleet_prod_stack.prefix}-tile-x86_64", "--ec2-on-demand-template", "${local.fcvm_fleet_prod_stack.prefix}-tile-on-demand-x86_64"],
    local.fcvm_fleet_tile_pool ? ["--ec2-pool", "8", "--ec2-pool-template", "${local.fcvm_fleet_prod_stack.prefix}-tile-pool-x86_64"] : [],
    ["--dns-zone", "fcvm.dev", "--tls-secret", aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].name, "--tls-scope", "subzone", "--secrets-region", local.fcvm_fleet_home_region],
    ["--dns-subzone", local.fcvm_fleet_prod_names_zone, "--dns-subzone-ns", join(",", local.fcvm_fleet_prod_names_ns), "--dns-listen", "0.0.0.0:${local.fcvm_fleet_dns_port}"],
    local.fcvm_fleet_broadcast ? ["--broadcast-listen", "127.0.0.1:${local.fcvm_fleet_broadcast_port}", "--broadcast-secret", aws_secretsmanager_secret.fcvm_fleet_prod_broadcast[0].name, "--direct-capacity", tostring(local.fcvm_fleet_direct_capacity)] : [],
    ["--text-check", "bedrock", "--text-check-model", local.bedrock_haiku_profile, "--text-check-region", local.bedrock_haiku_region],
    ["--access-team-domain", trimprefix(local.browser_manager_issuer, "https://"),
      "--access-aud", cloudflare_zero_trust_access_application.fcvm_dev_ops_prod[0].aud,
    "--agent-access-aud", cloudflare_zero_trust_access_application.fcvm_dev_agent_prod[0].aud],
    ["--daily-spend-usd", tostring(local.fcvm_fleet_prod_daily_spend_usd)],
    ["--awake-hosts", tostring(local.fcvm_fleet_prod_awake_hosts), "--host-spread", "az"],
  ) : []
}

# ------------------------------------------------------------------------------------------------ the certificate
# The coordinator first reads a JSON object, then issues *.tiles.fcvm.dev and adds the renderers' scene token.
resource "aws_secretsmanager_secret" "fcvm_fleet_prod_tls" {
  count                   = local.fcvm_fleet_prod
  provider                = aws.fcvm_fleet
  name                    = local.fcvm_fleet_prod_stack.tls_secret
  description             = "Wildcard TLS certificate for *.tiles.fcvm.dev, the prod fleet's launch names (JSON: cert, key, scene_token). Issued and renewed by the prod coordinator; see fcvm-fleet-prod.tf."
  recovery_window_in_days = 7
  tags                    = merge(local.fcvm_fleet_prod_tags, { Name = local.fcvm_fleet_prod_stack.tls_secret, Project = "fcvm-fleet" })
}

# Only a pristine secret needs a seed. Later versions belong to the coordinator and never enter Terraform state.
resource "terraform_data" "fcvm_fleet_prod_tls_bootstrap" {
  count            = local.fcvm_fleet_prod
  triggers_replace = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn

  provisioner "local-exec" {
    working_dir = path.module
    command     = "python3 -I scripts/fcvm-prod-tls-seed.py"
    environment = {
      PROD_TLS_ARN = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn
      FLEET_REGION = local.fcvm_fleet_home_region
      FLEET_ROLE   = local.fcvm_fleet_access_role_arn
    }
  }
}

# Whatever any other identity's IAM policy says, only prod's coordinator, hosts and tiles (and the role Terraform
# applies through) read or write it: no staging role holds a prod key.
resource "aws_secretsmanager_secret_policy" "fcvm_fleet_prod_tls" {
  count      = local.fcvm_fleet_prod
  provider   = aws.fcvm_fleet
  secret_arn = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheProdStackAndTerraform"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
      Resource  = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = [
        aws_iam_role.fcvm_fleet_prod_coordinator_task[0].arn,
        aws_iam_role.fcvm_fleet_prod_host[0].arn,
        aws_iam_role.fcvm_fleet_prod_tile[0].arn,
        local.fcvm_lambda_tile_role_arns.prod,
        local.fcvm_fleet_access_role_arn,
      ] } }
    }]
  })
}

# ------------------------------------------------------------------------------------------------ instance roles
resource "aws_iam_role" "fcvm_fleet_prod_host" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_fleet_prod_stack.prefix}-host"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${local.fcvm_fleet_prod_stack.prefix}-host", Fleet = local.fcvm_fleet_prod_stack.fleet }
}

# Prod's certificate and prod's agent token, and the release artifacts (the asset bucket is shared, read only).
resource "aws_iam_role_policy" "fcvm_fleet_prod_host" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "fleet-host"
  role     = aws_iam_role.fcvm_fleet_prod_host[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTheCertificateAndTheAgentToken"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = [aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn, aws_secretsmanager_secret.fcvm_fleet_prod_agent_access[0].arn]
      },
      {
        Sid      = "ReadReleaseArtifacts"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.fcvm_fleet_assets.arn}/*"
      },
      {
        Sid      = "ListReleaseArtifacts"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.fcvm_fleet_assets.arn
      },
    ]
  })
}

resource "aws_iam_role" "fcvm_fleet_prod_tile" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_fleet_prod_stack.prefix}-tile"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${local.fcvm_fleet_prod_stack.prefix}-tile", Fleet = local.fcvm_fleet_prod_stack.fleet }
}

resource "aws_iam_role_policy" "fcvm_fleet_prod_tile" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "fleet-tile"
  role     = aws_iam_role.fcvm_fleet_prod_tile[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadTheCertificate"
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_prod_ssm" {
  for_each   = var.fcvm_fleet_prod_enabled ? { host = aws_iam_role.fcvm_fleet_prod_host[0].name, tile = aws_iam_role.fcvm_fleet_prod_tile[0].name } : {}
  provider   = aws.fcvm_fleet
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "fcvm_fleet_prod_host" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_fleet_prod_stack.prefix}-host"
  role     = aws_iam_role.fcvm_fleet_prod_host[0].name
}

resource "aws_iam_instance_profile" "fcvm_fleet_prod_tile" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_fleet_prod_stack.prefix}-tile"
  role     = aws_iam_role.fcvm_fleet_prod_tile[0].name
}

# ------------------------------------------------------------------------------------------------ the image pointer
# Prod's own software release pointer (the schema of staging's /fcvm-fleet/ami-release, fcvm-fleet-ami-release.tf). It
# starts at staging's release as it stands on the apply that creates it; afterwards only the prod promotion (plan A4,
# which moves it only to a release the image gate verified, while prod sleeps) changes it. Its images are the prod
# templates' (module.fcvm_fleet_prod_east1).
locals {
  fcvm_prod_initial_ami_release = var.fcvm_ami_pipeline_enabled ? nonsensitive(data.aws_ssm_parameter.fcvm_ami_release[0].value) : jsonencode({
    version      = 1
    generation   = 0
    source_sha   = ""
    build_id     = ""
    guard_sha256 = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
    host_amis    = var.fcvm_fleet_host_amis
    tile_amis    = var.fcvm_fleet_tile_amis
    versions     = {}
    previous     = null
  })
  fcvm_prod_seed_query = {
    role   = local.fcvm_fleet_access_role_arn
    region = local.fcvm_ami_region
    prod   = local.fcvm_prod_ami_release_parameter
  }
}

# The seed must be checked under Terraform's apply lock. A normal data source is read at plan time:
# timestamp() defers the read to the saved apply, only during creation. Later releases belong to the publisher.
data "external" "fcvm_prod_ami_pointer_present" {
  count   = local.fcvm_fleet_prod
  program = ["python3", "${path.module}/scripts/fcvm-prod-ami-seed.py", "present"]
  query   = local.fcvm_prod_seed_query
}

data "external" "fcvm_prod_ami_seed" {
  count   = local.fcvm_fleet_prod
  program = ["python3", "${path.module}/scripts/fcvm-prod-ami-seed.py", "verify"]
  query = merge(local.fcvm_prod_seed_query, data.external.fcvm_prod_ami_pointer_present[0].result.present == "true" ? {
    when = "existing"
    } : {
    when         = timestamp()
    release      = local.fcvm_prod_initial_ami_release
    stage        = var.fcvm_ami_pipeline_enabled ? aws_ssm_parameter.fcvm_ami_release.name : ""
    version      = var.fcvm_ami_pipeline_enabled ? tostring(data.aws_ssm_parameter.fcvm_ami_release[0].version) : ""
    copy_regions = jsonencode(local.fcvm_ami_copy_regions)
  })
}

resource "aws_ssm_parameter" "fcvm_fleet_prod_ami_release" {
  count       = local.fcvm_fleet_prod
  provider    = aws.fcvm_fleet_east1
  name        = "/fcvm-fleet/prod/ami-release"
  description = "The prod fleet's validated AMI pair and previous release; the prod promotion owns the value"
  type        = "String"
  value       = local.fcvm_prod_initial_ami_release
  tags        = local.fcvm_fleet_prod_tags
  lifecycle {
    ignore_changes  = [value]
    prevent_destroy = true
    precondition {
      condition     = data.external.fcvm_prod_ami_seed[0].result.verified == "true"
      error_message = "The production AMI seed must pass its apply-time staging release and image availability checks."
    }
    postcondition {
      condition = (
        try(jsondecode(self.value).version, 0) == 1 &&
        try(jsondecode(self.value).guard_sha256, "") == filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
      )
      error_message = "The prod Fleet AMI release schema or guard digest is incompatible; keep the current release and review the candidate."
    }
  }
}

ephemeral "aws_ssm_parameter" "fcvm_fleet_prod_ami_release_guard" {
  provider        = aws.fcvm_fleet_east1
  count           = local.fcvm_fleet_prod
  arn             = aws_ssm_parameter.fcvm_fleet_prod_ami_release[0].arn
  with_decryption = false
  lifecycle {
    postcondition {
      condition     = self.version == aws_ssm_parameter.fcvm_fleet_prod_ami_release[0].version
      error_message = "The Fleet AMI release changed after planning; create a fresh plan."
    }
  }
}

# ------------------------------------------------------------------------------------------------ hosts and tiles
module "fcvm_fleet_prod_east1" {
  count                   = local.fcvm_fleet_prod
  fleet                   = local.fcvm_fleet_prod_stack.fleet
  name_prefix             = local.fcvm_fleet_prod_stack.prefix
  ip_options_bootstrap    = local.linux_ip_options_bootstrap
  ip_options_guard_sha256 = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
  source                  = "./modules/fcvm-fleet-region"
  providers               = { aws = aws.fcvm_fleet_east1 }
  cidr_block              = local.fcvm_fleet_prod_cidr
  gpu_amis                = local.fcvm_fleet_prod_host_amis
  tile_amis               = local.fcvm_fleet_prod_tile_amis

  ami_release_guarded           = true
  ami_release_parameter_version = one(ephemeral.aws_ssm_parameter.fcvm_fleet_prod_ami_release_guard[*].version)
  hosts_enabled                 = local.fcvm_fleet_hosts_enabled
  # Prod's alarms are their own PR (plan A5); the fcvm-fleet-alerts topic stays staging's.
  alert_topic     = false
  alert_email     = local.browser_manager_owner
  host_agent_args = local.fcvm_fleet_prod_host_agent_args
  tile_tls_secret = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].name
  # All modes public (owner, 2026-11-19): the same reviewed mode images as staging's us-east-1 (module.fcvm_fleet_east1);
  # scripts/test-fcvm-fleet.py holds these four equal to staging's.
  potato_host_amis = concat([
    local.fcvm_ami_bootstrap["host/x86_64/95217caa"].image,
    local.fcvm_ami_bootstrap["host/x86_64/a4a0bd3e"].image,
    local.fcvm_ami_bootstrap["host/x86_64/92a26793"].image,
    local.fcvm_ami_bootstrap["host/x86_64/343b2287"].image,
  ], flatten([for copies in data.aws_ami_ids.fcvm_ami_build_region_copies : copies.ids]))
  windows_host_amis   = concat(data.aws_ami_ids.fcvm_ami_build_region_copies["host/x86_64/cad0ddad"].ids, data.aws_ami_ids.fcvm_ami_build_region_copies["host/x86_64/c12ad070"].ids, ["ami-089b83ada9f380d5e"])
  minecraft_host_amis = concat(data.aws_ami_ids.fcvm_ami_build_region_copies["host/x86_64/853eb034"].ids, data.aws_ami_ids.fcvm_ami_build_region_copies["host/x86_64/0e910245"].ids, data.aws_ami_ids.fcvm_ami_build_region_copies["host/x86_64/cad0ddad"].ids)
  minecraft_java_access = {
    host_amis   = ["ami-0c3ea14848ddbeb93", "ami-089b83ada9f380d5e"]
    gpu_slots   = local.fcvm_fleet_host_gpu_slots
    source_cidr = "::/0"
  }
  slot_tls_ports            = local.fcvm_fleet_ipv4_viewer_ports["us-east-1"]
  windows_count             = local.fcvm_fleet_host_windows
  minecraft_count           = local.fcvm_fleet_host_minecraft
  host_profile_name         = aws_iam_instance_profile.fcvm_fleet_prod_host[0].name
  tile_profile_name         = aws_iam_instance_profile.fcvm_fleet_prod_tile[0].name
  flow_log_destination      = "${aws_s3_bucket.fcvm_fleet_flow_logs.arn}/vpc-flow"
  flow_log_bucket_policy_id = aws_s3_bucket_policy.fcvm_fleet_flow_logs.id
}

# ------------------------------------------------------------------------------------------------ the coordinator's image
# The prod coordinator's release pointer (staging's is /fcvm-fleet/stage/coordinator-image, fcvm-fleet-ci.tf). It starts
# at the digest staging runs on the apply that creates it; afterwards only the prod promotion (plan A4) changes it.
resource "aws_ssm_parameter" "fcvm_fleet_prod_coordinator_release" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "/fcvm-fleet/prod/coordinator-image"
  type     = "String"
  value    = nonsensitive(data.aws_ssm_parameter.fcvm_coordinator_release.value)
  tags     = local.fcvm_fleet_prod_tags
  lifecycle {
    ignore_changes = [value]
  }
}

# Re-opened at apply time under the backend lock, as staging's: a promotion between plan and apply invalidates the plan.
ephemeral "aws_ssm_parameter" "fcvm_fleet_prod_coordinator_release_guard" {
  count           = local.fcvm_fleet_prod
  provider        = aws.fcvm_fleet
  arn             = aws_ssm_parameter.fcvm_fleet_prod_coordinator_release[0].arn
  with_decryption = false
  lifecycle {
    postcondition {
      condition     = self.version == aws_ssm_parameter.fcvm_fleet_prod_coordinator_release[0].version
      error_message = "The prod coordinator release changed after this plan; create a fresh plan before applying."
    }
  }
}

# ------------------------------------------------------------------------------------------------ the coordinator (us-east-2)
resource "aws_ecs_cluster" "fcvm_fleet_prod" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = local.fcvm_fleet_prod_stack.prefix
  tags     = { Name = local.fcvm_fleet_prod_stack.prefix, Fleet = local.fcvm_fleet_prod_stack.fleet }
}

resource "aws_cloudwatch_log_group" "fcvm_fleet_prod_coordinator" {
  count             = local.fcvm_fleet_prod
  provider          = aws.fcvm_fleet
  name              = "/ecs/${local.fcvm_fleet_prod_coordinator}"
  retention_in_days = 30
  tags              = local.fcvm_fleet_prod_tags
}

# Named as fcvm-dev-prod.tf's Turnstile secret policy expects (local.fcvm_fleet_prod_coordinator).
resource "aws_iam_role" "fcvm_fleet_prod_coordinator_execution" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_fleet_prod_coordinator}-execution"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Fleet = local.fcvm_fleet_prod_stack.fleet }
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_prod_coordinator_execution" {
  count      = local.fcvm_fleet_prod
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_prod_coordinator_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The execution role injects prod's tunnel token into cloudflared and the Turnstile keys into the coordinator.
resource "aws_iam_role_policy" "fcvm_fleet_prod_coordinator_execution_secret" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "tunnel-token-and-turnstile"
  role     = aws_iam_role.fcvm_fleet_prod_coordinator_execution[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = [aws_secretsmanager_secret.fcvm_fleet_prod_coordinator_tunnel[0].arn, aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].arn]
    }]
  })
}

resource "aws_iam_role" "fcvm_fleet_prod_coordinator_task" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = local.fcvm_fleet_prod_coordinator
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Fleet = local.fcvm_fleet_prod_stack.fleet }
}

# Its state object and its earlier versions; prod's certificate and broadcast secrets; the viewer word check; and, for
# instances, everything but the launches (aws_iam_policy.fcvm_fleet_prod_coordinator_launch): tag at launch, terminate,
# start, stop, retag and cancel fleet instances and tiles' spot requests by Purpose, describe, and pass the prod host and
# tile roles. aws_iam_policy.fcvm_fleet_stack_scope["prod"] adds Fleet=prod to every one of those.
resource "aws_iam_role_policy" "fcvm_fleet_prod_coordinator_task" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "coordinator"
  role     = aws_iam_role.fcvm_fleet_prod_coordinator_task[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "StateObject"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${local.fcvm_fleet_prod_stack.state_key}" # FLEET_STATE_KEY
      },
      {
        # A missing object reads as 404, not 403, so a fresh stack starts empty (as staging's StateObjectMissingReadsAs404).
        Sid      = "StateObjectMissingReadsAs404"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.fcvm_fleet_coordinator_state.arn
      },
      {
        # When the state does not parse, the coordinator reads an earlier version of that one object (ejc3/aws#912).
        Sid      = "StateObjectEarlierVersions"
        Effect   = "Allow"
        Action   = "s3:GetObjectVersion"
        Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${local.fcvm_fleet_prod_stack.state_key}" # FLEET_STATE_KEY
      },
      {
        Sid       = "StateObjectVersionList"
        Effect    = "Allow"
        Action    = "s3:ListBucketVersions"
        Resource  = aws_s3_bucket.fcvm_fleet_coordinator_state.arn
        Condition = { StringEquals = { "s3:prefix" = local.fcvm_fleet_prod_stack.state_key } } # FLEET_STATE_KEY
      },
      {
        # The certificate it issues and renews, and the broadcast's ingest and playback keys. No Cloudflare token.
        Sid      = "FleetSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = concat([aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn], aws_secretsmanager_secret.fcvm_fleet_prod_broadcast[*].arn)
      },
      {
        Sid      = "StoreTheIssuedCertificate"
        Effect   = "Allow"
        Action   = "secretsmanager:PutSecretValue"
        Resource = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn
      },
      {
        Sid    = "CheckViewerWordsWithClaudeHaiku55"
        Effect = "Allow"
        Action = "bedrock:InvokeModel"
        Resource = [
          "arn:aws:bedrock:*:${local.fcvm_fleet_account_id}:inference-profile/${local.bedrock_haiku_profile}",
          "arn:aws:bedrock:*::foundation-model/${local.bedrock_haiku_model}",
        ]
      },
      {
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
      {
        Sid       = "TagAtLaunchOnly"
        Effect    = "Allow"
        Action    = "ec2:CreateTags"
        Resource  = "*"
        Condition = { StringEquals = { "ec2:CreateAction" = ["RunInstances", "CreateFleet"] } }
      },
      {
        Sid       = "TerminateFleetInstancesOnly"
        Effect    = "Allow"
        Action    = "ec2:TerminateInstances"
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = ["fcvm-fleet-tile", "fcvm-fleet-host"], "aws:RequestedRegion" = [local.fcvm_fleet_prod_region] } }
      },
      {
        Sid       = "StartAndStopTilesOnly"
        Effect    = "Allow"
        Action    = ["ec2:StartInstances", "ec2:StopInstances"]
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = [local.fcvm_fleet_prod_region] } }
      },
      {
        Sid      = "RetagTilesForThePool"
        Effect   = "Allow"
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"]
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          StringEquals                = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = [local.fcvm_fleet_prod_region] }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["FleetPool", "FleetTile"] }
        }
      },
      {
        Sid       = "CancelTileSpotRequestsOnly"
        Effect    = "Allow"
        Action    = "ec2:CancelSpotInstanceRequests"
        Resource  = "arn:aws:ec2:*:*:spot-instances-request/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = [local.fcvm_fleet_prod_region] } }
      },
      {
        Sid      = "DescribeEc2"
        Effect   = "Allow"
        Action   = ["ec2:Describe*"]
        Resource = "*"
      },
      {
        Sid       = "PassTheProdHostAndTileRolesOnly"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = [aws_iam_role.fcvm_fleet_prod_host[0].arn, aws_iam_role.fcvm_fleet_prod_tile[0].arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      ], [for channel in aws_ivs_channel.fcvm_fleet_prod_broadcast : {
        # BroadcastLive (ejc3/fcvm#1974, src/fleet/coord/metrics.rs): GetStream on the broadcast secret's channel_arn
        # every pass. Without it the metric always reads 0. The prod channel only.
        Sid      = "AskWhetherTheProdBroadcastIsLive"
        Effect   = "Allow"
        Action   = "ivs:GetStream"
        Resource = channel.arn
    }])
  })
}

# The launches, as staging's (fcvm-fleet.tf, the RunInstances and CreateFleet statements of fcvm_fleet_coordinator_task),
# through prod's templates and with prod's resources only. A managed policy, so the inline budget above keeps room.
resource "aws_iam_policy" "fcvm_fleet_prod_coordinator_launch" {
  count       = local.fcvm_fleet_prod
  provider    = aws.fcvm_fleet
  name        = "${local.fcvm_fleet_prod_coordinator}-launch"
  description = "The prod fleet coordinator launches GPU hosts and EC2 tiles from the prod templates only"
  tags        = local.fcvm_fleet_prod_tags
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "LaunchTilesFromTheirTemplates"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = local.fcvm_fleet_prod_created_arns
        Condition = { ArnLike = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_tile_template_condition } }
      },
      {
        Sid       = "LaunchTilesWithTheirOwnResources"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = local.fcvm_fleet_prod_tile_referenced_arns
        Condition = { ArnLike = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_tile_template_condition } }
      },
      {
        Sid      = "LaunchTaggedTileSpotRequestsOnly"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = local.fcvm_fleet_prod_spot_request_arns
        Condition = { StringEquals = {
          "aws:RequestedRegion"    = [local.fcvm_fleet_prod_region]
          "aws:RequestTag/Purpose" = "fcvm-fleet-tile"
        } }
      },
      {
        Sid      = "LaunchHostsAsInstantFleets"
        Effect   = "Allow"
        Action   = "ec2:CreateFleet"
        Resource = concat(local.fcvm_fleet_prod_fleet_arns, local.fcvm_fleet_prod_volume_arns)
      },
      {
        Sid      = "LaunchFleetInstancesInFleetRegionsOnly"
        Effect   = "Allow"
        Action   = "ec2:CreateFleet"
        Resource = local.fcvm_fleet_prod_instance_arns
      },
      {
        Sid       = "LaunchHostsFromTheirTemplates"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = local.fcvm_fleet_prod_created_arns
        Condition = { ArnLike = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_host_template_condition } }
      },
      {
        Sid       = "LaunchHostsWithTheirOwnResources"
        Effect    = "Allow"
        Action    = ["ec2:RunInstances", "ec2:CreateFleet"]
        Resource  = local.fcvm_fleet_prod_host_referenced_arns
        Condition = { ArnLikeIfExists = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_host_template_condition } }
      },
      {
        Sid       = "FleetTypesOnly"
        Effect    = "Deny"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringNotEquals = { "ec2:InstanceType" = concat(local.fcvm_fleet_tile_types, local.fcvm_fleet_host_types) } }
      },
      {
        Sid      = "TileTemplatesLaunchTileTypesOnly"
        Effect   = "Deny"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_tile_template_condition }
          StringNotEquals = { "ec2:InstanceType" = local.fcvm_fleet_tile_types }
        }
      },
      {
        Sid      = "HostTemplatesLaunchHostTypesOnly"
        Effect   = "Deny"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_host_template_condition }
          StringNotEquals = { "ec2:InstanceType" = local.fcvm_fleet_host_types }
        }
      },
      {
        Sid       = "FleetInstancesAreTagged"
        Effect    = "Deny"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringNotEquals = { "aws:RequestTag/Purpose" = ["fcvm-fleet-tile", "fcvm-fleet-host"] } }
      },
      {
        Sid      = "HostTemplatesAreTaggedHostsOnly"
        Effect   = "Deny"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_host_template_condition }
          StringNotEquals = { "aws:RequestTag/Purpose" = "fcvm-fleet-host" }
        }
      },
      {
        Sid      = "TileTemplatesAreTaggedTilesOnly"
        Effect   = "Deny"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_tile_template_condition }
          StringNotEquals = { "aws:RequestTag/Purpose" = "fcvm-fleet-tile" }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_prod_coordinator_launch" {
  count      = local.fcvm_fleet_prod
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_prod_coordinator_task[0].name
  policy_arn = aws_iam_policy.fcvm_fleet_prod_coordinator_launch[0].arn
}

# Images: staging's fcvm-fleet-validated-images (fcvm-fleet-ami-release.tf) for prod's templates.
resource "aws_iam_policy" "fcvm_fleet_prod_validated_images" {
  count       = local.fcvm_fleet_prod
  provider    = aws.fcvm_fleet
  name        = "${local.fcvm_fleet_prod_stack.prefix}-validated-images"
  description = "Prod: launch only owned private validated images with the reviewed guard and matching Fleet kind"
  tags        = local.fcvm_fleet_prod_tags
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ValidatedTileImagesOnly"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = ["arn:aws:ec2:${local.fcvm_fleet_prod_region}::image/*"]
        Condition = {
          StringEquals = {
            "ec2:Owner"                           = local.fcvm_fleet_account_id
            "ec2:ResourceTag/Purpose"             = "fcvm-fleet"
            "ec2:ResourceTag/FleetImageKind"      = "tile"
            "ec2:ResourceTag/FleetImageValidated" = "true"
            "ec2:ResourceTag/GuardHelperSHA256"   = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
          }
          Bool    = { "ec2:Public" = "false" }
          ArnLike = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_tile_template_condition }
        }
      },
      {
        Sid      = "ValidatedHostImagesOnly"
        Effect   = "Allow"
        Action   = ["ec2:RunInstances", "ec2:CreateFleet"]
        Resource = ["arn:aws:ec2:${local.fcvm_fleet_prod_region}::image/*"]
        Condition = {
          StringEquals = {
            "ec2:Owner"                           = local.fcvm_fleet_account_id
            "ec2:ResourceTag/Purpose"             = "fcvm-fleet"
            "ec2:ResourceTag/FleetImageKind"      = "host"
            "ec2:ResourceTag/FleetImageValidated" = "true"
            "ec2:ResourceTag/GuardHelperSHA256"   = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
          }
          Bool            = { "ec2:Public" = "false" }
          ArnLikeIfExists = { "ec2:LaunchTemplate" = local.fcvm_fleet_prod_host_template_condition }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_prod_validated_images" {
  count      = local.fcvm_fleet_prod
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_prod_coordinator_task[0].name
  policy_arn = aws_iam_policy.fcvm_fleet_prod_validated_images[0].arn
}

# The Java ingress group of prod's host template (staging's fcvm_fleet_coordinator_java).
resource "aws_iam_policy" "fcvm_fleet_prod_coordinator_java" {
  count       = var.fcvm_fleet_prod_enabled ? length(module.fcvm_fleet_prod_east1[0].host_minecraft_java_security_group_arns) : 0
  provider    = aws.fcvm_fleet
  name        = "${local.fcvm_fleet_prod_coordinator}-java"
  description = "Allow the prod fleet coordinator to use the prod Virginia host template's Java ingress group."
  tags        = local.fcvm_fleet_prod_tags
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "LaunchVirginiaHostWithTemplateJavaGroup"
      Effect   = "Allow"
      Action   = "ec2:RunInstances"
      Resource = module.fcvm_fleet_prod_east1[0].host_minecraft_java_security_group_arns[count.index]
      Condition = {
        ArnEquals = { "ec2:LaunchTemplate" = module.fcvm_fleet_prod_east1[0].host_minecraft_java_template_arn }
        Bool      = { "ec2:IsLaunchTemplateResource" = "true" }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_prod_coordinator_java" {
  count      = length(aws_iam_policy.fcvm_fleet_prod_coordinator_java)
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_prod_coordinator_task[0].name
  policy_arn = aws_iam_policy.fcvm_fleet_prod_coordinator_java[count.index].arn
}

# Nothing comes in but the subzone's DNS (fcvm-fleet-prod-dns.tf); browsers and agents arrive through prod's tunnel.
resource "aws_security_group" "fcvm_fleet_prod_coordinator" {
  count       = local.fcvm_fleet_prod
  provider    = aws.fcvm_fleet
  name        = local.fcvm_fleet_prod_coordinator
  description = "fcvm prod fleet coordinator task: DNS for tiles.fcvm.dev in, all out"
  vpc_id      = module.fcvm_fleet_east2.vpc_id

  ingress {
    description = "DNS over UDP"
    from_port   = local.fcvm_fleet_dns_port
    to_port     = local.fcvm_fleet_dns_port
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "DNS over TCP"
    from_port   = local.fcvm_fleet_dns_port
    to_port     = local.fcvm_fleet_dns_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description      = "All outbound"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = local.fcvm_fleet_prod_coordinator, Fleet = local.fcvm_fleet_prod_stack.fleet }
}

resource "aws_ecs_task_definition" "fcvm_fleet_prod_coordinator" {
  count                    = local.fcvm_fleet_prod
  depends_on               = [ephemeral.aws_ssm_parameter.fcvm_fleet_prod_coordinator_release_guard]
  provider                 = aws.fcvm_fleet
  family                   = local.fcvm_fleet_prod_coordinator
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.fcvm_fleet_broadcast ? "4096" : "512"
  memory                   = local.fcvm_fleet_broadcast ? "8192" : "1024"
  execution_role_arn       = aws_iam_role.fcvm_fleet_prod_coordinator_execution[0].arn
  task_role_arn            = aws_iam_role.fcvm_fleet_prod_coordinator_task[0].arn
  # The prod promotion (plan A4) changes only the image, as staging's release function does.
  track_latest = true
  tags         = { Project = "fcvm-fleet", Fleet = local.fcvm_fleet_prod_stack.fleet }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name                   = "coordinator"
      image                  = nonsensitive(aws_ssm_parameter.fcvm_fleet_prod_coordinator_release[0].value)
      essential              = true
      command                = local.fcvm_fleet_prod_coordinator_command
      readonlyRootFilesystem = true
      portMappings           = [{ containerPort = local.fcvm_fleet_dns_port, protocol = "udp" }]
      environment = [
        { name = "FLEET_STATE_BUCKET", value = aws_s3_bucket.fcvm_fleet_coordinator_state.bucket },
        { name = "FLEET_STATE_KEY", value = local.fcvm_fleet_prod_stack.state_key },
        { name = "FLEET_REGIONS", value = local.fcvm_fleet_prod_region },
        { name = "FLEET_HOST_TEMPLATES", value = join(",", local.fcvm_fleet_prod_host_templates) },
        { name = "FLEET_HOST_SUBNETS", value = join(",", local.fcvm_fleet_prod_host_subnets) },
        { name = "FLEET_HOST_ON_DEMAND_MAX", value = tostring(local.fcvm_fleet_on_demand_max) },
        { name = "FLEET_EC2_SUBNETS", value = join(" ", [for r, m in local.fcvm_fleet_prod_regional : "${r}:${join(",", m.subnet_ids)}"]) },
        { name = "FLEET_EC2_SECURITY_GROUPS", value = join(" ", [for r, m in local.fcvm_fleet_prod_regional : "${r}:${m.security_group_id}"]) },
        { name = "RUST_LOG", value = "info" },
      ]
      # ejc3/fcvm src/cli/args.rs: --turnstile-secret and --turnstile-site-key from these two variables.
      secrets = [
        { name = "FLEET_TURNSTILE_SECRET", valueFrom = "${aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].arn}:FLEET_TURNSTILE_SECRET::" },
        { name = "FLEET_TURNSTILE_SITE_KEY", valueFrom = "${aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].arn}:FLEET_TURNSTILE_SITE_KEY::" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options   = { awslogs-group = aws_cloudwatch_log_group.fcvm_fleet_prod_coordinator[0].name, awslogs-region = local.fcvm_fleet_home_region, awslogs-stream-prefix = "coordinator" }
      }
    },
    {
      name                   = "cloudflared"
      image                  = "cloudflare/cloudflared:${local.cloudflared_version}"
      essential              = true
      command                = ["tunnel", "--no-autoupdate", "run"]
      readonlyRootFilesystem = true
      secrets                = [{ name = "TUNNEL_TOKEN", valueFrom = "${aws_secretsmanager_secret.fcvm_fleet_prod_coordinator_tunnel[0].arn}:TUNNEL_TOKEN::" }]
      logConfiguration = {
        logDriver = "awslogs"
        options   = { awslogs-group = aws_cloudwatch_log_group.fcvm_fleet_prod_coordinator[0].name, awslogs-region = local.fcvm_fleet_home_region, awslogs-stream-prefix = "cloudflared" }
      }
    },
  ])
  lifecycle {
    precondition {
      condition = can(regex(
        "^${replace(aws_ecr_repository.fcvm_fleet_coordinator.repository_url, ".", "\\.")}@sha256:[0-9a-f]{64}$",
        nonsensitive(aws_ssm_parameter.fcvm_fleet_prod_coordinator_release[0].value),
      ))
      error_message = "The prod coordinator release must be an immutable digest in its own ECR repository."
    }
    # --host-spread az places each host in a zone no live host is in; with two zones a replacement can only go to the
    # zone just reclaimed. us-east-1 offers g4dn.metal in five of its six zones.
    precondition {
      condition     = length(local.fcvm_fleet_prod_host_subnets) >= local.fcvm_fleet_prod_min_host_zones
      error_message = "The prod hosts need subnets in at least three us-east-1 zones that offer the host type (FLEET_HOST_SUBNETS)."
    }
  }
}

resource "aws_ecs_service" "fcvm_fleet_prod_coordinator" {
  count = local.fcvm_fleet_prod
  # The listener attaches the target group; the credentials and grants must exist before the first task reads them.
  depends_on = [
    ephemeral.aws_ssm_parameter.fcvm_fleet_prod_coordinator_release_guard,
    aws_lb_listener.fcvm_fleet_prod_dns,
    terraform_data.fcvm_fleet_prod_tls_bootstrap,
    aws_secretsmanager_secret_version.fcvm_fleet_prod_broadcast,
    aws_secretsmanager_secret_version.fcvm_fleet_prod_turnstile,
    aws_secretsmanager_secret_version.fcvm_fleet_prod_coordinator_tunnel,
    aws_iam_role_policy.fcvm_fleet_prod_coordinator_task,
    aws_iam_role_policy.fcvm_fleet_prod_coordinator_execution_secret,
    aws_iam_role_policy_attachment.fcvm_fleet_prod_coordinator_execution,
    aws_iam_role_policy_attachment.fcvm_fleet_prod_coordinator_launch,
    aws_iam_role_policy_attachment.fcvm_fleet_prod_validated_images,
    aws_iam_role_policy_attachment.fcvm_fleet_stack_scope,
  ]
  provider        = aws.fcvm_fleet
  name            = local.fcvm_fleet_prod_coordinator
  cluster         = aws_ecs_cluster.fcvm_fleet_prod[0].id
  task_definition = aws_ecs_task_definition.fcvm_fleet_prod_coordinator[0].arn
  launch_type     = "FARGATE"
  # One task: the coordinator is the single authority for its fleet's state.
  desired_count                      = 1
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  # The coordinator owns EC2 workers outside Terraform. Drain its pool before removing the service.
  lifecycle {
    prevent_destroy = true
  }

  network_configuration {
    subnets          = local.fcvm_fleet_prod_dns_subnets
    security_groups  = [aws_security_group.fcvm_fleet_prod_coordinator[0].id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.fcvm_fleet_prod_dns[0].arn
    container_name   = "coordinator"
    container_port   = local.fcvm_fleet_dns_port
  }
  health_check_grace_period_seconds = 60

  tags = { Fleet = local.fcvm_fleet_prod_stack.fleet }
}

output "fcvm_fleet_prod" {
  description = "The prod fcvm fleet (off until fcvm_fleet_prod_enabled): cluster, service, pointers and the launch names' subzone"
  value = var.fcvm_fleet_prod_enabled ? {
    cluster              = aws_ecs_cluster.fcvm_fleet_prod[0].name
    service              = aws_ecs_service.fcvm_fleet_prod_coordinator[0].name
    state_key            = local.fcvm_fleet_prod_stack.state_key
    tls_secret           = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].name
    ami_release          = aws_ssm_parameter.fcvm_fleet_prod_ami_release[0].name
    coordinator_release  = aws_ssm_parameter.fcvm_fleet_prod_coordinator_release[0].name
    host_templates       = local.fcvm_fleet_prod_host_templates
    tile_template_arns   = local.fcvm_fleet_prod_tile_template_arns
    names_zone           = local.fcvm_fleet_prod_names_zone
    names_ns_addresses   = { for i, n in local.fcvm_fleet_prod_names_ns : n => aws_eip.fcvm_fleet_prod_dns[i].public_ip }
    broadcast_channel_id = one(aws_ivs_channel.fcvm_fleet_prod_broadcast[*].id)
  } : null
}
