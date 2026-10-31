# macpbox discovery and the single installed launcher. Terraform owns durable configuration only;
# allocating a billed Mac host is a separate, explicitly owner-approved administration action.
locals {
  macpbox_discovery = {
    version             = 1
    region              = "us-west-2"
    type                = "mac-m4pro.metal"
    launch_template_id  = aws_launch_template.macpbox.id
    ssh_user            = "ec2-user"
    admin_role_arns     = aws_iam_role.jumpbox_admin[*].arn
    admin_identity_file = "~/.ssh/fcvm-ec2"
    dev_identity_file   = "~/.ssh/dev_hop"
    name                = "macpbox"
    purpose             = "macpbox"
    lease_hours         = 24
  }

  # Merged into an EXISTING shared policy, avoiding both the managed-attachment limit and
  # dev-server-role's nearly full inline-policy budget. Nothing here can allocate or launch.
  macpbox_readonly_statements = [
    {
      Sid      = "MacpboxDiscovery"
      Effect   = "Allow"
      Action   = "ssm:GetParameter"
      Resource = aws_ssm_parameter.macpbox.arn
    },
    {
      Sid      = "MacpboxHostAndPriceRead"
      Effect   = "Allow"
      Action   = ["ec2:DescribeHosts", "pricing:GetProducts", "ssm:DescribeInstanceInformation"]
      Resource = "*"
    },
  ]

  macpbox_tool_instances = concat(
    values(local.admin_tmux_boxes),
    local.metal_update_guard_instances,
    var.enable_nextjs_ct || var.enable_nextjs_dev ? [local.nextjs_live_instance.id] : [],
  )

  # Publish only reviewed source through the admin-owned association, rather than giving an
  # admin host a root download/updater. Atomic replacement needs no session/service restart.
  macpbox_tool_install = <<-SH
    set -eu
    install -d -m 755 /usr/local/bin
    tmp=$(mktemp /usr/local/bin/.macpbox.XXXXXXXX)
    trap 'rm -f "$tmp"' EXIT
    printf '%s' '${base64encode(file("${path.module}/scripts/macpbox.py"))}' | base64 -d > "$tmp"
    echo '${filesha256("${path.module}/scripts/macpbox.py")}  '"$tmp" | sha256sum -c --quiet
    chown root:root "$tmp"
    chmod 755 "$tmp"
    if ! cmp -s "$tmp" /usr/local/bin/macpbox; then
      mv -f "$tmp" /usr/local/bin/macpbox
    fi
    # The existing read-only /opt/nextjs-ct bind exposes this file to all containers.
    # Run ln INSIDE a running container's namespace; never follow a container-controlled
    # /usr/local/bin from host root. Cold containers get the same link from provision.
    if [ -d /var/lib/nextjs-ct/shared ]; then
      shared=$(mktemp /var/lib/nextjs-ct/shared/.macpbox.XXXXXXXX)
      install -m 755 /usr/local/bin/macpbox "$shared"
      mv -f "$shared" /var/lib/nextjs-ct/shared/macpbox
      for user in ${join(" ", local.nextjs_users)}; do
        if [ "$(machinectl show "$user" -p State --value 2>/dev/null || true)" = running ]; then
          systemd-run --machine="$user" --wait --pipe --collect --service-type=exec \
            /bin/ln -sfn /opt/nextjs-ct/macpbox /usr/local/bin/macpbox
        fi
      done
    fi
    echo 'macpbox: ${substr(filesha256("${path.module}/scripts/macpbox.py"), 0, 12)} installed'
  SH
}

resource "aws_ssm_parameter" "macpbox" {
  name        = "/infra/macpbox"
  description = "Public Mac build-host discovery; new paid leases require direct owner approval"
  type        = "String"
  value       = jsonencode(local.macpbox_discovery)
  tags        = { Name = "macpbox", Purpose = "macpbox" }
}

resource "aws_ssm_association" "macpbox_tools" {
  count = length(local.macpbox_tool_instances) > 0 ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "macpbox-tools"
  schedule_expression = "rate(1 day)"
  max_concurrency     = "2"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = local.macpbox_tool_instances
  }

  parameters = {
    commands         = local.macpbox_tool_install
    executionTimeout = "120"
  }
}
