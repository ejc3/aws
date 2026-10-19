# metal-box-tools.tf
#
# The box tools on the metal boxes (pbox, gbox, wbox, codex-restart), kept equal to main every day, not only at boot.
# The boot script and the Monday self-update install them (dev-user-data.tf, dev-selfupdate.tf), but a box that has not
# booted since a change keeps the old copy: on 2026-10-19 fcvm-metal-arm still ran the four-slot pbox after #522 (eight
# slots) and #525 (the volume-race fix), and a gbox from 2026-09-29. nextjs-dev and the nextjs-ct containers pick the
# tools up within minutes through their own setup sync.
#
# The scripts are too big for one SSM command (about 60 KB), so Terraform publishes each to the dev-scripts bucket and a
# daily association, re-run whenever a script changes (its sha256 is in the commands), downloads each one, checks the
# sha256 and renames it into place. It writes files only: nothing is restarted.

locals {
  metal_box_tools = {
    pbox          = "parallel-box.sh"
    gbox          = "gpu-box.sh"
    wbox          = "wbox.sh"
    codex-restart = "codex-restart.sh"
  }
}

resource "aws_s3_object" "metal_box_tools" {
  for_each     = length(local.metal_update_guard_instances) > 0 ? local.metal_box_tools : {}
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "box-tools/${each.key}"
  content      = file("${path.module}/scripts/${each.value}")
  content_type = "text/x-shellscript"
}

resource "aws_ssm_association" "metal_box_tools" {
  count = length(local.metal_update_guard_instances) > 0 ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "metal-box-tools"
  schedule_expression = "rate(1 day)"
  max_concurrency     = "2"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = local.metal_update_guard_instances
  }

  parameters = {
    executionTimeout = "120"
    commands = join("\n", concat(
      ["set -eu", "export PATH=\"$PATH:/usr/local/bin:/snap/bin\"", "tmp=$(mktemp)", "trap 'rm -f \"$tmp\"' EXIT"],
      [for name, script in local.metal_box_tools : join("\n", [
        "aws s3 cp 's3://${aws_s3_bucket.dev_scripts.id}/${aws_s3_object.metal_box_tools[name].key}' \"$tmp\" --region ${var.aws_region} --only-show-errors",
        "echo '${filesha256("${path.module}/scripts/${script}")}  '\"$tmp\" | sha256sum -c --quiet",
        "install -m 755 \"$tmp\" /usr/local/bin/${name}.new && mv -f /usr/local/bin/${name}.new /usr/local/bin/${name}",
        "echo 'metal-box-tools: ${name} ${substr(filesha256("${path.module}/scripts/${script}"), 0, 12)}'",
      ])],
    ))
  }
}
