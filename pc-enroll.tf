# PC enrollment: a Windows PC gets its Cloudflare connector token from a person's Cloudflare Access login on the PC, not
# from a reverse tunnel and a jumpbox run. The PC's script (scripts/windows/enroll.ps1, the next pull request) runs
# `cloudflared access login` against this hostname, then POSTs its SSH host key here with the Access token it got:
#
#   enroll.cc-games.dev  -> Cloudflare Access (the owner only) -> API Gateway (regional custom domain) -> Lambda pc-enroll
#
#   GET  /pcs                         the PCs this person may enroll, with their pinned host-key fingerprints
#   POST /pcs/<pc>/enroll             {host_key, user} -> the PC's connector token, its clients' public keys, its hostname
#   POST /pcs/<pc>/host-key/replace   an owner replaces a PC's pinned host key (a reinstalled PC)
#
# Access is the first gate. The custom domain's origin is still reachable without Cloudflare (disable_execute_api_endpoint
# removes only API Gateway's default address), so the Lambda verifies the Access token itself on every request: RS256 by
# the team's keys, this application's AUD, the team issuer, the time, and an email the PC lists. That check is the
# boundary (scripts/pc-enroll.mjs, tests in scripts/test-pc-enroll.mjs).
#
# What a PC may be given is fixed here: a PC is enrollable only if it is in local.pc_enroll_pcs, which names its existing
# tunnel's connector-token secret (built per PC, e.g. colin-pc-ssh.tf), its hostname, and its client slots. Enrollment
# creates nothing in Cloudflare. A first enrollment pins the PC's host key in SSM only when none is pinned; this file
# seeds the pin from the key mac-ssh-client.tf already trusts, so an enrollment must present that same key. Only an owner
# replaces a pin. Client keys are read from SSM slots that only trusted processes write (the next pull request); until a
# PC's slots all hold a key, its enrollment is refused (409) before anything is written.
#
# The Lambda's role reads exactly these PCs' connector-token secrets (whose resource policies admit it by name), reads
# exactly their client slots and pins, and writes only the pins. A compromised Lambda could hand out these PCs' connector
# tokens (a rogue connector can take or break that PC's tunnel traffic, but Access still gates every connection) and
# change a pin (clients then refuse the real PC); it cannot create a client key, a tunnel or an Access policy.

locals {
  pc_enroll_hostname = "enroll.cc-games.dev"
  pc_enroll_name     = "pc-enroll"
  # Who may enroll and who may replace a pin, per PC. The owner only (v3 design, 2026-10-20).
  pc_enroll_owners = [local.browser_manager_owner]

  pc_enroll_pcs = {
    colin-pc = {
      secret_arn = aws_secretsmanager_secret.colin_pc_ssh_tunnel.arn
      hostname   = local.colin_pc_ssh_hostname
      host_key   = local.mac_ssh_macs["colin-pc"].host_key
      # <box>-<account>: the client accounts colin-pc-ssh.tf gives a service token, each publishing its ~/.ssh/to-colin-pc.pub.
      clients = ["fcvm-ubuntu", "nextjs-colin"]
    }
  }

  pc_enroll_host_key_params = { for pc, _ in local.pc_enroll_pcs : pc => "/pc-ssh/${pc}/host-key" }
  pc_enroll_client_params   = { for pc, c in local.pc_enroll_pcs : pc => { for slot in c.clients : slot => "/pc-ssh/${pc}/clients/${slot}" } }
  pc_enroll_param_arn       = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter"

  pc_enroll_config = {
    teamDomain = "https://ejc3.cloudflareaccess.com"
    aud        = cloudflare_zero_trust_access_application.pc_enroll.aud
    pcs = { for pc, c in local.pc_enroll_pcs : pc => {
      secret_arn     = c.secret_arn
      hostname       = c.hostname
      host_key_param = local.pc_enroll_host_key_params[pc]
      approvers      = local.pc_enroll_owners
      owners         = local.pc_enroll_owners
      clients        = local.pc_enroll_client_params[pc]
    } }
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# Cloudflare: Access in front, the hostname, and Full (strict) TLS to the origin
# ---------------------------------------------------------------------------------------------------------------------

resource "cloudflare_zero_trust_access_application" "pc_enroll" {
  account_id       = var.cloudflare_account_id
  name             = "PC enrollment"
  domain           = local.pc_enroll_hostname
  type             = "self_hosted"
  session_duration = "1h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )

  # The policy attachment MUST be declared here; see cc_games_dev in cloudflare.tf. A person only: no service token.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.browser_manager_owner.id
      precedence = 1
    },
  ]
}

# Overrides the *.cc-games.dev wildcard (which points at the nextjs-dev tunnel). After the Access application, so the name
# never answers without it.
resource "cloudflare_dns_record" "pc_enroll" {
  zone_id = var.cc_games_zone_id
  name    = local.pc_enroll_hostname
  type    = "CNAME"
  content = aws_apigatewayv2_domain_name.pc_enroll.domain_name_configuration[0].target_domain_name
  proxied = true
  ttl     = 1
  comment = "PC enrollment (pc-enroll.tf); Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.pc_enroll,
    cloudflare_ruleset.cc_games_dev_config,
    aws_apigatewayv2_api_mapping.pc_enroll,
  ]
}

# The zone's SSL mode is not managed here, and Flexible or Full would let Cloudflare reach the origin without checking its
# certificate, while the response carries a connector token. This host gets Full (strict) whatever the zone says. The zone
# has one config-rules entrypoint: if one exists outside Terraform, this create fails rather than leaving TLS unchecked.
resource "cloudflare_ruleset" "cc_games_dev_config" {
  zone_id     = var.cc_games_zone_id
  name        = "cc-games.dev config rules"
  description = "Per-host settings (pc-enroll.tf)"
  kind        = "zone"
  phase       = "http_config_settings"

  rules = [
    {
      ref               = "pc_enroll_ssl_strict"
      description       = "enroll.cc-games.dev: Full (strict) to its API Gateway origin"
      expression        = "(http.host eq \"${local.pc_enroll_hostname}\")"
      action            = "set_config"
      action_parameters = { ssl = "strict" }
      enabled           = true
    },
  ]
}

# ---------------------------------------------------------------------------------------------------------------------
# API Gateway: one regional custom domain, no default endpoint, three routes, tight throttling
# ---------------------------------------------------------------------------------------------------------------------

resource "aws_acm_certificate" "pc_enroll" {
  domain_name       = local.pc_enroll_hostname
  validation_method = "DNS"
  tags              = { Name = local.pc_enroll_hostname }
  lifecycle {
    create_before_destroy = true
  }
}

# DNS-only: Cloudflare must hand ACM the literal CNAME, not proxy it.
resource "cloudflare_dns_record" "pc_enroll_acm_validation" {
  zone_id = var.cc_games_zone_id
  name    = trimsuffix(one(aws_acm_certificate.pc_enroll.domain_validation_options).resource_record_name, ".")
  type    = one(aws_acm_certificate.pc_enroll.domain_validation_options).resource_record_type
  content = trimsuffix(one(aws_acm_certificate.pc_enroll.domain_validation_options).resource_record_value, ".")
  proxied = false
  ttl     = 300
  comment = "ACM DNS validation for enroll.cc-games.dev (pc-enroll.tf)"
}

resource "aws_acm_certificate_validation" "pc_enroll" {
  certificate_arn         = aws_acm_certificate.pc_enroll.arn
  validation_record_fqdns = [cloudflare_dns_record.pc_enroll_acm_validation.name]
}

resource "aws_apigatewayv2_api" "pc_enroll" {
  name                         = local.pc_enroll_name
  protocol_type                = "HTTP"
  disable_execute_api_endpoint = true
}

resource "aws_apigatewayv2_domain_name" "pc_enroll" {
  domain_name = local.pc_enroll_hostname
  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.pc_enroll.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_stage" "pc_enroll" {
  api_id      = aws_apigatewayv2_api.pc_enroll.id
  name        = "$default"
  auto_deploy = true
  # A handful of enrollments a year: this bounds what anyone reaching the public origin directly can cost.
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 1
  }
}

resource "aws_apigatewayv2_api_mapping" "pc_enroll" {
  api_id      = aws_apigatewayv2_api.pc_enroll.id
  domain_name = aws_apigatewayv2_domain_name.pc_enroll.id
  stage       = aws_apigatewayv2_stage.pc_enroll.id
}

resource "aws_apigatewayv2_integration" "pc_enroll" {
  api_id                 = aws_apigatewayv2_api.pc_enroll.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.pc_enroll.invoke_arn
  payload_format_version = "2.0"
  depends_on             = [aws_lambda_permission.pc_enroll]
}

resource "aws_apigatewayv2_route" "pc_enroll" {
  for_each  = toset(["GET /pcs", "POST /pcs/{pc}/enroll", "POST /pcs/{pc}/host-key/replace"])
  api_id    = aws_apigatewayv2_api.pc_enroll.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.pc_enroll.id}"
}

# ---------------------------------------------------------------------------------------------------------------------
# The Lambda and its exact grants
# ---------------------------------------------------------------------------------------------------------------------

data "archive_file" "pc_enroll" {
  type        = "zip"
  source_file = "${path.module}/scripts/pc-enroll.mjs"
  output_path = "${path.module}/.terraform/pc-enroll.zip"
}

resource "aws_cloudwatch_log_group" "pc_enroll" {
  name              = "/aws/lambda/${local.pc_enroll_name}"
  retention_in_days = 365
}

resource "aws_iam_role" "pc_enroll" {
  name = local.pc_enroll_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = local.pc_enroll_name }
}

resource "aws_iam_role_policy" "pc_enroll" {
  name = local.pc_enroll_name
  role = aws_iam_role.pc_enroll.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "TheEnrollablePcsConnectorTokens"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = [for pc, c in local.pc_enroll_pcs : c.secret_arn]
      },
      {
        Sid    = "ReadTheirPinsAndClientSlots"
        Effect = "Allow"
        Action = "ssm:GetParameter"
        Resource = concat(
          [for pc, p in local.pc_enroll_host_key_params : "${local.pc_enroll_param_arn}${p}"],
          flatten([for pc, slots in local.pc_enroll_client_params : [for slot, p in slots : "${local.pc_enroll_param_arn}${p}"]]),
        )
      },
      {
        # The pins only; the code writes one only when absent unless an owner replaces it.
        Sid      = "WriteTheirPins"
        Effect   = "Allow"
        Action   = "ssm:PutParameter"
        Resource = [for pc, p in local.pc_enroll_host_key_params : "${local.pc_enroll_param_arn}${p}"]
      },
      {
        Sid      = "ItsOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.pc_enroll.arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "pc_enroll" {
  function_name                  = local.pc_enroll_name
  description                    = "PC enrollment behind Access on enroll.cc-games.dev (pc-enroll.tf)"
  role                           = aws_iam_role.pc_enroll.arn
  handler                        = "pc-enroll.handler"
  runtime                        = "nodejs22.x"
  architectures                  = ["arm64"]
  memory_size                    = 256
  timeout                        = 15
  reserved_concurrent_executions = 2
  filename                       = data.archive_file.pc_enroll.output_path
  source_code_hash               = data.archive_file.pc_enroll.output_base64sha256

  environment {
    variables = {
      PC_ENROLL_CONFIG = jsonencode(local.pc_enroll_config)
    }
  }

  depends_on = [aws_cloudwatch_log_group.pc_enroll]
  tags       = { Name = local.pc_enroll_name }
}

# Only this API's routes may invoke it.
resource "aws_lambda_permission" "pc_enroll" {
  statement_id  = "AllowPcEnrollApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pc_enroll.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.pc_enroll.execution_arn}/*/*"
}

# The pins, seeded from the host keys mac-ssh-client.tf already trusts. After creation the pin belongs to enrollment (an
# owner's replace is logged by the Lambda), so Terraform does not put it back.
resource "aws_ssm_parameter" "pc_enroll_host_key" {
  for_each    = local.pc_enroll_pcs
  name        = local.pc_enroll_host_key_params[each.key]
  description = "${each.key}'s pinned OpenSSH host key (pc-enroll.tf); replaced only by an owner through enrollment"
  type        = "String"
  value       = each.value.host_key
  tags        = { Name = local.pc_enroll_host_key_params[each.key] }
  lifecycle {
    ignore_changes = [value]
  }
}

output "pc_enroll" {
  description = "PC enrollment (no credentials)"
  value = {
    url             = "https://${local.pc_enroll_hostname}"
    pcs             = keys(local.pc_enroll_pcs)
    host_key_params = local.pc_enroll_host_key_params
    client_params   = local.pc_enroll_client_params
  }
}
