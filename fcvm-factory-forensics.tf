# Preserve this failed builder's local logs before its disposable root is deleted.
# Remove the snapshot after the bounded forensic read is complete.
data "aws_ebs_volume" "fcvm_factory_failed_host" {
  provider = aws.fcvm_fleet_east1

  filter {
    name   = "volume-id"
    values = ["vol-0e06d7b66bf2ccb85"]
  }
  filter {
    name   = "attachment.instance-id"
    values = ["i-0e474d0f7c471e76d"]
  }
  filter {
    name   = "attachment.device"
    values = ["/dev/sda1"]
  }
}

resource "aws_ebs_snapshot" "fcvm_factory_failed_host" {
  provider    = aws.fcvm_fleet_east1
  volume_id   = data.aws_ebs_volume.fcvm_factory_failed_host.id
  description = "Failed factory host build logs for bounded forensic inspection"

  tags = {
    Purpose        = "fcvm-fleet-ami-forensics"
    FactoryBuild   = sha256("fcvm-fleet-image-factory:c4c1739d-beab-4a41-986b-c2f6695896ba")
    FactorySlot    = "host-build"
    FailedInstance = "i-0e474d0f7c471e76d"
    SourceSHA      = "71cbb71d42dfb3ecae40db617316c7c618e57b1f"
  }

  lifecycle {
    precondition {
      condition = (
        data.aws_ebs_volume.fcvm_factory_failed_host.encrypted &&
        data.aws_ebs_volume.fcvm_factory_failed_host.size == 64 &&
        lookup(data.aws_ebs_volume.fcvm_factory_failed_host.tags, "Purpose", "") == "fcvm-fleet-ami-builder" &&
        lookup(data.aws_ebs_volume.fcvm_factory_failed_host.tags, "FactoryBuild", "") == sha256("fcvm-fleet-image-factory:c4c1739d-beab-4a41-986b-c2f6695896ba") &&
        lookup(data.aws_ebs_volume.fcvm_factory_failed_host.tags, "FactorySlot", "") == "host-build" &&
        lookup(data.aws_ebs_volume.fcvm_factory_failed_host.tags, "SourceSHA", "") == "71cbb71d42dfb3ecae40db617316c7c618e57b1f"
      )
      error_message = "Forensics requires the exact encrypted 64 GiB root of the failed factory host build."
    }
  }
}
