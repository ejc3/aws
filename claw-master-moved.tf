# claw-master-moved.tf
#
# The Terraform addresses were renamed claude_master -> claw_master with the project (2026-10-11). These blocks move
# the existing state, so nothing is replaced: every AWS and Cloudflare name stays as it was (AGENTS.md lists them).
# They can be removed once every working copy has applied them.

moved {
  from = aws_cloudwatch_dashboard.claude_master
  to   = aws_cloudwatch_dashboard.claw_master
}

moved {
  from = aws_cloudwatch_event_rule.claude_master_cert_renew
  to   = aws_cloudwatch_event_rule.claw_master_cert_renew
}

moved {
  from = aws_cloudwatch_event_target.claude_master_cert_renew
  to   = aws_cloudwatch_event_target.claw_master_cert_renew
}

moved {
  from = aws_cloudwatch_log_group.claude_master_server
  to   = aws_cloudwatch_log_group.claw_master_server
}

moved {
  from = aws_cloudwatch_log_metric_filter.claude_master_server
  to   = aws_cloudwatch_log_metric_filter.claw_master_server
}

moved {
  from = aws_cloudwatch_metric_alarm.claude_master_cert_expiring
  to   = aws_cloudwatch_metric_alarm.claw_master_cert_expiring
}

moved {
  from = aws_cloudwatch_metric_alarm.claude_master_cert_renew_errors
  to   = aws_cloudwatch_metric_alarm.claw_master_cert_renew_errors
}

moved {
  from = aws_cloudwatch_metric_alarm.claude_master_cert_renew_not_running
  to   = aws_cloudwatch_metric_alarm.claw_master_cert_renew_not_running
}

moved {
  from = aws_cloudwatch_metric_alarm.claude_master_pool
  to   = aws_cloudwatch_metric_alarm.claw_master_pool
}

moved {
  from = aws_cloudwatch_metric_alarm.claude_master_server_log
  to   = aws_cloudwatch_metric_alarm.claw_master_server_log
}

moved {
  from = aws_cloudwatch_metric_alarm.claude_master_server_memory
  to   = aws_cloudwatch_metric_alarm.claw_master_server_memory
}

moved {
  from = aws_cloudwatch_metric_alarm.claude_master_server_status
  to   = aws_cloudwatch_metric_alarm.claw_master_server_status
}

moved {
  from = aws_cloudwatch_metric_alarm.claude_master_server_swap
  to   = aws_cloudwatch_metric_alarm.claw_master_server_swap
}

moved {
  from = aws_iam_access_key.claude_master_dashboard
  to   = aws_iam_access_key.claw_master_dashboard
}

moved {
  from = aws_iam_instance_profile.claude_master_server
  to   = aws_iam_instance_profile.claw_master_server
}

moved {
  from = aws_iam_policy.claude_master_backup_key_read
  to   = aws_iam_policy.claw_master_backup_key_read
}

moved {
  from = aws_iam_role.claude_master_cert_renew
  to   = aws_iam_role.claw_master_cert_renew
}

moved {
  from = aws_iam_role.claude_master_server
  to   = aws_iam_role.claw_master_server
}

moved {
  from = aws_iam_role_policy.claude_master_cert_renew
  to   = aws_iam_role_policy.claw_master_cert_renew
}

moved {
  from = aws_iam_role_policy.claude_master_server_account_labels
  to   = aws_iam_role_policy.claw_master_server_account_labels
}

moved {
  from = aws_iam_role_policy.claude_master_server_bootstrap
  to   = aws_iam_role_policy.claw_master_server_bootstrap
}

moved {
  from = aws_iam_role_policy.claude_master_server_telemetry
  to   = aws_iam_role_policy.claw_master_server_telemetry
}

moved {
  from = aws_iam_role_policy.claude_master_server_tunnel_token
  to   = aws_iam_role_policy.claw_master_server_tunnel_token
}

moved {
  from = aws_iam_role_policy.dev_server_claude_master_dashboard_access
  to   = aws_iam_role_policy.dev_server_claw_master_dashboard_access
}

moved {
  from = aws_iam_role_policy.dev_server_claude_master_logs_read
  to   = aws_iam_role_policy.dev_server_claw_master_logs_read
}

moved {
  from = aws_iam_role_policy_attachment.claude_master_backup_key_read
  to   = aws_iam_role_policy_attachment.claw_master_backup_key_read
}

moved {
  from = aws_iam_role_policy_attachment.claude_master_backup_key_read_server
  to   = aws_iam_role_policy_attachment.claw_master_backup_key_read_server
}

moved {
  from = aws_iam_role_policy_attachment.claude_master_cert_renew_basic
  to   = aws_iam_role_policy_attachment.claw_master_cert_renew_basic
}

moved {
  from = aws_iam_role_policy_attachment.claude_master_server_ssm
  to   = aws_iam_role_policy_attachment.claw_master_server_ssm
}

moved {
  from = aws_iam_user.claude_master_dashboard
  to   = aws_iam_user.claw_master_dashboard
}

moved {
  from = aws_iam_user_policy.claude_master_dashboard
  to   = aws_iam_user_policy.claw_master_dashboard
}

moved {
  from = aws_instance.claude_master_server
  to   = aws_instance.claw_master_server
}

moved {
  from = aws_lambda_function.claude_master_cert_renew
  to   = aws_lambda_function.claw_master_cert_renew
}

moved {
  from = aws_lambda_function_event_invoke_config.claude_master_cert_renew
  to   = aws_lambda_function_event_invoke_config.claw_master_cert_renew
}

moved {
  from = aws_lambda_permission.claude_master_cert_renew
  to   = aws_lambda_permission.claw_master_cert_renew
}

moved {
  from = aws_s3_object.claude_master_server_user_data
  to   = aws_s3_object.claw_master_server_user_data
}

moved {
  from = aws_secretsmanager_secret.claude_master_account_labels
  to   = aws_secretsmanager_secret.claw_master_account_labels
}

moved {
  from = aws_secretsmanager_secret.claude_master_backup_key
  to   = aws_secretsmanager_secret.claw_master_backup_key
}

moved {
  from = aws_secretsmanager_secret.claude_master_dashboard_agent_access
  to   = aws_secretsmanager_secret.claw_master_dashboard_agent_access
}

moved {
  from = aws_secretsmanager_secret.claude_master_dashboard_reader
  to   = aws_secretsmanager_secret.claw_master_dashboard_reader
}

moved {
  from = aws_secretsmanager_secret.claude_master_mac_access
  to   = aws_secretsmanager_secret.claw_master_mac_access
}

moved {
  from = aws_secretsmanager_secret.claude_master_tunnel_token
  to   = aws_secretsmanager_secret.claw_master_tunnel_token
}

moved {
  from = aws_secretsmanager_secret_policy.claude_master_account_labels
  to   = aws_secretsmanager_secret_policy.claw_master_account_labels
}

moved {
  from = aws_secretsmanager_secret_policy.claude_master_backup_key
  to   = aws_secretsmanager_secret_policy.claw_master_backup_key
}

moved {
  from = aws_secretsmanager_secret_policy.claude_master_dashboard_agent_access
  to   = aws_secretsmanager_secret_policy.claw_master_dashboard_agent_access
}

moved {
  from = aws_secretsmanager_secret_policy.claude_master_mac_access
  to   = aws_secretsmanager_secret_policy.claw_master_mac_access
}

moved {
  from = aws_secretsmanager_secret_policy.claude_master_tunnel_token
  to   = aws_secretsmanager_secret_policy.claw_master_tunnel_token
}

moved {
  from = aws_secretsmanager_secret_version.claude_master_dashboard_agent_access
  to   = aws_secretsmanager_secret_version.claw_master_dashboard_agent_access
}

moved {
  from = aws_secretsmanager_secret_version.claude_master_dashboard_reader
  to   = aws_secretsmanager_secret_version.claw_master_dashboard_reader
}

moved {
  from = aws_secretsmanager_secret_version.claude_master_mac_access
  to   = aws_secretsmanager_secret_version.claw_master_mac_access
}

moved {
  from = aws_secretsmanager_secret_version.claude_master_tunnel_token
  to   = aws_secretsmanager_secret_version.claw_master_tunnel_token
}

moved {
  from = aws_security_group.claude_master_server
  to   = aws_security_group.claw_master_server
}

moved {
  from = aws_ssm_association.claude_master_client
  to   = aws_ssm_association.claw_master_client
}

moved {
  from = aws_ssm_document.claude_master_cert
  to   = aws_ssm_document.claw_master_cert
}

moved {
  from = cloudflare_dns_record.claude_master
  to   = cloudflare_dns_record.claw_master
}

moved {
  from = cloudflare_workers_script_subdomain.claude_master_dashboard
  to   = cloudflare_workers_script_subdomain.claw_master_dashboard
}

moved {
  from = cloudflare_zero_trust_access_application.claude_master
  to   = cloudflare_zero_trust_access_application.claw_master
}

moved {
  from = cloudflare_zero_trust_access_application.claude_master_dashboard
  to   = cloudflare_zero_trust_access_application.claw_master_dashboard
}

moved {
  from = cloudflare_zero_trust_access_policy.claude_master_dashboard_agents
  to   = cloudflare_zero_trust_access_policy.claw_master_dashboard_agents
}

moved {
  from = cloudflare_zero_trust_access_policy.claude_master_dashboard_owner
  to   = cloudflare_zero_trust_access_policy.claw_master_dashboard_owner
}

moved {
  from = cloudflare_zero_trust_access_policy.claude_master_macs
  to   = cloudflare_zero_trust_access_policy.claw_master_macs
}

moved {
  from = cloudflare_zero_trust_access_service_token.claude_master_dashboard_agents
  to   = cloudflare_zero_trust_access_service_token.claw_master_dashboard_agents
}

moved {
  from = cloudflare_zero_trust_access_service_token.claude_master_macs
  to   = cloudflare_zero_trust_access_service_token.claw_master_macs
}

moved {
  from = cloudflare_zero_trust_tunnel_cloudflared.claude_master
  to   = cloudflare_zero_trust_tunnel_cloudflared.claw_master
}

moved {
  from = cloudflare_zero_trust_tunnel_cloudflared_config.claude_master
  to   = cloudflare_zero_trust_tunnel_cloudflared_config.claw_master
}

moved {
  from = random_bytes.claude_master_tunnel_secret
  to   = random_bytes.claw_master_tunnel_secret
}

moved {
  from = terraform_data.claude_master_server_converge
  to   = terraform_data.claw_master_server_converge
}
