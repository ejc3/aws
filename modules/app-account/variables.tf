# One app's own AWS account (constellation docs/APP-ACCOUNTS.md): what astryx-account.tf and
# astryx-stage.tf build for Astryx, as one module call. The app's repository applies inside the
# account with the role made here; nothing here builds what the app runs.

variable "app" {
  type        = string
  description = "The app's name: the account, its OU (ou_name), <app>-terraform, <app>-terraform-state-<id>, /infra/<app>."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.app))
    error_message = "app is lowercase letters, digits and hyphens."
  }
}
variable "ou_name" {
  type        = string
  description = "The OU's display name, e.g. Family."
}
variable "email" {
  type        = string
  description = "The account's root email: a people/addresses key (people.tf), never written in a file."
  sensitive   = true
}
variable "organization_root_id" {
  type = string
}
variable "region" {
  type = string
}
variable "global_actions" {
  type        = list(string)
  description = "Actions exempt from the region guardrail (the global services; dolphin-region-scp.tf's list)."
}
variable "appliers" {
  type = object({
    dev_server_role_arn  = string # fcvm-arm's role, pinned by instance below
    applier_instance_arn = string # fcvm-arm's instance
    nextjs_dev_role_arn  = string # the owner's container (member sessions deny sts:AssumeRole)
  })
}
variable "monthly_budget_usd" {
  type = number
}
variable "budget_email" {
  type      = string
  sensitive = true
}

# The app's staging host (optional): a remote-managed tunnel to its task, one hostname, and Access.
# On the host: login_path is behind Access (a native app's sign-in); api_path_regex goes to the API
# with no Access (the API checks its own tokens); with zero_port, everything else goes to zero-cache;
# without, everything else is a 404. A path not named is never reachable.
variable "stage" {
  type = object({
    cloudflare_account_id = string
    zone_id               = string
    hostname              = string
    team_name             = string
    access_issuer         = string
    idp_ids               = list(string)
    api_port              = number
    zero_port             = optional(number)
    login_path            = string
    api_path_regex        = string
    # Who may sign in at login_path: existing Access policies (e.g. the family allowlist).
    login_policy_ids = list(string)
  })
  default = null
  validation {
    # The connector's Access-required rule is this path as a regex: plain path characters only, so
    # no metacharacter can make it miss while the API's bypass rule still matches.
    condition     = var.stage == null || can(regex("^/[A-Za-z0-9/_-]+$", var.stage.login_path))
    error_message = "stage.login_path is a plain path: /, letters, digits, _ and -."
  }
}
