# Install only the host memory recorder. Publishing the main Next.js setup object
# would also rerun shared provisioning and restart unrelated services.
locals {
  nextjs_memory_history_artifacts = [
    "install-nextjs-memory-history.sh",
    "nextjs-memory-history.py",
    "nextjs-memory-history.service",
    "nextjs-memory-history.timer",
  ]
  nextjs_memory_history_install = <<-SH
    set -eu
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    umask 077
    d=$(mktemp -d)
    trap 'rm -rf -- "$d"' EXIT
    cd "$d"
    %{for name in local.nextjs_memory_history_artifacts~}
    printf '%s' '${base64gzip(file("${path.module}/scripts/${name}"))}' | base64 -d | gzip -d > '${name}'
    %{endfor~}
    printf '%s\n' ${join(" ", [for name in local.nextjs_memory_history_artifacts : "'${filesha256("${path.module}/scripts/${name}")}  ${name}'"])} > manifest.sha256
    sha256sum --check --strict --status manifest.sha256
    /bin/bash ./install-nextjs-memory-history.sh "$d"
  SH
}

resource "aws_ssm_association" "nextjs_memory_history" {
  count = var.enable_nextjs_ct && local.nextjs_ct_serving ? 1 : 0

  name                             = "AWS-RunShellScript"
  association_name                 = "nextjs-memory-history-install"
  schedule_expression              = "rate(1 day)"
  wait_for_success_timeout_seconds = 300
  max_concurrency                  = "1"
  max_errors                       = "1"

  # State Manager requires both tag keys to match. Follow replacement hosts
  # without a dependency on the Next.js instance or its S3 setup object.
  targets {
    key    = "tag:Box"
    values = ["nextjs-ct"]
  }

  targets {
    key    = "tag:Name"
    values = ["nextjs-dev"]
  }

  parameters = {
    commands         = local.nextjs_memory_history_install
    executionTimeout = "120"
  }

  lifecycle {
    precondition {
      condition     = length(local.nextjs_memory_history_install) < 60000
      error_message = "The compressed Next.js memory history installer exceeds the SSM payload budget."
    }
  }
}
