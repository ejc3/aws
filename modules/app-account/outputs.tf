output "account_id" {
  value = local.account_id
}
output "terraform_role_arn" {
  value = aws_iam_role.terraform.arn
}
output "pointer_name" {
  value = aws_ssm_parameter.pointer.name
}
output "pointer_arn" {
  value = aws_ssm_parameter.pointer.arn
}
output "pointer" {
  description = "What the app's repository's backend and provider name (no credentials); also in /infra/<app>"
  value       = local.pointer
}
