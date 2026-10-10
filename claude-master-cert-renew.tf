# claude-master-cert-renew.tf -- no claude-master client certificate ever expires.
#
# WHY. Client certificates last 30 days on purpose (at most 90: a lost box expires on its own), and renewing one was a person
# running scripts/claude-master-enroll.sh before the date. Nobody should have to remember that; a missed renewal is an outage
# for every session on the box. Renewal is now a daily job, and the CA (ten years) and the server's own certificate (renewed
# in the proxy) need nothing.
#
# WHAT. scripts/claude-master-cert-renew.py, once a day: read each client's certificate end date; under RENEW_BEFORE_DAYS (14)
# left, make a new key and request on the client, have the server sign it, verify and swap it in on the client. The key never
# leaves the client. It works on an expired certificate too, so a stopped box catches up when it is next running.
#
# HOW IT STAYS NARROW. The Lambda cannot run arbitrary commands anywhere. Its role may send exactly FOUR custom SSM documents
# (scripts/ssm/claude-master-cert-*.sh), every parameter pinned by a regex, to instances named claude-master-server, nextjs-dev
# or fcvm-metal-arm. The signing document signs only for the name the caller asks for: it refuses a request whose own CN is
# different. The direction is admin -> box, like every other automation here; nothing on a dev box can reach this role.
#
# WHAT IT DOES NOT DO. Enrol a new client (an account with no certificate is skipped: "enrolling is the switch" stays a
# person's decision), touch a stopped box, or restart anything.
#
# WATCHING THE WATCHER. Every run publishes ClientCertDaysLeft per client (namespace ClaudeMasterCerts). Alarms: the function
# errors, it has not run for two days, or a certificate has under 7 days left (that one still fires if the function has died,
# because the metric it watches was last published by a run that already knew the date).
#
# `aws lambda invoke --function-name claude-master-cert-renew --payload '{"dry_run": true}' ...` shows what it would do;
# `{"renew_before_days": 60}` forces a renewal of anything with under 60 days (proves the whole path).

locals {
  # name = the certificate name the server sees (claude-master-enroll.sh NAME); instance = the Name tag of the box;
  # account = the Unix account whose ~/.config/claude-master holds the certificate. Add a client here when it is enrolled.
  claude_master_clients = {
    "nextjs-colton" = { instance = "nextjs-dev", account = "colton" }
    "nextjs-connor" = { instance = "nextjs-dev", account = "connor" }
    "nextjs-colin"  = { instance = "nextjs-dev", account = "colin" }
    "nextjs-ejc3"   = { instance = "nextjs-dev", account = "ejc3" }
    "fcvm-arm"      = { instance = "fcvm-metal-arm", account = "ubuntu" }
  }
  claude_master_cert_instance_names = distinct(concat(["claude-master-server"], [for c in values(local.claude_master_clients) : c.instance]))

  # Every parameter a document accepts, and the only shape it accepts it in.
  cert_account_pattern = "^[a-z_][a-z0-9_-]{0,31}$"
  cert_name_pattern    = "^[a-z0-9][a-z0-9-]{0,62}$"
  # SSM's regex engine (RE2) refuses a repeat count over 1000, so a length bound cannot live here: the pattern pins the ALPHABET
  # (base64, nothing a shell could act on) and the scripts bound the size (a request at most 4096 characters, a certificate or CA at most 8192).
  cert_b64_csr_pattern = "^[A-Za-z0-9+/=]+$"
  cert_b64_pem_pattern = "^[A-Za-z0-9+/=]+$"
  cert_days_pattern    = "^[0-9]{1,2}$"

  claude_master_cert_documents = {
    "claude-master-cert-status" = {
      script      = "claude-master-cert-status.sh"
      description = "Read-only: the end date of an account's claude-master client certificate."
      parameters = {
        Account = { type = "String", description = "Unix account", allowedPattern = local.cert_account_pattern }
      }
    }
    "claude-master-cert-request" = {
      script      = "claude-master-cert-request.sh"
      description = "Make a new claude-master client key and certificate request for an account (the live directory is untouched)."
      parameters = {
        Account = { type = "String", description = "Unix account", allowedPattern = local.cert_account_pattern }
        Name    = { type = "String", description = "Client certificate name", allowedPattern = local.cert_name_pattern }
      }
    }
    "claude-master-cert-sign" = {
      script      = "claude-master-cert-sign.sh"
      description = "On the server: sign a client request, only for the exact name asked for."
      parameters = {
        Name = { type = "String", description = "Client certificate name; the request's CN must equal it", allowedPattern = local.cert_name_pattern }
        Csr  = { type = "String", description = "Base64 certificate request", allowedPattern = local.cert_b64_csr_pattern }
        Days = { type = "String", description = "Lifetime in days (1-90)", allowedPattern = local.cert_days_pattern, default = "30" }
      }
    }
    "claude-master-cert-install" = {
      script      = "claude-master-cert-install.sh"
      description = "Verify a renewed claude-master client certificate and swap it in atomically."
      parameters = {
        Account = { type = "String", description = "Unix account", allowedPattern = local.cert_account_pattern }
        Name    = { type = "String", description = "Client certificate name", allowedPattern = local.cert_name_pattern }
        Cert    = { type = "String", description = "Base64 client certificate PEM", allowedPattern = local.cert_b64_pem_pattern }
        Ca      = { type = "String", description = "Base64 CA certificate PEM", allowedPattern = local.cert_b64_pem_pattern }
        MinDays = { type = "String", description = "Refuse a certificate that lasts fewer days", allowedPattern = local.cert_days_pattern, default = "20" }
      }
    }
  }
}

resource "aws_ssm_document" "claude_master_cert" {
  for_each = local.claude_master_cert_documents

  name            = each.key
  document_type   = "Command"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = each.value.description
    parameters    = each.value.parameters
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "run"
      inputs = { runCommand = split("\n", trimsuffix(file("${path.module}/scripts/ssm/${each.value.script}"), "\n")) }
    }]
  })

  tags = { Name = each.key }
}

data "archive_file" "claude_master_cert_renew" {
  type        = "zip"
  output_path = "${path.module}/.terraform/claude-master-cert-renew.zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/claude-master-cert-renew.py")
  }
}

resource "aws_iam_role" "claude_master_cert_renew" {
  name = "claude-master-cert-renew"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "claude_master_cert_renew_basic" {
  role       = aws_iam_role.claude_master_cert_renew.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "claude_master_cert_renew" {
  name = "claude-master-cert-renew"
  role = aws_iam_role.claude_master_cert_renew.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "FindTheBoxes"
        Effect   = "Allow"
        Action   = "ec2:DescribeInstances"
        Resource = "*"
      },
      {
        # Exactly the four documents above, and no other: not AWS-RunShellScript, not any other custom document.
        Sid      = "SendOnlyTheFourCertificateDocuments"
        Effect   = "Allow"
        Action   = "ssm:SendCommand"
        Resource = [for name in keys(local.claude_master_cert_documents) : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:document/${name}"]
      },
      {
        # ...and only to the server and the client boxes, by Name tag.
        Sid      = "OnlyToTheServerAndTheClientBoxes"
        Effect   = "Allow"
        Action   = "ssm:SendCommand"
        Resource = "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = { "ssm:resourceTag/Name" = local.claude_master_cert_instance_names }
        }
      },
      {
        # Reading a result back cannot be scoped to a resource.
        Sid      = "ReadTheResults"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
        Resource = "*"
      },
      {
        Sid       = "PublishDaysLeft"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "ClaudeMasterCerts" } }
      },
      {
        Sid      = "Tell"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
    ]
  })
}

resource "aws_lambda_function" "claude_master_cert_renew" {
  function_name                  = "claude-master-cert-renew"
  role                           = aws_iam_role.claude_master_cert_renew.arn
  handler                        = "index.lambda_handler"
  runtime                        = "python3.12"
  timeout                        = 600
  reserved_concurrent_executions = 1 # two renewals of one client at once would race on its pending key
  filename                       = data.archive_file.claude_master_cert_renew.output_path
  source_code_hash               = data.archive_file.claude_master_cert_renew.output_base64sha256

  environment {
    variables = {
      CLIENTS             = jsonencode([for name, c in local.claude_master_clients : { name = name, instance = c.instance, account = c.account }])
      SERVER_NAME         = "claude-master-server"
      RENEW_BEFORE_DAYS   = "14"
      CERT_DAYS           = "30"
      MIN_DAYS_ON_INSTALL = "20"
      SNS_TOPIC_ARN       = aws_sns_topic.cost_alerts.arn
    }
  }

  depends_on = [aws_ssm_document.claude_master_cert]
  tags       = { Name = "claude-master-cert-renew" }
}

resource "aws_cloudwatch_event_rule" "claude_master_cert_renew" {
  name                = "claude-master-cert-renew-daily"
  description         = "Renew any claude-master client certificate with under two weeks left"
  schedule_expression = "cron(0 8 * * ? *)"
}

resource "aws_cloudwatch_event_target" "claude_master_cert_renew" {
  rule = aws_cloudwatch_event_rule.claude_master_cert_renew.name
  arn  = aws_lambda_function.claude_master_cert_renew.arn
}

# No automatic retry: it runs again tomorrow, with two weeks of margin, and a retry of a half-done renewal is what the
# per-client pending directory on the box exists to make harmless anyway.
resource "aws_lambda_function_event_invoke_config" "claude_master_cert_renew" {
  function_name          = aws_lambda_function.claude_master_cert_renew.function_name
  maximum_retry_attempts = 0
}

resource "aws_lambda_permission" "claude_master_cert_renew" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.claude_master_cert_renew.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.claude_master_cert_renew.arn
}

resource "aws_cloudwatch_metric_alarm" "claude_master_cert_renew_errors" {
  alarm_name          = "claude-master-cert-renew-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 86400
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "The claude-master certificate renewal job failed for at least one client. Certificates are not expired yet; it retries daily."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.claude_master_cert_renew.function_name }
}

resource "aws_cloudwatch_metric_alarm" "claude_master_cert_renew_not_running" {
  alarm_name          = "claude-master-cert-renew-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 86400
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The claude-master certificate renewal job has not run for two days (it runs daily)."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "breaching"
  dimensions          = { FunctionName = aws_lambda_function.claude_master_cert_renew.function_name }
}

# The last line of defence: a certificate that is actually about to expire, whatever the job is doing. A box that is stopped
# publishes nothing (missing data is ignored), and the job's own health is watched by the two alarms above.
resource "aws_cloudwatch_metric_alarm" "claude_master_cert_expiring" {
  for_each = local.claude_master_clients

  alarm_name          = "claude-master-cert-expiring-${each.key}"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ClientCertDaysLeft"
  namespace           = "ClaudeMasterCerts"
  period              = 86400
  statistic           = "Minimum"
  threshold           = 7
  alarm_description   = "The claude-master client certificate ${each.key} has under 7 days left and the renewal job has not replaced it."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { Client = each.key }
}
