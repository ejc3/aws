# Preserve the independently verified encrypted 64 GiB failed-builder root.
# Pin its immutable volume ID so later plans do not query an auto-deleted source.
# Remove this snapshot after the bounded forensic read is complete.
resource "aws_ebs_snapshot" "fcvm_factory_failed_host" {
  provider    = aws.fcvm_fleet_east1
  volume_id   = "vol-0e06d7b66bf2ccb85"
  description = "Failed factory host build logs for bounded forensic inspection"

  tags = {
    Purpose        = "fcvm-fleet-ami-forensics"
    FactoryBuild   = sha256("fcvm-fleet-image-factory:c4c1739d-beab-4a41-986b-c2f6695896ba")
    FactorySlot    = "host-build"
    FailedInstance = "i-0e474d0f7c471e76d"
    SourceSHA      = "71cbb71d42dfb3ecae40db617316c7c618e57b1f"
  }

}
