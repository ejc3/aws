# Interim CVE-2026-98432 protection for shared Linux kernels. The reviewed helper
# owns only its two nft tables and its early-boot unit; it never restarts services.
locals {
  # gzip keeps direct EC2 user data below its 16 KiB limit. No credentials are in
  # this payload. Existing/live machines never install packages through SSM.
  linux_ip_options_setup = <<-SH
    (
      set -eu
      export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      dir=$(mktemp -d) || exit 1
      trap 'rm -rf "$dir"' EXIT
      printf '%s' '${base64gzip(file("${path.module}/scripts/cve-2026-98432-mitigation.py"))}' | base64 -d | gzip -d > "$dir/mitigation.py" || exit 1
      echo '${filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")}  '"$dir/mitigation.py" | sha256sum -c --quiet || exit 1
      python3 "$dir/mitigation.py" --apply || exit 1
    ) || { echo 'ERROR: IPv4 TCP IP-options protection failed' >&2; exit 1; }
  SH

  # Cold stock-image bootstrap only, before starting application services. The ARM
  # live association uses linux_ip_options_setup directly and never invokes apt.
  linux_ip_options_bootstrap = <<-SH
    (
      set -eu
      if ! command -v nft >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get -o DPkg::Lock::Timeout=600 update || exit 1
        apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends nftables python3 || exit 1
      fi
      ${local.linux_ip_options_setup}
    ) || { echo 'ERROR: IPv4 TCP IP-options bootstrap failed' >&2; exit 1; }
  SH

  linux_ip_options_names = concat(
    ["jumpbox", "jumpbox-2", "fcvm-metal-arm", "fcvm-metal-x86", "nextjs-dev", "claude-master-server", "io-box"],
    local.parallel_box_names, local.gpu_box_names, local.member_pbox_names, local.member_gbox_names
  )
  linux_ip_options_parameters = {
    commands         = local.linux_ip_options_setup
    executionTimeout = "180"
  }
}

# Tag targets also catch replacements and currently stopped boxes when their SSM
# agent returns. They cannot allocate, wake, or restart an instance.
resource "aws_ssm_association" "linux_ip_options" {
  name                = "AWS-RunShellScript"
  association_name    = "cve-2026-98432"
  schedule_expression = "rate(30 minutes)"
  max_concurrency     = "2"
  max_errors          = "1"
  targets {
    key    = "tag:Name"
    values = local.linux_ip_options_names
  }
  parameters = local.linux_ip_options_parameters
}

resource "aws_ssm_association" "linux_ip_options_west2" {
  provider            = aws.west2
  name                = "AWS-RunShellScript"
  association_name    = "cve-2026-98432"
  schedule_expression = "rate(30 minutes)"
  targets {
    key    = "tag:Name"
    values = local.linux_ip_options_names
  }
  parameters = local.linux_ip_options_parameters
}

resource "aws_ssm_association" "linux_ip_options_ohio" {
  provider            = aws.ohio
  name                = "AWS-RunShellScript"
  association_name    = "cve-2026-98432"
  schedule_expression = "rate(30 minutes)"
  targets {
    key    = "tag:Name"
    values = local.linux_ip_options_names
  }
  parameters = local.linux_ip_options_parameters
}

resource "aws_ssm_association" "linux_ip_options_east1" {
  provider            = aws.dr
  name                = "AWS-RunShellScript"
  association_name    = "cve-2026-98432"
  schedule_expression = "rate(30 minutes)"
  targets {
    key    = "tag:Name"
    values = local.linux_ip_options_names
  }
  parameters = local.linux_ip_options_parameters
}

resource "aws_ssm_association" "linux_ip_options_fleet_east2" {
  provider            = aws.fcvm_fleet
  name                = "AWS-RunShellScript"
  association_name    = "cve-2026-98432"
  schedule_expression = "rate(30 minutes)"
  targets {
    key    = "tag:Purpose"
    values = ["fcvm-fleet-host", "fcvm-fleet-tile", "fcvm-fleet-gpubox"]
  }
  parameters = local.linux_ip_options_parameters
}

resource "aws_ssm_association" "linux_ip_options_fleet_west2" {
  provider            = aws.fcvm_fleet_west2
  name                = "AWS-RunShellScript"
  association_name    = "cve-2026-98432"
  schedule_expression = "rate(30 minutes)"
  targets {
    key    = "tag:Purpose"
    values = ["fcvm-fleet-host", "fcvm-fleet-tile", "fcvm-fleet-gpubox"]
  }
  parameters = local.linux_ip_options_parameters
}

resource "aws_ssm_association" "linux_ip_options_fleet_east1" {
  provider            = aws.fcvm_fleet_east1
  name                = "AWS-RunShellScript"
  association_name    = "cve-2026-98432"
  schedule_expression = "rate(30 minutes)"
  targets {
    key    = "tag:Purpose"
    values = ["fcvm-fleet-host", "fcvm-fleet-tile", "fcvm-fleet-gpubox"]
  }
  parameters = local.linux_ip_options_parameters
}
