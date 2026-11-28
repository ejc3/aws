# Install the common pbox guidance without publishing nextjs-ct.sh: setup-sync
# would reprovision the live box and restart unrelated services.
locals {
  nextjs_pbox_policy_artifacts = [
    "nextjs-pbox-policy-install.sh",
    "nextjs-pbox-policy.py",
    "nextjs-pbox-policy.md",
    "antigravity-models.md",
  ]
  nextjs_pbox_policy_install = <<-SH
    set -eu
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    umask 077
    d=$(mktemp -d)
    trap 'rm -rf -- "$d"' EXIT
    cd "$d"
    %{for name in local.nextjs_pbox_policy_artifacts~}
    printf '%s' '${base64gzip(file("${path.module}/scripts/${name}"))}' | base64 -d | gzip -d > '${name}'
    %{endfor~}
    printf '%s\n' ${join(" ", [for name in local.nextjs_pbox_policy_artifacts : "'${filesha256("${path.module}/scripts/${name}")}  ${name}'"])} > manifest.sha256
    sha256sum --check --strict --status manifest.sha256
    /bin/bash ./nextjs-pbox-policy-install.sh "$d" ${join(" ", local.nextjs_users)}
  SH
}

resource "aws_ssm_association" "nextjs_pbox_policy" {
  count = var.enable_nextjs_ct && local.nextjs_ct_serving ? 1 : 0

  name                             = "AWS-RunShellScript"
  association_name                 = "nextjs-pbox-policy-install"
  schedule_expression              = "rate(1 day)"
  wait_for_success_timeout_seconds = 420
  max_concurrency                  = "1"
  max_errors                       = "1"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.nextjs_ct[0].id]
  }

  parameters = {
    commands         = local.nextjs_pbox_policy_install
    executionTimeout = "360"
  }

  lifecycle {
    precondition {
      condition     = length(local.nextjs_pbox_policy_install) < 60000
      error_message = "The Next.js pbox policy installer exceeds the SSM payload budget."
    }
  }
}
