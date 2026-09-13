# GitHub's workflow_job deliveries reach the runner webhook through this front function.
#
# The webhook runs one execution at a time (reserved_concurrent_executions = 1 since
# 60f3782): concurrent invocations each read the same runner count before launching and put
# up more instances than MAX_RUNNERS. Straight behind API Gateway, a delivery that arrived
# while that execution was busy was throttled, API Gateway answered GitHub
# {"message":"Service Unavailable"}, and GitHub does not redeliver. From 2026-09-11 04:15Z
# to 2026-09-13 16:20Z, 69 of the 80 queued deliveries GitHub retained were lost that way or
# to its 10-second timeout.
#
# The front verifies the signature, keeps only what the webhook reads, answers 202, and
# invokes the webhook asynchronously. Lambda puts a throttled asynchronous event back in its
# queue and retries it, so a burst waits instead of being dropped, and the webhook still
# decides every launch one execution at a time.

data "archive_file" "runner_webhook_front" {
  type        = "zip"
  output_path = "${path.module}/.terraform/runner-webhook-front.zip"

  source {
    content  = <<-EOF
      import base64
      import hashlib
      import hmac
      import json
      import os
      import time

      import boto3

      lambda_client = boto3.client('lambda', region_name='us-west-1')

      # The webhook acts on these two; every other workflow_job action is answered here.
      FORWARDED_ACTIONS = ('queued', 'completed')
      # The workflow_job fields the webhook and github-runner-reuse read. Nothing else in a
      # delivery is forwarded, so a top-level launch_count, claim_only or queued_jobs - the
      # fields the controller's own invokes use to carry trust - never reaches the webhook
      # from here. The webhook refuses them on this path anyway; see DELIVERY_ALIAS there.
      JOB_FIELDS = ('id', 'run_id', 'labels', 'created_at', 'runner_id', 'runner_name',
                    'conclusion', 'completed_at')

      def verify_signature(body, signature, secret):
          """GitHub's X-Hub-Signature-256 over the raw body. No secret rejects everything."""
          if not secret or not isinstance(signature, str) or not signature.startswith('sha256='):
              return False
          expected = 'sha256=' + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
          return hmac.compare_digest(expected, signature)

      def handler(event, context):
          # Payload format 1.0 keeps GitHub's header casing, so headers are matched
          # case-insensitively (X-Hub-Signature-256).
          headers = {str(k).lower(): v for k, v in (event.get('headers') or {}).items()}
          body = event.get('body') or ''
          if event.get('isBase64Encoded'):
              try:
                  body = base64.b64decode(body).decode('utf-8')
              except (ValueError, UnicodeError):
                  return {'statusCode': 400, 'body': 'Unreadable body'}
          if not verify_signature(body, headers.get('x-hub-signature-256'),
                                  os.environ.get('WEBHOOK_SECRET', '')):
              return {'statusCode': 401, 'body': 'Invalid signature'}
          try:
              payload = json.loads(body)
          except ValueError:
              return {'statusCode': 400, 'body': 'Body is not JSON'}
          if not isinstance(payload, dict):
              return {'statusCode': 400, 'body': 'Body is not a JSON object'}
          action = payload.get('action')
          if action not in FORWARDED_ACTIONS:
              return {'statusCode': 200, 'body': f'Ignoring action: {action}'}
          job = payload.get('workflow_job')
          job = job if isinstance(job, dict) else {}
          delivery = str(headers.get('x-github-delivery', ''))[:64]
          event_for_webhook = {
              'body': json.dumps({'action': action,
                                  'workflow_job': {key: job[key] for key in JOB_FIELDS if key in job}}),
              'headers': {},
              'delivery': {'id': delivery, 'received_at': time.time()},
          }
          try:
              lambda_client.invoke(FunctionName=os.environ['DELIVERY_TARGET'], InvocationType='Event',
                                   Payload=json.dumps(event_for_webhook))
          except Exception as e:
              print(f'Delivery {delivery} not queued for the webhook: {type(e).__name__}')
              return {'statusCode': 503, 'body': 'Delivery not queued'}
          return {'statusCode': 202, 'body': f'Forwarded {action} to the runner webhook'}
    EOF
    filename = "lambda_function.py"
  }
}

resource "aws_iam_role" "runner_webhook_front" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-webhook-front-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# Its own logs and the webhook's delivery alias, nothing else: no EC2, SSM, DynamoDB, PAT or
# other function. The deny keeps that true under any policy attached to this role later.
resource "aws_iam_role_policy" "runner_webhook_front" {
  count = var.enable_github_runner ? 1 : 0
  name  = "forward-verified-deliveries"
  role  = aws_iam_role.runner_webhook_front[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OwnLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/github-runner-webhook-front",
          "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/github-runner-webhook-front:*",
        ]
      },
      {
        Sid      = "InvokeTheWebhookDeliveryAlias"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_alias.runner_webhook_delivery[0].arn
      },
      {
        Sid         = "DenyInvokingAnythingElse"
        Effect      = "Deny"
        Action      = ["lambda:InvokeFunction", "lambda:InvokeAsync", "lambda:InvokeFunctionUrl"]
        NotResource = aws_lambda_alias.runner_webhook_delivery[0].arn
      },
    ]
  })
}

resource "aws_lambda_function" "runner_webhook_front" {
  count            = var.enable_github_runner ? 1 : 0
  filename         = data.archive_file.runner_webhook_front.output_path
  source_code_hash = data.archive_file.runner_webhook_front.output_base64sha256
  function_name    = "github-runner-webhook-front"
  role             = aws_iam_role.runner_webhook_front[0].arn
  handler          = "lambda_function.handler"
  runtime          = "python3.12"
  timeout          = 5

  # Sized for a burst. One CI run delivered about 20 events in 10 seconds on 2026-09-13,
  # 9 of them within 0.32 seconds at 14:10:04Z. With 20 executions a burst of 20 arriving
  # together runs at once even if every execution starts cold. It also bounds how fast a
  # flood of signed deliveries can fill the webhook's queue.
  reserved_concurrent_executions = 20

  environment {
    variables = {
      WEBHOOK_SECRET  = random_password.github_webhook[0].result
      DELIVERY_TARGET = aws_lambda_alias.runner_webhook_delivery[0].arn
    }
  }

  tags = {
    Name = "github-runner-webhook-front"
  }

  depends_on = [aws_iam_role_policy.runner_webhook_front]
}

resource "aws_lambda_permission" "runner_webhook_front" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_webhook_front[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.runner_webhook[0].execution_arn}/*/*"
}

# Forwarded deliveries enter the webhook through this alias and nothing else does. IAM lets
# only the front invoke it, the controller's own invokes (the cleanup poll, github-runner-reuse)
# use the unqualified function, and the webhook reads which one was invoked from
# invoked_function_arn: arriving here makes an event a public delivery, so its launch_count
# and claim_only are refused whatever the payload says. It points at $LATEST, so it always
# runs the code Terraform deployed, and reserved concurrency belongs to the function, so the
# alias shares the webhook's one execution.
resource "aws_lambda_alias" "runner_webhook_delivery" {
  count            = var.enable_github_runner ? 1 : 0
  name             = "delivery"
  function_name    = aws_lambda_function.runner_webhook[0].function_name
  function_version = "$LATEST"
}

# The same settings as the unqualified function's config, for the same reasons. No retry
# after a function error: a retry after a partial launch can overshoot the cap. Throttles are
# not errors; Lambda keeps retrying a throttled event, with backoff, until it is 300 seconds
# old, and by then the cleanup poll has run. A late queued delivery does not rely on that age:
# the webhook skips a forwarded job created before the start of a complete queue scan that
# counted it. A late completed delivery is dropped sooner still, by its 90-second reuse window,
# which the webhook checks before handing it on and github-runner-reuse checks again.
resource "aws_lambda_function_event_invoke_config" "runner_webhook_delivery" {
  count                        = var.enable_github_runner ? 1 : 0
  function_name                = aws_lambda_function.runner_webhook[0].function_name
  qualifier                    = aws_lambda_alias.runner_webhook_delivery[0].name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}
